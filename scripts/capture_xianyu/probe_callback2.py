"""callback 提交契约精确探测（第2轮）：
先 query 拿新鲜 callbackCode，再立刻用多种合理形态调 callback（假订单号，零风险）。
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
APP_KEY = "34839810"
QUERY_API = "mtop.taobao.heracles.inno.smart.form.query"
CALLBACK_API = "mtop.taobao.heracles.inno.smart.form.callback"


def extract_token(cookies: str) -> str:
    for kv in cookies.replace("\n", "").replace("\r", "").split("; "):
        if kv.startswith("_m_h5_tk="):
            return kv[len("_m_h5_tk="):].split("_")[0]
    return ""


def mtop_call(api: str, version: str, cookies: str, data: dict):
    import requests
    import urllib3
    urllib3.disable_warnings()
    token = extract_token(cookies)
    ts = str(int(time.time() * 1000))
    data_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    sign = generate_sign(ts, token, data_str)
    params = {
        "jsv": "2.7.2", "appKey": APP_KEY, "t": ts, "sign": sign,
        "v": version, "type": "originaljson", "accountSite": "xianyu",
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
    url = f"https://h5api.m.goofish.com/h5/{api}/{version}/"
    resp = requests.post(url, params=params, data={"data": data_str},
                         headers=headers, timeout=20, verify=False)
    try:
        return resp.json()
    except Exception:
        return {"_status": resp.status_code, "_text": resp.text[:2000]}


def main():
    conn = pymysql.connect(**DB)
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("SELECT cookie FROM xy_accounts WHERE status='active' "
                "AND account_id='2217728125233'")
    ck = cur.fetchone()["cookie"]
    cur.close()
    conn.close()

    # 第1步：新鲜 query，取最新 callbackCode
    print("########## 新鲜 query ##########")
    q = mtop_call(QUERY_API, "1.0", ck,
                  {"sceneCode": "YCH", "outerType": "ORDER",
                   "outerId": "3317147580022070667"})
    print("ret:", q.get("ret"))
    try:
        schema = json.loads(q["data"]["formSchema"]["schemaData"])
        code = schema["properties"]["acceptButton"]["props"]["callbackCode"]
        print("最新 callbackCode:", code)
        print("schema keys:", list(schema.get("properties", {}).keys()))
        print("acceptButton 完整:", json.dumps(
            schema["properties"].get("acceptButton"), ensure_ascii=False))
    except Exception as e:
        print("解析 schema 失败:", e, json.dumps(q, ensure_ascii=False)[:1500])
        return
    time.sleep(1.5)

    # 第2步：多种形态调 callback（假订单号，零风险）
    fake = "123456"
    variants = [
        ("V1 formData=字符串", {"callbackCode": code, "event": "s_init",
          "sceneCode": "YCH", "outerType": "ORDER", "outerId": fake,
          "formData": json.dumps({"concertName": "测试", "ticketCount": 1,
                                  "date": "2026-12-01"})}),
        ("V2 formData=对象", {"callbackCode": code, "event": "s_init",
          "sceneCode": "YCH", "outerType": "ORDER", "outerId": fake,
          "formData": {"concertName": "测试", "ticketCount": 1, "date": "2026-12-01"}}),
        ("V3 params 嵌套", {"callbackCode": code, "event": "s_init",
          "params": {"sceneCode": "YCH", "outerType": "ORDER", "outerId": fake,
                     "formData": {"concertName": "测试", "ticketCount": 1,
                                  "date": "2026-12-01"}}}),
        ("V4 加 formSchema 回传", {"callbackCode": code, "event": "s_init",
          "sceneCode": "YCH", "outerType": "ORDER", "outerId": fake,
          "formData": {"concertName": "测试", "ticketCount": 1, "date": "2026-12-01"},
          "formSchema": json.dumps(schema, ensure_ascii=False)}),
    ]
    for name, data in variants:
        print(f"\n########## {name} ##########")
        try:
            res = mtop_call(CALLBACK_API, "1.0", ck, data)
            print("响应:", json.dumps(res, ensure_ascii=False, indent=2)[:2500])
        except Exception as e:
            print(f"调用异常: {type(e).__name__}: {e}")
        time.sleep(1.5)


if __name__ == "__main__":
    main()
