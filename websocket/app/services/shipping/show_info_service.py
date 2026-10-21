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
是 Flutter/Kraken 页面，提交接口在原生层，JS bundle 里没有。

重要（风控约束）：提交接口名与参数只在 APP 原生层可见，必须抓包拿到，
**禁止硬编码猜测**——猜错不但提交失败，失败的写操作还可能计入账号风控。
因此接口名改为从系统设置读取（xy_system_settings 的
`shipping.show_info_api` / `shipping.show_info_api_version`），抓到后填进设置即可
生效，无需改码；未配置时 submit_show_info 返回 not_configured，由前端退化为
打开 APP 深链填写，卖家不会被卡住。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

import aiohttp
from loguru import logger
from sqlalchemy import select

from app.services.shipping.base import BaseShippingService
from common.models.system_setting import SystemSetting
from common.utils.xianyu_utils import generate_sign

# 演出信息提交接口（默认 None：必须从系统设置 shipping.show_info_api 配置；
# 仅当设置未配置时回退到此处，保持历史行为——未配置即不调用）。
SHOW_INFO_SUBMIT_API: Optional[str] = None
SHOW_INFO_SUBMIT_API_VERSION = "1.0"

# 系统设置键名
SETTING_SHOW_INFO_API = "shipping.show_info_api"
SETTING_SHOW_INFO_API_VERSION = "shipping.show_info_api_version"

# 字段约束（与卡片 schema 对齐）
CONCERT_NAME_MAX = 30
TICKET_COUNT_MIN = 1
TICKET_COUNT_MAX = 99


class ShowInfoService(BaseShippingService):
    """演出信息提交服务。"""

    async def _load_api_config(self) -> tuple[Optional[str], str]:
        """从系统设置读取提交接口名与版本，回退到模块常量。

        优先读 xy_system_settings（管理员可随时改，无需重启）；读不到或为空时
        回退到本模块常量 SHOW_INFO_SUBMIT_API（默认 None）。
        """
        api_name: Optional[str] = None
        api_version = SHOW_INFO_SUBMIT_API_VERSION
        try:
            # 逐键查询，避免 in_ 顺序不确定导致版本/名称错配
            api_row = await self.db_session.execute(
                select(SystemSetting.value).where(SystemSetting.key == SETTING_SHOW_INFO_API)
            )
            api_val = api_row.scalar_one_or_none()
            if api_val and api_val.strip():
                api_name = api_val.strip()
            ver_row = await self.db_session.execute(
                select(SystemSetting.value).where(SystemSetting.key == SETTING_SHOW_INFO_API_VERSION)
            )
            ver_val = ver_row.scalar_one_or_none()
            if ver_val and ver_val.strip():
                api_version = ver_val.strip()
        except Exception as e:  # 读设置失败不应阻断，回退到模块常量
            logger.warning(f"【{self.account_id}】读取演出信息提交接口设置失败，回退默认: {e}")

        if not api_name and SHOW_INFO_SUBMIT_API:
            api_name = SHOW_INFO_SUBMIT_API
        return api_name, api_version

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
        """构造提交业务参数（原始值，不带卡片展示用的「演出名称：」前缀）。

        订单号字段用 orderId（与改价接口一致）；门票数量保留「N张」或纯数字，
        date 为 yyyy-MM-dd。提交入参若与抓包不一致，以抓包为准，调整此处即可。
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
        """提交演出信息。

        返回 dict：
          - 成功: {"success": True, "order_id", "message"}
          - 校验失败 / 接口未配置: {"error": ..., "order_id", "pending_api"/"not_configured": True}
          - 调用失败: {"error": ..., "order_id"}
        """
        err = self.validate_form(concert_name, ticket_count, show_date)
        if err:
            return {"error": err, "order_id": order_id}

        api_name, api_version = await self._load_api_config()
        if not api_name:
            logger.error(
                f"【{self.account_id}】演出信息提交接口未配置（系统设置 {SETTING_SHOW_INFO_API}），"
                f"订单 {order_id} 退回 APP 深链填写"
            )
            return {
                "error": "演出信息提交接口尚未配置（请在系统设置填写抓包得到的 mtop 接口名），可先到闲鱼 APP 填写",
                "order_id": order_id,
                "pending_api": True,
                "not_configured": True,
            }

        if not self._account and not await self._load_account():
            return {"error": "账号信息加载失败", "order_id": order_id}
        if not self.http_session:
            return {"error": "HTTP Session未创建", "order_id": order_id}

        data_val = self._build_submit_data(order_id, concert_name, ticket_count, show_date)
        import json

        data_str = json.dumps(data_val, ensure_ascii=False, separators=(",", ":"))
        max_retry = 3
        last_error = "未知错误"
        for attempt in range(max_retry):
            try:
                cookies = (self.cookies_str or "").replace("\n", "").replace("\r", "")
                token = ""
                for kv in cookies.split("; "):
                    if kv.startswith("_m_h5_tk="):
                        token = kv[len("_m_h5_tk="):].split("_")[0]
                        break
                ts = str(int(time.time() * 1000))
                sign = generate_sign(ts, token, data_str)

                params = {
                    "jsv": "2.7.2",
                    "appKey": "34839810",
                    "t": ts,
                    "sign": sign,
                    "v": api_version,
                    "type": "originaljson",
                    "accountSite": "xianyu",
                    "dataType": "json",
                    "timeout": "20000",
                    "api": api_name,
                    "sessionOption": "AutoLoginOnly",
                }
                headers = {
                    "accept": "application/json",
                    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "cache-control": "no-cache",
                    "content-type": "application/x-www-form-urlencoded",
                    "idle_site_biz_code": "COMMONPRO",
                    "idle_user_group_member_id": "",
                    "pragma": "no-cache",
                    "origin": "https://seller.goofish.com",
                    "referer": "https://seller.goofish.com/?site=COMMONPRO&spm=a21107h.44911108.0.0",
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36",
                    "cookie": cookies,
                }

                logger.info(
                    f"【{self.account_id}】提交演出信息（第{attempt + 1}次），订单 {order_id}，接口 {api_name}"
                )
                async with self.http_session.post(
                    f"https://h5api.m.goofish.com/h5/{api_name}/{api_version}/",
                    params=params,
                    data={"data": data_str},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as response:
                    res_json = await response.json()
                    await self._handle_response_cookies(response)
                    logger.info(f"【{self.account_id}】演出信息提交响应: {res_json}")

                    ret_list = res_json.get("ret", []) or []
                    data = res_json.get("data") or {}
                    biz_success = bool(data.get("success")) if isinstance(data, dict) else False
                    if any("SUCCESS" in r for r in ret_list) and (biz_success or not isinstance(data, dict) or not data):
                        return {"success": True, "order_id": order_id, "message": ret_list[0] if ret_list else "提交成功"}

                    # 失败：若 ret 含明确错误文案则采用，否则用 data.msg
                    last_error = ret_list[0] if ret_list else (data.get("msg") or "提交失败")
                    # token/登录态失效导致的失败，用更新后的 cookie 重试
                    if any("TOKEN" in r or "USER_VALIDATE" in r or "ILLEGAL_ACCESS" in r for r in ret_list):
                        logger.warning(f"【{self.account_id}】演出信息提交鉴权失败，重试: {last_error}")
                        continue
                    # 其它业务失败不再重试
                    return {"error": last_error, "order_id": order_id}
            except Exception as e:
                last_error = f"网络异常: {self._safe_str(e)}"
                logger.error(f"【{self.account_id}】演出信息提交异常: {self._safe_str(e)}")
                continue

        return {"error": last_error, "order_id": order_id}
