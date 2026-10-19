"""
闲鱼 IM 订单卡片富数据解析（只读，零风控）

背景：订单卡片（我已拍下待付款+修改价格、我已付款请填写演出信息+去填写、
我已发货请查看发货凭证等）的富内容——标题、描述、按钮、动作 URL、订单号、
演出信息字段——全部藏在消息的 `msg["1"]["6"]["3"]["5"]`（一个 JSON 字符串）
里，结构为 dxCard。两套解析链路此前都只取 `["2"]`（方括号摘要），富数据从未
被解析（2026-10-19 用真实订单流程采样 16 条验证，见
backend-web/logs/order_card_samples.jsonl）。

本模块把这份富数据提出来，供：
- websocket 服务 App 通道转发给 backend-web → 前端渲染富卡片；
- backend-web 推送/历史解析直接附带；
- 后续卡片按钮 → 系统操作（发货/改价/填写演出信息）的映射依据。

安全承诺：纯解析本地已收到的消息，不产生任何闲鱼请求。
所有函数绝不抛异常——解析失败返回 None，由调用方降级到原有文本展示。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

# 卡片按钮的动作类型（按 targetUrl 的 scheme 识别）
_ACTION_PATTERNS = (
    ("adjust_price", "fleamarket://adjust_price"),
    ("smart_form", "fleamarket://smart_form"),
    ("shipping_proof", "shippingProof"),
    ("order_detail", "fleamarket://order_detail"),
    ("goto_pay", "GotoPay"),
)

# 卡片类型分类：优先按 bizTag.taskName（精准），退化按标题文本
_KIND_BY_TASK_NAME = (
    ("已拍下_未付款", "placed"),           # 已拍下，待付款（+修改价格）
    ("修改价格待付款", "price_modified"),   # 我已修改价格，等待你付款（+去付款）
    ("请填写门票信息", "to_fill_show"),     # 我已付款，请填写演出信息（+去填写）
    ("待卖家填写演出信息", "to_fill_show"),
    ("卖家提交信息", "show_submitted"),     # 我已填写好演出信息，待买家确认
    ("买家确认", "show_confirmed"),         # 买家已确认演出信息
    ("提醒发货", "to_ship"),                # 待卖家发货
    ("发货凭证", "shipped"),                # 我已发货，请查看发货凭证（H5凭证）
    ("我已交货", "shipped_notice"),         # 你已发货（无按钮通知）
    ("提醒我已交货", "shipped_notice"),
)
_KIND_BY_TITLE = (
    ("拍下", "placed"),
    ("修改价格", "price_modified"),
    ("填写演出信息", "to_fill_show"),
    ("演出信息，待买家确认", "show_submitted"),
    ("填写好演出信息", "show_submitted"),
    ("确认演出信息", "show_confirmed"),
    ("待发货", "to_ship"),
    ("等待你发货", "to_ship"),
    ("发货凭证", "shipped"),
    ("已发货", "shipped_notice"),
)

_ORDER_ID_PATTERNS = (
    re.compile(r"order_detail\?id=(\d+)"),
    re.compile(r"[?&]orderId=(\d+)"),
    re.compile(r"[?&]outerId=(\d+)"),
    re.compile(r"[?&]bizOrderId=(\d+)"),
    re.compile(r'"bizOrderId"\s*:\s*(\d+)'),
)


def _extract_order_id(*texts: str) -> str:
    for text in texts:
        if not text:
            continue
        for pattern in _ORDER_ID_PATTERNS:
            m = pattern.search(text)
            if m:
                return m.group(1)
    return ""


def _classify_action(url: str, front_action: Any) -> str:
    """识别按钮动作类型。

    frontAction（APP 原生动作，如 GotoPay）优先于 targetUrl（兜底跳转页）——
    去付款按钮两者都有，语义上以前者为准。
    """
    fa_blob = ""
    if front_action:
        try:
            fa_blob = json.dumps(front_action, ensure_ascii=False)
        except Exception:
            fa_blob = ""
    for blob in (fa_blob, url or ""):
        if not blob:
            continue
        for action, needle in _ACTION_PATTERNS:
            if needle in blob:
                return action
    return ""


def _classify_kind(task_name: str, title: str) -> str:
    for needle, kind in _KIND_BY_TASK_NAME:
        if needle in task_name:
            return kind
    for needle, kind in _KIND_BY_TITLE:
        if needle in title:
            return kind
    return "unknown"


def _parse_show_info(data_field: Any) -> Optional[Dict[str, str]]:
    """演出信息字段：data 是 JSON 字符串，含 concertName/ticketCount/date"""
    if not data_field or not isinstance(data_field, str):
        return None
    try:
        obj = json.loads(data_field)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    info: Dict[str, str] = {}
    for key in ("concertName", "ticketCount", "date"):
        val = obj.get(key)
        if val:
            info[key] = str(val)
    return info or None


def _extract_from_payload(
    payload: dict,
    title_fallback: str = "",
    task_name: str = "",
) -> Optional[Dict[str, Any]]:
    """从 dxCard 载荷提取富数据（推送与历史共用）。

    Args:
        payload: custom.data / msg["1"]["6"]["3"]["5"] 解码后的 dict
        title_fallback: 标题兜底（推送侧的方括号摘要）
        task_name: bizTag.taskName（推送侧有；历史侧通常没有，传空串）
    """
    try:
        dx_card = payload.get("dxCard")
        if not isinstance(dx_card, dict):
            return None
        main = dx_card.get("item", {}).get("main", {})
        if not isinstance(main, dict):
            return None
        ex = main.get("exContent")
        if not isinstance(ex, dict):
            return None

        title = str(ex.get("title") or title_fallback or "")
        desc = str(ex.get("desc") or "")
        card_url = str(main.get("targetUrl") or ex.get("targetUrl") or "")

        # 按钮（button 为 {} 表示无按钮）
        button: Optional[Dict[str, str]] = None
        btn_raw = ex.get("button")
        if isinstance(btn_raw, dict) and btn_raw.get("text"):
            btn_url = str(btn_raw.get("targetUrl") or "")
            front_action = btn_raw.get("frontAction")
            button = {
                "text": str(btn_raw.get("text") or ""),
                "url": btn_url,
                "action": _classify_action(btn_url, front_action),
            }
        elif card_url.startswith("http"):
            # 整卡可点但没配按钮文案（如发货凭证卡：点击卡片打开 H5 凭证页），
            # 用卡片标题合成一个按钮，保证前端有点的地方
            button = {
                "text": title or "查看",
                "url": card_url,
                "action": _classify_action(card_url, None),
            }

        order_id = _extract_order_id(
            card_url,
            btn_raw.get("targetUrl", "") if isinstance(btn_raw, dict) else "",
            json.dumps(btn_raw, ensure_ascii=False) if isinstance(btn_raw, dict) else "",
        )

        template = ""
        tpl = dx_card.get("template")
        if isinstance(tpl, dict):
            template = str(tpl.get("name") or "")

        return {
            "kind": _classify_kind(task_name, title),
            "title": title,
            "desc": desc,
            "order_id": order_id,
            "button": button,
            "show_info": _parse_show_info(ex.get("data")),
            "task_name": task_name,
            "target_url": card_url,
            "bg_color": str(ex.get("bgColor") or ""),
            "template": template,
        }
    except Exception:
        return None


def parse_rich_card(message: dict) -> Optional[Dict[str, Any]]:
    """从一条已解密的 IM 推送消息提取订单卡片富数据。

    Args:
        message: 解密后的消息 dict（实时推送帧结构：富内容在
            msg["1"]["6"]["3"]["5"] 的 dxCard JSON 字符串里）

    Returns:
        富数据 dict（kind/title/desc/order_id/button/show_info/task_name/
        target_url/bg_color/template），不是交易卡片或解析失败返回 None。
    """
    try:
        if not isinstance(message, dict):
            return None
        msg_1 = message.get("1")
        if not isinstance(msg_1, dict):
            return None
        msg_6 = msg_1.get("6")
        if not isinstance(msg_6, dict):
            return None
        msg_6_3 = msg_6.get("3")
        if not isinstance(msg_6_3, dict):
            return None
        payload_raw = msg_6_3.get("5")
        if not payload_raw or not isinstance(payload_raw, str):
            return None

        try:
            payload = json.loads(payload_raw)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None

        # bizTag.taskName（精准分类标签）
        task_name = ""
        msg_10 = msg_1.get("10")
        if isinstance(msg_10, dict):
            biz_tag = msg_10.get("bizTag")
            if isinstance(biz_tag, str):
                try:
                    task_name = str(json.loads(biz_tag).get("taskName") or "")
                except Exception:
                    task_name = ""

        return _extract_from_payload(
            payload,
            title_fallback=str(msg_6_3.get("2") or ""),
            task_name=task_name,
        )
    except Exception:
        return None


def parse_rich_card_decoded(decoded: Any, title_fallback: str = "") -> Optional[Dict[str, Any]]:
    """从 HTTP 历史消息解码后的 custom.data 提取订单卡片富数据。

    历史卡片载荷有两种形态：
    1. 与推送一致的 dxCard 富形式 → 全量提取（按钮/订单号/演出信息）；
    2. 简化 {title/template} 形式 → 合成一条仅标题的卡片记录
       （前端仍按富卡片样式渲染，只是没有按钮）。

    Args:
        decoded: load_content_json 的返回值
        title_fallback: 标题兜底
    """
    if not isinstance(decoded, dict):
        return None
    if "dxCard" in decoded:
        return _extract_from_payload(decoded, title_fallback=title_fallback)
    if "title" in decoded or "template" in decoded:
        title = str(decoded.get("title") or decoded.get("template") or title_fallback or "")
        if not title:
            return None
        tpl = decoded.get("template")
        return {
            "kind": _classify_kind("", title),
            "title": title,
            "desc": "",
            "order_id": _extract_order_id(title),
            "button": None,
            "show_info": None,
            "task_name": "",
            "target_url": "",
            "bg_color": "",
            "template": str(tpl) if isinstance(tpl, str) else "",
        }
    return None
