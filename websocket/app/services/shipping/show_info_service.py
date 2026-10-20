"""演出信息（门票）提交服务。

背景：演唱会门票订单在「我已付款」后，卖家需要先**填写演出信息**
（演出名称 / 门票数量 / 演出时间），买家确认后订单才进入「待发货」。

表单结构来自真实提交卡片的 schema（2026-10-20 从账号 2217728125233
会话 67957318520 的「你已填写好演出信息」消息中提取）：

    {
      "concertName": "演出名称：北京张杰",   # string, max 30, required
      "ticketCount": "门票数量：1张",        # string, 1~99, required
      "date": "演出时间：2026-10-20"         # date, 今起 365 天内, required
    }

APP 原生页（fleamarket://smart_form?sceneCode=YCH&outerType=ORDER&outerId=订单号）
是 Flutter/Kraken 页面，提交接口在原生层，JS bundle 里没有；接口名与参数
待手机抓包确认后填入下方 SHOW_INFO_SUBMIT_API / _build_submit_data。
"""
from __future__ import annotations

import time
from typing import Any, Dict

import aiohttp
from loguru import logger

from app.services.shipping.base import BaseShippingService
from common.utils.xianyu_utils import generate_sign

# 演出信息提交接口（待抓包确认后填写）。
# 说明：mtop H5 同族接口（与改价 mtop.taobao.idle.trade.user.adjust.price、
# 无物流发货 mtop.taobao.idle.logistic.consign.dummy 同一网关），
# 但名称与入参只在 APP 原生层可见，必须抓包拿到，禁止猜测——
# 猜错不但提交失败，失败的写操作还可能计入账号风控。
SHOW_INFO_SUBMIT_API: str | None = None
SHOW_INFO_SUBMIT_API_VERSION = "1.0"

# 字段约束（与卡片 schema 对齐）
CONCERT_NAME_MAX = 30
TICKET_COUNT_MIN = 1
TICKET_COUNT_MAX = 99


class ShowInfoService(BaseShippingService):
    """演出信息提交服务。"""

    def validate_form(
        self,
        concert_name: str,
        ticket_count: str,
        show_date: str,
    ) -> str:
        """按 schema 约束校验表单，返回错误文案（空串 = 通过）。"""
        name = (concert_name or "").strip()
        count = (ticket_count or "").strip()
        date = (show_date or "").strip()
        if not name:
            return "请填写演出名称"
        if len(name) > CONCERT_NAME_MAX:
            return f"演出名称不能超过 {CONCERT_NAME_MAX} 字"
        if not count:
            return "请填写门票数量"
        try:
            n = int(count.replace("张", ""))
        except ValueError:
            return "门票数量必须是数字"
        if not (TICKET_COUNT_MIN <= n <= TICKET_COUNT_MAX):
            return f"门票数量需在 {TICKET_COUNT_MIN}~{TICKET_COUNT_MAX} 之间"
        if not date:
            return "请选择演出时间"
        return ""

    def _build_submit_data(
        self,
        order_id: str,
        concert_name: str,
        ticket_count: str,
        show_date: str,
    ) -> Dict[str, Any]:
        """构造提交业务参数。

        已证实的展示形态（提交后卡片 data）：
            {"concertName": "演出名称：北京张杰",
             "ticketCount": "门票数量：1张",
             "date": "演出时间：2026-10-20"}
        提交入参待抓包核对（是否带标签前缀、订单号字段名 orderId/bizOrderId）。
        """
        return {
            "orderId": order_id,
            "concertName": concert_name.strip(),
            "ticketCount": ticket_count.strip(),
            "date": show_date.strip(),
        }

    async def submit_show_info(
        self,
        order_id: str,
        concert_name: str,
        ticket_count: str,
        show_date: str,
    ) -> Dict[str, Any]:
        """提交演出信息。"""
        err = self.validate_form(concert_name, ticket_count, show_date)
        if err:
            return {"error": err, "order_id": order_id}

        if not SHOW_INFO_SUBMIT_API:
            logger.error(
                f"【{self.account_id}】演出信息提交接口未接入（等待手机抓包确认），"
                f"订单 {order_id}"
            )
            return {
                "error": "演出信息提交接口尚未接入（等待抓包确认），请先到闲鱼 APP 填写",
                "order_id": order_id,
                "pending_api": True,
            }

        if not self._account and not await self._load_account():
            return {"error": "账号信息加载失败", "order_id": order_id}
        if not self.http_session:
            return {"error": "HTTP Session未创建", "order_id": order_id}

        data_val = self._build_submit_data(
            order_id, concert_name, ticket_count, show_date
        )
        import json

        data_str = json.dumps(data_val, ensure_ascii=False, separators=(",", ":"))
        ts = str(int(time.time()) * 1000)
        params = {
            "jsv": "2.7.2",
            "appKey": "34839810",
            "t": ts,
            "sign": generate_sign(ts, self._get_token_from_cookies(), data_str),
            "v": SHOW_INFO_SUBMIT_API_VERSION,
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": SHOW_INFO_SUBMIT_API,
            "sessionOption": "AutoLoginOnly",
        }
        try:
            logger.info(f"【{self.account_id}】提交演出信息，订单 {order_id}")
            async with self.http_session.post(
                f"https://h5api.m.goofish.com/h5/{SHOW_INFO_SUBMIT_API}/{SHOW_INFO_SUBMIT_API_VERSION}/",
                params=params,
                data={"data": data_str},
                headers=self._build_headers(),
            ) as response:
                res_json = await response.json()
                await self._handle_response_cookies(response)
                logger.info(f"【{self.account_id}】演出信息提交响应: {res_json}")
                ret_msg = (
                    res_json.get("ret", ["未知错误"])[0]
                    if res_json.get("ret")
                    else "未知错误"
                )
                if "SUCCESS" in ret_msg:
                    return {"success": True, "order_id": order_id, "message": ret_msg}
                return {"error": ret_msg, "order_id": order_id}
        except Exception as e:
            logger.error(f"【{self.account_id}】演出信息提交异常: {self._safe_str(e)}")
            return {"error": f"网络异常: {self._safe_str(e)}", "order_id": order_id}

    def _build_headers(self) -> Dict[str, str]:
        return {
            "Cookie": self.cookies_str,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://www.goofish.com/",
        }
