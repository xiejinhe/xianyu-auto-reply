"""定案实验：callback 失败是否因为「写接口只认 APP 通道」。

实验组：
  A. 用 APP 端 appKey(12574478) 签名调 callback —— 看报错是否变化
  B. 假订单 query 对照 —— 确认 query 是否根本不校验表单存在性
"""
import sys
import json
import time

ROOT = "/Users/jhx/code/xy-auto-reply/xianyu-auto-reply"
sys.path.insert(0, ROOT)

import pymysql  # noqa: E402
from common.utils.xianyu_utils import generate_sign  # noqa: E402

sys.path.insert(0, "/Users/jhx/code/xy-auto-reply/xianyu-auto-reply/scripts/capture_xianyu")
from probe_real_submit import (  # noqa: E402
    call_mtop, extract_token, load_order, API_QUERY, API_CALLBACK, TASK_ID,
)

ORDER = "3317145744099072190"
NAME, COUNT, DATE = "北京张杰", 1, "2026-10-21"


def call_mtop_appkey(cookies: str, api: str, data: dict, app_key: str):
    import requests
    import urllib3
    urllib3.disable_warnings()
    token = extract_token(cookies)
    ts = str(int(time.time() * 1000))
    data_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    sign = generate_sign(ts, token, data_str)
    params = {
        "jsv": "2.7.2", "appKey": app_key, "t": ts, "sign": sign,
        "v": "1.0", "type": "originaljson", "accountSite": "xianyu",
        "dataType": "json", "timeout": "20000", "api": api,
        "sessionOption": "AutoLoginOnly",
    }
    headers = {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "idle_site_biz_code": "COMMONPRO",
        "origin": "https://seller.goofish.com",
        "referer": "https://seller.goofish.com/?site=COMMONPRO",
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36"),
        "cookie": cookies,
    }
    url = f"https://h5api.m.goofish.com/h5/{api}/1.0/"
    resp = requests.post(url, params=params, data={"data": data_str},
                         headers=headers, timeout=20, verify=False)
    try:
        return resp.json()
    except Exception:
        return {"_status": resp.status_code, "_text": resp.text[:500]}


def main():
    order, cookie = load_order(ORDER)
    assert order and cookie, "订单/cookie 缺失"

    form = {"concertName": NAME, "ticketCount": COUNT, "date": DATE}
    data_val = {
        "callbackCode": "uZet4GmNwP", "event": "s_init",
        "sceneCode": "YCH", "outerType": "ORDER", "outerId": ORDER,
        "formData": json.dumps(form, ensure_ascii=False, separators=(",", ":")),
    }

    print("===== A. APP appKey=12574478 调 callback =====")
    for ak in ("12574478", "34839810"):
        res = call_mtop_appkey(cookie, API_CALLBACK, data_val, ak)
        print(f"appKey={ak} -> {json.dumps(res, ensure_ascii=False)[:400]}")
        time.sleep(2)

    print("\n===== B. 假订单 query 对照（确认 query 是否校验表单存在性）=====")
    for oid in ("3317145744099072190", "1111111111111111111"):
        res = call_mtop(cookie, API_QUERY,
                        {"sceneCode": "YCH", "outerType": "ORDER", "outerId": oid})
        print(f"outerId={oid} -> ret={res.get('ret')}")
        time.sleep(2)


if __name__ == "__main__":
    main()
