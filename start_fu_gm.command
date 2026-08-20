#!/bin/zsh
set -u

SCRIPT_DIR="${0:A:h}"
RUNNER="$SCRIPT_DIR/scripts/run_fu_gm_http.sh"
RUNTIME_HOME="${FU_GM_RUNTIME_HOME:-$HOME/.fu-gm}"
RUNTIME_ENV="$RUNTIME_HOME/fu_gm.env"
WORKSPACE_ENV="$SCRIPT_DIR/.env"

fail() {
  print -u2 -- "FU-GM 一键启动失败：$1"
  return 1
}

open_dashboard() {
  local url="$1"
  if [[ "${FU_GM_OPEN_DASHBOARD:-1}" == "0" ]]; then
    return 0
  fi
  if command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 || true
  fi
}

is_fu_gm_healthy() {
  local health_url="$1"
  local response
  response="$(curl -fsS --connect-timeout 1 --max-time 2 "$health_url" 2>/dev/null)" || return 1
  [[ "$response" == *'"ok": true'* && "$response" == *'"service": "fu-gm"'* ]]
}

if [[ ! -x "$RUNNER" ]]; then
  fail "找不到可执行启动脚本：$RUNNER"
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  fail "系统缺少 curl，无法执行启动健康检查。"
  exit 1
fi

# 与正式 runner 保持相同配置优先级；配置文件只应包含简单 KEY=value。
if [[ -f "$RUNTIME_ENV" ]]; then
  set -a
  source "$RUNTIME_ENV"
  set +a
elif [[ -f "$WORKSPACE_ENV" ]]; then
  set -a
  source "$WORKSPACE_ENV"
  set +a
fi

HTTP_HOST="${FU_GM_HTTP_HOST:-127.0.0.1}"
HTTP_PORT="${FU_GM_HTTP_PORT:-8765}"

case "$HTTP_HOST" in
  127.0.0.1|localhost)
    ;;
  *)
    fail "当前接口没有应用层鉴权，一键入口只允许 127.0.0.1 或 localhost，实际为 $HTTP_HOST。"
    exit 1
    ;;
esac

if [[ "$HTTP_PORT" != <-> ]] || (( HTTP_PORT < 1 || HTTP_PORT > 65535 )); then
  fail "FU_GM_HTTP_PORT 必须是 1 到 65535 的整数，实际为 $HTTP_PORT。"
  exit 1
fi

if [[ -n "${FU_GM_PYTHON:-}" ]]; then
  PYTHON_EXE="$FU_GM_PYTHON"
elif [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
  PYTHON_EXE="$SCRIPT_DIR/.venv/bin/python"
elif [[ -x /usr/bin/python3 ]]; then
  PYTHON_EXE=/usr/bin/python3
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_EXE="$(command -v python3)"
else
  fail "未找到 Python 3；请先安装 Python 3.9 或更高版本。"
  exit 1
fi

if ! "$PYTHON_EXE" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  fail "需要 Python 3.9 或更高版本，当前解释器为 $PYTHON_EXE。"
  exit 1
fi

HEALTH_URL="http://$HTTP_HOST:$HTTP_PORT/health"
DASHBOARD_URL="http://$HTTP_HOST:$HTTP_PORT/dashboard"

if is_fu_gm_healthy "$HEALTH_URL"; then
  print -- "FU-GM 已在运行：$HEALTH_URL"
  print -- "正在打开管理面板：$DASHBOARD_URL"
  open_dashboard "$DASHBOARD_URL"
  exit 0
fi

if command -v lsof >/dev/null 2>&1 && \
  lsof -nP -iTCP:"$HTTP_PORT" -sTCP:LISTEN 2>/dev/null | grep -q .; then
  fail "端口 $HTTP_PORT 已被其他程序占用；请关闭占用程序或修改 FU_GM_HTTP_PORT。"
  exit 1
fi

export FU_GM_WORKSPACE_DIR="$SCRIPT_DIR"
export FU_GM_RUNTIME_HOME="$RUNTIME_HOME"
export FU_GM_FORCE_WORKSPACE_SOURCE=1
export FU_GM_PYTHON="$PYTHON_EXE"

print -- "正在启动 FU-GM：http://$HTTP_HOST:$HTTP_PORT"
if [[ "${FU_GM_OFFLINE:-0}" == "1" ]]; then
  print -- "启动模式：离线（不会调用真实模型）"
else
  print -- "启动模式：在线配置（模型可用性请在 Dashboard 查看）"
fi
print -- "服务启动后会自动打开 Dashboard；按 Ctrl+C 可停止。"
print -- ""

"$RUNNER" &
SERVER_PID=$!

stop_server() {
  trap - EXIT HUP INT TERM
  if kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap stop_server EXIT HUP INT TERM

WAIT_SECONDS="${FU_GM_LAUNCH_WAIT_SECONDS:-30}"
if [[ "$WAIT_SECONDS" != <-> ]] || (( WAIT_SECONDS < 1 || WAIT_SECONDS > 60 )); then
  WAIT_SECONDS=30
fi

deadline=$(( SECONDS + WAIT_SECONDS ))
while (( SECONDS < deadline )); do
  if is_fu_gm_healthy "$HEALTH_URL"; then
    print -- ""
    print -- "FU-GM 启动成功：$HEALTH_URL"
    print -- "管理面板：$DASHBOARD_URL"
    open_dashboard "$DASHBOARD_URL"
    wait "$SERVER_PID"
    exit $?
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    wait "$SERVER_PID"
    exit $?
  fi
  sleep 1
done

fail "服务未能在 ${WAIT_SECONDS} 秒内通过健康检查：$HEALTH_URL"
exit 1
