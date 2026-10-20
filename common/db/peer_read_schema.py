"""对方已读回执持久化表的幂等建表与字段补齐。

背景：闲鱼把「对方已读」做成 IM 长连接回执帧，只推给发送方，且
websocket 服务解析后转发到 backend。回执原本只存在浏览器内存，刷新
页面就会退回历史值（v1.0.55 的已知边界）。v1.0.56 起落 MySQL 永久保留，
刷新/重开会话后仍能恢复「已读」标记。
"""
from __future__ import annotations

from datetime import datetime

from loguru import logger
from sqlalchemy import text


XY_CHAT_PEER_READ_DDL = {
    "xy_chat_peer_read": """
        CREATE TABLE IF NOT EXISTS xy_chat_peer_read (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            account_id VARCHAR(80) NOT NULL,
            cid VARCHAR(80) NOT NULL,
            message_id VARCHAR(80) NOT NULL,
            read_at BIGINT NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_peer_read (account_id, cid, message_id),
            KEY idx_peer_read_lookup (account_id, cid)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
}


async def ensure_peer_read_schema(conn, current_time: datetime | None = None) -> None:
    """创建已读回执表（幂等）。

    Args:
        conn: 数据库连接（与 auto_relist_schema 同款 ddl_connection）。
        current_time: 应用侧传入的北京时间，本项目建表约定参数，本表未使用。
    """
    for table_name, ddl in XY_CHAT_PEER_READ_DDL.items():
        await conn.execute(text(ddl))
        logger.info("已读回执表就绪: {}", table_name)
