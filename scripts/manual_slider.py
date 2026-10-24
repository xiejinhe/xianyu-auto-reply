#!/usr/bin/env python3
"""人工手拖滑块一次性通道（诊断用，不接入服务主流程）。

用途：当自动滑块恒返回 300 时，用「本机真实 Chrome + 账号 Cookie」打开
punish 页并把窗口交给人手动拖，用于区分「行为分问题」与「环境/账号分问题」。

与线上滑块引擎的区别：
- 用系统 Chrome（channel="chrome"），不是 Playwright/Patchright 自带内核；
- 不注入任何 stealth 脚本、不覆盖 UA（就是日常浏览器的真实指纹）；
- **不做任何自动滑动**，页面加载后只等待人工操作。

用法：
    NO_PROXY="localhost,127.0.0.1,::1" \
    ./websocket/.venv/bin/python scripts/manual_slider.py <account_id> <punish_url> [等待秒数]

成功判定：页面离开 punish / 出现新的 x5* Cookie。成功后把新的 x5* Cookie
合并写回 xy_accounts.cookie 并清空 xy_token_cache，让服务下次刷新直接生效。
"""
from __future__ import annotations

import os
import re
import sys
import time
from urllib.parse import urlparse

# 走系统代理会让 punish 页加载异常，本地诊断统一绕开
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
os.environ.setdefault("no_proxy", "localhost,127.0.0.1,::1")

import pymysql  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

DB_CFG = dict(
    host="127.0.0.1",
    port=3306,
    user="xianyu",
    password="xianyu@2026",
    database="xianyu_data",
    charset="utf8mb4",
)

COOKIE_DOMAIN = ".goofish.com"


def load_account_cookie(account_id: str) -> str:
    conn = pymysql.connect(**DB_CFG)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cookie FROM xy_accounts WHERE account_id=%s", (account_id,))
            row = cur.fetchone()
            return (row[0] or "") if row else ""
    finally:
        conn.close()


def parse_cookie_str(cookie_str: str) -> list[dict]:
    items = []
    for part in (cookie_str or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        items.append(
            {
                "name": name.strip(),
                "value": value.strip(),
                "domain": COOKIE_DOMAIN,
                "path": "/",
            }
        )
    return items


LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "websocket",
    "logs",
    "websocket.log",
)


def wait_fresh_punish_url(timeout: int = 300) -> str:
    """守株待兔：轮询 websocket 日志，返回一条刚生成的 punish 链接。"""
    start_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[WATCH] 已就绪，等待服务端刷出新的 punish 链接（基准时间 {start_ts}）...")
    deadline = time.time() + timeout
    seen: set[str] = set()
    while time.time() < deadline:
        try:
            with open(LOG_PATH, encoding="utf-8", errors="ignore") as fh:
                tail = fh.readlines()[-400:]
        except Exception:
            tail = []
        for line in tail:
            ts = line[:19]
            if ts <= start_ts:
                continue
            m = re.search(r"https://h5api[^\s'\"]*punish\?[^\s'\"]+", line)
            if m and len(m.group(0)) > 300:
                url = m.group(0)
                if url in seen:
                    continue
                seen.add(url)
                print(f"[WATCH] 捕获新链接 @ {ts}（{len(url)} 字符）")
                return url
        time.sleep(1)
    return ""


