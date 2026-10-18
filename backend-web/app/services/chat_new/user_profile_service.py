"""
对方闲鱼主页信息查询服务（在线聊天）

数据来源：
1. mtop.idle.web.user.page.head（闲鱼网页版个人主页头部接口）
   返回昵称/头像/属地/简介/店铺等级/好评率/粉丝数/认证标签等，
   用于在会话窗口直接展示"买家名片"，省掉打开主页逐个看的步骤。
2. mtop.taobao.idle.pc.detail（商品详情，需先用 xyh.item.list 取商品ID）
   sellerDO 里带"最近来过"、24h 回复率、平均回复间隔——这三个字段
   网页版主页接口不返回，只有商品详情挂了，所以要额外绕一跳。

调用方式与头像服务（avatar_service）一致：账号 Cookie + mtop 签名，
结果按用户ID缓存到 Redis。
"""
from __future__ import annotations

import json
import time
from typing import List, Optional, Tuple

import aiohttp
from loguru import logger
from sqlalchemy import select

from common.db.redis_client import get_redis_client
from common.models.xy_account import XYAccount
from common.services.xianyu_detail_client import XianyuItemDetailClient
from common.utils.xianyu_utils import generate_sign, trans_cookies

from .avatar_service import _handle_response_cookies, _update_account_cookies

# Redis缓存键前缀（按被查询的用户ID，与账号无关）
PROFILE_CACHE_PREFIX = "chat:user_profile:"
# 主页信息（昵称/头像/属地等）变化很慢，缓存 24 小时。
# 曾用 30 分钟：缓存一过期，第一波列表加载就要全量重查主页接口，
# 一口气十几个请求直接把接口打进风控（FAIL_SYS_USER_VALIDATE）。
PROFILE_CACHE_TTL = 86400

# 「最近来过」是相对时间文案（"12分钟前来过"），缓存久了会越显示越失真，
# 而且它要额外绕一跳商品详情，所以单独存一份短缓存，不跟主页信息混在一起。
ACTIVITY_CACHE_PREFIX = "chat:user_activity:"
ACTIVITY_CACHE_TTL = 300
# 查不到时的空占位缓存：避免每次进会话都重试一遍风控中的接口
ACTIVITY_MISS_TTL = 120

# mtop API 配置
USER_HEAD_API = "mtop.idle.web.user.page.head"
USER_HEAD_URL = f"https://h5api.m.goofish.com/h5/{USER_HEAD_API}/1.0/"
# 对方在售商品列表接口（拿一个商品ID，再借商品详情查卖家的"最近来过"）
USER_ITEM_LIST_API = "mtop.idle.web.xyh.item.list"
USER_ITEM_LIST_URL = f"https://h5api.m.goofish.com/h5/{USER_ITEM_LIST_API}/1.0/"

# 查「最近来过」时最多尝试的商品数与备用账号数
# 商品详情是闲鱼风控较严的接口，单个账号可能整体被拦（FAIL_SYS_USER_VALIDATE），
# 所以多备几个商品、并在必要时借用同一用户名下的其它账号兜底。
ACTIVITY_ITEM_LIMIT = 3
ACTIVITY_FALLBACK_ACCOUNTS = 3

# 最大令牌过期重试次数
MAX_TOKEN_RETRY = 1

# ==================== 主页接口账号级熔断 ====================
# 主页接口被闲鱼风控后，会对整个账号持续返回 FAIL_SYS_USER_VALIDATE
# （响应 set-cookie 里带 x5secdata/x5sectag 验证挑战标记）。此时继续重试
# 毫无意义且会把风控越养越死（实测 2 分钟内连打 382 次全部失败）。
# 因此：连续 VALIDATE 失败达阈值 → 该账号熔断一段时间，期间所有主页查询
# 直接返回 None（前端走缓存或留灰占位）；任何一次成功即解除熔断并清零。
VALIDATE_FAIL_TRIP = 3
CIRCUIT_OPEN_SECONDS = 600
_PROFILE_CIRCUIT: dict = {}        # account_id -> 熔断截止时间戳（time.time()）
_PROFILE_VALIDATE_FAILS: dict = {} # account_id -> 连续 VALIDATE 失败次数

