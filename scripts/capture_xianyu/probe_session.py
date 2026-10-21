"""定案实验 2：heracles 表单多段事件流假设。

C. 打印 query 完整 data（不只 formSchema，找会话/实例字段）
D. 两段式假设：先 callback(event=s_init, formData={}) 初始化会话，
   看响应是否给出新的 callbackCode / 下一步提示；有则继续提交。
E. 对照：s_init 带完整 formData 但省略 callbackCode / 换 event=空，
   观察报错差异以推断服务端校验顺序。
"""
import sys
import json
import time

ROOT = "/Users/jhx/code/xy-auto-reply/xianyu-auto-reply"
sys.path.insert(0, ROOT)
sys.path.insert(0, f"{ROOT}/scripts/capture_xianyu")

from probe_real_submit import (  # noqa: E402
    call_mtop, load_order, API_QUERY, API_CALLBACK, TASK_ID,
)

ORDER = "3317145744099072190"
NAME, COUNT, DATE = "北京张杰", 1, "2026-10-21"


def main():
    order, cookie = load_order(ORDER)
    assert order and cookie

    print("===== C. query 完整 data =====")
    q = call_mtop(cookie, API_QUERY,
                  {"sceneCode": "YCH", "outerType": "ORDER", "outerId": ORDER})
    print(json.dumps(q, ensure_ascii=False, indent=2)[:4000])

    def cb(event, form, code="uZet4GmNwP"):
        data_val = {
            "callbackCode": code, "event": event,
            "sceneCode": "YCH", "outerType": "ORDER", "outerId": ORDER,
            "formData": json.dumps(form, ensure_ascii=False, separators=(",", ":")),
        }
        return call_mtop(cookie, API_CALLBACK, data_val)

    print("\n===== D1. callback(event=s_init, formData={}) 纯初始化 =====")
    res = cb("s_init", {})
    print(json.dumps(res, ensure_ascii=False)[:1200])
    time.sleep(2.5)

    print("\n===== D2. 初始化后立刻带数据提交 =====")
    form = {"concertName": NAME, "ticketCount": COUNT, "date": DATE}
    res2 = cb("s_init", form)
    print(json.dumps(res2, ensure_ascii=False)[:1200])
    if any("SUCCESS" in r for r in res2.get("ret", [])) and \
            (res2.get("data") or {}).get("success"):
        print("\n✅✅ 两段式提交成功！")
        return
    time.sleep(2.5)

    print("\n===== E. event=空串（观察校验顺序）=====")
    print(json.dumps(cb("", form), ensure_ascii=False)[:600])


if __name__ == "__main__":
    main()
