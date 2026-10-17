"""
对方闲鱼主页信息查询服务（在线聊天）

数据来源：mtop.idle.web.user.page.head（闲鱼网页版个人主页头部接口）
返回昵称/头像/属地/简介/店铺等级/好评率/粉丝数/认证标签等，
用于在会话窗口直接展示"买家名片"，省掉打开主页逐个看的步骤。

调用方式与头像服务（avatar_service）一致：账号 Cookie + mtop 签名，
结果按用户ID缓存到 Redis。
"""
from __future__ import annotations

import json
import time
from typing import Optional

import aiohttp
from loguru import logger

from common.db.redis_client import get_redis_client
from common.utils.xianyu_utils import generate_sign, trans_cookies

from .avatar_service import _handle_response_cookies, _update_account_cookies

# Redis缓存键前缀（按被查询的用户ID，与账号无关）
PROFILE_CACHE_PREFIX = "chat:user_profile:"
# 主页信息变化慢，缓存 30 分钟
PROFILE_CACHE_TTL = 1800

# mtop API 配置
USER_HEAD_API = "mtop.idle.web.user.page.head"
USER_HEAD_URL = f"https://h5api.m.goofish.com/h5/{USER_HEAD_API}/1.0/"

# 最大令牌过期重试次数
MAX_TOKEN_RETRY = 1

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
) -> Optional[dict]:
    """
    查询对方的闲鱼主页信息

    Args:
        account_id: 发起查询的账号ID（用于日志和Cookie更新）
        user_id: 对方的闲鱼用户ID
        cookies_str: 账号Cookie字符串
        db: 数据库会话（用于更新Cookie）

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
                    return data
            except (json.JSONDecodeError, TypeError):
                pass
    except Exception as e:
        logger.warning(f"【{account_id}】Redis读取主页信息缓存失败: {e}")

    # 2. 调用 mtop API
    profile = await _fetch_profile_from_api(
        account_id=account_id,
        user_id=user_id,
        cookies_str=cookies_str,
        db=db,
        retry_count=0,
    )

    # 3. 写入缓存
    if profile:
        try:
            redis = await get_redis_client()
            await redis.set(
                cache_key, json.dumps(profile, ensure_ascii=False), ex=PROFILE_CACHE_TTL
            )
        except Exception as e:
            logger.warning(f"【{account_id}】Redis写入主页信息缓存失败: {e}")

    return profile


async def _fetch_profile_from_api(
    account_id: str,
    user_id: str,
    cookies_str: str,
    db,
    retry_count: int = 0,
) -> Optional[dict]:
    """调用 mtop.idle.web.user.page.head 查询对方主页信息"""
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
