"""
IM会话管理器

功能：
1. 管理多账号的GoofishImClient实例（单例模式）
2. 根据account_id获取或创建IM客户端连接
3. 断开指定账号连接
4. 从数据库获取账号Cookie
5. 管理前端WebSocket客户端，将IM推送消息转发给前端
"""
import asyncio
import json
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

from loguru import logger
from sqlalchemy import select
from starlette.websockets import WebSocket

from common.db.session import async_session_maker
from common.models import XYAccount

from .im_client import GoofishImClient

# 同一条消息可能被投递两次：系统内互发补偿（notify_peer_account 合成）
# 与闲鱼真实推送各来一次。前端虽按 messageId 去重只显示一条，但会话未读数
# 会被重复 +1（发 3 条显示 6 条）。这里在投递侧做短时去重，保证一条消息
# 只投递一次；即便误判，30 秒增量拉取仍会从历史补齐，不会漏消息。
_DELIVERY_DEDUPE_WINDOW_MS = 10_000  # 同一消息两次投递的时间容差
_DELIVERY_DEDUPE_TTL = 60.0  # 去重记录保留时长（秒）
_DELIVERY_DEDUPE_MAX = 100  # 单账号保留的去重记录条数
# 合成补偿前给真实推送的等待时间（秒）：真实推送带真昵称和服务端时间，优先让它送达
PEER_NOTIFY_WAIT = 1.2


