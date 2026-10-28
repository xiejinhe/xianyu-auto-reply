"""
消息处理模块

负责解析和处理WebSocket接收到的消息
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Dict, Any, Optional, Callable

from loguru import logger

from .utils import safe_str
from common.utils.xianyu_utils import decrypt
from common.utils.xianyu_message_parser import decode_first_content, interpret_content
from common.utils.card_sampler import sample_card
from common.utils.order_card_parser import parse_rich_card
from app.services.backend_push_forwarder import (
    schedule_chat_push,
    schedule_peer_read_push,
    schedule_typing_push,
)

# 原始帧调试开关（环境变量 IM_FRAME_DEBUG=1 开启）：把「不是聊天消息」的
# 推送帧原样落日志。闲鱼的「对方正在输入」「对方已读」这类状态帧既不是
# 聊天消息也没有卡片字段，默认在分发时整条丢弃，不开这个开关就完全看不到
# 服务端到底有没有下发，也就无从判断相关功能能不能做。
# 必须走 get_settings()：pydantic 只把 .env 读进配置对象，不会写进进程环境。
def _frame_debug_enabled() -> bool:
    try:
        from app.core.config import get_settings

        return bool(getattr(get_settings(), "im_frame_debug", False))
    except Exception:
        return False


def _sample_ws_message(account_id: str, message_id: str, message: dict) -> None:
    """订单卡片原始载荷采样（只读落盘，零风控）。

    这套长连接收到全部推送（backend-web 的网页 IM 连接收不到 APP 侧实时
    卡片），是订单卡片采样的主通道。chat/card/card-update 三类消息的原始
    解密 dict 原样交给共用采样器（common.utils.card_sampler），由它按
    messageId 去重、按交易关键词过滤后落盘。任何异常都不能影响主链路。
    """
    try:
        text = ""
        cid = ""
        msg_1 = message.get("1")
        if isinstance(msg_1, dict):
            # 聊天/卡片消息：cid 在 ["2"]，标题在 ["6"]["3"]["2"]，
            # 系统摘要在 ["10"]["reminderContent"]
            cid_raw = str(msg_1.get("2", "") or "")
            cid = cid_raw.split("@")[0] if "@" in cid_raw else cid_raw
            msg_6 = msg_1.get("6", {})
            msg_6_3 = msg_6.get("3", {}) if isinstance(msg_6, dict) else {}
            if isinstance(msg_6_3, dict):
                text = str(msg_6_3.get("2", "") or "")
            msg_10 = msg_1.get("10", {})
            if isinstance(msg_10, dict) and msg_10.get("reminderContent"):
                text = str(msg_10.get("reminderContent") or "")
        elif isinstance(message.get("4"), dict):
            # 卡片更新（付款状态变更等）：cid 在 ["2"]，摘要在 ["4"]
            cid_raw = str(message.get("2", "") or "")
            cid = cid_raw.split("@")[0] if "@" in cid_raw else cid_raw
            text = str(message["4"].get("reminderContent", "") or "")
        sample_card("ws_push", account_id, cid, message_id, text, message)
    except Exception:
        pass  # 采样失败绝不影响消息处理主链路


class MessageHandler:
    """消息处理器
    
    负责：
    - 消息解析
    - 消息去重
    - 消息防抖
    - 消息分发
    """
    
    def __init__(self, cookie_id: str, myid: str = None):
        """初始化消息处理器
        
        Args:
            cookie_id: 账号ID
            myid: 用户ID（从cookies的unb字段获取），用于判断是否是自己发出的消息
        """
        self.cookie_id = cookie_id
        self.myid = myid or cookie_id  # 如果没有传入myid，使用cookie_id作为备选

        # 消息去重
        self.processed_message_ids: Dict[str, float] = {}
        self.processed_message_ids_lock = asyncio.Lock()
        self.processed_message_ids_max_size = 10000

        # 消息防抖
        self.message_debounce_tasks: Dict[str, Dict] = {}
        self.message_debounce_delay = 3  # 秒
        self.message_debounce_lock = asyncio.Lock()

        # 消息处理回调
        # 必须在这里全部先置 None：分发处是直接 `if self._on_xxx:` 访问属性，
        # 而 set_*_handler 只会在启动时注册部分回调（订单/系统回调目前无人注册），
        # 未注册时连属性都没有会直接抛 AttributeError，把整条消息处理链路打断。
        self._on_chat_message: Optional[Callable] = None
        self._on_system_message: Optional[Callable] = None
        self._on_order_message: Optional[Callable] = None
        self._on_card_message: Optional[Callable] = None  # 卡片消息回调（小刀等）
        self._on_card_update_message: Optional[Callable] = None  # 卡片更新消息回调（付款状态变更等）
        self.message_expire_time = self._load_message_expire_time()  # 从数据库加载

    def _is_self_sender(self, sender_id: str) -> bool:
        """是否「本账号自己」发出的消息（严格只比 myid）。

        2026-10-28 修正：此前「任一托管账号都算自己」（2026-10-20 为防误响铃
        引入），副作用是托管账号互聊会话里，对方（自己的另一个托管号）的消息
        在长连接推送中被标成 isSelf=true，而 HTTP 历史接口（backend-web 的
        _parse_message，2026-10-26 已改为只比 myid）给的是 isSelf=false，
        两条数据源打架 → 前端互聊会话消息全渲染到同一侧（2026-10-28 实测）。

        防误响铃改由 managed_sender 字段承担（见 _is_managed_peer），不再污染
        isSelf。自动回复的「自己消息」判定在 auto_reply_service 里独立实现
        （myid 或托管账号，宽松语义），不受本次改动影响。
        """
        sid = str(sender_id or "").strip()
        if not sid:
            return False
        return bool(self.myid and sid == str(self.myid))

    def _is_managed_peer(self, sender_id: str) -> bool:
        """发送者是否为「其它托管账号」（非本账号）：前端据此跳过响铃与跨账号 toast。

        承接 2026-10-20 防误响铃的初衷：cid 跨账号共享时，另一个托管账号发的
        消息本账号长连接也收得到，渲染上是对方（isSelf=false，左侧），但不该
        当成真实买家消息去响铃/弹「账号收到新消息」toast。
        """
        sid = str(sender_id or "").strip()
        if not sid or not self.myid or sid == str(self.myid):
            return False
        try:
            from common.utils.managed_account_ids import is_managed_account

            return is_managed_account(sid)
        except Exception:
            return False

    def _load_message_expire_time(self) -> int:
        """从数据库加载当前账号的相同消息等待时间配置（参照旧框架）"""
        try:
            from common.db.compat import db_manager
            expire_time = db_manager.get_cookie_message_expire_time(self.cookie_id)
            if expire_time is not None and expire_time >= 60:
                logger.info(f"【{self.cookie_id}】加载消息等待时间配置: {expire_time}秒")
                return expire_time
            return 3600  # 默认1小时
        except Exception as e:
            logger.warning(f"【{self.cookie_id}】加载消息等待时间配置失败: {e}，使用默认值3600秒")
            return 3600
    
    def set_chat_message_handler(self, handler: Callable):
        """设置聊天消息处理回调"""
        self._on_chat_message = handler
    
    def set_system_message_handler(self, handler: Callable):
        """设置系统消息处理回调"""
        self._on_system_message = handler
    
    def set_order_message_handler(self, handler: Callable):
        """设置订单消息处理回调"""
        self._on_order_message = handler
    
    def set_card_message_handler(self, handler: Callable):
        """设置卡片消息处理回调（用于小刀等卡片消息）"""
        self._on_card_message = handler
    
    def set_card_update_message_handler(self, handler: Callable):
        """设置卡片更新消息处理回调（用于付款状态变更等）"""
        self._on_card_update_message = handler
    
    def is_card_update_message(self, message: dict) -> bool:
        """判断是否为卡片更新消息
        
        卡片更新消息的特征：message["1"]是字符串，message["4"]包含reminderContent
        这类消息用于通知付款状态变更等，需要提取订单信息并触发自动发货
        """
        try:
            return (
                isinstance(message, dict)
                and "1" in message
                and isinstance(message["1"], str)
                and "4" in message
                and isinstance(message["4"], dict)
                and "reminderContent" in message["4"]
            )
        except Exception:
            return False
    
    def is_system_tip_message(self, message: dict) -> bool:
        """判断是否为平台系统提示 / 营销活动类消息（如"队友喊你来打气"任务提醒）

        这类消息不是买卖双方的真实聊天，不应触发自动回复，也不写入消息日志。

        识别依据（唯一且最可靠的标记）：
            extJson 中 msgArg1 == "MsgTips"
            —— 这是闲鱼平台对"营销/运营提示类消息"的明确分类标记。

        为什么只用这一个条件（确保不误杀正常消息 / 发货消息）：
        - 正常文本、图片、卡片消息不属于该营销分类，不会带 msgArg1=="MsgTips"。
        - 交易类消息（[我已拍下，待付款]、[我已付款，等待你发货]、评价请求、
          确认收货等）是靠 reminderContent 文本识别并触发发货 / 评价流程的，
          它们同样不属于营销提示分类，不会带 msgArg1=="MsgTips"。
        - 不使用 message["1"]["6"]["1"]==101 判断：101 是"自定义消息类型"，
          正常卡片消息（小刀、评价、确认收货）也是 101，会误杀。
        - 不使用 contentType=="14" 判断：缺乏交易类消息的内容类型样本佐证，
          为避免任何潜在误杀，宁可放过个别提示消息，也不拦截真实业务消息。
        """
        try:
            if not isinstance(message, dict):
                return False
            inner = message.get("1")
            if not isinstance(inner, dict):
                return False
            meta = inner.get("10")
            if not isinstance(meta, dict):
                return False
            ext_json = meta.get("extJson", "")
            if not isinstance(ext_json, str) or not ext_json:
                return False
            try:
                ext = json.loads(ext_json)
            except (json.JSONDecodeError, TypeError):
                return False
            if isinstance(ext, dict) and ext.get("msgArg1") == "MsgTips":
                return True
        except Exception:
            return False
        return False

    def is_chat_message(self, message: dict) -> bool:
        """判断是否为用户聊天消息"""
        try:
            return (
                isinstance(message, dict)
                and "1" in message
                and isinstance(message["1"], dict)
                and "10" in message["1"]
                and isinstance(message["1"]["10"], dict)
                and "reminderContent" in message["1"]["10"]
            )
        except Exception:
            return False
    
    def is_sync_package(self, message_data: dict) -> bool:
        """判断是否为同步包消息"""
        try:
            return (
                isinstance(message_data, dict)
                and "body" in message_data
                and "syncPushPackage" in message_data["body"]
                and "data" in message_data["body"]["syncPushPackage"]
                and len(message_data["body"]["syncPushPackage"]["data"]) > 0
            )
        except Exception:
            return False
    
    def extract_message_id(self, message_data: dict) -> Optional[str]:
        """从消息数据中提取消息ID"""
        try:
            if isinstance(message_data, dict) and "1" in message_data:
                message_1 = message_data.get("1")
                if isinstance(message_1, dict) and "10" in message_1:
                    message_10 = message_1.get("10")
                    if isinstance(message_10, dict) and "bizTag" in message_10:
                        biz_tag = message_10.get("bizTag", "")
                        if isinstance(biz_tag, str):
                            try:
                                biz_tag_dict = json.loads(biz_tag)
                                if isinstance(biz_tag_dict, dict) and "messageId" in biz_tag_dict:
                                    return biz_tag_dict.get("messageId")
                            except (json.JSONDecodeError, TypeError):
                                pass
                        
                        if "extJson" in message_10:
                            ext_json = message_10.get("extJson", "")
                            if isinstance(ext_json, str):
                                try:
                                    ext_json_dict = json.loads(ext_json)
                                    if isinstance(ext_json_dict, dict) and "messageId" in ext_json_dict:
                                        return ext_json_dict.get("messageId")
                                except (json.JSONDecodeError, TypeError):
                                    pass
            # 卡片更新消息：消息ID在message["4"]中
            if isinstance(message_data, dict) and "4" in message_data:
                message_4 = message_data.get("4")
                if isinstance(message_4, dict):
                    ext_json = message_4.get("extJson", "")
                    if isinstance(ext_json, str):
                        try:
                            ext_json_dict = json.loads(ext_json)
                            if isinstance(ext_json_dict, dict) and "messageId" in ext_json_dict:
                                return ext_json_dict.get("messageId")
                        except (json.JSONDecodeError, TypeError):
                            pass
        except Exception as e:
            logger.debug(f"【{self.cookie_id}】提取消息ID失败: {safe_str(e)}")
        
        return None
    
    async def is_message_processed(self, message_id: str) -> bool:
        """检查消息是否已处理"""
        async with self.processed_message_ids_lock:
            current_time = time.time()
            
            if message_id in self.processed_message_ids:
                last_process_time = self.processed_message_ids[message_id]
                time_elapsed = current_time - last_process_time
                
                if time_elapsed < self.message_expire_time:
                    return True
            
            return False
    
    async def mark_message_processed(self, message_id: str):
        """标记消息为已处理"""
        async with self.processed_message_ids_lock:
            current_time = time.time()
            self.processed_message_ids[message_id] = current_time
            
            # 清理过期记录
            if len(self.processed_message_ids) > self.processed_message_ids_max_size:
                expired_ids = [
                    msg_id for msg_id, timestamp in self.processed_message_ids.items()
                    if current_time - timestamp > self.message_expire_time
                ]
                
                for msg_id in expired_ids:
                    del self.processed_message_ids[msg_id]
                
                if expired_ids:
                    logger.info(f"【{self.cookie_id}】清理了 {len(expired_ids)} 个过期消息ID")
    
    def parse_chat_message(self, message: dict) -> Optional[Dict[str, Any]]:
        """解析聊天消息（支持普通消息和卡片消息两种格式）"""
        try:
            message_1 = message.get("1", {})
            message_10 = message_1.get("10", {})
            
            # 提取会话ID（参照旧框架：从message_1["2"]提取）
            chat_id_raw = message_1.get("2", "")
            chat_id = chat_id_raw.split('@')[0] if '@' in str(chat_id_raw) else str(chat_id_raw)
            
            # 提取消息时间
            msg_time = message_1.get("5", 0)
            if msg_time:
                msg_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(msg_time / 1000))
            else:
                msg_time = time.strftime("%Y-%m-%d %H:%M:%S")

            # 判断消息格式：有"10"字段且有reminderContent是普通聊天消息
            # （revoked/revoked_original 先置位，两条分支都可能改写；见下方撤回识别）
            revoked = False
            revoked_original = ""
            if message_10 and message_10.get("reminderContent"):
                # 普通聊天消息格式
                send_user_id = message_10.get("senderUserId", "unknown")
                reminder_content = message_10.get("reminderContent", "")
                # 提取发送者名称（参照旧框架：优先使用senderNick，再用reminderTitle）
                # 注意：需要检查空字符串
                send_user_name = message_10.get("senderNick") or message_10.get("reminderTitle") or "系统"

                # 解析真实消息内容（复用公共模块）。买家发图片时 reminderContent 只是
                # 占位符 "[图片]"，这里从原始报文解码出真实图片 URL，并把 send_message
                # 直接替换为图片链接，使下游 AI 回复、关键词匹配、消息通知都能拿到链接。
                # 内容载荷在 message["1"]["6"]["3"]["5"]（明文JSON）或 ["1"]（base64）。
                _m6 = message_1.get("6", {})
                _m6_3 = _m6.get("3", {}) if isinstance(_m6, dict) else {}
                _candidates = (
                    [_m6_3.get("5", ""), _m6_3.get("1", "")]
                    if isinstance(_m6_3, dict) else []
                )
                content_dict = decode_first_content(_candidates)
                if content_dict is not None:
                    parsed_text, parsed_images, parsed_type = interpret_content(content_dict)
                    if parsed_type == "image" and parsed_images:
                        # 多张图片用换行拼接为链接文本，直接替换 send_message
                        reminder_content = "\n".join(parsed_images)
                    elif parsed_type == "text" and parsed_text:
                        # 用解码出的准确文本（回退保留 reminderContent）
                        reminder_content = parsed_text

                # 撤回占位识别：正文载荷（文本/图片/语音 JSON）整个消失，只剩
                # reminderContent 旧摘要——这是闲鱼「撤回」在长连接推送里的形态，
                # 与 HTTP 历史路径实测形态一致（2026-10-20，账号 2217728125233）。
                # 判据保守：正常聊天消息必有正文载荷，「载荷空 + 有摘要」只会
                # 出现在撤回场景。命中的消息 send_message 换成撤回提示文案，
                # 旧摘要随 revoked_original 下发，前端默认折叠、点「显示」才展开。
                revoked = False
                revoked_original = ""
                _has_payload = any(
                    isinstance(c, str) and c.strip() for c in _candidates
                )
                if not _has_payload and str(reminder_content or "").strip():
                    revoked = True
                    revoked_original = str(reminder_content).strip()
                    # 只有确认是撤回占位时才改写文案：这行必须在 if 块内。
                    # v1.0.51 改 isSelf 判定时把它误缩进到 if 外面，导致**每一条**
                    # 长连接推送的正文都被替换成「撤回」提示（2026-10-20 实测）。
                    reminder_content = (
                        "你撤回了一条消息"
                        if self._is_self_sender(send_user_id)
                        else "对方撤回了一条消息"
                    )
            else:
                # 卡片消息格式（如评价请求、确认收货等系统卡片）
                # 结构: {"1": {"1": {"1": "xxx@goofish"}, "2": "xxx@goofish", "6": {"3": {"2": "消息内容"}}}}
                message_1_1 = message_1.get("1", {})
                if isinstance(message_1_1, dict):
                    sender_raw = message_1_1.get("1", "")
                    send_user_id = sender_raw.split('@')[0] if '@' in str(sender_raw) else str(sender_raw)
                else:
                    send_user_id = "unknown"
                
                # 从卡片消息中提取内容
                message_6 = message_1.get("6", {})
                message_6_3 = message_6.get("3", {})
                reminder_content = message_6_3.get("2", "")  # 卡片消息的文本内容
                
                # 卡片消息通常是系统消息，用户名设为"系统"
                send_user_name = "系统"
            
            # 提取商品ID
            item_id = self._extract_item_id(message)

            return {
                "send_user_id": send_user_id,
                "send_user_name": send_user_name,
                "send_message": reminder_content,
                "chat_id": chat_id,
                "item_id": item_id,
                "msg_time": msg_time,
                # 引用消息（回复）：被引用消息的 IM messageId（协议实证
                # 2026-10-20，APP 样本：正文纯文本，引用关系在
                # message.extension.replyMessageId）
                "reply_to_message_id": str(message_10.get("replyMessageId", "") or ""),
                "is_plain_text": self.is_plain_text_message(message),
                "raw_message": message,
                # 撤回占位标记（False = 普通消息）：True 时 send_message 已是撤回
                # 提示文案，revoked_original 为撤回前旧摘要。见上方识别逻辑。
                "revoked": revoked,
                "revoked_original": revoked_original,
                # 订单卡片富数据（按钮/订单号/演出信息字段），非交易卡片为 None。
                # 富内容在 msg["1"]["6"]["3"]["5"] 的 dxCard JSON 里，见
                # common.utils.order_card_parser 说明。
                "rich_card": parse_rich_card(message),
            }
            
        except Exception as e:
            logger.error(f"【{self.cookie_id}】解析聊天消息失败: {safe_str(e)}")
            return None
    
    def parse_card_update_message(self, message: dict) -> Optional[Dict[str, Any]]:
        """解析卡片更新消息（message["1"]为字符串的特殊格式）
        
        此类消息结构：
        - message["1"]: 字符串（包含商品相关ID）
        - message["2"]: 会话ID
        - message["4"]: 类似标准消息的message["1"]["10"]，包含reminderContent等
        - message["5"]: 时间戳
        """
        try:
            message_4 = message.get("4", {})
            
            # 提取会话ID（从message["2"]）
            chat_id_raw = message.get("2", "")
            chat_id = chat_id_raw.split('@')[0] if '@' in str(chat_id_raw) else str(chat_id_raw)
            
            # 提取消息时间
            msg_time = message.get("5", 0)
            if msg_time:
                msg_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(msg_time / 1000))
            else:
                msg_time = time.strftime("%Y-%m-%d %H:%M:%S")
            
            # 从message["4"]中提取消息内容（结构与标准消息的message["1"]["10"]相同）
            reminder_content = message_4.get("reminderContent", "")
            send_user_id = message_4.get("senderUserId", "unknown")
            send_user_name = message_4.get("reminderTitle", "系统")
            
            # 提取商品ID
            item_id = self._extract_item_id_from_card_update(message_4)
            
            return {
                "send_user_id": send_user_id,
                "send_user_name": send_user_name,
                "send_message": reminder_content,
                "chat_id": chat_id,
                "item_id": item_id,
                "msg_time": msg_time,
                "is_plain_text": False,
                "raw_message": message,
            }
            
        except Exception as e:
            logger.error(f"【{self.cookie_id}】解析卡片更新消息失败: {safe_str(e)}")
            return None
    
    def _extract_item_id_from_card_update(self, message_4: dict) -> str:
        """从卡片更新消息的message["4"]中提取商品ID"""
        try:
            # 方法1: 从reminderUrl中提取
            url_info = message_4.get("reminderUrl", "")
            if url_info and "itemId=" in str(url_info):
                item_id = url_info.split("itemId=")[1].split("&")[0]
                if item_id:
                    return str(item_id)
            
            # 方法2: 从extJson中提取
            ext_json = message_4.get("extJson", "")
            if ext_json:
                try:
                    ext_json_dict = json.loads(ext_json)
                    item_id = ext_json_dict.get("itemId", "")
                    if item_id:
                        return str(item_id)
                except Exception:
                    pass
            
            return ""
        except Exception:
            return ""
    
    def _extract_item_id(self, message: dict) -> str:
        """从消息中提取商品ID（参照旧框架message_handler_core.py）"""
        try:
            message_1 = message.get("1", {})
            message_10 = message_1.get("10", {})
            
            # 方法1: 从reminderUrl提取（参照旧框架，优先级最高）
            url_info = message_10.get("reminderUrl", "")
            if url_info and "itemId=" in str(url_info):
                item_id = url_info.split("itemId=")[1].split("&")[0]
                if item_id:
                    return str(item_id)
            
            # 方法2: 尝试从bizTag提取
            biz_tag = message_10.get("bizTag", "")
            if biz_tag:
                try:
                    biz_tag_dict = json.loads(biz_tag)
                    item_id = biz_tag_dict.get("itemId", "")
                    if item_id:
                        return str(item_id)
                except Exception:
                    pass
            
            # 方法3: 尝试从extJson提取
            ext_json = message_10.get("extJson", "")
            if ext_json:
                try:
                    ext_json_dict = json.loads(ext_json)
                    item_id = ext_json_dict.get("itemId", "")
                    if item_id:
                        return str(item_id)
                except Exception:
                    pass
            
            # 方法4: 从卡片消息的JSON内容中提取（用于评价请求等卡片消息）
            message_6 = message_1.get("6", {})
            message_6_3 = message_6.get("3", {})
            card_json_str = message_6_3.get("5", "")
            if card_json_str:
                try:
                    card_content = json.loads(card_json_str)
                    # 尝试从jumpUrl中提取itemId
                    jump_url = card_content.get("dxCard", {}).get("item", {}).get("main", {}).get("exContent", {}).get("button", {}).get("intent", {}).get("page", {}).get("jumpUrl", "")
                    if jump_url and "itemId=" in jump_url:
                        item_id = jump_url.split("itemId=")[1].split("&")[0]
                        if item_id:
                            return str(item_id)
                except Exception:
                    pass
            
            return ""
        except Exception:
            return ""
    
    def extract_card_title(self, message: dict) -> Optional[str]:
        """从卡片消息中提取标题（参照旧框架message_handler_core.py）
        
        用于检测"小刀"等卡片消息
        
        Args:
            message: 消息数据
            
        Returns:
            卡片标题，如"我已小刀，待刀成"
        """
        try:
            message_1 = message.get("1", {})
            message_6 = message_1.get("6", {})
            message_6_3 = message_6.get("3", {})
            if "5" in message_6_3:
                card_content = json.loads(message_6_3["5"])
                return card_content.get("dxCard", {}).get("item", {}).get("main", {}).get("exContent", {}).get("title", "")
        except Exception:
            pass
        return None
    
    def is_card_message(self, message: dict) -> bool:
        """判断是否为卡片消息（参照旧框架）"""
        try:
            message_1 = message.get("1", {})
            message_6 = message_1.get("6", {})
            return isinstance(message_6, dict) and "3" in message_6
        except Exception:
            return False

    def is_plain_text_message(self, message: dict) -> bool:
        """判断是否为普通文本消息

        仅当消息内容载荷明确为文本，且不存在 dxCard 卡片结构时返回 True。
        图片、语音、卡片、卡片更新消息以及无法确认类型的消息均返回 False，
        保持原有自动发货和免拼发货逻辑。

        Args:
            message: 原始消息数据
        Returns:
            True 表示普通文本消息，False 表示非普通文本或无法确认类型
        """
        try:
            if not isinstance(message, dict):
                return False

            message_1 = message.get("1")
            if not isinstance(message_1, dict):
                return False

            message_10 = message_1.get("10")
            if not isinstance(message_10, dict):
                return False

            reminder_content = message_10.get("reminderContent", "")
            if not reminder_content or reminder_content == "[卡片消息]":
                return False

            message_6 = message_1.get("6")
            if not isinstance(message_6, dict):
                return False

            message_6_3 = message_6.get("3")
            if not isinstance(message_6_3, dict):
                return False

            candidates = [message_6_3.get("5", ""), message_6_3.get("1", "")]
            content_dict = decode_first_content(candidates)
            if not isinstance(content_dict, dict):
                return False

            # 卡片结构优先，即使同时携带 text 也不能按普通文本处理。
            if content_dict.get("dxCard"):
                return False

            content_type = content_dict.get("contentType")
            if content_type == 3 or "audio" in content_dict:
                return False
            if content_type == 2 or "image" in content_dict or "picUrl" in content_dict:
                return False
            if content_type == 1 and "text" in content_dict:
                return True
            return "text" in content_dict
        except Exception as e:
            logger.warning(f"【{self.cookie_id}】判断普通文本消息失败: {safe_str(e)}")
            return False
    
    async def handle_message(self, message_data: dict, websocket) -> bool:
        """处理消息"""
        try:
            # 发送ACK确认消息（参照旧框架，必须发送否则服务器会断开连接）
            await self._send_ack(message_data, websocket)
            
            # 检查是否为同步包
            if self.is_sync_package(message_data):
                sync_data_list = message_data["body"]["syncPushPackage"]["data"]
                for sync_data in sync_data_list:
                    if isinstance(sync_data, dict):
                        # 解密消息（参照旧框架）
                        message = self._decrypt_message(sync_data)
                        if message is not None and isinstance(message, dict):
                            if _frame_debug_enabled():
                                self._log_unrecognized_frame(message)
                            await self._process_single_message(message, websocket)
                return True
            
            # 处理单条消息（非同步包）
            return await self._process_single_message(message_data, websocket)
            
        except Exception as e:
            logger.error(f"【{self.cookie_id}】处理消息异常: {safe_str(e)}")
            return False
    
    async def _send_ack(self, message_data: dict, websocket) -> None:
        """发送ACK确认消息（参照旧框架实现）
        
        服务器需要收到ACK确认，否则可能会断开连接
        
        Args:
            message_data: 原始消息数据
            websocket: WebSocket连接
        """
        try:
            from common.utils.xianyu_utils import generate_mid
            
            headers = message_data.get("headers", {})
            ack = {
                "code": 200,
                "headers": {
                    "mid": headers.get("mid", generate_mid()),
                    "sid": headers.get("sid", ""),
                }
            }
            # 复制部分原始headers
            for key in ['app-key', 'ua', 'dt']:
                if key in headers:
                    ack["headers"][key] = headers[key]
            
            await websocket.send(json.dumps(ack))
        except Exception:
            pass  # ACK发送失败不影响主流程
    
    def _decrypt_message(self, sync_data: dict) -> Optional[dict]:
        """解密消息数据（参照旧框架实现）
        
        Args:
            sync_data: 同步包中的单条数据
            
        Returns:
            解密后的消息字典，解密失败返回None
        """
        if "data" not in sync_data:
            return None
        try:
            data = sync_data["data"]
            try:
                # 先尝试base64解码
                data = base64.b64decode(data).decode("utf-8")
                parsed_data = json.loads(data)
                # 处理未加密的消息（如系统提示等）
                if isinstance(parsed_data, dict) and 'chatType' in parsed_data:
                    # 系统消息不需要处理，直接返回None
                    return None
                # 过滤不需要打印的消息类型（如商品定价失败通知）
                biz_type = parsed_data.get('bizType', '') if isinstance(parsed_data, dict) else ''
                if biz_type not in ('IDLE_SPACE_PRICING',) and not self.is_system_tip_message(parsed_data):
                    logger.warning(f"【{self.cookie_id}】解密消息: {json.dumps(parsed_data, ensure_ascii=False)[:1000]}")
                return parsed_data
            except Exception:
                # base64解码失败，尝试使用decrypt解密
                decrypted = json.loads(decrypt(data))
                # 过滤不需要打印的消息类型
                biz_type = decrypted.get('bizType', '') if isinstance(decrypted, dict) else ''
                if biz_type not in ('IDLE_SPACE_PRICING',) and not self.is_system_tip_message(decrypted):
                    logger.info(f"【{self.cookie_id}】解密消息: {json.dumps(decrypted, ensure_ascii=False)[:1000]}")
                return decrypted
        except Exception as e:
            logger.debug(f"【{self.cookie_id}】消息解密失败: {safe_str(e)}")
            return None
    
    def parse_typing_event(self, message: dict) -> Optional[Dict[str, Any]]:
        """解析「对方正在输入」状态帧

        帧结构（实测，App 通道）：
            {"1": [{"1": "67673454243@goofish", "2": 1, "3": 1,
                    "4": "2217728125233@goofish"}]}
        - "1" 是**列表**（不是聊天消息那种 dict），元素里 "1" = 会话ID
        - "2" 固定为 1，状态类型判别位
        - "3" = 1 正在输入 / 0 停止输入
        - "4" = 接收方（本账号），说明描述的是对端在该会话的输入状态

        实测时间线：对端打字期间收到 3=1，停手后收到 3=0，全程没有消息发送，
        因此可以确认 "3" 就是输入中状态，而不是已读回执。

        Returns:
            {"cid": 会话ID, "typing": bool}；不是状态帧返回 None
        """
        try:
            inner = message.get("1")
            if not isinstance(inner, list) or not inner:
                return None
            item = inner[0]
            if not isinstance(item, dict):
                return None
            state = item.get("3")
            if state not in (0, 1):
                return None
            # "2" 必须是 1：与同结构的其它列表型帧区分开，避免误判成输入状态
            if str(item.get("2", "")) != "1":
                return None
            cid = str(item.get("1") or "").split("@")[0].strip()
            if not cid:
                return None
            return {"cid": cid, "typing": state == 1}
        except Exception:
            return None

    def _log_unrecognized_frame(self, message: dict) -> None:
        """IM_FRAME_DEBUG=1 时，把不是聊天消息的推送帧原样落日志

        闲鱼的「对方正在输入」「对方已读」这类状态帧不是聊天消息，
        默认在分发时被整条丢弃，不开开关就完全不知道服务端有没有下发。
        聊天消息与卡片更新帧不落日志，避免刷屏。
        """
        try:
            if self.is_chat_message(message) or self.is_card_update_message(message):
                return
            # 输入状态帧已单独解析成功能，不再当作「未识别」刷日志
            if self.parse_typing_event(message):
                return
            # 已读回执帧同理（parse_peer_read_frame 见 _process_single_message）
            if self.parse_peer_read_frame(message):
                return
            logger.info(
                f"【{self.cookie_id}】[未识别帧] "
                f"{json.dumps(message, ensure_ascii=False)[:800]}"
            )
        except Exception:
            pass

    def parse_peer_read_frame(self, message: dict) -> Optional[Dict[str, Any]]:
        """识别「对方已读回执」帧并解析（2026-10-20 协议实证）。

        闲鱼把已读做在 IM 长连接上：对端一打开会话，服务端就把被读消息的
        ID 推给**发送方**。日志里抓到两种形态，都处理：

        形态A（批量，部分账号/版本）：
            {"1": ["4351812252653.PNM", ...], "2": 2, "3": "cid@goofish",
             "4": 1, "5": 1792501465057}
        形态B（单条）：
            {"1": "4351730144026.PNM", "2": 2, "3": 0,
             "4": "68391672401@goofish", "5": 1, "6": 1792505206817}

        形态B 实证：消息 4351730144026.PNM 创建于 20:36:56，回执帧在
        22:06:46 到达（**晚 89.8 分钟**），且 36 秒后同一 cid 出现「对方
        正在输入」——说明是买家打开了会话把它读了，绝不是发送确认/送达回执
        （那种只会在发送后几秒内到）。之前它被当作未识别帧丢弃。

        共同判据："2" == 2。区分聊天消息（"2":1）和其他同步帧（"1" 是
        字符串且 "3"/"4" 都不含 @goofish）。
        """
        try:
            if str(message.get("2", "")) != "2":
                return None

            def _cid_of(value) -> str:
                text = str(value or "")
                return text.split("@")[0] if "@goofish" in text else ""

            raw_ids = message.get("1")

            # 形态A："1" 是列表、"3" 是 cid
            if isinstance(raw_ids, list) and raw_ids:
                cid = _cid_of(message.get("3"))
                if not cid:
                    return None
                ids = [str(i) for i in raw_ids if str(i).strip()]
                read_at = self._as_int(message.get("5"))
                shape = "A"

            # 形态B："1" 是单个 ID 字符串、"4" 是 cid、"6" 是时间戳
            elif isinstance(raw_ids, str) and raw_ids.strip():
                cid = _cid_of(message.get("4"))
                if not cid:
                    return None
                ids = [raw_ids.strip()]
                read_at = self._as_int(message.get("6"))
                shape = "B"

            else:
                return None

            if not ids:
                return None
            return {
                "cid": cid,
                "message_ids": ids,
                "read_at": read_at,
                "shape": shape,
            }
        except Exception:
            return None

    @staticmethod
    def _as_int(value) -> int:
        """时间戳字段转毫秒整数（闲鱼有时给字符串，有时给数字）"""
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    async def _process_single_message(self, message: dict, websocket) -> bool:
        """处理单条消息（参照旧框架message_handler_core.py）"""
        try:
            # 「对方正在输入」状态帧：不是聊天消息，单独转发给 backend-web
            # 让网页端显示输入提示，然后直接结束，不进入下面的消息处理流程
            typing_event = self.parse_typing_event(message)
            if typing_event:
                schedule_typing_push(
                    account_id=self.cookie_id,
                    cid=typing_event["cid"],
                    typing=typing_event["typing"],
                )
                logger.debug(
                    f"【{self.cookie_id}】输入状态: cid={typing_event['cid']}, "
                    f"typing={typing_event['typing']}"
                )
                return True

            # 「对方已读回执」帧：对方打开会话后，服务端把被读消息的 ID
            # 逐条推给发送方。转发给前端把对应消息标记成「已读」，
            # 这就是闲鱼 APP 气泡下已读/未读的数据源（此前一直当
            # 未识别帧丢弃，2026-10-20 实证后接入）。
            peer_read = self.parse_peer_read_frame(message)
            if peer_read:
                schedule_peer_read_push(
                    account_id=self.cookie_id,
                    cid=peer_read["cid"],
                    message_ids=peer_read["message_ids"],
                    read_at=peer_read["read_at"],
                )
                logger.info(
                    f"【{self.cookie_id}】对方已读回执(形态{peer_read['shape']}): "
                    f"cid={peer_read['cid']}, "
                    f"n={len(peer_read['message_ids'])}, "
                    f"ids={peer_read['message_ids'][:3]}"
                )
                return True

            # 提取消息ID进行去重
            message_id = self.extract_message_id(message)
            if message_id:
                if await self.is_message_processed(message_id):
                    logger.debug(f"【{self.cookie_id}】消息已处理，跳过: {message_id[:20]}...")
                    return True
                await self.mark_message_processed(message_id)
            
            # 过滤平台系统提示 / 营销活动类消息（如"打气"任务提醒）：
            # 这类消息会命中 is_chat_message（含 reminderContent），但并非真实聊天，
            # 不触发自动回复，也不记录消息日志，直接跳过。
            if self.is_system_tip_message(message):
                logger.debug(f"【{self.cookie_id}】跳过系统提示/营销活动消息")
                return True
            
            # 订单卡片原始载荷采样：chat/card/card-update 三类消息的原始解密
            # dict 落盘（只读，零风控）。这套长连接是订单卡片采样的主通道——
            # backend-web 的网页 IM 连接收不到 APP 侧实时卡片（2026-10-19 实测：
            # 用户 APP 走完整流程，backend-web 侧零推送）。
            if (self.is_chat_message(message) or self.is_card_message(message)
                    or self.is_card_update_message(message)):
                _sample_ws_message(self.cookie_id, message_id or "", message)
            
            # 判断消息类型并分发
            if self.is_chat_message(message):
                parsed = self.parse_chat_message(message)
                if parsed:
                    # 打印解密后的消息（参照旧框架）
                    self._log_chat_message(parsed)

                    # 转发给 backend-web：网页端会话列表/红点/窗口秒级更新。
                    # 纯被动转发，失败不影响自动回复（见 backend_push_forwarder）。
                    self._forward_to_backend(parsed, message_id)

                    send_message = parsed.get("send_message", "")

                    # 撤回占位消息：上面已转发给 backend-web 做前端展示（带头像 +
                    # 可展开原文），但**不触发自动回复**——对方撤回了消息，没有任何
                    # 可回复的内容，对「对方撤回了一条消息」文案做关键词/AI 回复
                    # 只会显得莫名其妙。
                    if parsed.get("revoked"):
                        logger.info(
                            f"【{self.cookie_id}】对方撤回消息，仅展示不自动回复: "
                            f"cid={parsed.get('chat_id')}"
                        )
                        return True

                    # 参照旧框架：检查是否为卡片消息（reminderContent为[卡片消息]）
                    # 小刀等卡片消息的reminderContent是[卡片消息]，需要提取card_title来判断具体类型
                    if send_message == '[卡片消息]' and self._on_card_message:
                        card_title = self.extract_card_title(message)
                        if card_title:
                            parsed["card_title"] = card_title
                            logger.info(f"【{self.cookie_id}】检测到卡片消息: {card_title}")
                            await self._on_card_message(parsed, websocket)
                            return True
                    
                    if self._on_chat_message:
                        await self._on_chat_message(parsed, websocket)
                return True
            
            # 检查是否为卡片更新消息（message["1"]为字符串的特殊格式，如付款状态变更）
            if self.is_card_update_message(message):
                parsed = self.parse_card_update_message(message)
                if parsed:
                    logger.info(f"【{self.cookie_id}】检测到卡片更新消息: {parsed.get('send_message', '')}")
                    if self._on_card_update_message:
                        await self._on_card_update_message(parsed, websocket)
                    else:
                        logger.debug(f"【{self.cookie_id}】卡片更新消息回调未设置，跳过")
                return True
            
            # 检查是否为卡片消息（无reminderContent的卡片消息，如系统卡片）
            if self.is_card_message(message):
                card_title = self.extract_card_title(message)
                if card_title and self._on_card_message:
                    # 解析基本信息用于卡片消息处理
                    parsed = self.parse_chat_message(message)
                    if parsed:
                        parsed["card_title"] = card_title
                        logger.info(f"【{self.cookie_id}】检测到系统卡片消息: {card_title}")
                        await self._on_card_message(parsed, websocket)
                return True
            
            # 其他类型消息
            if self._on_system_message:
                await self._on_system_message(message, websocket)
            
            return True
            
        except Exception as e:
            logger.error(f"【{self.cookie_id}】处理单条消息异常: {safe_str(e)}")
            return False
    
    def _log_chat_message(self, parsed: Dict[str, Any]) -> None:
        """打印聊天消息日志（参照旧框架格式）
        
        Args:
            parsed: 解析后的消息数据
        """
        try:
            send_user_id = parsed.get("send_user_id", "")
            send_user_name = parsed.get("send_user_name", "")
            send_message = parsed.get("send_message", "")
            chat_id = parsed.get("chat_id", "")
            item_id = parsed.get("item_id", "")
            msg_time = parsed.get("msg_time", "")
            
            # 打印chat_id用于调试
            logger.info(f"【{self.cookie_id}】解析消息: chat_id={chat_id}, send_user_id={send_user_id}")
            
            # 判断是否是自己发出的消息（托管账号集合：cid 跨账号共享时
            # 别的托管账号发的消息本连接也收得到，不能误判成买家）
            if self._is_self_sender(send_user_id):
                logger.info(f"[{msg_time}] 【手动发出】 商品({item_id}): {send_message}")
            else:
                logger.warning(f"[{msg_time}] 【收到】用户: {send_user_name}, 商品({item_id}): {send_message}")
        except Exception as e:
            logger.debug(f"【{self.cookie_id}】打印消息日志失败: {safe_str(e)}")

    def _forward_to_backend(self, parsed: Dict[str, Any], message_id: Optional[str]) -> None:
        """
        把解析好的聊天消息转发给 backend-web（网页端秒级展示）

        网页 IM 会话收 APP 来源消息的推送不稳定，而本服务 App 通道能收到全部帧，
        所以在这里补一条转发。只处理真实聊天内容：卡片消息、系统提示、
        空内容一律跳过，避免把占位文案当成聊天消息推到前端。

        Args:
            parsed: parse_chat_message 的返回值
            message_id: 已提取的消息ID（用于前端去重）
        """
        try:
            content = str(parsed.get("send_message") or "").strip()
            rich_card = parsed.get("rich_card")
            # 纯 "[卡片消息]" 占位（解析不出富数据）仍跳过；带富数据的订单卡片
            # 放行——前端要渲染的正是它（按钮 / 订单号 / 演出信息字段）
            if rich_card is None and (not content or content == "[卡片消息]"):
                return
            chat_id = str(parsed.get("chat_id") or "").strip()
            if not chat_id:
                return

            # 时间取原始报文里的毫秒时间戳（parsed["msg_time"] 是格式化字符串）
            raw = parsed.get("raw_message") or {}
            raw_body = raw.get("1") if isinstance(raw, dict) else None
            time_ms = 0
            if isinstance(raw_body, dict):
                try:
                    time_ms = int(raw_body.get("5") or 0)
                except (TypeError, ValueError):
                    time_ms = 0
            if not time_ms:
                time_ms = int(time.time() * 1000)

            # 图片消息：解析阶段已把图片链接写进 send_message（多张以换行拼接），
            # is_plain_text=False 且每行都是 http 链接时按图片类型转发
            lines = [line.strip() for line in content.split("\n") if line.strip()]
            is_image = (
                bool(lines)
                and not parsed.get("is_plain_text", True)
                and all(line.startswith("http") for line in lines)
            )

            sender_id = str(parsed.get("send_user_id") or "")
            revoked = bool(parsed.get("revoked"))
            # 消息类型：撤回占位 > 图片 > 订单卡片 > 文本
            if revoked:
                fwd_type = "system"
            elif is_image:
                fwd_type = "image"
            elif rich_card:
                fwd_type = "card"
            else:
                fwd_type = "text"
            # 打一行显式日志：App 通道是否真的被触发，只能靠它和 backend-web 的
            # `POST /api/v1/internal/chat-push` 两边对表确认（曾经因为没有日志，
            # 用户看到"5 秒"时无法判断到底是本通道生效还是 15 秒轮询撞上的）
            logger.info(
                f"【{self.cookie_id}】App通道转发: cid={chat_id}, "
                f"type={fwd_type}, id={message_id or '-'}"
            )
            schedule_chat_push(
                account_id=self.cookie_id,
                cid=chat_id,
                message_id=message_id or "",
                sender_id=sender_id,
                sender_name=str(parsed.get("send_user_name") or ""),
                is_self=self._is_self_sender(sender_id),
                # 托管账号互聊时对方（自己的另一个号）不是自己，但要标记成
                # managed_sender，让前端照旧跳过响铃/跨账号 toast
                managed_sender=self._is_managed_peer(sender_id),
                msg_type=fwd_type,
                text="" if is_image else content,
                images=lines if is_image else [],
                time_ms=time_ms,
                card=rich_card,
                # 撤回元数据透传（前端渲染「对方撤回了一条消息 [显示]」用）
                revoked=revoked,
                revoked_original=str(parsed.get("revoked_original") or ""),
                # 引用消息元数据透传（前端按 replyToMessageId 本地查表渲染引用块）
                reply_to_message_id=str(parsed.get("reply_to_message_id") or ""),
            )
        except Exception as e:
            logger.debug(f"【{self.cookie_id}】转发消息到 backend-web 失败: {safe_str(e)}")

    async def schedule_debounced_reply(
        self,
        chat_id: str,
        message_info: Dict[str, Any],
        reply_callback: Callable,
    ):
        """调度防抖回复"""
        async with self.message_debounce_lock:
            # 取消之前的防抖任务
            if chat_id in self.message_debounce_tasks:
                old_task = self.message_debounce_tasks[chat_id].get("task")
                if old_task and not old_task.done():
                    old_task.cancel()
            
            # 创建新的防抖任务
            async def debounced_reply():
                try:
                    await asyncio.sleep(self.message_debounce_delay)
                    await reply_callback(message_info)
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"【{self.cookie_id}】防抖回复异常: {safe_str(e)}")
                finally:
                    async with self.message_debounce_lock:
                        if chat_id in self.message_debounce_tasks:
                            del self.message_debounce_tasks[chat_id]
            
            task = asyncio.create_task(debounced_reply())
            self.message_debounce_tasks[chat_id] = {
                "task": task,
                "last_message": message_info,
                "timer": time.time(),
            }
