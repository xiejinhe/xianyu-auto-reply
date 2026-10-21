"""演出信息（门票）提交服务。

背景：演唱会门票订单在「我已付款」后，卖家需要先**填写演出信息**
（演出名称 / 门票数量 / 演出时间），买家确认后订单才进入「待发货」。

== 2026-10-21 协议破解（APK 反编译 + 真实订单只读探测）==

演出信息表单是闲鱼 Flutter `form_render` 包的 smart form（heracles 引擎），
真实链路分两步：

1. **拉表单**（只读，安全）：
   POST h5api.m.goofish.com/h5/mtop.taobao.heracles.inno.smart.form.query/1.0/
   data = {"sceneCode": "YCH", "outerType": "ORDER", "outerId": 订单号}
   响应 data.formSchema.schemaData 是 JSON 字符串：
     {"type":"object","properties":{
        "title":{"title":"演出信息"},
        "acceptButton":{"title":"确定","props":{"event":"s_init",
                       "callbackCode":"uZet4GmNwP"}},
        "concertName":{"title":"演出名称","type":"string","widget":"input",
                       "max":30,"required":true},
        "ticketCount":{"title":"门票数量","type":"int","widget":"counter",
                       "formattedValue":"%d张","min":1,"max":99,"required":true},
        "date":{"title":"演出时间","type":"string","widget":"datePicker",
                "required":true,"props":{"startFromNow":true,"daysAfterNow":365}}}}

2. **提交**（写）：走 mtop.taobao.heracles.inno.smart.form.callback
   （APK 二进制里 heracles 命名空间只有 query/callback 两个完整接口名，无独立
   submit——smart_form_submit_req.dart 就是 callback 调用的请求模型）。
   data = {"callbackCode": 取自schema, "event": 取自schema(默认s_init),
           "sceneCode": "YCH", "outerType": "ORDER", "outerId": 订单号,
           "formData": JSON字符串 {"concertName":str,"ticketCount":int,"date":"yyyy-MM-dd"}}

**关键约束（实测踩坑）**：data 里所有值必须是**字符串**；一旦 formData 传对象
或任何值非字符串，网关直接报 FAIL_SYS_BIZPARAM_MISSED::缺少业务参数callbackCode。

== 验证状态 ==

- query 已用真实订单实测成功（返回上述 schema）。
- callback 的参数契约实测：callbackCode 必填、值全字符串校验可过；但对
  「已填完演出信息」和「不存在」的订单均返回 FAIL_BIZ_SYSTEM_EXCEPTION
  （该引擎对表单不可编辑/不存在统一抛系统异常，无更细文案）。
  **尚未在「待填写演出信息」状态的订单上做过真实提交验证**——需要等一个
  真实门票订单（或用户自建测试单）实测一次；失败时前端自动退化 APP 深链，
  卖家不会被卡住。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

import aiohttp
from loguru import logger
from sqlalchemy import select

from app.services.shipping.base import BaseShippingService
from common.models.system_setting import SystemSetting
from common.utils.xianyu_utils import generate_sign

# 表单查询接口（只读）。2026-10-21 APK 反编译锁定，真实订单实测 SUCCESS。
SHOW_INFO_QUERY_API = "mtop.taobao.heracles.inno.smart.form.query"

# 提交接口（默认已内置破解结果；系统设置 shipping.show_info_api 仍可覆盖，
# 闲鱼若换接口名改设置即可，无需改码）。
SHOW_INFO_SUBMIT_API: Optional[str] = "mtop.taobao.heracles.inno.smart.form.callback"
SHOW_INFO_SUBMIT_API_VERSION = "1.0"

# 系统设置键名
SETTING_SHOW_INFO_API = "shipping.show_info_api"
SETTING_SHOW_INFO_API_VERSION = "shipping.show_info_api_version"

# smart form 场景参数（与 APP 深链 fleamarket://smart_form?sceneCode=YCH 一致）
SCENE_CODE = "YCH"
OUTER_TYPE = "ORDER"

# schema acceptButton 事件的兜底值（schema 未下发时使用）
DEFAULT_SUBMIT_EVENT = "s_init"

# 字段约束（与 query 实测 schema 对齐）
CONCERT_NAME_MAX = 30
TICKET_COUNT_MIN = 1
TICKET_COUNT_MAX = 99


class ShowInfoService(BaseShippingService):
    """演出信息提交服务。"""

    async def _load_api_config(self) -> tuple[Optional[str], str]:
        """从系统设置读取提交接口名与版本，回退到模块内置破解结果。

        优先读 xy_system_settings（管理员可随时改，无需重启）；读不到或为空时
        回退到本模块常量 SHOW_INFO_SUBMIT_API（已内置 APK 破解的接口名）。
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

    @staticmethod
    def _parse_ticket_count(ticket_count: str) -> int:
        """「2张」/「2」→ 2（schema 里 ticketCount 是 int，计数器组件）"""
        return int(ticket_count.replace("张", "").strip())

    async def _mtop_post(self, api_name: str, api_version: str,
                         data_val: Dict[str, Any]) -> Dict[str, Any]:
        """调用一次 mtop H5 网关并返回解析后的 JSON。

        注意：data_val 的所有值必须是字符串（含对象/数字会被网关拒绝，
        报「缺少业务参数callbackCode」这类误导性错误）。
        """
        data_str = json.dumps(data_val, ensure_ascii=False, separators=(",", ":"))
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
        async with self.http_session.post(
            f"https://h5api.m.goofish.com/h5/{api_name}/{api_version}/",
            params=params,
            data={"data": data_str},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as response:
            res_json = await response.json()
            await self._handle_response_cookies(response)
            return res_json

    async def _query_form(self, order_id: str) -> tuple[Optional[Dict[str, Any]], str]:
        """拉取订单的演出信息表单 schema（只读）。

        Returns:
            (acceptButton.props, "") 或 (None, 错误文案)
        """
        try:
            res = await self._mtop_post(SHOW_INFO_QUERY_API, "1.0", {
                "sceneCode": SCENE_CODE,
                "outerType": OUTER_TYPE,
                "outerId": str(order_id),
            })
        except Exception as e:
            return None, f"演出信息表单加载异常: {self._safe_str(e)}"

        ret_list = res.get("ret", []) or []
        if not any("SUCCESS" in r for r in ret_list):
            return None, f"演出信息表单加载失败（{ret_list[0] if ret_list else '未知错误'}）"

        data = res.get("data") or {}
        schema_raw = (data.get("formSchema") or {}).get("schemaData")
        if not schema_raw:
            return None, "订单没有可填写的演出信息表单（可能不在待填写环节）"
        try:
            schema = json.loads(schema_raw)
        except (ValueError, TypeError):
            return None, "演出信息表单 schema 解析失败"
        props = ((schema.get("properties") or {}).get("acceptButton") or {}).get("props") or {}
        return props, ""

    async def submit_show_info(
        self,
        order_id: str,
        concert_name: str,
        ticket_count: str,
        show_date: str,
    ) -> Dict[str, Any]:
        """提交演出信息（query 拿 callbackCode → callback 提交）。

        返回 dict：
          - 成功: {"success": True, "order_id", "message"}
          - 校验失败: {"error": ..., "order_id"}
          - 接口/表单失败: {"error": ..., "order_id", "fallback_deeplink": True}
            （前端据此自动退化打开 APP 深链，卖家不被卡住）
        """
        err = self.validate_form(concert_name, ticket_count, show_date)
        if err:
            return {"error": err, "order_id": order_id}

        if not self._account and not await self._load_account():
            return {"error": "账号信息加载失败", "order_id": order_id}
        if not self.http_session:
            return {"error": "HTTP Session未创建", "order_id": order_id}

        # 第一步：拉表单 schema（只读），拿 acceptButton 的 callbackCode/event
        props, q_err = await self._query_form(order_id)
        if q_err or not props:
            logger.warning(f"【{self.account_id}】演出信息表单加载失败，订单 {order_id}: {q_err}")
            return {"error": q_err or "表单加载失败", "order_id": order_id,
                    "fallback_deeplink": True}
        callback_code = str(props.get("callbackCode") or "")
        event = str(props.get("event") or DEFAULT_SUBMIT_EVENT)
        if not callback_code:
            return {"error": "表单 schema 缺少 callbackCode，无法提交",
                    "order_id": order_id, "fallback_deeplink": True}

        api_name, api_version = await self._load_api_config()
        if not api_name:
            # 理论上到不了（模块已内置破解接口名），保留兜底
            return {
                "error": "演出信息提交接口未配置，可先到闲鱼 APP 填写",
                "order_id": order_id,
                "pending_api": True,
                "not_configured": True,
            }

        # 第二步：callback 提交（所有值必须是字符串！）
        try:
            ticket_count_int = self._parse_ticket_count(ticket_count)
        except ValueError:
            return {"error": "门票数量必须是数字", "order_id": order_id}
        form_data = {
            "concertName": concert_name.strip(),
            "ticketCount": ticket_count_int,
            "date": show_date.strip(),
        }
        data_val: Dict[str, str] = {
            "callbackCode": callback_code,
            "event": event,
            "sceneCode": SCENE_CODE,
            "outerType": OUTER_TYPE,
            "outerId": str(order_id),
            "formData": json.dumps(form_data, ensure_ascii=False, separators=(",", ":")),
        }

        max_retry = 3
        last_error = "未知错误"
        for attempt in range(max_retry):
            try:
                logger.info(
                    f"【{self.account_id}】提交演出信息（第{attempt + 1}次），"
                    f"订单 {order_id}，接口 {api_name}，callbackCode={callback_code}"
                )
                res_json = await self._mtop_post(api_name, api_version, data_val)
                logger.info(f"【{self.account_id}】演出信息提交响应: {res_json}")

                ret_list = res_json.get("ret", []) or []
                data = res_json.get("data") or {}
                biz_success = bool(data.get("success")) if isinstance(data, dict) else False
                if any("SUCCESS" in r for r in ret_list) and biz_success:
                    return {"success": True, "order_id": order_id,
                            "message": ret_list[0] if ret_list else "提交成功"}

                # 失败：ret 含明确错误文案则采用，否则用 data.msg
                last_error = ret_list[0] if ret_list else (
                    data.get("msg") if isinstance(data, dict) else None) or "提交失败"
                # token/登录态失效导致的失败，用更新后的 cookie 重试
                if any("TOKEN" in r or "USER_VALIDATE" in r or "ILLEGAL_ACCESS" in r
                       for r in ret_list):
                    logger.warning(f"【{self.account_id}】演出信息提交鉴权失败，重试: {last_error}")
                    continue
                # 其它业务失败不再重试
                return {"error": last_error, "order_id": order_id,
                        "fallback_deeplink": True}
            except Exception as e:
                last_error = f"网络异常: {self._safe_str(e)}"
                logger.error(f"【{self.account_id}】演出信息提交异常: {self._safe_str(e)}")
                continue

        return {"error": last_error, "order_id": order_id, "fallback_deeplink": True}
