"""定案实验 5：扫描 heracles / 演出信息的兄弟接口是否存在。

mtop 对「不存在的 API」与「存在但参数不对」返回完全不同的错误码：
- 不存在 → FAIL_SYS_API_NOT_FOUND / FAIL_SYS_SERVICE_NOT_FOUND / 服务不可用
- 存在   → FAIL_BIZ_*（缺参数、业务校验失败）

据此一次扫出真实存在的接口名。若存在 form.submit / form.encrypt /
安全密钥查询之类的接口，直提交就有新路可走。

只读优先：对未知接口一律发最小参数（写接口也不带有效表单数据，
即便存在也不会真的改动订单）。
"""
import sys
import json
import time

ROOT = "/Users/jhx/code/xy-auto-reply/xianyu-auto-reply"
sys.path.insert(0, ROOT)
sys.path.insert(0, f"{ROOT}/scripts/capture_xianyu")

from probe_real_submit import (  # noqa: E402
    call_mtop, load_order, API_QUERY, API_CALLBACK,
)

ORDER = "3317145744099072190"

CB_DATA = {
    "callbackCode": "uZet4GmNwP", "event": "s_init",
    "sceneCode": "YCH", "outerType": "ORDER", "outerId": ORDER,
    "formData": json.dumps({"concertName": "测", "ticketCount": 1,
                            "date": "2026-10-21"}, ensure_ascii=False),
}

CANDIDATES = [
    # 对照：已知存在的两个
    (API_QUERY, {"sceneCode": "YCH", "outerType": "ORDER", "outerId": ORDER}),
    (API_CALLBACK, CB_DATA),
    # 猜测的兄弟接口
    ("mtop.taobao.heracles.inno.smart.form.submit", CB_DATA),
    ("mtop.taobao.heracles.inno.smart.form.save", CB_DATA),
    ("mtop.taobao.heracles.inno.smart.form.encrypt",
     {"sceneCode": "YCH", "plain": "793357549"}),
    ("mtop.taobao.heracles.inno.smart.form.decrypt", {"sceneCode": "YCH"}),
    ("mtop.taobao.heracles.inno.smart.form.config", {"sceneCode": "YCH"}),
    ("mtop.taobao.heracles.inno.security.key.query", {"sceneCode": "YCH"}),
    ("mtop.taobao.heracles.inno.smart.form.render",
     {"sceneCode": "YCH", "outerType": "ORDER", "outerId": ORDER}),
    # 演出信息专用接口猜测
    ("mtop.taobao.idle.show.info.submit", {"orderId": ORDER}),
    ("mtop.taobao.idle.order.show.info.submit", {"orderId": ORDER}),
    ("mtop.taobao.idle.show.info.query", {"orderId": ORDER}),
    ("mtop.taobao.idle.show.info.save", {"orderId": ORDER}),
]


def main():
    order, cookie = load_order(ORDER)
    assert order and cookie, "拿不到订单 cookie"

    for api, data in CANDIDATES:
        try:
            r = call_mtop(cookie, api, data)
            ret = r.get("ret") or []
            flag = "✅存在" if not any(
                ("NOT_FOUND" in x or "服务不可用" in x or "非法" in x)
                for x in ret) else "❌不存在"
            print(f"{flag}  {api}\n        {json.dumps(ret, ensure_ascii=False)[:220]}")
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 异常  {api}: {e}")
        time.sleep(1.5)


if __name__ == "__main__":
    main()
