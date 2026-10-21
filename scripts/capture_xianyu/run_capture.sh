#!/usr/bin/env bash
# run_capture.sh — 连接手机并抓取闲鱼「填写演出信息」提交接口
#
# 前置（见 setup_capture.sh）：frida-tools 已装 + 手机已 root 并运行 frida-server
#
# 用法：
#   bash scripts/capture_xianyu/run_capture.sh
#
# 跑起来后：
#   1. 脚本会拉起闲鱼（spawn），等 App 完全启动。
#   2. 你在闲鱼里走一次：买家已付款的门票订单 → 我填写演出信息 → 填完提交。
#   3. 终端会打印 [CAP]/[MTOP-REQ] 标记的报文，命中演出信息流量。
#   4. Ctrl+C 结束，结果已保存到 capture_out.txt。
#   5. 把 capture_out.txt 里 api= 那行和请求/响应 JSON 贴给开发者即可。
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HERE/.venv"
FRIDA="$VENV/bin/frida"

if [ ! -x "$FRIDA" ]; then
  echo "✗ 未找到 frida，请先运行: bash scripts/capture_xianyu/setup_capture.sh"
  exit 1
fi

echo "==> 检查 adb 设备 ..."
if ! adb devices | grep -qE 'device$'; then
  echo "✗ 没有已连接的安卓设备。请 USB 连接手机并开启 USB 调试。"
  exit 1
fi
adb devices

PKG="${1:-com.taobao.idlefish}"
OUT="$HERE/capture_out.txt"
echo "==> 拉起 $PKG 并注入抓包脚本，输出保存到 $OUT"
echo "==> 闲鱼启动后，在 App 内走一次「填写演出信息 → 提交」，然后 Ctrl+C 结束。"
echo

# -f  spawn 启动，-l 注入脚本，--no-pause 启动即跑；tee 落盘
"$FRIDA" -U -f "$PKG" -l "$HERE/capture_performance.js" --no-pause 2>&1 | tee "$OUT"

echo
echo "==> 抓包结束，结果在: $OUT"
echo "==> 请把其中 [MTOP-REQ] api=... 以及带 concertName/ticketCount 的报文发给开发者。"