def is_x5(name: str) -> bool:
    low = name.lower()
    return low.startswith("x5") or "x5sec" in low


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: python manual_slider.py <account_id> <punish_url> [等待秒数]")
        return 2
    account_id = sys.argv[1]
    punish_url = sys.argv[2]
    wait_seconds = int(sys.argv[3]) if len(sys.argv) > 3 else 180

    cookie_str = load_account_cookie(account_id)
    if not cookie_str:
        print(f"[ERROR] 账号 {account_id} 在 xy_accounts 中没有 cookie")
        return 1
    cookies = parse_cookie_str(cookie_str)
    before_x5 = {c["name"]: c["value"] for c in cookies if is_x5(c["name"])}
    print(f"[INFO] 账号 {account_id}，注入 cookie {len(cookies)} 个（其中 x5* {len(before_x5)} 个）")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False)
        context = browser.new_context(no_viewport=True)
        context.add_cookies(cookies)
        page = context.new_page()
        if punish_url.upper() == "WATCH":
            # punish 链接只有 1~2 分钟有效期：先起浏览器，再等服务端刷出一条
            # 新链接（Token 刷新每 3 分钟一次），拿到后立刻导航，把延迟压到秒级。
            punish_url = wait_fresh_punish_url()
            if not punish_url:
                print("[ERROR] 等待新鲜 punish 链接超时")
                browser.close()
                return 4

        print(f"[INFO] 导航到 punish 页: {punish_url[:80]}...")
        try:
            page.goto(punish_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:  # 页面慢/超时不致命，继续等人拖
            print(f"[WARN] goto 异常（继续等待人工操作）: {exc}")
        time.sleep(2.0)
        try:
            page.bring_to_front()
        except Exception:
            pass
        try:
            print(f"[INFO] 落地 URL: {page.url[:120]}")
            if "punish" not in (page.url or ""):
                print("[WARN] 页面未停留在 punish，链接可能已过期")
        except Exception:
            pass
        # 截图确认页面真的渲染了滑块，而不是"页面访问出现了问题"
        time.sleep(8)  # 等滑块本体渲染完（首帧往往还在转圈）
        shot = "/tmp/manual_slider.png"
        try:
            page.screenshot(path=shot)
            print(f"[INFO] 已截图确认页面状态: {shot}")
        except Exception as exc:
            print(f"[WARN] 截图失败: {exc}")

        print("\n" + "=" * 60)
        print("  请用鼠标手动拖动滑块。脚本不会自动滑动。")
        print(f"  最长等待 {wait_seconds} 秒，拖完看到页面跳转/消失即算成功。")
        print("=" * 60 + "\n")

        deadline = time.time() + wait_seconds
        result = None
        while time.time() < deadline:
            time.sleep(2)
            try:
                current_url = page.url
            except Exception:
                break
            try:
                now_cookies = {c["name"]: c["value"] for c in context.cookies()}
            except Exception:
                now_cookies = {}
            new_x5 = {
                k: v for k, v in now_cookies.items() if is_x5(k) and before_x5.get(k) != v
            }
            left_punish = "punish" not in (current_url or "")
            if new_x5 or left_punish:
                result = {
                    "url": current_url,
                    "cookies": now_cookies,
                    "new_x5": new_x5,
                    "left_punish": left_punish,
                }
                break

        if not result:
            print("\n[RESULT] 未检测到通过迹象（超时或页面无变化）")
            try:
                print(f"[DEBUG] 最终 URL: {page.url}")
            except Exception:
                pass
            browser.close()
            return 3

        print("\n[RESULT] 检测到通过迹象：")
        print(f"  最终 URL: {result['url'][:120]}")
        print(f"  新增/变化的 x5* Cookie: {list(result['new_x5'].keys())}")
        if not result["new_x5"]:
            print("  （只有 URL 变化，没有新 x5 Cookie，可能并未真正放行）")

        if result["new_x5"]:
            merged = parse_cookie_str(cookie_str)
            merged_map = {c["name"]: c for c in merged}
            for name, value in result["cookies"].items():
                if is_x5(name):
                    merged_map[name] = {
                        "name": name,
                        "value": value,
                        "domain": COOKIE_DOMAIN,
                        "path": "/",
                    }
            new_cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in merged_map.values())
            conn = pymysql.connect(**DB_CFG)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE xy_accounts SET cookie=%s WHERE account_id=%s",
                        (new_cookie_str, account_id),
                    )
                    cur.execute("DELETE FROM xy_token_cache WHERE user_id=%s", (account_id,))
                conn.commit()
                print(f"[OK] 已写回账号 cookie（{len(merged_map)} 项）并清空 Token 缓存")
            finally:
                conn.close()

        try:
            browser.close()
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
