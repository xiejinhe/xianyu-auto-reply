"""对方已读回执持久化：upsert 与查询。

闲鱼把「对方已读」做成 IM 长连接回执帧，只推给发送方；websocket 服务
解析后转发到 backend 的 /internal/chat-peer-read。本服务把回执落 MySQL
（xy_chat_peer_read），使前端刷新/重开会话后仍能恢复「已读」标记，不再
退回历史值。

cid 存储前统一按 normCid 规整（去掉 @goofish 后缀），与前端渲染层
（ChatNew.tsx 的 displayMessages 用 normCid 当 key）保持一致。
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import text

from common.db.session import async_session_maker


def _norm_cid(cid: str) -> str:
    """规整 cid：去掉 @goofish 后缀并去空白，与前端 normCid 对齐。"""
    if not cid:
        return ""
    return cid.split("@")[0].strip()


async def save_peer_reads(
    account_id: str,
    cid: str,
    message_ids: list,
    read_at: int = 0,
) -> int:
    """批量 upsert 已读回执，返回成功写入/更新的条数。

    message_ids 为被对方读掉的 IM messageId（如 "4351812252653.PNM"）。
    read_at 为该回执的时间戳（毫秒），供前端兜底处理 App 通道的 hex 孪生 ID。
    """
    cid = _norm_cid(cid)
    if not account_id or not cid or not message_ids:
        return 0
    rows = [
        {
            "account_id": account_id,
            "cid": cid,
            "message_id": str(mid).strip(),
            "read_at": int(read_at or 0),
        }
        for mid in message_ids
        if str(mid).strip()
    ]
    if not rows:
        return 0
    try:
        async with async_session_maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO xy_chat_peer_read
                        (account_id, cid, message_id, read_at, created_at, updated_at)
                    VALUES (:account_id, :cid, :message_id, :read_at, NOW(), NOW())
                    ON DUPLICATE KEY UPDATE
                        read_at = VALUES(read_at),
                        updated_at = NOW()
                    """
                ),
                rows,
            )
            await session.commit()
        logger.debug("已读回执落库: account=%s cid=%s n=%d", account_id, cid, len(rows))
        return len(rows)
    except Exception as exc:
        logger.warning("已读回执落库失败: {}", exc)
        return 0


async def load_peer_reads(account_id: str, cid: str) -> tuple[set, int]:
    """读取某会话已确认的已读回执。

    Returns:
        ``(message_id 集合, 最大 read_at)``。空结果返回 (set(), 0)。
        message_id 集合用于精确命中 IM messageId；max read_at 供前端
        对 App 通道 hex 孪生副本做时间兜底（早于该时刻的自己消息也算已读）。
    """
    cid = _norm_cid(cid)
    if not account_id or not cid:
        return set(), 0
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                text(
                    "SELECT message_id, read_at FROM xy_chat_peer_read "
                    "WHERE account_id = :account_id AND cid = :cid"
                ),
                {"account_id": account_id, "cid": cid},
            )
            rows = result.all()
        ids = {str(r[0]) for r in rows if r[0]}
        max_read_at = max((int(r[1] or 0) for r in rows), default=0)
        return ids, max_read_at
    except Exception as exc:
        logger.warning("读取已读回执失败: {}", exc)
        return set(), 0
