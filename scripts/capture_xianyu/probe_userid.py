"""定案实验 3：formData 注入 userId/ownerId（query 的 needDecryptKeys 揭示的隐藏必填字段）。

F1 明文 int（卖家/卖家）  F2 明文 str  F3 userId=买家  F4 base64
"""
import sys
import json
import time
import base64

ROOT = "/Users/jhx/code/xy-auto-reply/xianyu-auto-reply"
sys.path.insert(0, ROOT)
sys.path.insert(0, f"{ROOT}/scripts/capture_xianyu")

from probe_real_submit import call_mtop, load_order, API_CALLBACK  # noqa: E402

ORDER = "3317145744099072190"
NAME, COUNT, DATE = "北京张杰", 1, "2026-10-21"
BUYER, SELLER = "793357549", "1620778109"


def main():
    order, cookie = load_order(ORDER)
    assert order and cookie

    def cb(form, label):
        data_val = {
            "callbackCode": "uZet4GmNwP", "event": "s_init",
            "sceneCode": "YCH", "outerType": "ORDER", "outerId": ORDER,
            "formData": json.dumps(form, ensure_ascii=False, separators=(",", ":")),
        }
        res = call_mtop(cookie, API_CALLBACK, data_val)
        ok = any("SUCCESS" in r for r in res.get("ret", [])) and \
            (res.get("data") or {}).get("success")
        print(f"{label}: ret={res.get('ret')} success={((res.get('data') or {}).get('success'))}")
        if not ok:
            print(f"   完整: {json.dumps(res, ensure_ascii=False)[:600]}")
        time.sleep(2.5)
        return ok

    base = {"concertName": NAME, "ticketCount": COUNT, "date": DATE}
    variants = [
        ("F1 userId/ownerId=卖家(int)", {**base, "userId": int(SELLER), "ownerId": int(SELLER)}),
        ("F2 userId/ownerId=卖家(str)", {**base, "userId": SELLER, "ownerId": SELLER}),
        ("F3 userId=买家 ownerId=卖家", {**base, "userId": BUYER, "ownerId": SELLER}),
        ("F4 base64(卖家id)", {**base,
                               "userId": base64.b64encode(SELLER.encode()).decode(),
                               "ownerId": base64.b64encode(SELLER.encode()).decode()}),
    ]
    for label, form in variants:
        if cb(form, label):
            print(f"\n✅✅ {label} 成功！formData 必须含 userId/ownerId。")
            return
    print("\n仍未成功——下一步研究 needDecrypt 的加密方式（可能要真机抓包）")


if __name__ == "__main__":
    main()
