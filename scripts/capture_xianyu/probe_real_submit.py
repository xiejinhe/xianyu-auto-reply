"""真实「待填写演出信息」订单上的 callback 受控实验。

背景：v1.0.60 的两步协议（query→callback）在假订单上验证过契约，
但真实订单首跑报 FAIL_BIZ_SYSTEM_EXCEPTION::小鱼快要被挤坏啦。
heracles 对参数问题的报错没有细粒度文案，只能按变量矩阵实测。

流程：
1. query 拉真实 schema（只读），完整打印字段/按钮 props
2. 按「一次只改一个变量」依次尝试 callback：
   v1 ticketCount=int（当前实现，复现失败）
   v2 ticketCount=str
   v3 ticketCount=str + date 补齐成 schema 默认格式
   （后续变体按 schema 线索再扩展）

注意：callback 是写操作。任一变体成功 = 真实提交成功（正是卖家想要的
数据），立即停止；失败则继续下一个变体。每次间隔 2.5s 防风控。

用法：websocket/.venv/bin/python scripts/capture_xianyu/probe_real_submit.py \
      --order 3317145744099072190 --name 北京张杰 --count 1 --date 2026-10-21
"""
import sys
import json
import time
import argparse

ROOT = "/Users/jhx/code/xy-auto-reply/xianyu-auto-reply"
sys.path.insert(0, ROOT)

import pymysql  # noqa: E402
from common.utils.xianyu_utils import generate_sign  # noqa: E402

DB = dict(host="127.0.0.1", port=3306, user="xianyu", password="xianyu@2026",
          database="xianyu_data", charset="utf8mb4", connect_timeout=10)

API_QUERY = "mtop.taobao.heracles.inno.smart.form.query"
API_CALLBACK = "mtop.taobao.heracles.inno.smart.form.callback"
API_VERSION = "1.0"
APP_KEY = "34839810"

# 该订单「请填写演出信息」卡片 bizTag 里的任务号（sourceId=LTC_SHOW:<task_id>）
TASK_ID = "fpRgKlWXpBCw"


def extract_token(cookies: str) -> str:
    for kv in cookies.replace("\n", "").replace("\r", "").split("; "):
        if kv.startswith("_m_h5_tk="):
            return kv[len("_m_h5_tk="):].split("_")[0]
    return ""


def call_mtop(cookies: str, api: str, data: dict):
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
        "dataType": "json", "timeout": "20000", "api": api,
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
    url = f"https://h5api.m.goofish.com/h5/{api}/{API_VERSION}/"
    resp = requests.post(url, params=params, data={"data": data_str},
                         headers=headers, timeout=20, verify=False)
    try:
        return resp.json()
    except Exception:
        return {"_status": resp.status_code, "_text": resp.text[:2000]}


def load_order(order_no: str):
    conn = pymysql.connect(**DB)
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("SELECT order_no, account_id, status FROM xy_orders WHERE order_no=%s",
                (order_no,))
    order = cur.fetchone()
    acct_cookie = None
    if order:
        cur.execute("SELECT cookie FROM xy_accounts WHERE account_id=%s",
                    (order["account_id"],))
        row = cur.fetchone()
        acct_cookie = (row or {}).get("cookie")
    cur.close()
    conn.close()
    return order, acct_cookie


