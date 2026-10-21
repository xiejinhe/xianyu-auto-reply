"""定案实验 3：在 query 响应里找「加密材料」。

needDecryptKeys=[formData.userId, formData.ownerId] 说明客户端必须加密上报
这两个字段。能否在 H5 通道复现，取决于服务端是否下发密钥材料：

- 若响应里带公钥/密钥标识/加密上下文（publicKey/pubKey/keyId/securityInfo/
  encryptContext/whiteBox 等）→ 有可能在 H5 侧复现加密，直提交可成；
- 若什么都不给（只有 needDecryptKeys 这个名字）→ 密钥必然在 APP 的安全
  沙箱里生成（SecurityCipher，与设备绑定），H5 侧无从下手，直提交死路。

本脚本只读（仅 query），不会写任何订单数据。
"""
import sys
import json
import re

ROOT = "/Users/jhx/code/xy-auto-reply/xianyu-auto-reply"
sys.path.insert(0, ROOT)
sys.path.insert(0, f"{ROOT}/scripts/capture_xianyu")

from probe_real_submit import call_mtop, load_order, API_QUERY  # noqa: E402

ORDER = "3317145744099072190"

# 一切可能携带密钥材料的字段名
KEY_HINTS = [
    "key", "pubkey", "publickey", "secret", "cipher", "encrypt", "decrypt",
    "sign", "token", "security", "safe", "wua", "sec", "blackbox",
    "whitebox", "device", "appkey", "salt", "iv", "rsa", "aes", "sm2",
    "sm4", "seed", "seedmap", "guard", "jaq", "sg",
]


def walk(obj, path="", hits=None):
    """深度遍历，捞出名字里带密钥暗示的字段。"""
    if hits is None:
        hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            p = f"{path}.{k}" if path else str(k)
            if any(h in kl for h in KEY_HINTS):
                s = v if isinstance(v, (str, int, float, bool)) else json.dumps(
                    v, ensure_ascii=False)
                hits.append((p, str(s)[:200]))
            walk(v, p, hits)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            walk(v, f"{path}[{i}]", hits)
    return hits


def main():
    order, cookie = load_order(ORDER)
    assert order and cookie, "拿不到订单 cookie"

    q = call_mtop(cookie, API_QUERY,
                  {"sceneCode": "YCH", "outerType": "ORDER", "outerId": ORDER})

    print("===== ret =====")
    print(json.dumps(q.get("ret"), ensure_ascii=False))

    raw = json.dumps(q, ensure_ascii=False)
    print(f"\n===== 全响应长度 {len(raw)} 字符 =====")

    # 1) 直接正则找 needDecryptKeys 上下文
    for m in re.finditer(r"[^\"]*[Dd]ecrypt[^\"]*", raw):
        print("  ·", m.group(0)[:160])

    # 2) 深度遍历找密钥暗示字段
    hits = walk(q.get("data") or {})
    print(f"\n===== 名字含密钥暗示的字段（{len(hits)} 个）=====")
    seen = set()
    for p, s in hits:
        if s in seen:
            continue
        seen.add(s)
        print(f"  {p} = {s}")

    # 3) 打印 data 顶层键全貌
    data = q.get("data") or {}
    print("\n===== data 顶层键 =====")
    for k, v in data.items():
        preview = v if isinstance(v, (str, int, float, bool, type(None))) \
            else json.dumps(v, ensure_ascii=False)
        print(f"  {k}: {str(preview)[:300]}")


if __name__ == "__main__":
    main()
