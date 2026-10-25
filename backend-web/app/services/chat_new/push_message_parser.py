"""
推送消息解析器

功能：
1. 解密 IM WebSocket 主动推送的消息（base64/MessagePack）
2. 解析标准聊天消息、卡片消息、卡片更新消息
3. 提取文本/图片/语音等消息内容
4. 格式化为前端可消费的统一格式

参照自动回复 websocket/app/services/xianyu/message_handler.py 的解密和解析逻辑
"""
import base64
import json
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from common.utils.xianyu_utils import decrypt
from common.utils.xianyu_message_parser import (
    parse_content_with_audio,
    classify_event,
)
from common.utils.order_card_parser import parse_rich_card

from app.services.chat_new.card_sampler import sample_card


class PushMessageParser:
    """推送消息解析器，负责解密和解析 IM 主动推送的消息"""

    def __init__(self, account_id: str, myid: str):
        """
        初始化解析器

        Args:
            account_id: 账号标识（用于日志）
            myid: 当前登录用户 ID（用于判断是否自己发出）
        """
        self.account_id = account_id
        self.myid = myid

    def _is_self_sender(self, sender_id: str) -> bool:
        """是否「当前视角账号」发出的消息（渲染语义）。

        2026-10-26 修正：isSelf 只比 myid。此前把「任一托管账号」都算成自己
        （2026-10-20 为防跨账号误响铃引入），副作用是托管账号互聊的会话里
        对方（自己的另一个号）的消息全被渲染到右侧自己位——名字/头像/消息
        位置全错乱。防误响铃改由 managedSender 字段承担（前端跳过响铃即可）。
        """
        sid = str(sender_id or "").strip()
        return bool(self.myid and sid == self.myid)

    def _is_managed_sender(self, sender_id: str) -> bool:
        """是否「本系统托管的其它账号」发出的消息（防扰语义）。

        cid 跨账号共享：别的托管账号发的消息本账号的 IM 推送/历史也拉得到，
        这类消息渲染时是「对方」（isSelf=false），但不该响铃/弹 toast——
        2026-10-20 的误响铃事故由这个判断兜住，而不是把 isSelf 扩大化。
        """
        sid = str(sender_id or "").strip()
        if not sid or sid == self.myid:
            return False
        try:
            from common.utils.managed_account_ids import is_managed_account

            return is_managed_account(sid)
        except Exception:
            return False

    def decrypt_push_data(self, data: str) -> Optional[dict]:
        """
        解密推送消息数据

        参照自动回复 message_handler._decrypt_message：
        先尝试 base64 -> JSON，失败则用 decrypt() 解密 MessagePack

        Args:
            data: 原始推送数据字符串

        Returns:
            解密后的消息字典，失败返回 None
        """
        try:
            try:
                decoded_str = base64.b64decode(data).decode("utf-8")
                parsed = json.loads(decoded_str)
                if isinstance(parsed, dict) and "chatType" in parsed:
                    return None  # 系统提示消息，跳过
                return parsed
            except Exception:
                decrypted = json.loads(decrypt(data))
                return decrypted
        except Exception:
            return None

    def parse(self, msg: dict) -> Optional[dict]:
        """
        解析解密后的推送消息，提取聊天内容并打印日志

        Returns:
            解析后的消息字典（event/cid/message），无法解析返回 None
        """
        try:
            # 标准聊天消息：msg["1"] 为 dict 且有 msg["1"]["10"]["reminderContent"]
            msg_1 = msg.get("1")
            if isinstance(msg_1, dict):
                msg_10 = msg_1.get("10", {})
                if isinstance(msg_10, dict) and msg_10.get("reminderContent") is not None:
                    return self._parse_standard_chat(msg_1, msg_10)
                # 卡片消息（msg["1"]["6"]["3"] 存在）
                msg_6 = msg_1.get("6", {})
                if isinstance(msg_6, dict) and "3" in msg_6:
                    return self._parse_card_chat(msg_1, msg_6)

            # 卡片更新消息：msg["1"] 为字符串, msg["4"]["reminderContent"]
            if isinstance(msg.get("1"), str) and isinstance(msg.get("4"), dict):
                msg_4 = msg["4"]
                if "reminderContent" in msg_4:
                    return self._parse_card_update(msg, msg_4)

            return None
        except Exception as e:
            logger.warning(f"【{self.account_id}】解析推送消息失败: {e}")
            return None

    # ==================== 内部解析方法 ====================

    def _parse_standard_chat(self, msg_1: dict, msg_10: dict) -> Optional[dict]:
        """解析标准聊天推送消息"""
        try:
            sender_id_raw = str(msg_10.get("senderUserId", "") or "")
            # 去掉 @goofish 后缀，保证前端显示和对比一致
            sender_id = sender_id_raw.split("@")[0] if "@" in sender_id_raw else sender_id_raw
            sender_name = str(
                msg_10.get("senderNick") or msg_10.get("reminderTitle") or ""
            )
            reminder = msg_10.get("reminderContent", "")
            # 引用消息（回复）：msg_10（即 message.extension）里的 replyMessageId
            # 指向被引用的原消息。协议实证 2026-10-20：APP 引用消息正文仍是
            # 普通文本，引用关系只放在这个字段，前端据此渲染「引用块+正文」
            reply_to_message_id = str(msg_10.get("replyMessageId", "") or "")
            cid_raw = str(msg_1.get("2", ""))
            cid = cid_raw.split("@")[0] if "@" in cid_raw else cid_raw
            msg_time = msg_1.get("5", 0)

            # 解析消息内容：文本 / 图片 / 语音（语音会额外给出音频地址与时长）
            text_content, images, msg_type, audio_url, audio_duration = (
                self._decode_content(msg_1)
            )
            # 撤回占位识别：正文载荷（文本/图片/语音）全空、但 reminderContent
            # 非空——这是闲鱼「撤回」的形态：原消息位置正文被掏空，仅留撤回前
            # 的旧摘要（与 HTTP 历史路径实测形态一致，2026-10-20）。按撤回渲染，
            # 旧摘要作为 revokedOriginal 下发，前端默认折叠、点「显示」才展开。
            # 判据保守：正常聊天消息必有正文（文本/图片/语音至少其一），
            # 「空正文 + 有摘要」只会出现在撤回场景。
            revoked = False
            revoked_original = ""
            if not text_content and not images and not audio_url:
                if str(reminder or "").strip():
                    revoked = True
                    revoked_original = str(reminder).strip()
                    text_content = (
                        "你撤回了一条消息"
                        if self._is_self_sender(sender_id)
                        else "对方撤回了一条消息"
                    )
                    msg_type = "system"
                else:
                    text_content = reminder
                    msg_type = "text"

            is_self = self._is_self_sender(sender_id)
            preview = text_content or ("[语音]" if audio_url else "[图片]")

            # 方括号开头 = 闲鱼系统/交易摘要（如 [买家已付款]），原始载荷采样，
            # 供后续分析卡片按钮与结构化字段（只读落盘，零风控）
            rich_card = parse_rich_card(msg_1)
            if str(reminder or "").startswith("["):
                sample_card(
                    "push_summary", self.account_id, cid,
                    str(msg_1.get("3", "") or ""), str(reminder), msg_10,
                )

            if is_self:
                logger.info(
                    f"【{self.account_id}】[推送-发出] cid={cid}, 内容: {preview}"
                )
            else:
                logger.info(
                    f"【{self.account_id}】[推送-收到] cid={cid}, "
                    f"发送者={sender_name}({sender_id}), 内容: {preview}"
                )

            return {
                "event": "new_message",
                "cid": cid,
                "message": {
                    "messageId": str(msg_1.get("3", "") or ""),
                    "senderId": sender_id,
                    "senderName": sender_name,
                    "isSelf": is_self,
                    "managedSender": self._is_managed_sender(sender_id),
                    "type": msg_type,
                    "text": text_content,
                    "images": images,
                    "audioUrl": audio_url,
                    "audioDuration": audio_duration,
                    "time": msg_time,
                    # 撤回占位元数据（与历史路径 _parse_message 同格式）：
                    # 前端渲染「对方撤回了一条消息」带头像，旧摘要默认折叠
                    "revoked": revoked,
                    "revokedOriginal": revoked_original,
                    # 引用消息（回复）：被引用消息的 IM messageId，前端本地查表渲染
                    "replyToMessageId": reply_to_message_id,
                    # 订单卡片富数据（按钮/订单号/演出信息），非交易卡片为 None
                    "card": rich_card,
                },
            }
        except Exception as e:
            logger.warning(f"【{self.account_id}】解析标准聊天推送失败: {e}")
            return None

    def _parse_card_chat(self, msg_1: dict, msg_6: dict) -> Optional[dict]:
        """解析卡片类型聊天推送消息"""
        try:
            msg_6_3 = msg_6.get("3", {})
            text_content = msg_6_3.get("2", "[卡片消息]")
            cid_raw = str(msg_1.get("2", ""))
            cid = cid_raw.split("@")[0] if "@" in cid_raw else cid_raw
            msg_time = msg_1.get("5", 0)

            msg_1_1 = msg_1.get("1", {})
            sender_raw = msg_1_1.get("1", "") if isinstance(msg_1_1, dict) else ""
            sender_id = (
                sender_raw.split("@")[0]
                if "@" in str(sender_raw)
                else str(sender_raw)
            )

            logger.info(
                f"【{self.account_id}】[推送-卡片] cid={cid}, 内容: {text_content}"
            )

            # 订单卡片原始载荷采样：msg_6_3 是卡片内容 dict（标题在 ["2"]，
            # 疑似还带按钮/action/订单号等未解析字段），原样落盘供后续分析
            sample_card(
                "push_card", self.account_id, cid,
                str(msg_1.get("3", "") or ""), str(text_content), msg_6_3,
            )

            return {
                "event": "new_message",
                "cid": cid,
                "message": {
                    "messageId": str(msg_1.get("3", "") or ""),
                    "senderId": sender_id,
                    "senderName": "系统",
                    "isSelf": self._is_self_sender(sender_id),
                    "managedSender": self._is_managed_sender(sender_id),
                    "type": "card",
                    "text": str(text_content),
                    "images": [],
                    "time": msg_time,
                    # 订单卡片富数据（按钮/订单号/演出信息），非交易卡片为 None
                    "card": parse_rich_card(msg_1),
                    **dict(zip(("isEvent", "eventKind"), classify_event(str(text_content), "card"))),
                },
            }
        except Exception as e:
            logger.warning(f"【{self.account_id}】解析卡片推送失败: {e}")
            return None

    def _parse_card_update(self, msg: dict, msg_4: dict) -> Optional[dict]:
        """解析卡片更新推送消息（如付款状态变更）"""
        try:
            reminder = msg_4.get("reminderContent", "")
            sender_id_raw = str(msg_4.get("senderUserId", "") or "")
            # 去掉 @goofish 后缀
            sender_id = sender_id_raw.split("@")[0] if "@" in sender_id_raw else sender_id_raw
            sender_name = str(msg_4.get("reminderTitle", "") or "系统")
            cid_raw = str(msg.get("2", ""))
            cid = cid_raw.split("@")[0] if "@" in cid_raw else cid_raw
            msg_time = msg.get("5", 0)

            logger.info(
                f"【{self.account_id}】[推送-卡片更新] cid={cid}, "
                f"发送者={sender_name}, 内容: {reminder}"
            )

            # 卡片更新（付款状态变更等）原始载荷采样，整包 msg_4 落盘
            sample_card(
                "push_card_update", self.account_id, cid,
                str(msg.get("3", "") or ""), str(reminder), msg_4,
            )

            return {
                "event": "new_message",
                "cid": cid,
                "message": {
                    "messageId": str(msg.get("3", "") or ""),
                    "senderId": sender_id,
                    "senderName": sender_name,
                    "isSelf": self._is_self_sender(sender_id),
                    "managedSender": self._is_managed_sender(sender_id),
                    "type": "text",
                    "text": reminder,
                    "images": [],
                    "time": msg_time,
                    **dict(zip(("isEvent", "eventKind"), classify_event(reminder, "text"))),
                },
            }
        except Exception as e:
            logger.warning(f"【{self.account_id}】解析卡片更新推送失败: {e}")
            return None

    def _decode_content(self, msg_1: dict) -> Tuple[str, List[str], str, str, int]:
        """
        从推送消息中解码内容（兼容明文 JSON 与 base64 两种载荷）

        IM 主动推送的图片/文本/语音内容 JSON 实际位于 msg["1"]["6"]["3"]["5"]，且为
        【明文 JSON 字符串】；旧格式则在 ["1"] 为 base64。信封字段在此提取，内容
        解析（识别明文/base64、判定文本/图片/语音）统一交由公共模块
        common.utils.xianyu_message_parser 处理，与 HTTP 历史消息、自动回复侧共用
        同一套逻辑，避免各写一套互相不同步。

        Returns:
            (text, images, msg_type, audio_url, audio_duration) 五元组。
            语音消息会额外给出音频地址与时长，供前端播放 / 转文字。
        """
        try:
            msg_6 = msg_1.get("6", {})
            if not isinstance(msg_6, dict):
                return ("", [], "text", "", 0)
            msg_6_3 = msg_6.get("3", {})
            if not isinstance(msg_6_3, dict):
                return ("", [], "text", "", 0)

            # 候选载荷按优先级：["5"]（实时推送明文 JSON）→ ["1"]（旧格式 base64）
            return parse_content_with_audio(
                [msg_6_3.get("5", ""), msg_6_3.get("1", "")]
            )
        except Exception:
            return ("", [], "text", "", 0)
