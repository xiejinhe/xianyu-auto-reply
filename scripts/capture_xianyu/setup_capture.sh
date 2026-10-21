#!/usr/bin/env bash
# setup_capture.sh — 安装闲鱼演出信息抓包所需工具（frida-tools）
#
# 只需在【你的 Mac】上跑一次（本沙箱无外网，请在本地终端执行）：
#   bash scripts/capture_xianyu/setup_capture.sh
#
# 它会：
#   1. 在 scripts/capture_xianyu/.venv 建独立 Python 环境（不污染项目）
#   2. 安装 frida-tools
#   3. 提示你下载并部署 frida-server 到手机（抓包必须）
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HERE/.venv"
PY="${VENV}/bin/python"

echo "==> 使用系统 python3 创建独立 venv: $VENV"
if [ ! -x "$PY" ]; then
  python3 -m venv "$VENV"
fi

echo "==> 安装 frida-tools ..."
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install frida-tools

FRIDA_VER="$("$PY" -m pip show frida 2>/dev/null | awk -F': ' '/^Version/{print $2}')"
echo "==> 已安装 frida 版本: $FRIDA_VER"

echo
echo "============================================================"
echo "下一步（一次性）：在手机上部署 frida-server"
echo "============================================================"
echo "1) 确认手机已 root，并已开启「USB 调试」。"
echo "2) 下载与上面 frida 版本一致的 frida-server（Android arm64）："
echo "   https://github.com/frida/frida/releases/tag/${FRIDA_VER}"
echo "   例如: frida-server-${FRIDA_VER}-android-arm64.xz"
echo "3) 推送到手机并以 root 运行："
echo "   adb push frida-server /data/local/tmp/"
echo "   adb shell 'chmod 755 /data/local/tmp/frida-server'"
echo "   adb shell 'su -c /data/local/tmp/frida-server &'"
echo "4) 验证: adb shell 'su -c frida-ps -U' 能列出进程即成功。"
echo
echo "部署好后，运行: bash scripts/capture_xianyu/run_capture.sh"
