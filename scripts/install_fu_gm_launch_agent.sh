#!/bin/zsh
set -euo pipefail

PROJECT_DIR="/Users/example/Documents/New project"
RUNTIME_HOME="/Users/example/.fu-gm"
LAUNCH_AGENT_SRC="$PROJECT_DIR/launch_agents/com.fugm.http.plist"
LAUNCH_AGENT_DST="/Users/example/Library/LaunchAgents/com.fugm.http.plist"
SERVICE_LABEL="gui/$(id -u)/com.fugm.http"

cd "$PROJECT_DIR"

mkdir -p "$RUNTIME_HOME/src" "$RUNTIME_HOME/data/campaigns" "$PROJECT_DIR/logs" "/Users/example/Library/LaunchAgents"

if [[ -f ".env" ]]; then
  cp ".env" "$RUNTIME_HOME/fu_gm.env"
  chmod 600 "$RUNTIME_HOME/fu_gm.env"
fi

rm -rf "$RUNTIME_HOME/src/fu_gm"
cp -R "$PROJECT_DIR/src/fu_gm" "$RUNTIME_HOME/src/fu_gm"

cp "$PROJECT_DIR/scripts/run_fu_gm_http.sh" "$RUNTIME_HOME/run_fu_gm_http.sh"
chmod +x "$RUNTIME_HOME/run_fu_gm_http.sh"

plutil -lint "$LAUNCH_AGENT_SRC"
cp "$LAUNCH_AGENT_SRC" "$LAUNCH_AGENT_DST"

launchctl bootout "gui/$(id -u)" "$LAUNCH_AGENT_DST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENT_DST"
launchctl kickstart -k "$SERVICE_LABEL"

sleep 2
curl -fsS "http://127.0.0.1:${FU_GM_HTTP_PORT:-8765}/health"
echo
echo "FU-GM HTTP 服务已安装并启动：$SERVICE_LABEL"
