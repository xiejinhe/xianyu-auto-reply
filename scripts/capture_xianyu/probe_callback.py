"""探测 mtop.taobao.heracles.inno.smart.form.callback 的参数契约（只做安全探测）。

原则：
- 空参 / 假订单号 → 服务端必然参数校验失败，不触发任何业务写动作
- 从报错逐步反推必填字段与形态
- 绝不对真实订单提交真实表单数据（那一步必须用户确认）
"""
import sys
import json
import time
import pymysql

ROOT = "/Users/jhx/code/xy-auto-reply/xianyu-auto-reply"
sys.path.insert(0, ROOT)
from common.utils.xianyu_utils import generate_sign  # noqa: E402

DB = dict(host="127.0.0.1", port=3306, user="xianyu", password="xianyu@2026",
          database="xianyu_data", charset="utf8mb4", connect_timeout=10)

API = "mtop.taobao.heracles.inno.smart.form.callback"
VERSION = "1.0"
APP_KEY = "34839810"
CALLBACK_CODE = "uZet4GmNwP"  # 来自 query 响应 acceptButton.props


def extract_token(cookies: str) -> str:
    for kv in cookies.replace("\n", "").replace("\r", "").split("; "):
        if kv.startswith("_m_h5_tk="):
            return kv[len("_m_h5_tk="):].split("_")[0]
    return ""


def call_callback(cookies: str, data: dict):
    import requests
    import urllib3
    urllib3.disable_warnings()
    token = extract_token(cookies)
    ts = str(int(time.time() * 1000))
    data_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    sign = generate_sign(ts, token, data_str)
    params = {
        "jsv": "2.7.2", "appKey": APP_KEY, "t": ts, "sign": sign,
        "v": VERSION, "type": "originaljson", "accountSite": "xianyu",
        "dataType": "json", "timeout": "20000", "api": API,
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
    url = f"https://h5api.m.goofish.com/h5/{API}/{VERSION}/"
    resp = requests.post(url, params=params, data={"data": data_str},
                         headers=headers, timeout=20, verify=False)
    try:
        return resp.json()
    except Exception:
        return {"_status": resp.status_code, "_text": resp.text[:2000]}


def main():
    conn = pymysql.connect(**DB)
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("SELECT account_id, cookie FROM xy_accounts WHERE status='active' "
                "AND account_id='2217728125233'")
    row = cur.fetchone()
    cur.close()
    conn.close()
    ck = row["cookie"]
    real_order = "3317147580022070667"  # pending_ship 订单（只用于「不带表单数据」的空探测）

    probes = [
        ("P1 空参", {}),
        ("P2 假订单+callbackCode+event",
         {"callbackCode": CALLBACK_CODE, "event": "s_init",
          "sceneCode": "YCH", "outerType": "ORDER", "outerId": "123456"}),
        ("P3 真订单+event 但不带 formData（预期校验报错，无写动作）",
         {"callbackCode": CALLBACK_CODE, "event": "s_init",
          "sceneCode": "YCH", "outerType": "ORDER", "outerId": real_order}),
        ("P4 假订单+formData 完整形态试探",
         {"callbackCode": CALLBACK_CODE, "event": "s_init",
          "sceneCode": "YCH", "outerType": "ORDER", "outerId": "123456",
          "formData": json.dumps({"concertName": "测试", "ticketCount": 1,
                                  "date": "2026-12-01"}, ensure_ascii=False)}),
    ]
    for name, data in probes:
        print(f"\n########## {name} ##########")
        print("请求:", json.dumps(data, ensure_ascii=False))
        try:
            res = call_callback(ck, data)
            print("响应:", json.dumps(res, ensure_ascii=False, indent=2)[:3000])
        except Exception as e:
            print(f"调用异常: {type(e).__name__}: {e}")
        time.sleep(1.2)  # 控频


if __name__ == "__main__":
    main()