# 单个用户查询失败后的负缓存（Redis）：短时间内同一用户不再出网重试，
# 挡住前端列表轮询/页面刷新造成的反复重查。
PROFILE_FAIL_CACHE_PREFIX = "chat:user_profile_fail:"
PROFILE_FAIL_CACHE_TTL = 600

# 认证标签：tag 字段 -> 展示文案
_CERT_TAG_LABELS = (
    ("real_name_certification_77", "实名认证"),
    ("real_person_certification_77", "真人认证"),
    ("idle_zhima_zheng", "芝麻信用"),
    ("xianyu_user_upgrade", "闲鱼老用户"),
)


async def get_user_profile(
    account_id: str,
    user_id: str,
    cookies_str: str,
    db,
    with_activity: bool = True,
) -> Optional[dict]:
    """
    查询对方的闲鱼主页信息

    Args:
        account_id: 发起查询的账号ID（用于日志和Cookie更新）
        user_id: 对方的闲鱼用户ID
        cookies_str: 账号Cookie字符串
        db: 数据库会话（用于更新Cookie）
        with_activity: 是否顺带查「最近来过/回复速度」。这两个字段要额外
            绕商品详情接口，只要头像/昵称的场景（如批量查头像）应传 False，
            避免一次批量查询触发多次风控较严的商品详情请求。

    Returns:
        结构化主页信息字典，查询失败返回 None
    """
    if not user_id:
        return None

    # 1. 检查 Redis 缓存
    cache_key = f"{PROFILE_CACHE_PREFIX}{user_id}"
    try:
        redis = await get_redis_client()
        cached = await redis.get(cache_key)
        if cached:
            try:
                data = json.loads(cached)
                if isinstance(data, dict) and data.get("nick"):
                    # 主页信息缓存 30 分钟，但「最近来过」是 5 分钟短缓存：
                    # 命中主页缓存也必须补活跃信息，否则名片上的绿色
                    # 「X前来过」标签只在主页缓存恰好过期的瞬间才出现，
                    # 其余 25 分钟打开会话都会被当成"功能没加"。
                    # 真正的出网频率由活跃信息自己的缓存兜底
                    # （命中 5 分钟 / 未命中 2 分钟内不重复出网）。
                    if with_activity:
                        data = await _merge_activity(
                            account_id=account_id,
                            user_id=user_id,
                            cookies_str=cookies_str,
                            db=db,
                            profile=data,
                        )
                    return data
            except (json.JSONDecodeError, TypeError):
                pass
    except Exception as e:
        logger.warning(f"【{account_id}】Redis读取主页信息缓存失败: {e}")

    # 1.5 失败负缓存：该用户刚查失败（多为主页接口被风控），短时间内
    # 不再为它出网，避免列表轮询/页面刷新反复重查
    try:
        redis = await get_redis_client()
        if await redis.get(f"{PROFILE_FAIL_CACHE_PREFIX}{user_id}"):
            return None
    except Exception:
        pass

    # 2. 调用 mtop API
    profile = await _fetch_profile_from_api(
        account_id=account_id,
        user_id=user_id,
        cookies_str=cookies_str,
        db=db,
        retry_count=0,
    )

    # 3. 写入缓存（只存主页信息本身，不含下面的活跃信息）
    if profile:
        try:
            redis = await get_redis_client()
            await redis.set(
                cache_key, json.dumps(profile, ensure_ascii=False), ex=PROFILE_CACHE_TTL
            )
        except Exception as e:
            logger.warning(f"【{account_id}】Redis写入主页信息缓存失败: {e}")

    # 4. 对方有在售商品时，借商品详情补「最近来过 / 回复速度」。
    #    闲鱼把活跃状态挂在商品详情的 sellerDO 上（APP 主页的"X分钟前来过"
    #    就是它），主页接口本身没有这个字段。单独短缓存，失败静默跳过，
    #    不影响名片主体。
    if with_activity and profile:
        profile = await _merge_activity(
            account_id=account_id,
            user_id=user_id,
            cookies_str=cookies_str,
            db=db,
            profile=profile,
        )

    return profile


