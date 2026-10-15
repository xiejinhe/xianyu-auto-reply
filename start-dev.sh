#!/usr/bin/env bash
# 本地非 Docker 开发一键脚本：Docker 仅作 MySQL/Redis 基础设施，四个应用服务在宿主机跑源码。
# 用法:
#   ./start-dev.sh          # 启动（已在运行的自动跳过）
#   ./start-dev.sh stop     # 停止四个应用服务（保留 MySQL/Redis 基础设施）
#   ./start-dev.sh status   # 查看运行状态
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE="${ROOT}/docker-compose.deploy.yml"
OVERRIDE="${ROOT}/docker-compose.dev.yml"

# ---- 应用服务定义: 名称 | 端口 | 目录 | 日志 | 启动命令 ----
BACKENDS=(
  "backend-web 8089 backend-web /tmp/bw.log .venv/bin/python main.py"
  "websocket 8090 websocket /tmp/ws.log .venv/bin/python main.py"
  "scheduler 8091 scheduler /tmp/sch.log .venv/bin/python main.py"
)
FRONTEND="frontend 9000 frontend /tmp/fe.log npm run dev"

is_up() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

status() {
  echo ">> 基础设施 (Docker)"
  docker ps --format 'table {{.Names}}\t{{.Status}}' 2>/dev/null | grep -E 'xianyu-(mysql|redis)|(NAMES)' || echo "   (Docker 未运行)"
  echo ""
  echo ">> 应用服务"
  for s in "${BACKENDS[@]}" "${FRONTEND}"; do
    read -r name port _dir _log _cmd <<<"$s"
    if is_up "$port"; then
      echo "   [运行中] ${name}  :${port}"
    else
      echo "   [已停止] ${name}  :${port}"
    fi
  done
}

ensure_infra() {
  echo ">> 检查 MySQL/Redis 基础设施..."
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx xianyu-mysql && \
     docker ps --format '{{.Names}}' 2>/dev/null | grep -qx xianyu-redis; then
    echo "   已运行 (3306/6379 已暴露)"
    return 0
  fi
  echo "   启动中 (含端口映射)..."
  docker compose -f "$COMPOSE" -f "$OVERRIDE" --env-file "$ROOT/.env" up -d mysql redis
}

start_one() {
  local name="$1" port="$2" dir="$3" log="$4" cmd="$5"
  if is_up "$port"; then
    echo "   [已运行，跳过] ${name}  :${port}"
    return 0
  fi
  ( cd "$ROOT/$dir" && nohup $cmd >"$log" 2>&1 & disown )
  echo "   [已启动] ${name}  :${port}  日志: $log"
}

start() {
  ensure_infra
  echo ">> 启动应用服务"
  for s in "${BACKENDS[@]}"; do read -r name port dir log cmd <<<"$s"; start_one "$name" "$port" "$dir" "$log" "$cmd"; done
  read -r name port dir log cmd <<<"$FRONTEND"; start_one "$name" "$port" "$dir" "$log" "$cmd"

  echo ""
  echo ">> 等待就绪..."
  sleep 6
  ok=1
  for s in "${BACKENDS[@]}" "$FRONTEND"; do
    read -r name port _dir _log _cmd <<<"$s"
    if is_up "$port"; then echo "   ✔ ${name}  :${port}"; else echo "   ✘ ${name}  :${port} (未就绪，查看日志)"; ok=0; fi
  done
  echo ""
  local bweb=0
  if is_up 8089; then bweb=1; fi
  echo "--------------------"
  echo "前端:     http://localhost:9000"
  echo "API 文档: http://localhost:8089/docs"
  echo "登录账号: admin / admin123"
  if [ "$ok" -eq 1 ]; then
    echo "全部服务已就绪 ✔"
  else
    echo "有服务未就绪，请检查：tail -f /tmp/{bw,ws,sch,fe}.log"
  fi
}

stop() {
  echo ">> 停止四个应用服务 (保留 MySQL/Redis 基础设施)"
  for s in "${BACKENDS[@]}"; do
    read -r name port _dir log _cmd <<<"$s"
    if is_up "$port"; then
      local pid; pid="$(lsof -tnP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -n1)"
      if [ -n "$pid" ]; then kill "$pid" 2>/dev/null && echo "   已停止 ${name}  :${port} (pid $pid)"; fi
    else
      echo "   [未运行] ${name}  :${port}"
    fi
  done
  read -r name port _dir log _cmd <<<"$FRONTEND"
  if is_up "$port"; then
    pids="$(lsof -tnP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null)"
    [ -n "$pids" ] && kill $pids 2>/dev/null; echo "   已停止 ${name}  :${port}"
  else
    echo "   [未运行] ${name}  :${port}"
  fi
  echo "已停止。如需同时关闭基础设施: docker compose -f $COMPOSE down"
}

case "${1:-start}" in
  start)  start ;;
  stop)   stop ;;
  status) status ;;
  restart) stop; echo ""; start ;;
  *) echo "用法: $0 [start|stop|restart|status]" ;;
esac