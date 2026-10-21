"""定案实验 4：query 带上 APP 深链里的上下文参数，看是否回吐预填 formData。

推理：APP 打开表单时的深链是
  fleamarket://smart_form?flutter=true&opaque=false&sceneCode=YCH
      &outerType=ORDER&outerId=<订单>&buyerId=<买家>&sellerId=<卖家>
而 needDecryptKeys = [formData.userId, formData.ownerId]，恰好对应
buyerId / sellerId。APP 大概率把这些参数透传给 query，服务端据此生成
（加密的）预填 formData，客户端提交时原样回传。

若 query 带上下文后响应里出现 formData（哪怕值是密文），我们就拿到了
"正确的 userId/ownerId 表达"，直提交即可打通——不需要自己实现加密。

本脚本只读（仅 query），不写任何订单数据。
"""
import sys
import json
import time

ROOT = "/Users/jhx/code/xy-auto-reply/xianyu-auto-reply"
sys.path.insert(0, ROOT)
sys.path.insert(0, f"{ROOT}/scripts/capture_xianyu")

from probe_real_submit import call_mtop, load_order, API_QUERY  # noqa: E402

ORDER = "3317145744099072190"
BUYER = "793357549"
SELLER = "1620778109"
TASK_ID = "fpRgKlWXpBCw"

BASE = {"sceneCode": "YCH", "outerType": "ORDER", "outerId": ORDER}

VARIANTS = [
    ("q1 基线", dict(BASE)),
    ("q2 +buyerId/sellerId", dict(BASE, buyerId=BUYER, sellerId=SELLER)),
    ("q3 +flutter/opaque（完整深链上下文）",
     dict(BASE, buyerId=BUYER, sellerId=SELLER, flutter="true", opaque="false")),
    ("q4 +taskId", dict(BASE, taskId=TASK_ID)),
    ("q5 +taskId/buyer/seller", dict(BASE, buyerId=BUYER, sellerId=SELLER,
                                     taskId=TASK_ID)),
]


def main():
    order, cookie = load_order(ORDER)
    assert order and cookie, "拿不到订单 cookie"

    for name, data in VARIANTS:
        print(f"\n{'='*70}\n{name}\n  req : {json.dumps(data, ensure_ascii=False)}")
        q = call_mtop(cookie, API_QUERY, data)
        ret = q.get("ret")
        d = q.get("data") or {}
        print(f"  ret : {json.dumps(ret, ensure_ascii=False)}")
        keys = list(d.keys())
        print(f"  keys: {keys}")
        # 关键：有没有吐出 formData
        if "formData" in d:
            print(f"  🔑 formData = {json.dumps(d['formData'], ensure_ascii=False)[:500]}")
        # schemaData 之外的任何新东西都打印
        for k in keys:
            if k in ("formSchema",):
                continue
            print(f"  · {k} = {json.dumps(d[k], ensure_ascii=False)[:300]}")
        # schemaData 里是否含默认值
        try:
            schema = json.loads(d["formSchema"]["schemaData"])
            props = schema.get("properties", {})
            print("  schema 字段默认值：")
            for fk, fv in props.items():
                if not isinstance(fv, dict):
                    continue
                interesting = {k: v for k, v in fv.items()
                               if k in ("default", "defaultValue", "value",
                                        "props", "encrypt", "encrypted")}
                if interesting:
                    print(f"    {fk}: {json.dumps(interesting, ensure_ascii=False)[:200]}")
        except Exception as e:
            print(f"  (schema 解析跳过: {e})")
        time.sleep(2.0)


if __name__ == "__main__":
    main()
