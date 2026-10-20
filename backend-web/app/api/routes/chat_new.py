"""
在线聊天(新) API路由

功能：
1. 获取当前用户的账号列表（供前端选择）
2. 连接/断开指定账号的IM
3. 获取会话列表
4. 获取聊天记录
5. 获取已连接账号列表
6. WebSocket接口，实时推送IM消息给前端

与自动回复WebSocket隔离，使用独立的token和device_id
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import time
from urllib.parse import unquote, urlparse

import aiohttp
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from loguru import logger
from pydantic import BaseModel

from app.api.deps import get_current_active_user, get_db_session
from app.services.account_service import AccountService
from app.services.chat_new import get_im_session_manager
from app.services.chat_new.avatar_service import get_owner_user_info, AVATAR_CACHE_PREFIX, AVATAR_CACHE_TTL
from app.services.chat_new.card_sampler import sample_card
from app.services.chat_new.user_profile_service import (
    get_user_profile,
    _load_sibling_accounts,
    PROFILE_CACHE_PREFIX,
)
from app.services.chat_new.official_blacklist_service import official_blacklist_request
from common.db.redis_client import get_redis_client
from common.models import User, XYAccount
from common.schemas.common import ApiResponse
from common.utils.auth_scope import is_admin_user
from common.utils.managed_account_ids import is_managed_account
from common.services.account_cookie_status_service import compute_cookie_status_map
from common.utils.time_utils import safe_isoformat
from common.utils.xianyu_utils import trans_cookies
from common.utils.xianyu_message_parser import (
    AUDIO_PLACEHOLDER_TEXT,
    load_content_json,
    interpret_content,
    extract_audio_info,
    classify_event,
)
from common.utils.order_card_parser import parse_rich_card_decoded
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession


# 无效昵称黑名单（系统生成的非真实用户昵称 / 系统消息摘要）
_INVALID_NICK_SET = {
    "交易消息", "系统消息", "卡片消息",
    "我完成了评价", "对方完成了评价", "快给ta一个评价吧～",
    "卖家已发货", "买家已付款", "买家已确认收货", "等待您发货",
    "超时未付款，系统关闭了订单",
}

# 用户手动删除的会话：ZSET member=cid、score=删除时刻(ms)。
# 按「删除时刻」记录而不是永久隐藏：之后该会话若再来新消息
# （lastMessageTime 晚于删除时刻）会自动恢复显示，符合
# "对方重新找我，自然重新加载"的预期。
HIDDEN_CONVS_PREFIX = "chat:hidden_convs:"
HIDDEN_CONVS_MAX = 300

# 最近一次成功的会话列表缓存（首页）：闲鱼 IM 流控（400600001）时回退显示，
# 不然前端只能看到「暂无会话」。流控通常是突发性的，几分钟自愈，
# 期间前端 15 秒一次的对账轮询若一直拿不到数据，列表就一直白屏。
# TTL 刻意给长（6 小时）：这只是「最近一次成功的快照」，过期只会让数据变旧
# （仍带 rateLimited 标记可用），但一旦过期就直接弹「请求过于频繁」吓人；
# 每次成功拉取都会刷新，正常使用时数据始终是最新的。
CONVS_LASTGOOD_PREFIX = "chat:convs_lastgood:"
CONVS_LASTGOOD_TTL = 6 * 3600

# 进程内流控冷却：账号被闲鱼限流后一段时间内的请求不再转发给闲鱼，
# 直接回兜底缓存。实测这个限流需要较长的安静期才自愈，固定 5 分钟会在
# 到期瞬间再撞一次限流、把闲鱼的限流窗口一直续上（曾从 21:10 拖到 23:19
# 都没恢复），因此改为指数退避：连续被限流就翻倍冷却，成功一次后重置。
# 列表的实时更新有 App 通道推送兜着，冷却期长一点不影响消息及时性。
# 前端冷却只对刷新后的新页面有效，旧页面的轮询拦不住，必须在后端把闲鱼 shield 住。
CONVS_COOLDOWN_S = 300
CONVS_COOLDOWN_MAX_S = 1800
_convs_rate_limit_until: dict = {}
_convs_backoff_level: dict = {}  # 账号 → 连续被限流次数，用于指数退避


def _is_valid_nick(name: str) -> bool:
    """
    检查昵称是否有效（非空、非纯数字、非系统昵称）

    过滤规则：
    1. 空值或纯空白
    2. 纯数字（可能是用户ID误当昵称）
    3. 精确匹配系统消息摘要黑名单
    4. 被方括号包裹的文本（如"[卡片消息]"去括号后匹配）
    """
    if not name or not name.strip():
        return False
    stripped = name.strip()
    if stripped.isdigit():
        return False
    if stripped in _INVALID_NICK_SET:
        return False
    # 处理可能带方括号的系统消息摘要，如 "[我完成了评价]"
    if stripped.startswith("[") and stripped.endswith("]"):
        inner = stripped[1:-1]
        if inner in _INVALID_NICK_SET:
            return False
    return True

router = APIRouter(prefix="/chat-new")


@router.get("/accounts")
async def list_accounts(
    page: int = 1,
    page_size: int = 20,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    获取账号列表（管理员查看所有账号，普通用户只看自己的），支持分页
    """
    try:
        is_admin = is_admin_user(current_user)

        # 基础条件：有cookie的账号
        base_where = [XYAccount.cookie.isnot(None), XYAccount.cookie != ""]
        if not is_admin:
            base_where.append(XYAccount.owner_id == current_user.id)

        # 查总数
        count_q = select(func.count()).select_from(XYAccount).where(*base_where)
        total = (await db.execute(count_q)).scalar() or 0

        # 分页查询，按状态排序（active在前）
        query = (
            select(XYAccount)
            .where(*base_where)
            .order_by(
                # active排前面
                func.IF(XYAccount.status == "active", 0, 1),
                XYAccount.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await db.execute(query)
        accounts = result.scalars().all()

        # 推断每个账号的网页 Cookie 会话状态（normal/risk_control/expired/disabled），
        # 供在线聊天页展示，无需新增数据库字段/迁移。
        cookie_status_map = await compute_cookie_status_map(db, accounts)

        # 管理员场景批量查用户名
        owner_map: dict[int, str] = {}
        if is_admin:
            owner_ids = list({acc.owner_id for acc in accounts if acc.owner_id})
            if owner_ids:
                user_result = await db.execute(
                    select(User).where(User.id.in_(owner_ids))
                )
                for u in user_result.scalars().all():
                    owner_map[u.id] = u.username or str(u.id)

        manager = get_im_session_manager()
        connected_ids = manager.get_connected_account_ids()

        items = []
        display_names_updated = False
        for acc in accounts:
            display_name = acc.display_name or ""
            if not display_name and acc.cookie:
                try:
                    tracknick = trans_cookies(acc.cookie).get("tracknick")
                    if tracknick:
                        display_name = unquote(tracknick)
                        acc.display_name = display_name
                        display_names_updated = True
                except Exception as e:
                    logger.warning(f"账号 {acc.account_id} 解析昵称失败: {e}")
            item = {
                "account_id": acc.account_id,
                "display_name": display_name,
                "remark": acc.remark or "",
                "connected": acc.account_id in connected_ids,
                # 账号是否持有 IM 客户端对象（已连接 或 已掉线但可自动恢复）。
                # 前端据此决定是否订阅该账号的实时消息通道：即使当前"未连接"，
                # 只要 has_client 为 true 就保持订阅，待自动重连成功后由 reconnected
                # 事件补拉历史并响铃（实现"断线也响"）。完整断开（无客户端）的账号不订阅。
                "has_client": acc.account_id in manager.clients,
                "status": acc.status or "active",
                # 网页 Cookie 会话状态推断结果（无需新增字段/迁移）：
                # normal=正常 / risk_control=风控中 / expired=失效需重登 / disabled=已禁用
                "cookie_status": cookie_status_map.get(acc.account_id, "normal"),
                "last_login_at": safe_isoformat(acc.last_login_at),
                "last_refresh_at": safe_isoformat(acc.last_refresh_at),
            }
            if is_admin:
                item["owner"] = owner_map.get(acc.owner_id, str(acc.owner_id))
            items.append(item)
        if display_names_updated:
            await db.commit()

        return {
            "success": True,
            "data": items,
            "total": total,
            "hasMore": page * page_size < total,
        }

    except Exception as e:
        logger.error(f"获取账号列表失败: {e}")
        return ApiResponse(success=False, message=f"获取账号列表失败: {str(e)}")


@router.get("/connected-accounts")
async def list_connected_accounts(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    获取当前用户可访问且后端 IM 已连接的账号 ID 列表。

    用于页面刷新后自动恢复前端 WebSocket 实时消息订阅。
    """
    try:
        connected_ids = set(get_im_session_manager().get_connected_account_ids())
        if not connected_ids:
            return ApiResponse(success=True, message="查询成功", data={"account_ids": []})

        owner_id = None if is_admin_user(current_user) else current_user.id
        accounts = await AccountService(db).get_accounts_for_user(owner_id, list(connected_ids))
        account_ids = [account.account_id for account in accounts]
        return ApiResponse(
            success=True,
            message="查询成功",
            data={"account_ids": account_ids},
        )

    except Exception as e:
        logger.error(f"获取已连接账号列表失败: {e}")
        return ApiResponse(success=False, message=f"获取已连接账号列表失败: {str(e)}")


# 「连接过就该在线」清单：记录用户手动连接过哪些账号的 IM，后端重启后自动恢复。
# 否则每次重启后端，IM 客户端全部消失（manager.clients 在进程内存里），
# 所有账号掉线且无人重建，必须手动挨个点「连接」——用户实测踩过的坑。
AUTO_CONNECT_IDS_KEY = "chat_new.auto_connect_accounts"


async def _load_auto_connect_ids(db: AsyncSession) -> tuple[list[str], bool]:
    """读取自动连接清单，返回 (账号ID列表, 清单是否已存在)。

    「不存在」与「存在但为空」必须区分：前者是首次启用，需要从数据库推导
    初始清单（这样新装环境、后来新增的账号都能自动纳入，不用写死任何ID）；
    后者说明用户手动断开过，必须尊重、不再自动连。
    """
    from app.services.system_setting_service import SystemSettingService
    try:
        settings = await SystemSettingService(db).list_settings()
    except Exception as e:
        logger.warning(f"读取IM自动连接清单失败: {e}")
        return [], False
    if AUTO_CONNECT_IDS_KEY not in settings:
        return [], False
    # 存成逗号分隔，不存 JSON：set_setting 会对值做 XSS 转义，
    # JSON 的引号会被转成 &quot;，存进去就解析不回来了
    raw = str(settings.get(AUTO_CONNECT_IDS_KEY) or "")
    ids = [part.strip() for part in raw.split(",") if part.strip()]
    return ids, True


async def _bootstrap_auto_connect_ids(db: AsyncSession) -> list[str]:
    """首次启用：从数据库推导初始清单（活跃且填了 Cookie 的账号）并落盘"""
    from app.services.system_setting_service import SystemSettingService
    try:
        result = await db.execute(
            select(XYAccount.account_id, XYAccount.cookie).where(XYAccount.status == "active")
        )
        ids = [str(aid) for aid, cookie in result.all() if aid and (cookie or "").strip()]
        if ids:
            await SystemSettingService(db).set_setting(
                AUTO_CONNECT_IDS_KEY, ",".join(ids), "在线聊天重启后自动重连的账号清单"
            )
            logger.info(f"IM自动连接清单首次初始化（来自数据库）: {len(ids)} 个账号")
        return ids
    except Exception as e:
        logger.warning(f"初始化IM自动连接清单失败: {e}")
        return []


async def _save_auto_connect_id(db: AsyncSession, account_id: str, connected: bool) -> None:
    """连接成功 -> 加入清单；手动断开 -> 移出清单（尊重用户意愿，不再自动连）"""
    from app.services.system_setting_service import SystemSettingService
    try:
        ids, _ = await _load_auto_connect_ids(db)
        if connected and account_id not in ids:
            ids.append(account_id)
        elif not connected and account_id in ids:
            ids.remove(account_id)
        else:
            return
        await SystemSettingService(db).set_setting(
            AUTO_CONNECT_IDS_KEY, ",".join(ids), "在线聊天重启后自动重连的账号清单"
        )
    except Exception as e:
        logger.warning(f"更新IM自动连接清单失败: {e}")


async def auto_reconnect_chat_accounts() -> None:
    """重启后自动恢复「用户连接过」的账号 IM 连接。

    放在启动后台任务里跑：先等几秒让 DB/Redis 就绪，再逐个重连，
    单个失败（如 Cookie 失效）只记日志不阻塞其它账号。
    """
    await asyncio.sleep(5)
    from common.db.session import async_session_maker
    manager = get_im_session_manager()
    async with async_session_maker() as db:
        ids, exists = await _load_auto_connect_ids(db)
        if not exists:
            # 清单还没建过：从数据库推导（新装的、后来新增的账号都会自动纳入）
            ids = await _bootstrap_auto_connect_ids(db)
    if not ids:
        return
    logger.info(f"开始自动恢复在线聊天IM连接，共 {len(ids)} 个账号: {ids}")
    ok = 0
    for account_id in ids:
        try:
            await manager.get_or_connect(account_id)
            ok += 1
        except Exception as e:
            logger.warning(f"自动恢复IM连接失败（跳过）: {account_id}: {e}")
        await asyncio.sleep(1)
    logger.info(f"在线聊天IM连接自动恢复完成: {ok}/{len(ids)}")


@router.post("/connect/{account_id}")
async def connect_account(
    account_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    连接指定账号的IM WebSocket
    """
    try:
        # 校验账号归属（管理员可操作任意账号）
        query = select(XYAccount).where(XYAccount.account_id == account_id)
        if not is_admin_user(current_user):
            query = query.where(XYAccount.owner_id == current_user.id)
        result = await db.execute(query)
        account = result.scalar_one_or_none()
        if not account:
            return ApiResponse(success=False, message="账号不存在或无权操作")

        manager = get_im_session_manager()
        await manager.get_or_connect(account_id)
        # 连接成功记入「重启后自动恢复」清单
        await _save_auto_connect_id(db, account_id, True)
        return ApiResponse(success=True, message="连接成功")

    except ValueError as e:
        return ApiResponse(success=False, message=str(e))
    except Exception as e:
        logger.error(f"【{account_id}】连接IM失败: {e}")
        return ApiResponse(success=False, message=f"连接失败: {str(e)}")


@router.post("/disconnect/{account_id}")
async def disconnect_account(
    account_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    断开指定账号的IM WebSocket
    """
    try:
        manager = get_im_session_manager()
        await manager.disconnect(account_id)
        # 手动断开 = 用户不想让它在线，移出「重启后自动恢复」清单
        await _save_auto_connect_id(db, account_id, False)
        return ApiResponse(success=True, message="已断开连接")

    except Exception as e:
        logger.error(f"【{account_id}】断开IM失败: {e}")
        return ApiResponse(success=False, message=f"断开失败: {str(e)}")


@router.post("/conversations/{account_id}/{cid}/hide")
async def hide_conversation(
    account_id: str,
    cid: str,
    current_user: User = Depends(get_current_active_user),
):
    """
    从会话列表删除指定会话（仅本系统隐藏，不动闲鱼服务端）

    按「删除时刻」记录：之后该会话再来新消息（lastMessageTime 晚于删除
    时刻）会自动恢复显示，符合"对方重新找我，自然重新加载"的预期。
    """
    cid = (cid or "").strip()
    if not cid:
        return ApiResponse(success=False, message="缺少会话ID")
    try:
        redis_client = await get_redis_client()
        key = f"{HIDDEN_CONVS_PREFIX}{account_id}"
        await redis_client.zadd(key, {cid: int(time.time() * 1000)})
        # 防止无限增长：只保留最近 HIDDEN_CONVS_MAX 条删除记录
        if await redis_client.zcard(key) > HIDDEN_CONVS_MAX:
            await redis_client.zremrangebyrank(key, 0, -(HIDDEN_CONVS_MAX + 1))
        return ApiResponse(success=True, message="会话已删除")
    except Exception as e:
        logger.error(f"【{account_id}】删除会话 {cid} 失败: {e}")
        return ApiResponse(success=False, message=f"删除会话失败: {str(e)}")


async def _filter_hidden_conversations(
    account_id: str, conversations: list
) -> list:
    """
    过滤用户手动删除的会话

    删除后该会话又有新消息（lastMessageTime 晚于删除时刻）时自动恢复显示，
    并顺手清掉这条删除记录。
    """
    if not conversations:
        return conversations
    try:
        redis_client = await get_redis_client()
        key = f"{HIDDEN_CONVS_PREFIX}{account_id}"
        pipe = redis_client.pipeline()
        for conv in conversations:
            pipe.zscore(key, conv.get("cid", ""))
        scores = await pipe.execute()
    except Exception as e:
        logger.warning(f"【{account_id}】读取已删除会话记录失败: {e}")
        return conversations

    restored: list = []
    kept: list = []
    for conv, hidden_at in zip(conversations, scores):
        if hidden_at is None:
            kept.append(conv)
            continue
        last_ms = conv.get("lastMessageTime") or 0
        if last_ms and last_ms < 1e12:
            last_ms *= 1000  # 秒 → 毫秒
        if last_ms > float(hidden_at):
            restored.append(conv["cid"])  # 删除后又来了新消息，恢复显示
            kept.append(conv)
        # 否则保持隐藏（不加入 kept）
    if restored:
        try:
            await redis_client.zrem(key, *restored)
        except Exception as e:
            logger.warning(f"【{account_id}】清理会话删除记录失败: {e}")
    return kept


async def _convs_lastgood_response(account_id: str) -> ApiResponse | None:
    """读取流控兜底缓存，命中则返回带 rateLimited 标记的成功响应"""
    try:
        redis_client = await get_redis_client()
        raw = await redis_client.get(f"{CONVS_LASTGOOD_PREFIX}{account_id}")
        if raw:
            cached = json.loads(raw)
            logger.info(
                f"【{account_id}】会话列表被闲鱼限流，回退最近一次缓存"
                f"（{len(cached.get('conversations', []))} 条）"
            )
            return ApiResponse(success=True, data={**cached, "rateLimited": True})
    except Exception as e:
        logger.warning(f"【{account_id}】读取会话列表兜底缓存失败: {e}")
    return None


@router.get("/conversations/{account_id}")
async def get_conversations(
    account_id: str,
    cursor: int = None,
    limit: int = 20,
    current_user: User = Depends(get_current_active_user),
):
    """
    获取指定账号的会话列表

    Args:
        account_id: 账号ID
        cursor: 分页游标（首页不传，翻页传nextCursor）
        limit: 每页数量，默认20
    """
    try:
        manager = get_im_session_manager()
        client = manager.clients.get(account_id)
        if not client or not client.is_connected:
            return ApiResponse(success=False, message="账号未连接，请先连接")

        # 冷却期内不再向闲鱼发请求（仅首页），直接回兜底缓存
        if cursor is None and time.time() < _convs_rate_limit_until.get(account_id, 0):
            fallback = await _convs_lastgood_response(account_id)
            if fallback:
                return fallback
            return ApiResponse(success=False, message="请求过于频繁，请稍后再试")

        body = await client.get_conversations(
            start_timestamp=cursor, limit=limit
        )

        # 检测IM错误响应（流控或其他服务端错误）
        if isinstance(body, dict) and "reason" in body:
            reason = body.get("reason", "")
            err_code = body.get("code", "")
            logger.warning(
                f"【{account_id}】IM会话列表返回错误, "
                f"code={err_code}, reason={reason}"
            )
            # 流控错误：指数退避记冷却 + 优先回退最近一次成功的会话列表（仅首页）
            if err_code == "400600001":
                # 连续被限流就翻倍冷却（5→10→20→…封顶 30 分钟），成功一次后重置。
                # 固定冷却会在到期瞬间再撞一次限流，把闲鱼的限流窗口续上，
                # 账号半天缓不过来——退避才能给出真正的安静期让它自愈。
                level = _convs_backoff_level.get(account_id, 0) + 1
                _convs_backoff_level[account_id] = level
                cooldown = min(
                    CONVS_COOLDOWN_S * (2 ** (level - 1)), CONVS_COOLDOWN_MAX_S
                )
                _convs_rate_limit_until[account_id] = time.time() + cooldown
                logger.info(
                    f"【{account_id}】会话列表被闲鱼限流，进入冷却 {cooldown}s"
                    f"（第 {level} 次连续）"
                )
                if cursor is None:
                    fallback = await _convs_lastgood_response(account_id)
                    if fallback:
                        return fallback
                return ApiResponse(success=False, message="请求过于频繁，请稍后再试")
            return ApiResponse(success=False, message=f"IM服务异常: {reason or err_code}")

        # 解析会话列表
        conversations = []
        user_convs = body.get("userConvs", [])
        for item in user_convs:
            # 实际会话数据嵌套在 singleChatUserConversation 中
            conv = item.get("singleChatUserConversation", item) if isinstance(item, dict) else item
            conv_info = _parse_conversation(conv, client.myid)
            if conv_info:
                conversations.append(conv_info)

        # 用户手动删除的会话不再返回（删除后又有新消息的会自动恢复显示）
        conversations = await _filter_hidden_conversations(account_id, conversations)

        # 头像/昵称优先用主页信息缓存（按 userId、30 分钟）：
        # user.query 返回的 logo 实测不可靠——同一条会话查对方和自己，
        # 返回的是同一张错误头像。这里只读缓存不发起 mtop 请求。
        try:
            redis_client = await get_redis_client()
            profile_pipe = redis_client.pipeline()
            for c in conversations:
                profile_pipe.get(f"{PROFILE_CACHE_PREFIX}{c['otherUserId']}")
            profile_results = await profile_pipe.execute()
            for c, raw in zip(conversations, profile_results):
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("avatar"):
                    c["otherUserAvatar"] = data["avatar"]
                # 主页接口（user.page.head）的昵称也是打码的（樱***1）：
                # 只在会话列表连打码名都没有时才补位，别用它覆盖已有的昵称
                p_nick = data.get("nick", "")
                cur_nick = c.get("otherUserName", "")
                if p_nick and (not cur_nick or cur_nick.isdigit()):
                    c["otherUserName"] = p_nick
        except Exception as e:
            logger.warning(f"【{account_id}】读取主页信息缓存失败: {e}")

        # 从 Redis 批量读取所有会话的缓存，按优先级确定昵称并回写更优数据
        try:
            redis_client = await get_redis_client()
            read_pipe = redis_client.pipeline()
            for c in conversations:
                read_pipe.get(f"{AVATAR_CACHE_PREFIX}{c['cid']}")
            results = await read_pipe.execute()

            write_pipe = redis_client.pipeline()
            need_write = False

            for c, cached_raw in zip(conversations, results):
                # 解析缓存数据
                cached_data = {}
                if cached_raw:
                    try:
                        parsed = json.loads(cached_raw) if isinstance(cached_raw, str) else cached_raw
                        if isinstance(parsed, str):
                            cached_data = {"avatar": parsed, "nick": ""}
                        elif isinstance(parsed, dict):
                            cached_data = parsed
                    except (json.JSONDecodeError, TypeError):
                        cached_data = {}

                cached_nick = cached_data.get("nick", "")
                cached_avatar = cached_data.get("avatar", "")
                conv_nick = c.get("otherUserName", "")  # 从会话列表 reminderTitle 提取的

                # 昵称优先级：完整昵称 > 打码昵称（樱***1）> 空。
                # 会话列表的 reminderTitle 常被闲鱼打码，user.query 缓存里是完整名，
                # 不能让打码名压过完整名（此前会话列表有效就无条件用它并回写缓存）。
                conv_full = bool(conv_nick) and _is_valid_nick(conv_nick) and "***" not in conv_nick
                cache_full = bool(cached_nick) and _is_valid_nick(cached_nick) and "***" not in cached_nick
                if conv_full and (not cached_nick or not cache_full):
                    # 会话列表是完整名、缓存缺名或打码 → 用完整名升级缓存
                    new_cache = {"avatar": cached_avatar, "nick": conv_nick}
                    write_pipe.set(
                        f"{AVATAR_CACHE_PREFIX}{c['cid']}",
                        json.dumps(new_cache, ensure_ascii=False),
                        ex=AVATAR_CACHE_TTL,
                    )
                    need_write = True
                if not conv_full and cache_full:
                    # 会话列表被打码/缺失 → 用缓存里的完整名升级展示
                    c["otherUserName"] = cached_nick

                # 头像：不用 chat:avatar 缓存补填——那是 user.query 的结果，
                # 实测存的是查询账号自己的头像，主页缓存过期时会把对方头像
                # 错显示成自己的。头像只认主页接口（chat:user_profile），
                # 拿不到就留空由前端批量 /avatars 再查，宁可占位也不错认。

            if need_write:
                await write_pipe.execute()

        except Exception as e:
            logger.warning(f"【{account_id}】Redis批量处理用户信息失败: {e}")

        # 首页拉取成功：缓存一份作为流控兜底（翻页不缓存）
        if cursor is None:
            # 首页拉取成功：重置退避等级（账号已恢复，下次限流从 5 分钟重新算起）
            _convs_backoff_level.pop(account_id, None)
            try:
                redis_client = await get_redis_client()
                await redis_client.set(
                    f"{CONVS_LASTGOOD_PREFIX}{account_id}",
                    json.dumps({
                        "conversations": conversations,
                        "hasMore": body.get("hasMore", False),
                        "nextCursor": body.get("nextCursor", None),
                    }, ensure_ascii=False),
                    ex=CONVS_LASTGOOD_TTL,
                )
            except Exception as e:
                logger.warning(f"【{account_id}】写会话列表兜底缓存失败: {e}")

        return ApiResponse(
            success=True,
            data={
                "conversations": conversations,
                "hasMore": body.get("hasMore", False),
                "nextCursor": body.get("nextCursor", None),
            },
        )

    except Exception as e:
        logger.error(f"【{account_id}】获取会话列表失败: {e}")
        return ApiResponse(success=False, message=f"获取会话列表失败: {str(e)}")


async def _upgrade_account_display_name(db: AsyncSession, account_id: str, nick: str) -> None:
    """用完整昵称升级账号展示名（闲鱼账号/会话接口返回的昵称都是打码的）。

    只在当前展示名为空或含 * 时才写库，完整昵称一旦落库后续调用直接跳过。
    失败只告警不抛出——昵称升级是锦上添花，不能影响消息拉取主流程。
    """
    try:
        result = await db.execute(
            select(XYAccount.display_name).where(XYAccount.account_id == account_id)
        )
        current = (result.scalar() or "").strip()
        if current and "*" not in current:
            return
        await db.execute(
            update(XYAccount)
            .where(XYAccount.account_id == account_id)
            .values(display_name=nick)
        )
        await db.commit()
        logger.info(f"【{account_id}】账号展示名已从「{current or '空'}」升级为完整昵称「{nick}」")
    except Exception as e:
        logger.warning(f"【{account_id}】升级账号展示名失败: {e}")
        try:
            await db.rollback()
        except Exception:
            pass


@router.get("/messages/{account_id}/{cid}")
async def get_messages(
    account_id: str,
    cid: str,
    cursor: int = None,
    limit: int = 20,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    获取指定会话的聊天记录

    Args:
        account_id: 账号ID
        cid: 会话ID
        cursor: 分页游标（首页不传，翻页传nextCursor）
        limit: 每页数量，默认20
    """
    try:
        manager = get_im_session_manager()
        client = manager.clients.get(account_id)
        if not client or not client.is_connected:
            return ApiResponse(success=False, message="账号未连接，请先连接")

        body = await client.get_messages(
            cid=cid, start_timestamp=cursor, limit=limit
        )

        # IM 返回错误时不能伪装成成功的空分页，否则前端会把断线补偿边界
        # 当成已补齐并清除待补偿状态，导致真实消息永久漏拉。
        if isinstance(body, dict) and "reason" in body:
            err_msg = body.get("developerMessage", body.get("reason", ""))
            logger.warning(f"【{account_id}】IM消息列表返回错误: {err_msg}，保留补偿边界等待重试")
            return ApiResponse(
                success=False,
                message=f"获取聊天记录失败，IM服务暂时不可用: {err_msg or '未知错误'}",
            )

        # 解析消息列表（IM返回倒序，需反转为正序：最旧在前，最新在后）
        messages = []
        models = body.get("userMessageModels", [])
        for model in models:
            msg_info = _parse_message(model, client.myid, account_id, cid)
            if msg_info:
                messages.append(msg_info)
        messages.reverse()

        # 自己消息里的 senderName 是完整昵称（账号信息/会话列表接口返回的都是
        # 打码名，如 樱***1）。账号列表展示名还是打码/空的话，顺手用完整昵称升级。
        self_nick = next(
            (m.get("senderName") for m in messages
             if m.get("isSelf") and m.get("senderName") and "*" not in m["senderName"]),
            "",
        )
        if self_nick:
            await _upgrade_account_display_name(db, account_id, self_nick)

        return ApiResponse(
            success=True,
            data={
                "messages": messages,
                "hasMore": body.get("hasMore", False)
                    if isinstance(body.get("hasMore"), bool)
                    else body.get("hasMore", 0) == 1,
                "nextCursor": body.get("nextCursor", None),
            },
        )

    except Exception as e:
        logger.error(f"【{account_id}】获取聊天记录失败: {e}")
        return ApiResponse(success=False, message=f"获取聊天记录失败: {str(e)}")


# ==================== 发送消息请求模型 ====================


class SendMessageRequest(BaseModel):
    """发送消息请求体"""
    cid: str
    toUserId: str
    text: str


class ForwardMessageRequest(BaseModel):
    """转发消息请求体：把一条已有消息（文本/图片）原样发到另一个会话"""
    toCid: str
    toUserId: str
    msgType: str = "text"  # text / image
    text: str = ""         # msgType=text 时的内容
    imageUrl: str = ""     # msgType=image 时的闲鱼CDN图片地址
    width: int = 800
    height: int = 600


class RecallMessageRequest(BaseModel):
    messageId: str
    messageTime: int


class AvatarQueryItem(BaseModel):
    """单条头像查询项"""
    userId: str
    cid: str


class AvatarQueryRequest(BaseModel):
    """批量查询头像请求体"""
    queries: list[AvatarQueryItem]


@router.post("/send-message/{account_id}")
async def send_message(
    account_id: str,
    req: SendMessageRequest,
    current_user: User = Depends(get_current_active_user),
):
    """
    发送文本消息

    Args:
        account_id: 账号ID
        req: 包含 cid（会话ID）、toUserId（对方用户ID）、text（消息内容）
    """
    try:
        manager = get_im_session_manager()
        client = manager.clients.get(account_id)
        if not client or not client.is_connected:
            return ApiResponse(success=False, message="账号未连接，请先连接")

        if not req.text.strip():
            return ApiResponse(success=False, message="消息内容不能为空")

        send_result = await client.send_text_message(
            cid=req.cid,
            to_user_id=req.toUserId,
            text=req.text,
        )
        logger.info(
            f"【{account_id}】发送消息到 {req.toUserId}: {req.text[:50]}"
        )
        # 系统内互发实时性补偿：对方账号也在本系统在线时，向其前端合成
        # new_message 事件。闲鱼服务端不会把网页会话发出的消息实时推给
        # 其他网页会话（实测），不补偿的话接收方界面毫无动静。
        try:
            await manager.notify_peer_account(
                account_id,
                req.cid,
                req.toUserId,
                message_id=send_result.get("messageId", ""),
                msg_type="text",
                text=req.text,
            )
        except Exception as notify_e:
            logger.warning(f"【{account_id}】系统内互发补偿失败: {notify_e}")
        return ApiResponse(
            success=True,
            message="发送成功",
            data={"messageId": send_result.get("messageId", "")},
        )

    except Exception as e:
        # send_text_message 在被 IM 安全拦截等业务错误时会抛出明文原因，
        # 直接透传给前端展示（如"内容存在不当信息..."），便于用户调整后重发。
        logger.warning(f"【{account_id}】发送消息失败: {e}")
        return ApiResponse(success=False, message=f"发送失败：{str(e)}")


@router.post("/forward-message/{account_id}")
async def forward_message(
    account_id: str,
    req: ForwardMessageRequest,
    current_user: User = Depends(get_current_active_user),
):
    """
    转发消息：把一条已有消息（文本/图片）原样发到另一个会话

    Args:
        account_id: 账号ID
        req: toCid/toUserId（目标会话）+ msgType/text/imageUrl（源消息内容）
    """
    try:
        manager = get_im_session_manager()
        client = manager.clients.get(account_id)
        if not client or not client.is_connected:
            return ApiResponse(success=False, message="账号未连接，请先连接")

        if not req.toCid.strip() or not req.toUserId.strip():
            return ApiResponse(success=False, message="目标会话信息缺失")

        if req.msgType == "image":
            if not req.imageUrl.strip():
                return ApiResponse(success=False, message="图片地址缺失，无法转发")
            send_result = await client.send_image_message(
                cid=req.toCid,
                to_user_id=req.toUserId,
                image_url=req.imageUrl,
                width=req.width,
                height=req.height,
            )
            preview = "[图片]"
        else:
            if not req.text.strip():
                return ApiResponse(success=False, message="消息内容不能为空")
            send_result = await client.send_text_message(
                cid=req.toCid,
                to_user_id=req.toUserId,
                text=req.text,
            )
            preview = req.text[:50]

        logger.info(
            f"【{account_id}】转发消息到 {req.toUserId} (cid={req.toCid}): {preview}"
        )
        # 与发送一致：系统内其他账号在线时做实时性补偿
        try:
            await manager.notify_peer_account(
                account_id,
                req.toCid,
                req.toUserId,
                message_id=send_result.get("messageId", ""),
                msg_type=req.msgType if req.msgType in ("text", "image") else "text",
                text=req.text,
                images=[req.imageUrl] if req.msgType == "image" and req.imageUrl else None,
            )
        except Exception as notify_e:
            logger.warning(f"【{account_id}】转发系统内互发补偿失败: {notify_e}")
        return ApiResponse(
            success=True,
            message="转发成功",
            data={"messageId": send_result.get("messageId", "")},
        )

    except Exception as e:
        logger.warning(f"【{account_id}】转发消息失败: {e}")
        return ApiResponse(success=False, message=f"转发失败：{str(e)}")


@router.post("/recall-message/{account_id}")
async def recall_message(
    account_id: str,
    req: RecallMessageRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    if not await _get_owned_chat_account(account_id, current_user, db):
        return ApiResponse(success=False, message="账号不存在或无权操作")
    client = get_im_session_manager().clients.get(account_id)
    if not client or not client.is_connected:
        return ApiResponse(success=False, message="账号未连接")
    if not req.messageId:
        return ApiResponse(success=False, message="缺少消息ID，无法撤回")
    message_time_ms = req.messageTime * 1000 if req.messageTime < 1_000_000_000_000 else req.messageTime
    elapsed_ms = int(time.time() * 1000) - message_time_ms
    if elapsed_ms < -10_000 or elapsed_ms > 120_000:
        return ApiResponse(success=False, message="消息发送超过两分钟，无法撤回")
    try:
        await client.recall_message(req.messageId)
        return ApiResponse(success=True, message="消息已撤回")
    except Exception as e:
        logger.error(f"【{account_id}】撤回消息失败: {e}")
        return ApiResponse(success=False, message=f"撤回失败: {e}")


async def _get_owned_chat_account(account_id: str, current_user: User, db: AsyncSession) -> XYAccount | None:
    query = select(XYAccount).where(XYAccount.account_id == account_id)
    if not is_admin_user(current_user):
        query = query.where(XYAccount.owner_id == current_user.id)
    return (await db.execute(query)).scalar_one_or_none()


@router.get("/official-blacklist/{account_id}/{cid}")
async def query_official_blacklist(
    account_id: str,
    cid: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    account = await _get_owned_chat_account(account_id, current_user, db)
    if not account or not account.cookie:
        return ApiResponse(success=False, message="账号不存在或Cookie为空")
    try:
        data = await official_blacklist_request(account.cookie, cid, "query")
        return ApiResponse(success=True, data={"blocked": bool(data.get("isInBlack"))})
    except Exception as e:
        return ApiResponse(success=False, message=f"查询黑名单状态失败: {e}")


@router.post("/official-blacklist/{account_id}/{cid}/{action}")
async def change_official_blacklist(
    account_id: str,
    cid: str,
    action: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    if action not in {"add", "remove"}:
        return ApiResponse(success=False, message="无效操作")
    account = await _get_owned_chat_account(account_id, current_user, db)
    if not account or not account.cookie:
        return ApiResponse(success=False, message="账号不存在或Cookie为空")
    try:
        await official_blacklist_request(account.cookie, cid, action)
        blocked = action == "add"
        return ApiResponse(
            success=True,
            message="已加入闲鱼官方黑名单" if blocked else "已解除闲鱼官方黑名单",
            data={"blocked": blocked},
        )
    except Exception as e:
        return ApiResponse(success=False, message=f"黑名单操作失败: {e}")


@router.post("/avatars/{account_id}")
async def query_avatars(
    account_id: str,
    req: AvatarQueryRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    批量查询用户头像

    优先从Redis缓存获取，缓存未命中时调用mtop API查询，结果缓存24小时

    Args:
        account_id: 账号ID
        req: 包含 session_ids 列表
    """
    try:
        # 校验账号归属并获取cookie（管理员可操作任意账号）
        query = select(XYAccount).where(XYAccount.account_id == account_id)
        if not is_admin_user(current_user):
            query = query.where(XYAccount.owner_id == current_user.id)
        result = await db.execute(query)
        account = result.scalar_one_or_none()
        if not account or not account.cookie:
            return ApiResponse(success=False, message="账号不存在或Cookie为空")

        user_infos = {}
        for idx, item in enumerate(req.queries):
            # 每次请求间隔 0.8 秒：主页接口对突发频率敏感（0.3s 实测会触发
            # x5sec 风控挑战），宁可慢一点也别把账号查进黑名单
            if idx > 0:
                await asyncio.sleep(0.8)
            # 头像只走主页接口（按 userId 缓存 30 分钟，不查活跃信息）：
            # user.query 返回的 logo 实测是查询账号自己的头像——同一条会话查
            # 对方和自己，返回同一张错误头像。宁可不显示，也不显示成自己的。
            info = None
            try:
                profile = await get_user_profile(
                    account_id=account_id,
                    user_id=item.userId,
                    cookies_str=account.cookie,
                    db=db,
                    with_activity=False,
                )
            except Exception as profile_e:
                logger.warning(f"【{account_id}】主页信息查询失败（头像留空）: {profile_e}")
                profile = None
            if profile and (profile.get("avatar") or profile.get("nick")):
                info = {"avatar": profile.get("avatar", ""), "nick": profile.get("nick", "")}
            if info:
                user_infos[item.userId] = info

        return ApiResponse(success=True, data=user_infos)

    except Exception as e:
        logger.error(f"【{account_id}】批量查询头像失败: {e}")
        return ApiResponse(success=False, message=f"查询头像失败: {str(e)}")


# ==================== 聊天图片代理 ====================

# 允许代理的图片域名后缀（闲鱼 / 淘宝 CDN）
# 目的：防止本接口被当成任意 URL 的代理服务器（SSRF）
_IMAGE_PROXY_HOST_SUFFIXES = (
    ".alicdn.com",
    ".goofish.com",
    ".taobao.com",
    ".tbcdn.cn",
)
_IMAGE_PROXY_MAX_BYTES = 10 * 1024 * 1024
_IMAGE_PROXY_TIMEOUT = 15


def _is_allowed_image_host(hostname: str) -> bool:
    """仅允许闲鱼/淘宝 CDN 域名，其它域名一律拒绝"""
    host = (hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    return any(host == s.lstrip(".") or host.endswith(s) for s in _IMAGE_PROXY_HOST_SUFFIXES)


def _is_private_ip(text: str) -> bool:
    """判断解析出的 IP 是否属于内网/回环等不可访问网段"""
    try:
        ip_obj = ipaddress.ip_address(text)
    except ValueError:
        return False
    return (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_reserved
        or ip_obj.is_multicast
    )


@router.get("/proxy-image")
async def proxy_chat_image(
    url: str,
    current_user: User = Depends(get_current_active_user),
):
    """
    聊天图片代理：代拉闲鱼 CDN 图片，以同源响应返回

    用途：前端要把图片写进剪贴板（右键「复制图片」），而闲鱼 CDN 不带 CORS 头，
          浏览器直接 fetch 会被同源策略拦掉，所以改由后端代拉。

    安全措施（防 SSRF）：
    - 必须登录
    - 仅允许 http(s) 协议
    - 域名必须在闲鱼/淘宝 CDN 白名单内
    - DNS 解析结果不得落在内网/回环网段
    - 跟随重定向后再次校验最终域名
    - 响应必须是 image/*，且不超过 10MB / 15 秒
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return ApiResponse(success=False, message="不支持的图片地址")
        if not _is_allowed_image_host(parsed.hostname or ""):
            logger.warning(f"[图片代理] 拒绝非白名单域名: {parsed.hostname} user={current_user.id}")
            return ApiResponse(success=False, message="该图片地址不在允许范围内")

        # 防 SSRF：白名单域名也不允许解析到内网地址
        try:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(parsed.hostname, None)
        except Exception:
            return ApiResponse(success=False, message="图片地址解析失败")
        for _family, _type, _proto, _canonname, sockaddr in infos:
            if _is_private_ip(sockaddr[0]):
                logger.warning(f"[图片代理] 拒绝内网地址: {parsed.hostname} -> {sockaddr[0]}")
                return ApiResponse(success=False, message="该图片地址不在允许范围内")

        headers = {
            # 闲鱼 CDN 对无 Referer 的请求可能拒绝，伪装成官方页面来源
            "Referer": "https://www.goofish.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        timeout = aiohttp.ClientTimeout(total=_IMAGE_PROXY_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=True, max_redirects=3) as resp:
                if resp.status != 200:
                    return ApiResponse(success=False, message=f"图片获取失败({resp.status})")
                # 重定向后再次校验最终域名，避免借重定向绕过白名单
                if not _is_allowed_image_host(resp.url.host or ""):
                    logger.warning(f"[图片代理] 拒绝重定向后域名: {resp.url.host}")
                    return ApiResponse(success=False, message="该图片地址不在允许范围内")
                content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if not content_type.startswith("image/"):
                    return ApiResponse(success=False, message="该地址返回的不是图片")
                declared = resp.headers.get("Content-Length")
                if declared and int(declared) > _IMAGE_PROXY_MAX_BYTES:
                    return ApiResponse(success=False, message="图片过大")
                data = await resp.read()
                if len(data) > _IMAGE_PROXY_MAX_BYTES:
                    return ApiResponse(success=False, message="图片过大")

        return Response(
            content=data,
            media_type=content_type,
            headers={"Cache-Control": "private, max-age=300"},
        )
    except asyncio.TimeoutError:
        return ApiResponse(success=False, message="图片获取超时")
    except Exception as e:
        logger.error(f"[图片代理] 处理失败: {e}")
        return ApiResponse(success=False, message="图片获取失败")


# ==================== 消息解析辅助函数 ====================


@router.get("/account-profile/{account_id}")
async def get_account_profile(
    account_id: str,
    cid: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    """查询并持久化卖家在闲鱼的真实昵称"""
    query = select(XYAccount).where(XYAccount.account_id == account_id)
    if not is_admin_user(current_user):
        query = query.where(XYAccount.owner_id == current_user.id)
    account = (await db.execute(query)).scalar_one_or_none()
    if not account or not account.cookie:
        return ApiResponse(success=False, message="账号不存在或Cookie为空")

    # 优先走主页接口：user.query 返回的 logo 实测不可靠（同一条会话查对方
    # 和自己返回同一张错误头像），昵称还是脱敏的（如 x***1）；主页接口的
    # 才是真实昵称和头像。拿不到时再退回 user.query。
    manager = get_im_session_manager()
    client = manager.clients.get(account_id)
    # 自己的用户ID：优先取 IM 客户端的 myid，账号未连接时从 Cookie 的 unb 取
    my_id = (getattr(client, "myid", "") if client else "") or trans_cookies(account.cookie).get("unb", "")
    info = None
    if my_id:
        try:
            profile = await get_user_profile(
                account_id, my_id, account.cookie, db, with_activity=False,
            )
        except Exception as profile_e:
            logger.warning(f"【{account_id}】主页信息查询失败，退回 user.query: {profile_e}")
            profile = None
        if profile and (profile.get("avatar") or profile.get("nick")):
            info = {"avatar": profile.get("avatar", ""), "nick": profile.get("nick", "")}
    if not info:
        info = await get_owner_user_info(account_id, cid, account.cookie, db)
    if info and _is_valid_nick(info.get("nick", "")):
        account.display_name = info["nick"]
        await db.commit()
    return ApiResponse(success=True, data=info or {})


@router.get("/user-profile/{account_id}/{user_id}")
async def get_chat_user_profile(
    account_id: str,
    user_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    查询对方的闲鱼主页信息（会话窗口的"买家名片"）

    走闲鱼网页版个人主页接口 mtop.idle.web.user.page.head，拿到昵称/头像/
    属地/简介/店铺等级/好评率/粉丝数/认证标签等。按用户ID缓存 30 分钟，
    避免同一个买家反复查询触发风控。
    """
    query = select(XYAccount).where(XYAccount.account_id == account_id)
    if not is_admin_user(current_user):
        query = query.where(XYAccount.owner_id == current_user.id)
    account = (await db.execute(query)).scalar_one_or_none()
    if not account or not account.cookie:
        return ApiResponse(success=False, message="账号不存在或Cookie为空")

    profile = await get_user_profile(account_id, user_id, account.cookie, db)
    if not profile:
        return ApiResponse(success=False, message="未获取到对方主页信息")
    return ApiResponse(success=True, data=profile)


# ==================== 会话关联商品信息 ====================

# 商品详情缓存：key=f"{account_id}:{item_id}"，value=(过期时间戳, data)
# 会话 extension 里自带 itemId/itemTitle/itemMainPic，但没有价格，
# 价格需要额外调闲鱼 mtop.taobao.idle.pc.detail，故加缓存避免频繁请求。
_ITEM_INFO_CACHE: dict[str, tuple[float, dict]] = {}
_ITEM_INFO_TTL = 600


@router.get("/item-info/{account_id}/{item_id}")
async def get_chat_item_info(
    account_id: str,
    item_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    查询会话关联商品的详情（价格/原价/运费/状态）

    会话列表的 extension 里只有 itemId / itemTitle / itemMainPic，不含价格，
    所以价格要额外查一次闲鱼商品详情接口，结果缓存 10 分钟。

    商品详情是风控较严的接口（账号可能整体被拦 FAIL_SYS_USER_VALIDATE），
    当前账号不可用时自动借用同一用户名下的其它账号兜底。
    """
    cache_key = f"{account_id}:{item_id}"
    now = time.time()
    hit = _ITEM_INFO_CACHE.get(cache_key)
    if hit and hit[0] > now:
        return ApiResponse(success=True, data=hit[1])

    manager = get_im_session_manager()
    client = manager.clients.get(account_id)

    from common.services.xianyu_detail_client import XianyuItemDetailClient

    async def try_detail(aid: str, cookies: str) -> dict:
        return await XianyuItemDetailClient(aid, cookies).get_detail(item_id)

    result: dict | None = None
    if client and client.is_connected:
        result = await try_detail(account_id, client.cookies_str)

    # 当前账号不在线，或被商品详情接口风控（account_invalid）→ 换同用户其它账号重试。
    # 注意：商品级失败（下架/不存在，item_invalid）不代表账号有问题，不重试。
    account_blocked = result is None or bool(result.get("account_invalid"))
    if account_blocked:
        for fb_account, fb_cookies in await _load_sibling_accounts(account_id, db, limit=3):
            result = await try_detail(fb_account, fb_cookies)
            if result.get("success"):
                logger.info(f"【{account_id}】商品详情由备用账号 {fb_account} 查到: {item_id}")
                break

    if not result or not result.get("success"):
        return ApiResponse(
            success=False,
            message=(result or {}).get("error") or "商品信息获取失败",
        )

    item_do = (result.get("detail") or {}).get("itemDO") or {}

    # 商品图：imageInfos 是一个列表，major=True 的是主图，排到最前
    images: list[dict] = []
    for img in item_do.get("imageInfos") or []:
        if isinstance(img, dict) and img.get("url"):
            url = str(img["url"])
            # 详情接口返回的图片是 http://，统一成 https 避免将来 https 部署时被浏览器
            # 按混合内容拦截（alicdn 同时支持 https）
            if url.startswith("http://"):
                url = "https://" + url[len("http://"):]
            images.append({"url": url, "major": bool(img.get("major"))})
    images.sort(key=lambda x: not x["major"])

    data = {
        "itemId": str(item_id),
        "title": item_do.get("title", "") or "",
        "price": item_do.get("soldPrice", "") or "",
        "originalPrice": item_do.get("originalPrice", "") or "",
        "transportFee": item_do.get("transportFee", "") or "",
        "status": item_do.get("itemStatusStr", "") or "",
        # 下面这些用于前端自渲染商品详情，避免在弹窗里嵌闲鱼页面（那样会要求浏览器登录）
        "desc": item_do.get("desc", "") or "",
        "images": images,
        "browseCnt": item_do.get("browseCnt", "") or "",
        "collectCnt": item_do.get("collectCnt", "") or "",
        "quantity": item_do.get("quantity", "") or "",
        "sellerId": result.get("seller_user_id") or "",
        "sellerNick": result.get("seller_nick") or "",
    }
    _ITEM_INFO_CACHE[cache_key] = (now + _ITEM_INFO_TTL, data)
    return ApiResponse(success=True, data=data)


def _parse_conversation(conv: dict, myid: str) -> dict | None:
    """
    解析单个会话数据

    数据结构参照 goofish-client 类型定义：
    userConvs[i] = {
        type: number,
        singleChatUserConversation: {
            singleChatConversation: { cid, pairFirst, pairSecond, extension },
            lastMessage: { message: { content, extension, ... } },
            modifyTime,
            redPoint,
        }
    }

    Args:
        conv: 原始会话数据（已解包 singleChatUserConversation）
        myid: 当前账号的用户ID

    Returns:
        格式化后的会话字典
    """
    try:
        # 获取单聊会话信息
        single_conv = conv.get("singleChatConversation", {})
        cid = single_conv.get("cid", "")
        if not cid:
            return None
        # 去掉 @goofish 后缀
        raw_cid = cid
        if "@goofish" in cid:
            cid = cid.split("@")[0]
        if not cid:
            return None

        # 通过 pairFirst/pairSecond 确定对方用户ID
        # 注意：pairFirst/pairSecond 可能带 @goofish 后缀，需先去掉再比较
        pair_first_raw = single_conv.get("pairFirst", "")
        pair_second_raw = single_conv.get("pairSecond", "")
        pair_first = pair_first_raw.split("@")[0] if "@" in pair_first_raw else pair_first_raw
        pair_second = pair_second_raw.split("@")[0] if "@" in pair_second_raw else pair_second_raw
        other_user_id = pair_second if pair_first == myid else pair_first

        # 过滤无效会话：otherUserId 为空或 "0" 的是系统/通知类会话，不返回给前端
        if not other_user_id or other_user_id == "0":
            return None

        # 从 extension 获取商品信息（可用作显示补充）
        ext = single_conv.get("extension", {})
        if isinstance(ext, str):
            try:
                ext = json.loads(ext)
            except (json.JSONDecodeError, TypeError):
                ext = {}
        # extension 里带的商品信息（实测字段：itemId / itemTitle / itemMainPic /
        # itemSellerId；价格不在其中，需要另外调商品详情接口）
        item_title = ext.get("itemTitle", "") if isinstance(ext, dict) else ""
        item_id = str(ext.get("itemId", "") or "") if isinstance(ext, dict) else ""
        item_main_pic = ext.get("itemMainPic", "") if isinstance(ext, dict) else ""

        # 最后一条消息
        last_msg_obj = conv.get("lastMessage", {})
        last_message = last_msg_obj.get("message", {}) if last_msg_obj else {}
        last_msg_summary = _extract_message_summary(last_message, myid)
        last_msg_time = conv.get("modifyTime", 0)

        # 未读数（redPoint）
        unread_count = conv.get("redPoint", 0)

        # 从最后一条消息的 extension 中提取对方名称
        # reminderTitle 是最后一条消息的发送者名称，需要判断是否为对方
        last_ext = last_message.get("extension", {})
        if isinstance(last_ext, str):
            try:
                last_ext = json.loads(last_ext)
            except (json.JSONDecodeError, TypeError):
                last_ext = {}
        sender_user_id_raw = last_ext.get("senderUserId", "") if isinstance(last_ext, dict) else ""
        sender_user_id = str(sender_user_id_raw).split("@")[0] if "@" in str(sender_user_id_raw) else str(sender_user_id_raw)
        reminder_title = last_ext.get("reminderTitle", "") if isinstance(last_ext, dict) else ""
        # 只有当最后一条消息的发送者是对方时，reminderTitle 才是对方名称
        # 用 other_user_id 比较比 myid 更可靠（避免格式不一致）
        # 昵称必须是有效的（非空、非纯数字），否则视为未获取到
        if sender_user_id and sender_user_id == other_user_id and _is_valid_nick(reminder_title):
            other_user_name = reminder_title
        else:
            other_user_name = ""

        # 摘要加发送者前缀：自己的消息「你：」，对方的「他：」。
        # 性别数据拿不到，对方统一用「他」；系统消息 sender 为空不加前缀。
        if last_msg_summary and sender_user_id:
            # 托管账号发送也算「你」（cid 跨账号共享，同上）
            prefix = "你" if (sender_user_id == myid or is_managed_account(sender_user_id)) else "他"
            last_msg_summary = f"{prefix}：{last_msg_summary}"

        return {
            "cid": cid,
            "rawCid": raw_cid,
            "otherUserId": other_user_id,
            "otherUserName": other_user_name,
            "otherUserAvatar": "",
            "itemTitle": item_title,
            "itemId": item_id,
            "itemMainPic": item_main_pic,
            "lastMessageSummary": last_msg_summary,
            "lastMessageTime": last_msg_time,
            "unreadCount": unread_count,
        }
    except Exception as e:
        logger.warning(f"解析会话数据失败: {e}")
        return None


def _is_revoked_placeholder(message: dict, content: dict) -> bool:
    """识别闲鱼「撤回」在历史消息里留下的占位形态。

    实测（2026-10-20，账号 2217728125233，cid=68391672401）：对方撤回后，
    原消息位置（messageId / createAt 不变）内容被掏空：
    - ``content.custom`` 整个缺失（普通文本消息必有 custom.summary/data）；
    - ``content.text.content`` 为空串（正文清空）；
    - ``searchableContent.summary`` 为空串（摘要清空）；
    - ``extension.reminderContent`` 仍保留撤回前的旧摘要——**不展示**，避免泄露撤回内容。

    只有三者同时满足才判撤回；结构不全的怪消息保持原有兜底行为。
    """
    if not isinstance(content, dict) or "custom" in content:
        return False
    text_obj = content.get("text")
    if not isinstance(text_obj, dict):
        return False
    if str(text_obj.get("content", "") or "").strip():
        return False
    searchable = message.get("searchableContent")
    if isinstance(searchable, dict) and str(searchable.get("summary", "") or "").strip():
        return False
    return True


def _revoked_text(message: dict, myid: str) -> str:
    """按发送者返回撤回提示文案（自己撤的 vs 对方撤的）。

    发送者是任一托管账号即「自己撤的」——cid 跨账号共享，别的托管账号
    撤的消息本账号历史里也看得到（2026-10-20 isSelf 误判事故同源）。
    """
    sender = message.get("sender", {}) if isinstance(message, dict) else {}
    sender_uid = str(sender.get("uid", "") or "").split("@")[0]
    is_self = bool(myid and sender_uid == myid) or is_managed_account(sender_uid)
    return "你撤回了一条消息" if is_self else "对方撤回了一条消息"


def _parse_message(model: dict, myid: str, account_id: str = "", cid: str = "") -> dict | None:
    """
    解析单条消息

    参照 XianYuApis-master/goofish_live.py 的解析逻辑：
    - send_user_name = user_message["message"]["extension"]["reminderTitle"]
    - send_user_id   = user_message["message"]["extension"]["senderUserId"]
    - base64解码 user_message["message"]["content"]["custom"]["data"]
    - 解码后格式：
      文本: {"contentType": 1, "text": {"text": "实际消息"}}
      图片: {"contentType": 2, "image": {"pics": [{"url":"...", "width":0, "height":0}]}}

    Args:
        model: userMessageModels 中的单条数据
        myid: 当前账号的用户ID
        account_id: 账号标识（订单卡片采样用）
        cid: 会话 id（订单卡片采样用）

    Returns:
        格式化后的消息字典
    """
    try:
        message = model.get("message", {})
        extension = message.get("extension", {})
        if isinstance(extension, str):
            try:
                extension = json.loads(extension)
            except (json.JSONDecodeError, TypeError):
                extension = {}

        sender_id_raw = str(extension.get("senderUserId", "") or "") if isinstance(extension, dict) else ""
        # 去掉 @goofish 后缀，保证与 myid 比较和前端显示一致
        sender_id = sender_id_raw.split("@")[0] if "@" in sender_id_raw else sender_id_raw
        sender_name = str(extension.get("reminderTitle", "") or "") if isinstance(extension, dict) else ""
        # isSelf：本账号或任一托管账号（cid 跨账号共享，A 发的消息 B 的历史
        # 接口也拉得到，只比 myid 会把托管账号发送者误判成买家——
        # 2026-10-20 事故：误渲染成左侧对方消息+署名、误响铃、误弹 toast）
        is_self = bool(myid and sender_id == myid) or is_managed_account(sender_id)

        # 解析消息内容
        content = message.get("content", {})
        custom = content.get("custom", {})
        custom_data = custom.get("data", "")

        msg_type = "text"
        msg_text = ""
        msg_images = []
        decoded = None  # custom.data 解码结果（卡片采样用；无 custom_data 时为 None）
        revoked = False  # 是否为「撤回」占位消息（原消息位置内容被掏空）
        revoked_original = ""  # 撤回前原文线索：extension.reminderContent 旧摘要

        if custom_data:
            # 文本/图片/语音等通用内容解析统一走公共模块（HTTP 历史消息载荷为 base64）；
            # 卡片(card)与 summary 降级为本 HTTP 接口特有。这里用 load_content_json 只做解码
            # （不套 looks_like_content 内容白名单），保证纯 {title/template} 卡片（不带
            # contentType/内容键）仍能进入下方 elif 分支被识别为 card，而非被误降级成 summary。
            decoded = load_content_json(custom_data)
            if decoded is not None:
                msg_text, msg_images, msg_type = interpret_content(decoded)
                if msg_type == "image" and not msg_images:
                    # 图片但未取到 URL，占位提示
                    msg_text = "[图片]"
                elif not msg_type:
                    # 公共模块未识别：卡片/系统消息或未知类型，走本接口特有降级
                    content_type = decoded.get("contentType", 0)
                    if "title" in decoded or "template" in decoded:
                        msg_type = "card"
                        msg_text = str(
                            decoded.get("title", decoded.get("template", "[卡片消息]"))
                        )
                    else:
                        msg_type = "text"
                        summary = custom.get("summary", "")
                        msg_text = str(summary) if summary else f"[未知消息类型:{content_type}]"
            else:
                # base64解码或JSON解析失败，用 summary 降级
                summary = custom.get("summary", "")
                msg_text = str(summary) if summary else "[无法解析的消息]"
        else:
            # 没有 custom.data。先识别「撤回」占位（内容被掏空的原消息位置），
            # 命中则渲染为系统提示「对方撤回了一条消息」，而非 [系统消息] 兜底；
            # extension.reminderContent 仍保留撤回前的旧摘要，作为 revokedOriginal
            # 随消息下发，前端默认折叠、用户点「显示」才展开（文本消息即原文，
            # 图片/语音等为 [图片] 之类占位摘要）。
            summary = custom.get("summary", "")
            if _is_revoked_placeholder(message, content):
                msg_type = "system"
                msg_text = _revoked_text(message, myid)
                revoked = True
                revoked_original = (
                    str(extension.get("reminderContent", "") or "").strip()
                    if isinstance(extension, dict)
                    else ""
                )
            else:
                msg_text = str(summary) if summary else "[系统消息]"

        # 消息时间戳（goofish IM 中字段名为 createAt）
        msg_time = message.get("createAt", 0) or message.get("time", 0)

        # 语音：contentType==3 + audio，interpret_content 会降级成占位文本，
        # 这里把真实的音频地址与时长取出来，让前端可以播放 / 转文字。
        # 判据与推送侧共用 AUDIO_PLACEHOLDER_TEXT，避免两处硬编码不一致。
        audio_url = ""
        audio_duration = 0
        if msg_type == "text" and msg_text == AUDIO_PLACEHOLDER_TEXT and isinstance(decoded, dict):
            audio_url, audio_duration = extract_audio_info(decoded)
            if audio_url:
                msg_type = "audio"
                msg_text = ""

        # 系统 / 交易事件卡片识别（付款状态、演出信息、发货凭证等）：
        # 这类消息闲鱼以"方括号摘要"或卡片形式下发，前端据此渲染成居中事件卡，
        # 而非普通聊天气泡。纯文本规则，不依赖采样真实载荷。
        is_event, event_kind = classify_event(msg_text, msg_type)

        # 订单卡片原始载荷采样（只读落盘，零风控）：卡片消息或方括号摘要，
        # 把解密后的完整 decoded dict 原样写入采样文件，供后续分析卡片按钮 /
        # action / 订单号等未解析字段的真实 key 名。采集到数据前，卡片按钮
        # 映射（修改价格/去填写/提醒收货等）无从设计。
        rich_card = None
        if msg_type == "card" or str(msg_text or "").startswith("["):
            sample_card(
                "http_history", account_id, cid,
                str(message.get("messageId", "") or ""), msg_text, decoded,
            )
            # 富数据提取：历史载荷与推送一致（dxCard）时全量提取，
            # 简化 {title/template} 形式时合成仅标题记录，前端统一按富卡片渲染
            rich_card = parse_rich_card_decoded(decoded, msg_text)

        return {
            "messageId": str(message.get("messageId", "") or ""),
            "senderId": sender_id,
            "senderName": sender_name,
            "isSelf": is_self,
            "type": msg_type,
            "text": msg_text,
            "images": msg_images,
            "audioUrl": audio_url,
            "audioDuration": audio_duration,
            "time": msg_time,
            "isEvent": is_event,
            "eventKind": event_kind,
            # 订单卡片富数据（按钮/订单号/演出信息），非交易卡片为 None
            "card": rich_card,
            # 撤回占位元数据：revoked=True 时前端渲染「对方撤回了一条消息」并
            # 带头像；revokedOriginal 为撤回前原文线索（reminderContent 旧摘要，
            # 可能为空），用户点「显示」才展开，默认折叠不泄露
            "revoked": revoked,
            "revokedOriginal": revoked_original,
            # 读状态（IM 原始字段，实测 0=未读、2=已读）：
            # 自己发的消息 = 对方是否已读；对方发的消息 = 自己是否已读
            "readStatus": model.get("readStatus"),
        }
    except Exception as e:
        logger.warning(f"解析消息失败: {e}")
        return None


def _extract_message_summary(message: dict, myid: str = "") -> str:
    """
    从 lastMessage.message 提取摘要文本

    数据结构：message.content.custom.summary / message.content.custom.data (base64)
    """
    try:
        content = message.get("content", {})
        custom = content.get("custom", {})

        # 撤回占位（内容被掏空）：列表摘要显示「对方撤回了一条消息」
        if _is_revoked_placeholder(message, content):
            return _revoked_text(message, myid)

        # 优先使用 summary 字段
        summary = custom.get("summary", "")
        if summary:
            return summary[:50]

        # 尝试解码 data (base64)
        custom_data = custom.get("data", "")
        if custom_data:
            try:
                decoded = json.loads(
                    base64.b64decode(custom_data).decode("utf-8")
                )
                if "text" in decoded:
                    text_obj = decoded["text"]
                    # text 可能是 dict（如 {"text": "实际内容"}）或字符串
                    if isinstance(text_obj, dict):
                        text = text_obj.get("text", "")
                    else:
                        text = str(text_obj)
                    return text[:50] if text else ""
                if "picUrl" in decoded:
                    return "[图片]"
                if "title" in decoded:
                    return decoded["title"][:50]
            except Exception:
                pass

        # 降级使用 degrade 字段
        degrade = custom.get("degrade", "")
        if degrade:
            return degrade[:50]
    except Exception:
        pass
    return ""
