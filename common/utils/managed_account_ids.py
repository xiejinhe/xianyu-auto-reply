"""托管账号 ID 集合（isSelf 判断用）

背景：闲鱼会话 cid 跨账号共享（2026-10-19 实测：同一买家与多个卖家账号
共用同一个 cid，任一账号请求同一 cid 的消息都返回 200）。因此账号 A
发出的消息，账号 B 的 IM 长连接与历史接口同样拉得到。

isSelf 判断若只认「发送者 == 本账号」，就会把同为托管账号的发送者误判成
买家——2026-10-20 真实事故：账号 2217728125233 给买家发的消息，在
1620778109 侧被渲染成左侧「对方消息」（还署名），并误触发跨账号来消息
toast 与响铃，用户视角即「我发的消息跑到别的窗口、还变成了对方发的」。

因此：发送者是任一托管账号（含本账号）即视为「自己发出的消息」。
"""
from __future__ import annotations

import time

from loguru import logger

_cache: set[str] = set()
_loaded_at: float = 0.0
_TTL_SECONDS = 300


def _load_from_db() -> set[str]:
    """同步查库（pymysql 直连）。

    不能用 SQLAlchemy 的 async_engine.sync_engine——它底层仍是 asyncmy
    的 greenlet 适配，在事件循环线程里同步调用会抛
    「greenlet_spawn has not been called」（2026-10-20 实测）。
    pymysql 直连是独立同步连接，查完即关，不占连接池。
    """
    ids: set[str] = set()
    try:
        import pymysql

        from common.core.config import get_settings

        cfg = get_settings()
        conn = pymysql.connect(
            host=cfg.mysql_host,
            port=cfg.mysql_port,
            user=cfg.mysql_user,
            password=cfg.mysql_password,
            database=cfg.mysql_database,
            connect_timeout=5,
            read_timeout=5,
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT account_id FROM xy_accounts")
                rows = cur.fetchall()
            ids = {str(r[0]).strip() for r in rows if r and r[0]}
        finally:
            conn.close()
    except Exception as e:  # 查库失败不炸调用方，返回旧缓存（最坏情况空集=只按 myid 判）
        logger.debug(f"加载托管账号ID集合失败: {e}")
    return ids


def get_managed_account_ids() -> set[str]:
    """所有托管账号 ID（进程内缓存 5 分钟）。

    账号新增/删除后最多 5 分钟生效——账号变更是极低频操作，可接受。
    """
    global _cache, _loaded_at
    now = time.time()
    if _cache and now - _loaded_at < _TTL_SECONDS:
        return _cache
    fresh = _load_from_db()
    if fresh:
        _cache = fresh
        _loaded_at = now
    elif not _cache:
        # 首次且查库失败：置一个短过期，避免下次调用继续压查库逻辑
        _loaded_at = now
    return _cache


def is_managed_account(sender_id: str) -> bool:
    """发送者是否为托管账号（含未连接的账号）。"""
    sid = str(sender_id or "").strip()
    if not sid:
        return False
    return sid in get_managed_account_ids()
