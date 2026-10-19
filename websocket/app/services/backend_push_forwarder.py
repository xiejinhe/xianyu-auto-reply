"""
IM 推送转发到 backend-web（网页端秒级展示）

背景（重要，勿删）：
闲鱼服务端对「网页 IM 会话」的推送不稳定——APP 来源的消息常常一帧都不推，
网页端只能靠 15 秒对账轮询兜底，表现为"铃声响了但会话列表和红点不动"。
而本服务的 App 通道长连接（7×24 跑自动回复的那条）能收到全部推送帧，
所以在解析出聊天消息后顺手转发给 backend-web，由它按 messageId 去重后
广播给前端，展示延迟从 ≤15 秒降到帧到即显示。

约束：
- 纯被动转发，不产生任何额外的闲鱼请求，风控零增加；
- fire-and-forget：转发失败只记 debug 日志，绝不影响自动回复主流程；
- backend-web 侧没有前端订阅该账号时会立即返回，不做多余工作。
"""
from __future__ import annotations

import asyncio
import json
from typing import List, Optional, Set

import aiohttp
from loguru import logger

from app.core.config import get_settings
from common.utils.internal_auth import (
    INTERNAL_AUTH_HEADER,
    MIN_INTERNAL_TOKEN_LENGTH,
)

# 单次转发的超时（秒）：本机/同容器网络，正常在毫秒级完成
_FORWARD_TIMEOUT = 3.0
# 同时进行的转发任务上限，防止极端情况下堆积
_MAX_PENDING = 200

_pending_tasks: Set[asyncio.Task] = set()
_auth_warned = False


def _build_url(path: str = "/api/v1/internal/chat-push") -> str:
    """拼接 backend-web 的接收地址"""
    base = (get_settings().backend_web_service_url or "").strip().rstrip("/")
    return f"{base}{path}"


def _build_headers() -> Optional[dict]:
    """构造内部鉴权头；令牌缺失时返回 None（首次告警一次，避免刷日志）"""
    global _auth_warned
    token = (get_settings().internal_api_token or "").strip()
    if len(token) < MIN_INTERNAL_TOKEN_LENGTH:
        if not _auth_warned:
            logger.warning("App通道推送转发已跳过：服务间令牌未初始化")
            _auth_warned = True
        return None
    return {INTERNAL_AUTH_HEADER: token}


async def _post(payload: dict, path: str = "/api/v1/internal/chat-push") -> None:
    """实际发送（失败静默，只记 debug）"""
    headers = _build_headers()
    if not headers:
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _build_url(path),
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=_FORWARD_TIMEOUT),
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.debug(
                        f"【{payload.get('account_id')}】App通道推送转发失败 "
                        f"HTTP {response.status}: {body[:120]}"
                    )
    except asyncio.TimeoutError:
        logger.debug(f"【{payload.get('account_id')}】App通道推送转发超时")
    except Exception as exc:
        logger.debug(f"【{payload.get('account_id')}】App通道推送转发异常: {exc}")


def schedule_chat_push(
    *,
    account_id: str,
    cid: str,
    message_id: str = "",
    sender_id: str = "",
    sender_name: str = "",
    is_self: bool = False,
    msg_type: str = "text",
    text: str = "",
    images: Optional[List[str]] = None,
    time_ms: int = 0,
    card: Optional[dict] = None,
) -> None:
    """
    安排一次转发（非阻塞）

    调用方（message_handler）解析完一条聊天消息后调用，本函数立即返回，
    真正的 HTTP 请求在后台任务里跑，失败不影响自动回复。

    card: 订单卡片富数据（parse_rich_card 的返回值，非交易卡片为 None），
    随消息一起转发给前端渲染富卡片（按钮/订单号/演出信息字段）。
    """
    if not account_id or not cid:
        return
    if len(_pending_tasks) >= _MAX_PENDING:
        logger.debug(f"【{account_id}】App通道推送转发积压，跳过本条")
        return

    payload = {
        "account_id": account_id,
        "cid": cid,
        "message_id": message_id or "",
        "sender_id": sender_id or "",
        "sender_name": sender_name or "",
        "is_self": bool(is_self),
        "msg_type": msg_type,
        "text": text or "",
        "images": images or [],
        "time_ms": int(time_ms or 0),
        "card": card,
    }
    try:
        task = asyncio.create_task(_post(payload))
    except RuntimeError:
        # 事件循环已关闭（服务停机中），直接放弃
        return
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


def schedule_typing_push(*, account_id: str, cid: str, typing: bool) -> None:
    """
    把「对方正在输入」状态转发给 backend-web

    闲鱼的状态帧形如
        {"1": [{"1": cid, "2": 1, "3": 1, "4": 本账号ID}]}
    其中 "3" 为 1 = 正在输入、0 = 停止输入；"4" 是接收方（本账号），
    所以这条帧描述的是「对端在这个会话里的输入状态」。

    与消息转发同样 fire-and-forget：状态丢了只影响一个提示气泡，
    绝不能影响自动回复主流程。
    """
    if not account_id or not cid:
        return
    if len(_pending_tasks) >= _MAX_PENDING:
        return
    payload = {"account_id": account_id, "cid": cid, "typing": bool(typing)}
    try:
        task = asyncio.create_task(_post(payload, "/api/v1/internal/chat-typing"))
    except RuntimeError:
        return
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


def build_payload_debug(payload: dict) -> str:
    """调试用：把载荷压成一行日志"""
    try:
        return json.dumps(payload, ensure_ascii=False)[:200]
    except Exception:
        return str(payload)[:200]
