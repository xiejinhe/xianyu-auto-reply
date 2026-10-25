"""聊天消息本地归档：为「聊天记录搜索」积累数据。

两处落库（都异步执行，不阻塞响应/推送转发）：
- 历史拉取：GET /messages 解析出的每页消息
- 实时推送：im_client 收到并解析成功的推送消息

搜索只查本地库（xy_chat_messages），零闲鱼接口请求——2026-10-26
限流教训的直接产物：任何搜索功能都不得打闲鱼侧接口。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)

# 单批归档上限：推送一次一条，历史一页 20-50 条，防御异常巨页
_MAX_BATCH = 200


def _norm_cid(cid: str) -> str:
    c = str(cid or "")
    return c.split("@")[0] if "@" in c else c


def _to_dt(ms) -> datetime:
    try:
        v = int(ms or 0)
        if v <= 0:
            return datetime.now()
        # 秒级时间戳兼容（10 位）
        if v < 10_000_000_000:
            v *= 1000
        return datetime.fromtimestamp(v / 1000)
    except Exception:
        return datetime.now()


def _media_json(msg: dict) -> str:
    media = {}
    if msg.get("images"):
        media["images"] = msg["images"][:6]
    if msg.get("audioUrl"):
        media["audioUrl"] = msg["audioUrl"]
    return json.dumps(media, ensure_ascii=False) if media else ""


async def archive_messages(
    owner_id: int,
    account_id: str,
    cid: str,
    messages: list[dict],
) -> None:
    """把一批已解析的消息 upsert 进 xy_chat_messages（message_id 冲突则忽略）。

    message_id 为空的消息（部分系统/卡片推送）跳过——文本搜索的主体是
    正常消息，历史拉取路径会带出大部分系统消息。
    """
    if not messages:
        return
    cid_n = _norm_cid(cid)
    rows = []
    for msg in messages[:_MAX_BATCH]:
        mid = str(msg.get("messageId", "") or "").strip()
        if not mid:
            continue
        t = str(msg.get("text", "") or "")
        if not t.strip():
            # 图片/语音至少留占位文本，保证会话时间线完整可搜
            t = "[图片]" if (msg.get("images") or msg.get("type") == "image") else ""
            if not t and msg.get("type") == "audio":
                t = "[语音]"
        rows.append({
            "owner_id": int(owner_id or 1),
            "account_id": str(account_id),
            "cid": cid_n,
            "message_id": mid,
            "sender_id": str(msg.get("senderId", "") or ""),
            "sender_name": str(msg.get("senderName", "") or "")[:250],
            "is_self": 1 if msg.get("isSelf") else 0,
            "managed_sender": 1 if msg.get("managedSender") else 0,
            "msg_type": str(msg.get("type", "text") or "text")[:16],
            "text": t,
            "media_json": _media_json(msg),
            "msg_time": _to_dt(msg.get("time")),
        })
    if not rows:
        return
    try:
        from common.db.session import async_session_maker
        async with async_session_maker() as session:
            await session.execute(
                sql_text(
                    "INSERT IGNORE INTO xy_chat_messages "
                    "(owner_id, account_id, cid, message_id, sender_id, sender_name, "
                    "is_self, managed_sender, msg_type, text, media_json, msg_time) "
                    "VALUES (:owner_id, :account_id, :cid, :message_id, :sender_id, "
                    ":sender_name, :is_self, :managed_sender, :msg_type, :text, "
                    ":media_json, :msg_time)"
                ),
                rows,
            )
            await session.commit()
    except Exception as e:
        logger.debug(f"【{account_id}】消息归档失败（不影响聊天）: {e}")


def spawn_archive(
    owner_id: int,
    account_id: str,
    cid: str,
    messages: Optional[list[dict]],
) -> None:
    """fire-and-forget 包装：归档失败静默，绝不影响主流程"""
    if not messages:
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(archive_messages(owner_id, account_id, cid, messages))
    except RuntimeError:
        pass
