"""
Playwright 无头滑块的触摸协议工具。

功能：
1. 统一滑块请求与浏览器页面的 Chrome UA/Client Hints。
2. 通过 CDP 发送带触点物理参数的拖动，不调用系统鼠标或窗口输入。
"""
from __future__ import annotations

import random
import sys
import time
from typing import Any, Sequence, Tuple

# 滑块浏览器 UA 指纹铁律（2026-10-23 定位 v1.0.70）：
# Baxia 会用 JS 探针交叉核对「UA 字符串 ↔ navigator.userAgentData（低熵）↔
# canvas/WebGL/字体（高熵）」。此前写死 Windows Chrome 139，而 macOS 机器上
# 真实内核是 Chromium 15x + macOS 字体/画布 —— 低熵平台一行 JS 就穿帮，
# 环境评分不及格，**轨迹再像人（含真人手动拖）也一律响应 300 / error:pgEdp**。
# 因此 UA 的平台部分必须跟随运行机器的真实操作系统；Chrome 主版本在拿不到
# 真实内核版本时用兼容值（版本偏高熵、老版本很常见，风险远小于平台矛盾）。
SLIDER_CHROME_VERSION = "139"


def slider_platform_profile() -> dict:
    """返回与当前操作系统自洽的 UA 平台片段与 UA-CH 元数据。"""
    if sys.platform == "darwin":
        return {
            "os_token": "Macintosh; Intel Mac OS X 10_15_7",
            "platform": "macOS",
            "platform_version": "13.5.0",
            "architecture": "arm",
        }
    if sys.platform == "win32":
        return {
            "os_token": "Windows NT 10.0; Win64; x64",
            "platform": "Windows",
            "platform_version": "10.0.0",
            "architecture": "x86",
        }
    return {
        "os_token": "X11; Linux x86_64",
        "platform": "Linux",
        "platform_version": "5.15.0",
        "architecture": "x86",
    }


def build_slider_user_agent() -> str:
    profile = slider_platform_profile()
    return (
        f"Mozilla/5.0 ({profile['os_token']}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{SLIDER_CHROME_VERSION}.0.0.0 Safari/537.36"
    )


# 兼容既有引用（slider_stealth 启动参数、CDP override）。
# 注意：可见窗口（非 headless）模式下 slider_stealth 已不再覆盖 UA，
# 直接用浏览器真实 UA —— 那是与内核/平台/字体完全自洽的最优解。
SLIDER_USER_AGENT = build_slider_user_agent()
TouchPoint = Tuple[float, float, float]


def configure_slider_user_agent(context: Any, page: Any) -> None:
    """
    配置与当前真实操作系统自洽的 UA Client Hints 指纹。

    平台字段（platform/platformVersion/architecture/os_token）跟随本机真实
    系统 —— 见模块顶部「UA 指纹铁律」注释，写死 Windows 在 macOS 上必穿帮。

    Args:
        context: Playwright 浏览器上下文。
        page: 当前滑块页面。
    Returns:
        None
    """
    profile = slider_platform_profile()
    session = context.new_cdp_session(page)
    session.send(
        "Emulation.setUserAgentOverride",
        {
            "userAgent": SLIDER_USER_AGENT,
            "acceptLanguage": "zh-CN,zh;q=0.9,en;q=0.8",
            "platform": profile["platform"],
            "userAgentMetadata": {
                "brands": [
                    {"brand": "Not;A=Brand", "version": "99"},
                    {"brand": "Google Chrome", "version": SLIDER_CHROME_VERSION},
                    {"brand": "Chromium", "version": SLIDER_CHROME_VERSION},
                ],
                "fullVersionList": [
                    {"brand": "Not;A=Brand", "version": "99.0.0.0"},
                    {"brand": "Google Chrome", "version": f"{SLIDER_CHROME_VERSION}.0.0.0"},
                    {"brand": "Chromium", "version": f"{SLIDER_CHROME_VERSION}.0.0.0"},
                ],
                "fullVersion": f"{SLIDER_CHROME_VERSION}.0.0.0",
                "platform": profile["platform"],
                "platformVersion": profile["platform_version"],
                "architecture": profile["architecture"],
                "model": "",
                "mobile": False,
                "bitness": "64",
                "wow64": False,
            },
        },
    )


def dispatch_touch_drag(
    page: Any,
    start_x: float,
    start_y: float,
    trajectory: Sequence[TouchPoint],
) -> None:
    """
    通过浏览器协议派发一段触摸拖动，不触碰操作系统鼠标。

    Args:
        page: 当前滑块页面。
        start_x: 滑块按钮中心 X 坐标。
        start_y: 滑块按钮中心 Y 坐标。
        trajectory: 相对起点的 (x, y, delay_seconds) 轨迹。
    Returns:
        None
    """
    if not trajectory:
        raise ValueError("触摸轨迹不能为空")

    session = page.context.new_cdp_session(page)
    radius_x = 4.0
    radius_y = 4.0
    rotation = 0.0
    force = 0.5

    def send(event_type: str, x: float, y: float, active: bool = True) -> None:
        """发送一个带接触面积、方向和压力的触摸事件。"""
        touch_point = {
            "id": 0,
            "x": x,
            "y": y,
            "radiusX": radius_x,
            "radiusY": radius_y,
            "rotationAngle": rotation,
            "force": force,
            "tangentialPressure": 0.0,
        }
        session.send(
            "Input.dispatchTouchEvent",
            {
                "type": event_type,
                "touchPoints": [touch_point] if active else [],
                "modifiers": 0,
                "timestamp": time.time(),
            },
        )

    send("touchStart", start_x, start_y)
    time.sleep(random.uniform(0.06, 0.11))
    for offset_x, offset_y, delay in trajectory:
        send("touchMove", start_x + offset_x, start_y + offset_y)
        time.sleep(delay)
    time.sleep(random.uniform(0.20, 0.30))
    end_x, end_y, _ = trajectory[-1]
    send("touchEnd", start_x + end_x, start_y + end_y, active=False)