async def _merge_activity(
    account_id: str,
    user_id: str,
    cookies_str: str,
    db,
    profile: dict,
) -> dict:
    """把「最近来过 / 回复速度」合并进名片数据（没有在售商品时原样返回）

    出网与否由 _get_activity_cached 的短缓存兜底：命中 5 分钟内不重复查，
    查不到 2 分钟内不重试，所以每次打开会话调用一次也是安全的。
    """
    try:
        if not profile.get("itemCount"):
            return profile
        activity = await _get_activity_cached(
            account_id=account_id,
            user_id=user_id,
            cookies_str=cookies_str,
            db=db,
            expected_nick=profile.get("nick", ""),
        )
    except Exception as e:
        logger.warning(f"【{account_id}】合并对方活跃信息失败: {e}")
        return profile
    if activity:
        profile.update(activity)
    return profile


async def _fetch_profile_from_api(
    account_id: str,
    user_id: str,
    cookies_str: str,
    db,
    retry_count: int = 0,
) -> Optional[dict]:
    """调用 mtop.idle.web.user.page.head 查询对方主页信息"""
    # 账号熔断中：直接放弃本次出网（上层自然落到缓存或留灰占位）
    circuit_until = _PROFILE_CIRCUIT.get(account_id, 0)
    if circuit_until > time.time():
        logger.debug(
            f"【{account_id}】主页接口熔断中（剩 {int(circuit_until - time.time())} 秒），"
            f"跳过 {user_id} 的查询"
        )
        return None

    try:
        cookies = trans_cookies(cookies_str)
        timestamp = str(int(time.time() * 1000))
        data_val = json.dumps({"userId": str(user_id)}, separators=(",", ":"))

        token = cookies.get("_m_h5_tk", "").split("_")[0] if cookies.get("_m_h5_tk") else ""
        sign = generate_sign(timestamp, token, data_val)

        params = {
            "jsv": "2.7.2",
            "appKey": "34839810",
            "t": timestamp,
            "sign": sign,
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": USER_HEAD_API,
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.im.0.0",
        }

        headers = {
            "accept": "application/json",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/146.0.0.0 Safari/537.36"
            ),
            "referer": "https://www.goofish.com/",
            "origin": "https://www.goofish.com",
            "cookie": cookies_str.replace("\n", "").replace("\r", ""),
        }

        async with aiohttp.ClientSession() as http_session:
            async with http_session.post(
                USER_HEAD_URL,
                params=params,
                data={"data": data_val},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                res_json = await response.json()

                # 处理响应中的 set-cookie，合并更新到数据库
                new_cookies_str = _handle_response_cookies(response, cookies_str)
                if new_cookies_str and new_cookies_str != cookies_str:
                    await _update_account_cookies(db, account_id, new_cookies_str)
                    cookies_str = new_cookies_str

                ret = res_json.get("ret", [])
                ret_str = ret[0] if ret else ""

                if "SUCCESS" in ret_str:
                    profile = _parse_profile(user_id, res_json.get("data", {}))
                    if _PROFILE_VALIDATE_FAILS.get(account_id):
                        logger.info(f"【{account_id}】主页接口已从风控中恢复")
                    _PROFILE_VALIDATE_FAILS[account_id] = 0
                    _PROFILE_CIRCUIT.pop(account_id, None)
                    # 该用户若在负缓存中，成功后移除，允许后续刷新
                    try:
                        redis = await get_redis_client()
                        await redis.delete(f"{PROFILE_FAIL_CACHE_PREFIX}{user_id}")
                    except Exception:
                        pass
                    if profile:
                        logger.info(
                            f"【{account_id}】获取对方主页信息成功: "
                            f"nick={profile.get('nick', '')}, 属地={profile.get('ipLocation', '')}"
                        )
                    return profile

                # 令牌过期：用更新后的cookie重试
                if (
                    "TOKEN_EXOIRED" in ret_str or "TOKEN_EXPIRED" in ret_str
                ) and retry_count < MAX_TOKEN_RETRY:
                    logger.info(
                        f"【{account_id}】查询主页信息令牌过期，准备重试({retry_count + 1})"
                    )
                    return await _fetch_profile_from_api(
                        account_id=account_id,
                        user_id=user_id,
                        cookies_str=cookies_str,
                        db=db,
                        retry_count=retry_count + 1,
                    )

                # 风控挑战：账号级封锁该接口，重试无效。记负缓存 + 连续失败
                # 计数，达到阈值即熔断，避免 382 连击式地把风控养死
                if "FAIL_SYS_USER_VALIDATE" in ret_str:
                    fails = _PROFILE_VALIDATE_FAILS.get(account_id, 0) + 1
                    _PROFILE_VALIDATE_FAILS[account_id] = fails
                    try:
                        redis = await get_redis_client()
                        await redis.set(
                            f"{PROFILE_FAIL_CACHE_PREFIX}{user_id}",
                            "1",
                            ex=PROFILE_FAIL_CACHE_TTL,
                        )
                    except Exception:
                        pass
                    if fails >= VALIDATE_FAIL_TRIP:
                        _PROFILE_CIRCUIT[account_id] = time.time() + CIRCUIT_OPEN_SECONDS
                        _PROFILE_VALIDATE_FAILS[account_id] = 0
                        logger.warning(
                            f"【{account_id}】主页接口连续 {fails} 次被风控"
                            f"(FAIL_SYS_USER_VALIDATE)，熔断 {CIRCUIT_OPEN_SECONDS // 60} 分钟；"
                            f"期间头像/名片走缓存或留空，等风控自行放行"
                        )
                    return None

                logger.warning(f"【{account_id}】查询主页信息失败: {ret_str}")
                return None

    except aiohttp.ClientError as e:
        logger.warning(f"【{account_id}】查询主页信息网络失败: {e}")
        return None
    except Exception as e:
        logger.warning(f"【{account_id}】查询主页信息异常: {e}")
        return None


def _to_int(value) -> Optional[int]:
    """闲鱼的计数字段有时是字符串，统一成 int；非法值返回 None"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _nick_mismatch(expected: str, actual: str) -> bool:
    """判断商品详情的卖家昵称与主页昵称是否矛盾（True=矛盾，应丢弃）

    两边都完整时要求完全一致；任一侧带 * 掩码（如主页接口返回"樱***1"，
    商品详情返回"樱花飞舞4321"）时无法精确比对，退化为首尾字符比对。
    任一侧为空时视为无法判断，不拦截。
    """
    if not expected or not actual:
        return False
    if "*" in expected or "*" in actual:
        return not (expected[0] == actual[0] and expected[-1] == actual[-1])
    return expected != actual


async def _get_activity_cached(
    account_id: str,
    user_id: str,
    cookies_str: str,
    db,
    expected_nick: str = "",
) -> Optional[dict]:
    """
    带短缓存的活跃信息查询

    「最近来过」是相对时间文案，必须比主页信息更快过期；同时这一项要
    多花一跳商品详情，不能每次进会话都真查。查不到时缓存一个空标记，
    免得对正在风控的账号反复无效重试。
    """
    cache_key = f"{ACTIVITY_CACHE_PREFIX}{user_id}"
    try:
        redis = await get_redis_client()
        cached = await redis.get(cache_key)
        if cached is not None:
            return json.loads(cached) or None
    except Exception:
        pass

    activity = await _fetch_activity(
        account_id=account_id,
        user_id=user_id,
        cookies_str=cookies_str,
        db=db,
        expected_nick=expected_nick,
    )
    try:
        redis = await get_redis_client()
        await redis.set(
            cache_key,
            json.dumps(activity, ensure_ascii=False),
            ex=ACTIVITY_CACHE_TTL if activity else ACTIVITY_MISS_TTL,
        )
    except Exception as e:
        logger.warning(f"【{account_id}】Redis写入活跃信息缓存失败: {e}")
    return activity


async def _fetch_activity(
    account_id: str,
    user_id: str,
    cookies_str: str,
    db,
    expected_nick: str = "",
) -> Optional[dict]:
    """
    查对方的「最近来过 / 回复速度」

    链路：主页商品列表接口拿对方的在售商品 → 商品详情接口的 sellerDO
    里带 lastVisitTime（如"12分钟前来过"）、24h 回复率、平均回复间隔。
    闲鱼网页版主页本身不返回这两个字段，只有商品详情有，所以必须绕这一跳。

    商品详情是风控较严的接口，实测会出现整个账号被拦的情况
    （ret=FAIL_SYS_USER_VALIDATE，换商品也一样），因此这里做两级兜底：
    多试几个商品；账号被拦时借用同一用户名下的其它账号再查。
    查不到就返回 None，名片主体照常展示。
    """
    try:
        item_ids = await _fetch_item_ids(
            account_id=account_id,
            user_id=user_id,
            cookies_str=cookies_str,
            db=db,
            size=ACTIVITY_ITEM_LIMIT,
        )
        if not item_ids:
            return None

        candidates: List[Tuple[str, str]] = [(account_id, cookies_str)]
        candidates.extend(
            await _load_sibling_accounts(account_id, db, limit=ACTIVITY_FALLBACK_ACCOUNTS)
        )

        for idx, (query_account, query_cookies) in enumerate(candidates):
            activity = await _try_activity_on_items(
                account_id=query_account,
                cookies_str=query_cookies,
                item_ids=item_ids,
                user_id=user_id,
                expected_nick=expected_nick,
            )
            if activity:
                if idx:
                    logger.info(
                        f"【{account_id}】对方活跃信息由备用账号 {query_account} 查到: "
                        f"{activity.get('lastVisitTime', '')}"
                    )
                return activity
        return None
    except Exception as e:
        logger.warning(f"【{account_id}】查询对方活跃信息失败: {e}")
        return None


async def _try_activity_on_items(
    account_id: str,
    cookies_str: str,
    item_ids: List[str],
    user_id: str,
    expected_nick: str = "",
) -> Optional[dict]:
    """用一个账号依次查几个商品的详情，取第一个能用的卖家活跃信息"""
    for item_id in item_ids:
        try:
            client = XianyuItemDetailClient(account_id, cookies_str)
            result = await client.get_detail(item_id)
        except Exception as e:
            logger.warning(f"【{account_id}】商品详情请求异常({item_id}): {e}")
            return None

        if not result.get("success"):
            # 账号在本接口上不可用（风控/登录态失效），换商品没意义，交给上层换账号
            if result.get("account_invalid"):
                logger.info(
                    f"【{account_id}】商品详情接口不可用({result.get('error', '')})，换账号重试"
                )
                return None
            continue

        seller = (result.get("detail") or {}).get("sellerDO") or {}
        # 商品列表理论上只返回对方的在售商品，但保险起见校验一下卖家昵称，
        # 避免拿错人的活跃信息显示在会话里。注意主页接口返回的可能是打码
        # 昵称（如"樱***1"），商品详情返回的是完整昵称（"樱花飞舞4321"），
        # 两侧永远不相等——任一侧带掩码时退化为首尾字符比对，
        # 否则备用账号查到的结果会被这道校验整单丢弃（实测踩坑）。
        nick = str(seller.get("nick") or "")
        if _nick_mismatch(expected_nick, nick):
            continue

        activity: dict = {}
        if seller.get("lastVisitTime"):
            activity["lastVisitTime"] = str(seller["lastVisitTime"])
        if seller.get("replyRatio24h"):
            activity["replyRatio24h"] = str(seller["replyRatio24h"])
        if seller.get("replyInterval"):
            activity["replyInterval"] = str(seller["replyInterval"])
        if activity:
            logger.info(
                f"【{account_id}】对方活跃信息: user={user_id}, "
                f"{activity.get('lastVisitTime', '')}"
            )
            return activity
    return None


async def _load_sibling_accounts(
    account_id: str, db, limit: int = ACTIVITY_FALLBACK_ACCOUNTS
) -> List[Tuple[str, str]]:
    """取同一用户名下其它活跃账号，作为商品详情风控时的备用查询账号"""
    try:
        owner_id = await db.scalar(
            select(XYAccount.owner_id).where(XYAccount.account_id == account_id)
        )
        if owner_id is None:
            return []
        rows = (
            await db.execute(
                select(XYAccount.account_id, XYAccount.cookie).where(
                    XYAccount.owner_id == owner_id,
                    XYAccount.account_id != account_id,
                    XYAccount.status == "active",
                    XYAccount.cookie.isnot(None),
                    XYAccount.cookie != "",
                ).limit(limit)
            )
        ).all()
        return [(row[0], row[1]) for row in rows if row[0] and row[1]]
    except Exception as e:
        logger.warning(f"【{account_id}】读取备用账号失败: {e}")
        return []


async def _fetch_item_ids(
    account_id: str,
    user_id: str,
    cookies_str: str,
    db,
    size: int = 1,
) -> List[str]:
    """查对方在售商品列表，返回前 size 个商品ID；没有在售商品返回空列表"""
    try:
        cookies = trans_cookies(cookies_str)
        timestamp = str(int(time.time() * 1000))
        data_val = json.dumps(
            {
                "userId": str(user_id),
                "pageNumber": 1,
                "pageSize": max(1, size),
            },
            separators=(",", ":"),
        )

        token = cookies.get("_m_h5_tk", "").split("_")[0] if cookies.get("_m_h5_tk") else ""
        sign = generate_sign(timestamp, token, data_val)

        params = {
            "jsv": "2.7.2",
            "appKey": "34839810",
            "t": timestamp,
            "sign": sign,
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": USER_ITEM_LIST_API,
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.im.0.0",
        }

        headers = {
            "accept": "application/json",
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/146.0.0.0 Safari/537.36"
            ),
            "referer": "https://www.goofish.com/",
            "origin": "https://www.goofish.com",
            "cookie": cookies_str.replace("\n", "").replace("\r", ""),
        }

        async with aiohttp.ClientSession() as http_session:
            async with http_session.post(
                USER_ITEM_LIST_URL,
                params=params,
                data={"data": data_val},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                res_json = await response.json()

                new_cookies_str = _handle_response_cookies(response, cookies_str)
                if new_cookies_str and new_cookies_str != cookies_str:
                    await _update_account_cookies(db, account_id, new_cookies_str)

                ret = res_json.get("ret", [])
                if "SUCCESS" not in (ret[0] if ret else ""):
                    logger.warning(
                        f"【{account_id}】查询对方在售商品失败: {(ret[0] if ret else '')[:80]}"
                    )
                    return []

                item_ids: List[str] = []
                for card in (res_json.get("data") or {}).get("cardList") or []:
                    detail_params = (card.get("cardData") or {}).get("detailParams") or {}
                    if detail_params.get("itemId"):
                        item_ids.append(str(detail_params["itemId"]))
                    if len(item_ids) >= size:
                        break
                return item_ids

    except Exception as e:
        logger.warning(f"【{account_id}】查询对方在售商品异常: {e}")
        return []


def _parse_profile(user_id: str, data: dict) -> Optional[dict]:
    """把接口返回裁剪成前端要的精简结构"""
    if not isinstance(data, dict):
        return None

    module = data.get("module") or {}
    base = module.get("base") or {}
    shop = module.get("shop") or {}
    social = module.get("social") or {}
    tabs = module.get("tabs") or {}
    base_info = data.get("baseInfo") or {}
    tags = base_info.get("tags") or {}

    avatar_url = ""
    avatar_obj = base.get("avatar")
    if isinstance(avatar_obj, dict):
        avatar_url = avatar_obj.get("avatar", "") or ""
    elif isinstance(avatar_obj, str):
        avatar_url = avatar_obj

    certifications = [
        label for key, label in _CERT_TAG_LABELS if tags.get(key) is True
    ]

    # 店铺等级/好评率等只对卖家有效，买家没有 shop 模块；
    # tabs 里的数量字段闲鱼有时给字符串有时给数字，统一成 int 便于前端直接比较
    return {
        "userId": str(user_id),
        "nick": base.get("displayName", "") or "",
        "avatar": avatar_url,
        "ipLocation": base.get("ipLocation", "") or "",
        "introduction": (base.get("introduction", "") or "").replace("\n", " ").strip(),
        "shopLevel": shop.get("level", "") or "",
        "praiseRatio": _to_int(shop.get("praiseRatio")),
        "reviewNum": _to_int(shop.get("reviewNum")),
        "creditScore": _to_int(shop.get("score")),
        "licensed": (shop.get("businessQuality") or {}).get("type") == "licensedOperation",
        "followers": str(social.get("followers", "") or ""),
        "following": str(social.get("following", "") or ""),
        "itemCount": _to_int((tabs.get("item") or {}).get("number")),
        "rateCount": _to_int((tabs.get("rate") or {}).get("number")),
        "certifications": certifications,
    }
