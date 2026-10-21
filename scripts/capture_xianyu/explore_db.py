"""一次性只读探查：找 YCH(演出)订单 + 其所属账号 cookie。

只做 SELECT，不改任何数据。
"""
import pymysql
import json

DB = dict(host="127.0.0.1", port=3306, user="xianyu", password="xianyu@2026",
          database="xianyu_data", charset="utf8mb4", connect_timeout=10)

conn = pymysql.connect(**DB)
cur = conn.cursor(pymysql.cursors.DictCursor)

print("=== xy_orders 行数 ===")
cur.execute("SELECT COUNT(*) AS c FROM xy_orders")
print(cur.fetchone())

print("\n=== xy_orders 各 status 计数 ===")
cur.execute("SELECT status, COUNT(*) AS c FROM xy_orders GROUP BY status ORDER BY c DESC")
for r in cur.fetchall():
    print(f"  {r['status']!r}: {r['c']}")

print("\n=== 含『演出/门票/YCH』线索的订单(任意字段) 前20 ===")
cur.execute("""
SELECT id, order_no, status, account_id, buyer_nick, item_snapshot, metadata
FROM xy_orders
WHERE status LIKE '%演%' OR status LIKE '%票%' OR status LIKE '%YCH%'
   OR metadata LIKE '%演%' OR metadata LIKE '%票%' OR metadata LIKE '%YCH%'
   OR item_snapshot LIKE '%演%' OR item_snapshot LIKE '%票%'
LIMIT 20
""")
rows = cur.fetchall()
for r in rows:
    print(f"  id={r['id']} order_no={r['order_no']} status={r['status']!r} acct={r['account_id']} buyer={r['buyer_nick']}")

print(f"\n=== 命中线索订单数: {len(rows)} ===")

print("\n=== 活跃账号(取 cookie 概要) ===")
cur.execute("SELECT account_id, status, LENGTH(cookie) AS cklen, "
            "LOCATE('_m_h5_tk', cookie) AS has_tk FROM xy_accounts "
            "WHERE status='active' ORDER BY cklen DESC LIMIT 10")
for r in cur.fetchall():
    print(f"  acct={r['account_id']} status={r['status']} cklen={r['cklen']} has_m_h5_tk={'是' if r['has_tk'] else '否'}")

cur.close()
conn.close()
print("\nDONE")
