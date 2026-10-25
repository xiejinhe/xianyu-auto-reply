"""闲鱼游客（无登录态）mtop 会话。

背景（2026-10-26 实测）：
- mtop.idle.web.user.page.head（主页资料）、mtop.idle.web.xyh.item.list（在售商品）
  游客即可调通——这两步过去挂在账号登录态上，每次查询都记到账号风控头上，
  2026-10-25 晚就因此触发 FAIL_SYS_USER_VALIDATE 连带拖累会话列表限流。
- mtop.taobao.idle.pc.detail（商品详情）闲鱼官方网页版游客可调（同一个接口），
  但裸 HTTP 游客会被 Baxia 拒（RGV587）；仍按"游客优先"尝试，失败回退登录态。

设计：
- 模块级单例 aiohttp 会话，cookie jar 自动维护游客 _m_h5_tk 等票据；
- 首调令牌为空时自动重试一次（mtop 首次调用下发 token 的惯例）；
- 游客被风控拒绝（RGV587 / USER_VALIDATE / SESSION_EXPIRED）连续两次后熔断
  10 分钟，期间调用直接返回 circuit=True，调用方零成本走登录态，不白烧请求。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Optional

import aiohttp

APP_KEY = "34839810"
BASE_HEADERS = {
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
}

# 游客被拒几次进入熔断、熔断多久
GUEST_FAIL_THRESHOLD = 2
GUEST_CIRCUIT_SECONDS = 600

_state = {
    "session": None,  # aiohttp.ClientSession 单例
    "lock": None,  # asyncio.Lock，懒创建
}
# 按接口独立熔断：{api: {"fails": int, "circuit_until": float}}。
# 详情接口游客必拒会常态触发熔断，不能连带把游客可通的主页接口也熔了。
_circuits: dict = {}

_REJECT_MARKS = ("RGV587", "FAIL_SYS_USER_VALIDATE", "FAIL_SYS_SESSION_EXPIRED")


def _now() -> float:
    return time.time()


def _circuit_state(api: str) -> dict:
    return _circuits.setdefault(api, {"fails": 0, "circuit_until": 0.0})


def _circuit_active(api: str) -> bool:
    return _circuit_state(api)["circuit_until"] > _now()


def _note_result(api: str, ret_str: str) -> None:
    """按返回码更新该接口的游客熔断状态"""
    st = _circuit_state(api)
    if "SUCCESS" in ret_str:
        if st["fails"] or st["circuit_until"]:
            logger_debug(f"游客会话 {api} 已从风控中恢复")
        st["fails"] = 0
        st["circuit_until"] = 0.0
        return
    if any(mark in ret_str for mark in _REJECT_MARKS):
        st["fails"] += 1
        if st["fails"] >= GUEST_FAIL_THRESHOLD:
            st["circuit_until"] = _now() + GUEST_CIRCUIT_SECONDS
            st["fails"] = 0
            logger_debug(f"游客会话 {api} 被风控拒绝，熔断 {GUEST_CIRCUIT_SECONDS} 秒")


def logger_debug(msg: str) -> None:
    # 单独包一层避免模块级循环导入 logging 配置
    import logging
    logging.getLogger(__name__).debug(msg)


async def _get_session() -> aiohttp.ClientSession:
    """懒创建游客会话；进程内复用，cookie jar 跨请求保留游客票据"""
    if _state["session"] is None or _state["session"].closed:
        if _state["lock"] is None:
            _state["lock"] = asyncio.Lock()
        async with _state["lock"]:
            if _state["session"] is None or _state["session"].closed:
                session = aiohttp.ClientSession(headers=BASE_HEADERS)
                _state["session"] = session
                # 暖场：种下游客基础 cookie（cna/isg 等）。失败不阻断，
                # 首次 mtop 调用仍会按需下发 _m_h5_tk。
                try:
                    async with session.get(
                        "https://www.goofish.com/",
                        timeout=aiohttp.ClientTimeout(total=10),
                    ):
                        pass
                except Exception:
                    pass
    return _state["session"]


def _guest_token(session: aiohttp.ClientSession) -> str:
    for cookie in session.cookie_jar:
        if cookie.key == "_m_h5_tk" and cookie.value:
            return cookie.value.split("_")[0]
    return ""


async def _post_mtop(
    session: aiohttp.ClientSession, api: str, data_val: str, spm: str
) -> dict:
    timestamp = str(int(time.time() * 1000))
    token = _guest_token(session)
    sign = hashlib.md5(
        f"{token}&{timestamp}&{APP_KEY}&{data_val}".encode()
    ).hexdigest()
    params = {
        "jsv": "2.7.2",
        "appKey": APP_KEY,
        "t": timestamp,
        "sign": sign,
        "v": "1.0",
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": api,
        "spm_cnt": spm,
    }
    async with session.post(
        f"https://h5api.m.goofish.com/h5/{api}/1.0/",
        params=params,
        data={"data": data_val},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as response:
        return await response.json()


async def guest_mtop_call(
    api: str, data_obj: dict, spm: str = "a21ybx.im.0.0"
) -> dict:
    """游客身份调 mtop 接口。

    Returns:
        {
          "ok": bool,        # 业务成功（ret 含 SUCCESS）
          "res": dict|None,  # 成功时的完整响应
          "ret": str,        # 首个返回码（诊断用）
          "circuit": bool,   # True=游客熔断中，调用方应直接走登录态
        }
    """
    if _circuit_active(api):
        return {"ok": False, "res": None, "ret": "guest_circuit", "circuit": True}
    try:
        session = await _get_session()
        data_val = json.dumps(data_obj, separators=(",", ":"))
        res = await _post_mtop(session, api, data_val, spm)
        ret_str = (res.get("ret") or [""])[0]
        # 令牌为空/过期：服务端已在响应头下发新 token（jar 已收），原样重试一次
        if "FAIL_SYS_TOKEN" in ret_str:
            await asyncio.sleep(0.3)
            res = await _post_mtop(session, api, data_val, spm)
            ret_str = (res.get("ret") or [""])[0]
        _note_result(api, ret_str)
        return {"ok": "SUCCESS" in ret_str, "res": res, "ret": ret_str, "circuit": False}
    except Exception as e:
        logger_debug(f"游客调用 {api} 异常: {e}")
        return {"ok": False, "res": None, "ret": f"exception:{e}", "circuit": False}