def walk_buttons(node, path=""):
    """递归打印 schema 里所有 button/props，找 event 与 callbackCode"""
    found = []
    if isinstance(node, dict):
        if "event" in node and ("callbackCode" in node or "callback" in node):
            found.append((path, node))
        for k, v in node.items():
            found.extend(walk_buttons(v, f"{path}.{k}" if path else k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found.extend(walk_buttons(v, f"{path}[{i}]"))
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--order", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--count", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--buyer", default="")
    ap.add_argument("--seller", default="")
    ap.add_argument("--task-id", dest="task_id", default="")
    args = ap.parse_args()

    order, cookie = load_order(args.order)
    if not order or not cookie:
        print(f"订单或 cookie 未找到: {args.order}")
        return
    print(f"订单 {order['order_no']} status={order['status']} acct={order['account_id']}")

    # 第一步：query 拉真实 schema
    q_data = {"sceneCode": "YCH", "outerType": "ORDER", "outerId": args.order}
    q_res = call_mtop(cookie, API_QUERY, q_data)
    ret = q_res.get("ret", [])
    print(f"\n===== query ret={ret} =====")
    if not any("SUCCESS" in r for r in ret):
        print("query 失败，无法继续:", json.dumps(q_res, ensure_ascii=False)[:2000])
        return
    schema_str = ((q_res.get("data") or {}).get("formSchema") or {}).get("schemaData") or ""
    try:
        schema = json.loads(schema_str)
    except Exception:
        schema = {}
    print("===== schema 完整内容 =====")
    print(json.dumps(schema, ensure_ascii=False, indent=2)[:8000])
    print("===== schema 中的按钮/props =====")
    for path, node in walk_buttons(schema):
        print(f"  {path}: {json.dumps(node, ensure_ascii=False)}")

    # 取 acceptButton 的 event/callbackCode（真实 schema 里 acceptButton 在 properties 下）
    props = {}
    accept = schema.get("acceptButton")
    if isinstance(accept, dict) and isinstance(accept.get("props"), dict):
        props = accept["props"]
    else:
        props = ((schema.get("properties") or {}).get("acceptButton") or {}).get("props") or {}
    callback_code = str(props.get("callbackCode") or "")
    event = str(props.get("event") or "s_init")
    print(f"\ncallbackCode={callback_code}  event={event}")
    if not callback_code:
        print("schema 无 callbackCode，停止")
        return

    # 第二步：变量矩阵（IM 卡片深链带 buyerId/sellerId，bizTag 带 taskId，
    # callback 很可能需要任务/双方上下文——按「加字段」方向逐组试）
    count_val = args.count.strip()
    base_form = {"concertName": args.name, "ticketCount": int(count_val), "date": args.date}
    str_form = {"concertName": args.name, "ticketCount": count_val, "date": args.date}
    ctx = {"taskId": TASK_ID, "buyerId": args.buyer, "sellerId": args.seller}
    variants = [
        ("v1 ticketCount=int(当前实现)", {}, base_form),
        ("v2 ticketCount=str", {}, str_form),
        ("v3 +taskId(data层)", {"taskId": TASK_ID}, base_form),
        ("v4 +buyerId+sellerId(data层)",
         {"buyerId": args.buyer, "sellerId": args.seller}, base_form),
        ("v5 +taskId+buyerId+sellerId(data层)", ctx, base_form),
        ("v6 上下文放formData里", ctx, {**base_form, **ctx}),
        ("v7 sourceId全值+双方", {"sourceId": f"LTC_SHOW:{TASK_ID}",
                                  "buyerId": args.buyer, "sellerId": args.seller}, base_form),
        ("v8 全上下文+event=s_submit", {**ctx, "event": "s_submit"}, base_form),
    ]

    for label, extra_data, form_data in variants:
        data_val = {
            "callbackCode": callback_code,
            "event": event,
            "sceneCode": "YCH",
            "outerType": "ORDER",
            "outerId": args.order,
            "formData": json.dumps(form_data, ensure_ascii=False, separators=(",", ":")),
        }
        data_val.update({k: v for k, v in extra_data.items() if k != "event"})
        use_event = extra_data.get("event", event)
        data_val["event"] = use_event
        print(f"\n===== {label} =====")
        print(f"data: {json.dumps(data_val, ensure_ascii=False)[:400]}")
        res = call_mtop(cookie, API_CALLBACK, data_val)
        print("响应:", json.dumps(res, ensure_ascii=False)[:1500])
        r = res.get("ret", [])
        if any("SUCCESS" in x for x in r) and (res.get("data") or {}).get("success"):
            print(f"\n✅✅ {label} 提交成功！协议定案。")
            return
        time.sleep(2.5)

    # 第三步：query 也带上 buyerId/sellerId，看 schema/callbackCode 是否变化
    print("\n===== q2 query 带 buyerId/sellerId =====")
    q2_data = {"sceneCode": "YCH", "outerType": "ORDER", "outerId": args.order,
               "buyerId": args.buyer, "sellerId": args.seller, "taskId": TASK_ID}
    q2 = call_mtop(cookie, API_QUERY, q2_data)
    print("ret:", q2.get("ret"))
    try:
        s2 = json.loads(((q2.get("data") or {}).get("formSchema") or {}).get("schemaData") or "{}")
        p2 = ((s2.get("properties") or {}).get("acceptButton") or {}).get("props") or {}
        print("q2 acceptButton.props:", json.dumps(p2, ensure_ascii=False))
        print("q2 与 q1 props 相同:" , p2 == props)
    except Exception as e:
        print("q2 schema 解析失败:", e)

    print("\n所有变体均失败——需要新线索（看上面 schema 是否有隐藏字段/其它按钮）")


if __name__ == "__main__":
    main()