class ImSessionManager:
    """IM会话管理器（单例），管理多账号的IM WebSocket客户端"""

    _instance: Optional["ImSessionManager"] = None

    def __init__(self):
        # account_id -> GoofishImClient
        self.clients: Dict[str, GoofishImClient] = {}
        self._lock = asyncio.Lock()
        # 前端 WebSocket 客户端: account_id -> Set[WebSocket]
        self._ws_clients: Dict[str, Set[WebSocket]] = {}
        # 已投递消息去重：account_id -> deque[(内容键, messageId, 消息时间ms, 记录时刻)]
        self._recent_deliveries: Dict[str, Deque[Tuple[str, str, float, float]]] = {}
        # 延迟补偿任务集合（保活，避免被 GC 中途回收）
        self._pending_tasks: Set[asyncio.Task] = set()

    @classmethod
    def get_instance(cls) -> "ImSessionManager":
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = ImSessionManager()
        return cls._instance

    async def get_or_connect(self, account_id: str) -> GoofishImClient:
        """
        获取已连接的客户端，或新建连接

        Args:
            account_id: 账号ID

        Returns:
            已连接的GoofishImClient

        Raises:
            ValueError: 账号不存在或Cookie为空
            Exception: 连接失败
        """
        async with self._lock:
            # 已有连接且仍在线，直接返回
            if account_id in self.clients:
                client = self.clients[account_id]
                if client.is_connected:
                    return client
                # 已断开，清理后重建。
                # 必须关掉旧对象的自动恢复：这个 client 马上要被丢弃，
                # 若看门狗还活着会把它重连上线，与新连接互踢。
                await client.disconnect(keep_auto_recover=False)
                del self.clients[account_id]

            # 从数据库获取账号行 ID 和 Cookie，供滑块成功后精准持久化。
            account_context = await self._get_account_context(account_id)
            if not account_context:
                raise ValueError(f"账号Cookie为空: {account_id}")
            account_row_id, cookies_str = account_context

            # 创建并连接
            client = GoofishImClient(
                account_id,
                cookies_str,
                account_row_id=account_row_id,
            )
            success = await client.connect()
            if not success:
                raise Exception(f"IM连接失败: {account_id}")

            self.clients[account_id] = client
            # 连接成功后，如果有前端WS客户端在等待，挂载推送回调
            await self._setup_push_callback_after_connect(account_id, client)
            return client

    async def disconnect(self, account_id: str):
        """
        断开指定账号的IM连接

        Args:
            account_id: 账号ID
        """
        async with self._lock:
            client = self.clients.pop(account_id, None)
            if client:
                # 用户主动断开：连自动恢复一起停掉，否则会被看门狗重连回来
                await client.disconnect(keep_auto_recover=False)
                logger.info(f"【{account_id}】IM会话已断开并移除")

    async def disconnect_all(self):
        """断开所有连接"""
        async with self._lock:
            for account_id, client in list(self.clients.items()):
                try:
                    await client.disconnect(keep_auto_recover=False)
                except Exception as e:
                    logger.warning(f"【{account_id}】断开连接异常: {e}")
            self.clients.clear()
            logger.info("所有IM会话已断开")

    def get_connected_account_ids(self) -> list:
        """获取所有已连接的账号ID列表"""
        return [
            aid for aid, client in self.clients.items()
            if client.is_connected
        ]

    # ==================== 前端 WebSocket 客户端管理 ====================

    async def register_ws_client(self, account_id: str, ws: WebSocket):
        """
        注册前端 WebSocket 客户端，同时在 IM 客户端上挂载推送回调

        Args:
            account_id: 账号ID
            ws: 前端 WebSocket 连接
        """
        if account_id not in self._ws_clients:
            self._ws_clients[account_id] = set()
        self._ws_clients[account_id].add(ws)
        logger.info(f"【{account_id}】前端WebSocket客户端已注册，当前连接数: {len(self._ws_clients[account_id])}")

        # 确保 IM 客户端上已挂载推送回调
        client = self.clients.get(account_id)
        if client and client.is_connected:
            self._ensure_push_callback(account_id, client)

    async def unregister_ws_client(self, account_id: str, ws: WebSocket):
        """
        注销前端 WebSocket 客户端

        Args:
            account_id: 账号ID
            ws: 前端 WebSocket 连接
        """
        clients = self._ws_clients.get(account_id)
        if clients:
            clients.discard(ws)
            if not clients:
                del self._ws_clients[account_id]
        logger.info(
            f"【{account_id}】前端WebSocket客户端已注销，"
            f"剩余连接数: {len(self._ws_clients.get(account_id, set()))}"
        )

    # ==================== 投递去重 ====================
    # 一条消息只应向某账号的前端投递一次，重复投递会让未读数翻倍。

    @staticmethod
    def _delivery_key(cid: str, message: Dict[str, Any]) -> str:
        """消息内容键：无 messageId（或两侧 ID 体系不一致）时按内容比对"""
        images = message.get("images") or []
        return "|".join([
            str(cid),
            str(message.get("senderId", "") or ""),
            str(message.get("type", "") or ""),
            str(message.get("text", "") or ""),
            ",".join(str(url) for url in images),
            str(message.get("audioUrl", "") or ""),
        ])

    def _is_duplicate_delivery(self, account_id: str, cid: str, message: Dict[str, Any]) -> bool:
        """判断该消息最近是否已向该账号的前端投递过"""
        now = time.monotonic()
        records = self._recent_deliveries.setdefault(account_id, deque())
        while records and now - records[0][3] > _DELIVERY_DEDUPE_TTL:
            records.popleft()
        key = self._delivery_key(cid, message)
        msg_id = str(message.get("messageId", "") or "")
        msg_time = float(message.get("time", 0) or 0)
        for r_key, r_id, r_time, _created in records:
            # ID 一致必定是同一条消息
            if msg_id and r_id and msg_id == r_id:
                return True
            # 无 ID 或 ID 不一致时退化为内容 + 时间窗口比对
            if r_key == key and abs(msg_time - r_time) <= _DELIVERY_DEDUPE_WINDOW_MS:
                return True
        return False

    def _mark_delivered(self, account_id: str, cid: str, message: Dict[str, Any]) -> None:
        """记录已投递，供后续重复事件识别"""
        records = self._recent_deliveries.setdefault(account_id, deque())
        records.append((
            self._delivery_key(cid, message),
            str(message.get("messageId", "") or ""),
            float(message.get("time", 0) or 0),
            time.monotonic(),
        ))
        while len(records) > _DELIVERY_DEDUPE_MAX:
            records.popleft()

    def _ensure_push_callback(self, account_id: str, client: GoofishImClient):
        """确保 IM 客户端上已挂载推送回调（避免重复注册）"""
        # 用属性标记是否已挂载
        if getattr(client, "_push_cb_registered", False):
            return

        async def _forward_to_frontend(parsed_msg: dict):
            """将解析后的推送消息转发给所有前端 WebSocket 客户端"""
            ws_set = self._ws_clients.get(account_id)
            if not ws_set:
                return
            # 已被系统内互发补偿投递过的消息，真实推送不再重复下发（反之亦然）
            if parsed_msg.get("event") == "new_message":
                cid = str(parsed_msg.get("cid", "") or "")
                message = parsed_msg.get("message") or {}
                if self._is_duplicate_delivery(account_id, cid, message):
                    logger.info(
                        f"【{account_id}】推送与已投递内容重复，跳过转发 "
                        f"(cid={cid}, id={message.get('messageId', '')})"
                    )
                    return
                self._mark_delivered(account_id, cid, message)
            payload = json.dumps(parsed_msg, ensure_ascii=False)
            dead: List[WebSocket] = []
            for ws in ws_set:
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            # 清理已断开的连接
            for ws in dead:
                ws_set.discard(ws)
            if not ws_set and account_id in self._ws_clients:
                del self._ws_clients[account_id]

        async def _broadcast_reconnected(aid: str):
            """IM 断线重连后广播给前端（断线期间的消息不会补推，需前端自行补拉）"""
            await self._broadcast_event(aid, {
                "event": "reconnected",
                "account_id": aid,
            })

        client.add_push_callback(_forward_to_frontend)
        client.add_reconnected_callback(_broadcast_reconnected)
        client._push_cb_registered = True
        logger.info(f"【{account_id}】IM推送回调已挂载")

    async def _broadcast_event(self, account_id: str, payload_dict: dict):
        """向某账号的所有前端 WebSocket 广播一条事件，顺带清理已断开的连接"""
        ws_set = self._ws_clients.get(account_id)
        if not ws_set:
            return
        payload = json.dumps(payload_dict, ensure_ascii=False)
        dead: List[WebSocket] = []
        for ws in ws_set:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            ws_set.discard(ws)
        if not ws_set and account_id in self._ws_clients:
            del self._ws_clients[account_id]

    async def notify_peer_account(
        self,
        sender_account_id: str,
        cid: str,
        to_user_id: str,
        *,
        message_id: str,
        msg_type: str,
        text: str = "",
        images: Optional[List[str]] = None,
        audio_url: str = "",
        audio_duration: int = 0,
    ) -> bool:
        """
        系统内互发的实时性补偿（重要，勿删）：

        实测闲鱼 IM 服务端对「网页 IM 会话（sendByReceiverScope）发出的消息」
        不做跨网页会话的实时 syncPush —— 接收方的在线聊天长连接收不到任何
        推送帧（同账号的自动回复长连接反而能收到），导致两个都在本系统在线
        的账号互发时，接收方界面毫无动静，刷新页面才能看到。

        这里在发送成功后，向接收账号的前端 WebSocket 合成一条与真实推送
        同构的 new_message 事件。

        真实推送优先：它带的是闲鱼真实昵称和服务端时间，而合成事件只能取到
        数据库里脱敏的昵称（如 x***1）。所以合成事件延迟 PEER_NOTIFY_WAIT
        秒再发，期间真实推送到达就跳过合成。

        Args:
            sender_account_id: 发送方账号ID（用于查昵称与日志）
            cid: 会话ID
            to_user_id: 接收方账号ID（对方用户ID，同时是本系统的账号ID）
            message_id: 发送成功后服务端返回的消息ID（前端去重键）
            msg_type: 消息类型（text / image / audio）
            text: 文本内容
            images: 图片URL列表
            audio_url: 语音URL
            audio_duration: 语音时长（毫秒，与前端 ChatMessage.audioDuration 一致）

        Returns:
            是否已安排向该账号的前端补偿投递
        """
        if not self._ws_clients.get(to_user_id):
            return False
        try:
            task = asyncio.create_task(self._peer_notify_after_push(
                sender_account_id, cid, to_user_id,
                message_id=message_id, msg_type=msg_type, text=text,
                images=images, audio_url=audio_url, audio_duration=audio_duration,
            ))
            # 事件循环只持有弱引用，必须自己留住任务，否则可能被中途回收
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
            return True
        except RuntimeError as e:
            # 事件循环已关闭（服务停机中），跳过补偿即可
            logger.warning(f"【{sender_account_id}】系统内互发补偿未安排: {e}")
            return False

    async def _peer_notify_after_push(
        self,
        sender_account_id: str,
        cid: str,
        to_user_id: str,
        *,
        message_id: str,
        msg_type: str,
        text: str = "",
        images: Optional[List[str]] = None,
        audio_url: str = "",
        audio_duration: int = 0,
    ) -> bool:
        """等待真实推送一小会儿；推送没来才合成补偿事件（保证一条消息只投递一次）"""
        try:
            await asyncio.sleep(PEER_NOTIFY_WAIT)
            if not self._ws_clients.get(to_user_id):
                return False
            return await self._deliver_peer_notify(
                sender_account_id, cid, to_user_id,
                message_id=message_id, msg_type=msg_type, text=text,
                images=images, audio_url=audio_url, audio_duration=audio_duration,
            )
        except Exception as e:
            logger.warning(f"【{sender_account_id}】系统内互发补偿异常: {e}")
            return False

    async def _deliver_peer_notify(
        self,
        sender_account_id: str,
        cid: str,
        to_user_id: str,
        *,
        message_id: str,
        msg_type: str,
        text: str = "",
        images: Optional[List[str]] = None,
        audio_url: str = "",
        audio_duration: int = 0,
    ) -> bool:
        """合成并向接收账号的前端投递一条 new_message 事件"""
        sender_id = sender_account_id
        sender_client = self.clients.get(sender_account_id)
        if sender_client:
            sender_id = getattr(sender_client, "myid", "") or sender_account_id

        sender_name = ""
        try:
            async with async_session_maker() as db:
                result = await db.execute(
                    select(XYAccount.display_name, XYAccount.username).where(
                        XYAccount.account_id == sender_account_id
                    )
                )
                row = result.first()
                if row:
                    sender_name = (row[0] or row[1] or "").strip()
        except Exception as e:
            logger.warning(f"查询发送方昵称失败（{sender_account_id}）: {e}")

        message: Dict[str, Any] = {
            "messageId": message_id,
            "senderId": sender_id,
            "senderName": sender_name,
            "isSelf": False,
            "type": msg_type,
            "text": text,
            "images": images or [],
            "time": int(time.time() * 1000),
        }
        if audio_url:
            message["audioUrl"] = audio_url
            message["audioDuration"] = audio_duration

        # 真实推送先到时不再重复合成，避免一条消息投递两次
        if self._is_duplicate_delivery(to_user_id, cid, message):
            logger.info(
                f"【{sender_account_id}】系统内互发补偿跳过：{to_user_id} 已收到该消息"
                f" (cid={cid}, id={message_id})"
            )
            return False
        self._mark_delivered(to_user_id, cid, message)

        await self._broadcast_event(to_user_id, {
            "event": "new_message",
            "cid": cid,
            "message": message,
        })
        logger.info(
            f"【{sender_account_id}】系统内互发补偿：已向 {to_user_id} 的前端"
            f"合成 new_message 事件 (cid={cid}, type={msg_type})"
        )
        return True

    async def _setup_push_callback_after_connect(
        self, account_id: str, client: GoofishImClient
    ):
        """连接成功后，如果已有前端客户端在等待，立即挂载推送回调"""
        if account_id in self._ws_clients and self._ws_clients[account_id]:
            self._ensure_push_callback(account_id, client)

    async def _get_account_context(self, account_id: str) -> tuple[int, str] | None:
        """
        从数据库获取账号行 ID 和 Cookie。

        Args:
            account_id: 账号ID

        Returns:
            ``(账号行ID, Cookie字符串)``，账号不存在或 Cookie 为空时返回 ``None``。
        """
        try:
            async with async_session_maker() as db:
                result = await db.execute(
                    select(XYAccount).where(
                        XYAccount.account_id == account_id
                    )
                )
                account = result.scalar_one_or_none()

                if not account:
                    logger.error(
                        f"【{account_id}】数据库中未找到账号"
                    )
                    return None

                if not account.cookie:
                    logger.error(
                        f"【{account_id}】账号Cookie为空"
                    )
                    return None

                return int(account.id), account.cookie

        except Exception as e:
            logger.error(f"【{account_id}】获取账号Cookie失败: {e}")
            return None


def get_im_session_manager() -> ImSessionManager:
    """获取IM会话管理器单例的快捷方法"""
    return ImSessionManager.get_instance()
