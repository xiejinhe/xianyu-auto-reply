#!/usr/bin/env python3
"""闲鱼订单「按钮契约」读取工具（只读，零风控）

用途
----
想知道闲鱼订单详情页某个按钮（发货 / 取消订单 / 提醒收货 / 填写演出信息等）
点下去到底调用了哪个接口、传了什么参数？**不用抓包**，直接问服务端：

Web 端订单详情接口会把按钮的动作契约原封不动下发给前端：

    data.bottomBarVO.buttonList[] = [
      {
        "name": "直接免拼",
        "tradeAction": "SKIP_PIN",
        "buttonDisable": false,
        "unlockTime": "1天1时后解锁",        # 有值时表示当前不可操作
        "clickEvent": {
          "type": "mtop",                  # mtop / actionCode / toast
          "data": {
            "apiName": "mtop.idle.groupon.activity.seller.freeshipping",
            "apiVersion": "1.0",
            "params": {"itemId": "...", "bizOrderId": "...", "buyerId": "..."},
            "refresh": "true",
            "successDialog": {...}
          },
          "doubleCheck": {...}
        }
      }
    ]

clickEvent.type 的含义：
  - mtop        → data 里就是完整接口名 + 业务参数，拿到即可照着调（本工具的产出）
  - actionCode  → 只有 data.code（如 CLOSE_ORDER），需要自建 code → 接口映射
  - toast       → 该状态下不可操作，只有提示文案（例如「演出结束后才可提醒收货~」）

为什么重要
----------
手机端根本不需要安装 CA 证书做 HTTPS 抓包。只要让订单进入目标状态，
跑一次本脚本就能拿到写侧接口的全部信息，风险为零（就是系统本来就在调的
只读详情接口 + 一次请求）。

用法
----
    # 用账号 2217728125233 的登录态查订单 3317145996026060554
    backend-web/.venv/bin/python scripts/dump_order_actions.py <订单号> [账号ID]

    # 账号ID 省略时自动按订单归属账号推断（从 xy_orders 查）

输出：每个按钮的 tradeAction / 是否可点 / 接口名与参数。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)
# pydantic-settings 按 CWD 加载 .env，必须切到 backend-web 才能拿到正确的数据库配置
os.chdir(os.path.join(_REPO_ROOT, "backend-web"))

import aiohttp  # noqa: E402
from sqlalchemy import text  # noqa: E402

from common.db.session import async_session_maker  # noqa: E402
from common.utils.xianyu_utils import trans_cookies, generate_sign  # noqa: E402

DETAIL_API = "mtop.idle.web.trade.order.detail"


async def _resolve_account(order_no: str) -> str:
    """订单未指定账号时，从本地订单表反查归属卖家账号。"""
    async with async_session_maker() as s:
        r = await s.execute(
            text("SELECT account_id FROM xy_orders WHERE order_no=:o LIMIT 1"),
            {"o": order_no},
        )
        return r.scalar() or ""


async def _load_cookie(account_id: str) -> str:
    async with async_session_maker() as s:
        r = await s.execute(
            text("SELECT cookie FROM xy_accounts WHERE account_id=:a"),
            {"a": account_id},
        )
        return r.scalar() or ""


async def fetch_order_detail(order_no: str, account_id: str) -> dict:
    """拉取订单详情原始响应（与 OrderService._fetch_raw_order_detail 同口径）。"""
    cookies_str = await _load_cookie(account_id)
    if not cookies_str:
        raise RuntimeError(f"账号 {account_id} 没有可用 cookie")

    cookies = trans_cookies(cookies_str)
    data_val = json.dumps({"tid": order_no}, separators=(",", ":"))
    token = cookies.get("_m_h5_tk", "").split("_")[0] if cookies.get("_m_h5_tk") else ""
    ts = str(int(time.time() * 1000))
    params = {
        "jsv": "2.7.2",
        "appKey": "34839810",
        "t": ts,
        "sign": generate_sign(ts, token, data_val),
        "v": "1.0",
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": DETAIL_API,
        "sessionOption": "AutoLoginOnly",
        "spm_cnt": "a21ybx.order-detail.0.0",
    }
    headers = {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://www.goofish.com",
        "referer": "https://www.goofish.com/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
        ),
        "cookie": cookies_str,
    }
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as sess:
        async with sess.post(
            f"https://h5api.m.goofish.com/h5/{DETAIL_API}/1.0/",
            params=params,
            data={"data": data_val},
            headers=headers,
            timeout=30,
        ) as resp:
            return json.loads(await resp.text())


def _describe_button(btn: dict) -> str:
    name = btn.get("name", "")
    action = btn.get("tradeAction", "")
    ev = btn.get("clickEvent") or {}
    ev_type = ev.get("type", "")
    data = ev.get("data") or {}

    lines = [f"  [{name}] tradeAction={action}  可点={'是' if not btn.get('buttonDisable') else '否'}"]
    if btn.get("unlockTime"):
        lines.append(f"      解锁条件: {btn['unlockTime']}")
    if ev_type == "mtop":
        lines.append(f"      接口: {data.get('apiName')}  v{data.get('apiVersion', '')}")
        lines.append(f"      参数: {json.dumps(data.get('params', {}), ensure_ascii=False)}")
        if data.get("refresh"):
            lines.append(f"      refresh={data['refresh']}")
    elif ev_type == "actionCode":
        lines.append(f"      actionCode: {data.get('code')}  参数: {json.dumps(data.get('params', {}), ensure_ascii=False)}")
    elif ev_type == "toast":
        lines.append(f"      toast: {data.get('text', '')}")
    else:
        lines.append(f"      未识别 clickEvent: {json.dumps(ev, ensure_ascii=False)[:200]}")
    return "\n".join(lines)


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        print("用法: python scripts/dump_order_actions.py <订单号> [账号ID]")
        return
    order_no = sys.argv[1]
    account_id = sys.argv[2] if len(sys.argv) > 2 else await _resolve_account(order_no)
    if not account_id:
        print(f"本地查不到订单 {order_no} 的归属账号，请手动指定卖家账号ID作为第二个参数")
        return

    print(f"订单 {order_no} · 账号 {account_id}")
    raw = await fetch_order_detail(order_no, account_id)
    data = (raw or {}).get("data") or {}
    if not data:
        print("未取到详情（可能风控/未登录）:", json.dumps(raw, ensure_ascii=False)[:400])
        return

    buttons = ((data.get("bottomBarVO") or {}).get("buttonList")) or []
    print(f"状态 status={data.get('status')}  按钮数={len(buttons)}\n")
    if not buttons:
        print("该订单当前没有可操作按钮（已完成/已关闭通常是这样）")
    for btn in buttons:
        print(_describe_button(btn))
        print()

    out = f"/tmp/order_actions_{order_no}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)
    print(f"原始响应已落盘: {out}")


if __name__ == "__main__":
    asyncio.run(main())
