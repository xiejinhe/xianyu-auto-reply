"""
账号网页 Cookie 会话状态推断服务。

出于零风险考虑，本服务**不新增任何数据库字段 / 迁移**，而是基于现有信息
推断账号的网页 Cookie 会话状态，供「账号列表」与「在线聊天」两处展示：

- normal        : 正常
- risk_control  : 风控中（近 24h 内有 failed/error 的风控日志，且失败晚于最近一次登录/续期）
- expired       : 网页 Cookie 会话失效，需要重新登录
- disabled      : 账号被禁用（非网页会话问题，仅作归类展示）

推断优先级：disabled > risk_control > expired > normal。

说明：闲鱼网页会话没有可靠的“到期时刻”，所以「失效」用
``last_refresh_at``（网页 cookie 最近一次成功续期时间）的陈旧度做代理判断；
而「风控中」用 ``xy_risk_control_logs`` 近 24h 的 processing/failed 记录判断。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import func, select

from common.db.session import async_session_maker
from common.models.risk_control_log import XYRiskControlLog
from common.models.xy_account import XYAccount

# 网页 Cookie 会话失效的判定阈值：last_refresh_at 距现在超过该天数视为需要重新登录。
# 因为闲鱼网页会话没有可靠的“到期时刻”，只能用“多久没成功续期”做代理判断。
COOKIE_STALE_DAYS = 7

# 判定“风控中”的风控日志时间窗口（小时）。
RISK_CONTROL_WINDOW_HOURS = 24


def _to_naive_local(dt: Optional[datetime]) -> Optional[datetime]:
    """把（可能带时区的）datetime 规整成本机 naive 时间，便于直接相减。

    MySQL 的 DATETIME 不存时区信息，本系统写入时混用了 naive（北京本地）
    与 aware（UTC）两种，这里统一按“本机本地时间”解释，差值是天级判断，
    与时区偏差（最多 8h）相比可忽略。
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def _status_from_parts(
    account: XYAccount,
    recent_risk_control: bool,
    session_expired: bool,
) -> str:
    """根据账号自身字段与补充信号推断 cookie_status。"""
    # 1) 账号被禁用（业务开关），优先归类，避免与网页会话问题混淆。
    if account.status != "active":
        return "disabled"
    # 2) 近 24h 内有风控日志（滑块挑战 pending 或失败）→ 风控中。
    if recent_risk_control:
        return "risk_control"
    # 3) 同进程内检测到网页 Session 刚刚过期（5 分钟冷却）→ 直接判失效。
    #    注意：该冷却在内存、且只存在于调用 mark_account_session_expired 的进程
    #    （如 websocket 服务），backend-web 大概率读不到，仅作 best-effort 补充。
    if session_expired:
        return "expired"
    # 4) last_refresh_at 陈旧（超过阈值）→ 网页会话可能已失效，提示重新登录。
    last_refresh = _to_naive_local(account.last_refresh_at)
    now = datetime.now()
    if last_refresh is None:
        # 从未成功续期过：保守判定为正常，避免对老账号误报“失效”。
        return "normal"
    if (now - last_refresh) > timedelta(days=COOKIE_STALE_DAYS):
        return "expired"
    return "normal"


def _session_expired_for(account_id: str) -> bool:
    """同进程内的 Session 过期冷却（5 分钟），作为失效判断的即时补充信号。"""
    try:
        from common.utils.cookie_refresh import is_account_session_cooled

        return bool(is_account_session_cooled(account_id))
    except Exception:
        return False


def _is_failed_active(account: XYAccount, failed_at: Optional[datetime]) -> bool:
    """判断最近一次失败风控事件是否仍“有效”（应展示为风控中）。

    规则：失败时间晚于账号最近一次成功刷新网页 Cookie（last_refresh_at）才视为有效。
    即：用户重新登录（刷新网页会话）之后发生的失败才标风控中；重登之前的历史失败
    已被新的网页会话“覆盖”，不再展示，避免“刚登完号却还显示风控中”的误报。
    """
    failed_at = _to_naive_local(failed_at)
    if failed_at is None:
        return False
    last_refresh = _to_naive_local(account.last_refresh_at)
    if last_refresh is None:
        # 未记录过刷新时间：保守视为有效（历史上确有失败且无法确认已被新会话覆盖）。
        return True
    return failed_at > last_refresh


async def get_recent_risk_control_account_ids(
    session,
    account_ids: Iterable[str],
    *,
    hours: int = RISK_CONTROL_WINDOW_HOURS,
) -> dict[str, datetime]:
    """查询近 ``hours`` 小时内存在失败风控日志的账号及其最近失败时间。

    Args:
        session: 复用调用方的数据库会话（避免额外建连）。
        account_ids: 待查询的账号业务标识列表。
        hours: 时间窗口（小时），默认 24。
    Returns:
        ``{account_identifier: 最近一次失败日志的 created_at}``。

        仅统计 ``failed`` / ``error``（已终结）状态；``processing``（进行中）不计入——
        进行中日志若卡死会由求解器兜底标记失败，且 Dashboard 不应把“进行中”误报为风控中。
    """
    ids = [str(i) for i in account_ids]
    if not ids:
        return {}
    since = datetime.now() - timedelta(hours=hours)
    rows = await session.execute(
        select(
            XYRiskControlLog.account_identifier,
            func.max(XYRiskControlLog.created_at),
        ).where(
            XYRiskControlLog.account_identifier.in_(ids),
            XYRiskControlLog.processing_status.in_(["failed", "error"]),
            XYRiskControlLog.created_at >= since,
        ).group_by(XYRiskControlLog.account_identifier)
    )
    return {r[0]: r[1] for r in rows.all() if r[0]}


async def compute_cookie_status_map(
    session,
    accounts: list[XYAccount],
) -> dict[str, str]:
    """为一批账号计算 cookie_status。

    Args:
        session: 数据库会话（调用方已有的 session 即可，本函数不另开连接）。
        accounts: 账号对象列表。
    Returns:
        ``{account_id: cookie_status}`` 字典。
    """
    if not accounts:
        return {}
    account_ids = [acc.account_id for acc in accounts]
    try:
        recent_failed = await get_recent_risk_control_account_ids(session, account_ids)
    except Exception:
        # 风控日志查询失败：保守按“无风控”处理，不阻塞列表展示。
        recent_failed = {}
    result: dict[str, str] = {}
    for acc in accounts:
        failed_at = recent_failed.get(acc.account_id)
        status = _status_from_parts(
            acc,
            recent_risk_control=_is_failed_active(acc, failed_at),
            session_expired=_session_expired_for(acc.account_id),
        )
        result[acc.account_id] = status
    return result
