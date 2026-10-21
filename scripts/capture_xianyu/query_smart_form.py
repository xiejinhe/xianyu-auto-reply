"""一次性：调用只读 smart_form_query，拿 YCH 演出信息表单 schema + 真实提交接口名。

走 h5api.m.goofish.com 网关（与 show_info_service 同范式），只读，不改任何数据，
不触发写操作风控。

步骤：
1. 从 xy_orders 取真实 order_no + 所属 account_id
2. 从 xy_accounts 取该账号 cookie（需含 _m_h5_tk）
3. 调 mtop.taobao.heracles.inno.smart.form.query（sceneCode=YCH）
4. 打印完整响应，重点看 schema / submitApi / formKey / 字段定义
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

API_QUERY = "mtop.taobao.heracles.inno.smart.form.query"
API_VERSION = "1.0"
APP_KEY = "34839810"


def extract_token(cookies: str) -> str:
    for kv in cookies.replace("\n", "").replace("\r", "").split("; "):
        if kv.startswith("_m_h5_tk="):
            return kv[len("_m_h5_tk="):].split("_")[0]
    return ""


def call_query(cookies: str, data: dict):
    import requests
    import urllib3
    urllib3.disable_warnings()
    token = extract_token(cookies)
    ts = str(int(time.time() * 1000))
    data_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    sign = generate_sign(ts, token, data_str)
    params = {
        "jsv": "2.7.2", "appKey": APP_KEY, "t": ts, "sign": sign,
        "v": API_VERSION, "type": "originaljson", "accountSite": "xianyu",
        "dataType": "json", "timeout": "20000", "api": API_QUERY,
        "sessionOption": "AutoLoginOnly",
    }
    headers = {
        "accept": "application/json",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "content-type": "application/x-www-form-urlencoded",
        "idle_site_biz_code": "COMMONPRO",
        "origin": "https://seller.goofish.com",
        "referer": "https://seller.goofish.com/?site=COMMONPRO",
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36"),
        "cookie": cookies,
    }
    url = f"https://h5api.m.goofish.com/h5/{API_QUERY}/{API_VERSION}/"
    resp = requests.post(url, params=params, data={"data": data_str},
                         headers=headers, timeout=20, verify=False)
    try:
        return resp.json()
    except Exception:
        return {"_status": resp.status_code, "_text": resp.text[:2000]}


def main():
    conn = pymysql.connect(**DB)
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("SELECT order_no, account_id, status FROM xy_orders "
                "ORDER BY id DESC LIMIT 5")
    orders = cur.fetchall()
    cur.execute("SELECT account_id, cookie FROM xy_accounts WHERE status='active'")
    accts = {r["account_id"]: r["cookie"] for r in cur.fetchall()}
    cur.close()
    conn.close()

    print(f"取到 {len(orders)} 个订单、{len(accts)} 个活跃账号")

    # 探测 1：真实订单号 + sceneCode=YCH
    for o in orders:
        acct = o["account_id"]
        ck = accts.get(acct)
        if not ck:
            print(f"[skip] 订单 {o['order_no']} 的账号 {acct} 不在活跃账号中")
            continue
        data = {"sceneCode": "YCH", "outerType": "ORDER", "outerId": o["order_no"]}
        print(f"\n########## 订单 {o['order_no']} (status={o['status']}, acct={acct}) ##########")
        print(f"请求 data: {json.dumps(data, ensure_ascii=False)}")
        try:
            res = call_query(ck, data)
            print("响应:", json.dumps(res, ensure_ascii=False, indent=2)[:6000])
        except Exception as e:
            print(f"调用异常: {type(e).__name__}: {e}")
        break  # 先看第一个订单的响应形态，再决定后续

    # 探测 2：不带 outerId，仅 sceneCode（看 schema 是否静态可取）
    any_acct = next(iter(accts))
    print(f"\n########## 仅 sceneCode=YCH（无订单，账号 {any_acct}）##########")
    try:
        res = call_query(accts[any_acct], {"sceneCode": "YCH", "outerType": "ORDER"})
        print("响应:", json.dumps(res, ensure_ascii=False, indent=2)[:6000])
    except Exception as e:
        print(f"调用异常: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
