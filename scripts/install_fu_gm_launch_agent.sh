#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_HOME="${FU_GM_RUNTIME_HOME:-$HOME/.fu-gm}"
LAUNCH_AGENT_SRC="$PROJECT_DIR/launch_agents/com.fugm.http.plist"
LAUNCH_AGENT_DST="$HOME/Library/LaunchAgents/com.fugm.http.plist"
SERVICE_LABEL="gui/$(id -u)/com.fugm.http"

cd "$PROJECT_DIR"

mkdir -p \
  "$RUNTIME_HOME/src" \
  "$RUNTIME_HOME/config/gm_styles" \
  "$RUNTIME_HOME/data/campaigns" \
  "$RUNTIME_HOME/data/nortantis_maps" \
  "$RUNTIME_HOME/integrations/nortantis/build/libs" \
  "$RUNTIME_HOME/assets/nortantis_custom" \
  "$RUNTIME_HOME/assets/fonts" \
  "$RUNTIME_HOME/.runtime/jdks" \
  "$RUNTIME_HOME/logs" \
  "$HOME/Library/LaunchAgents"

if [[ -f ".env" && ( ! -f "$RUNTIME_HOME/fu_gm.env" || "${FU_GM_REFRESH_ENV:-0}" == "1" ) ]]; then
  cp ".env" "$RUNTIME_HOME/fu_gm.env"
  chmod 600 "$RUNTIME_HOME/fu_gm.env"
fi

rm -rf "$RUNTIME_HOME/src/fu_gm"
cp -R "$PROJECT_DIR/src/fu_gm" "$RUNTIME_HOME/src/fu_gm"
find "$RUNTIME_HOME/src/fu_gm" -type d -name __pycache__ -prune -exec rm -rf {} +

cp "$PROJECT_DIR/scripts/run_fu_gm_http.sh" "$RUNTIME_HOME/run_fu_gm_http.sh"
chmod +x "$RUNTIME_HOME/run_fu_gm_http.sh"
cp \
  "$PROJECT_DIR/config/gm_styles/acg_highschool_gm.md" \
  "$RUNTIME_HOME/config/gm_styles/acg_highschool_gm.md"

# LaunchAgent cannot reliably traverse a workspace inside Documents. Deploy
# every resource required by the map renderer beside the service itself.
NORTANTIS_JAR="$PROJECT_DIR/integrations/nortantis/build/libs/Nortantis.jar"
if [[ -f "$NORTANTIS_JAR" ]]; then
  cp "$NORTANTIS_JAR" "$RUNTIME_HOME/integrations/nortantis/build/libs/Nortantis.jar"
else
  echo "Warning: Nortantis.jar was not found; map generation will be unavailable." >&2
fi

rsync -a --delete \
  "$PROJECT_DIR/assets/nortantis_custom/" \
  "$RUNTIME_HOME/assets/nortantis_custom/"
rsync -a --delete \
  "$PROJECT_DIR/assets/fonts/" \
  "$RUNTIME_HOME/assets/fonts/"

JDK_JAVA="$(find "$PROJECT_DIR/.runtime/jdks" -type f -path '*/bin/java' -print -quit 2>/dev/null || true)"
if [[ -n "$JDK_JAVA" ]]; then
  case "$JDK_JAVA" in
    */Contents/Home/bin/java)
      JDK_ROOT="${JDK_JAVA%/Contents/Home/bin/java}"
      ;;
    */bin/java)
      JDK_ROOT="${JDK_JAVA%/bin/java}"
      ;;
  esac
  JDK_TARGET="$RUNTIME_HOME/.runtime/jdks/$(basename "$JDK_ROOT")"
  mkdir -p "$JDK_TARGET"
  rsync -a --delete "$JDK_ROOT/" "$JDK_TARGET/"
else
  echo "Warning: bundled JDK was not found; map generation requires a usable system Java." >&2
fi

cp "$LAUNCH_AGENT_SRC" "$LAUNCH_AGENT_DST"
plutil -replace ProgramArguments.1 -string "$RUNTIME_HOME/run_fu_gm_http.sh" "$LAUNCH_AGENT_DST"
plutil -replace WorkingDirectory -string "$RUNTIME_HOME" "$LAUNCH_AGENT_DST"
plutil -replace StandardOutPath -string "$RUNTIME_HOME/logs/fu_gm_http_server.launchd.log" "$LAUNCH_AGENT_DST"
plutil -replace StandardErrorPath -string "$RUNTIME_HOME/logs/fu_gm_http_server.launchd.err.log" "$LAUNCH_AGENT_DST"
plutil -replace EnvironmentVariables.PYTHONPATH -string "$RUNTIME_HOME/src" "$LAUNCH_AGENT_DST"
plutil -lint "$LAUNCH_AGENT_DST"

launchctl bootout "gui/$(id -u)" "$LAUNCH_AGENT_DST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENT_DST"
launchctl kickstart -k "$SERVICE_LABEL"

health_url="http://127.0.0.1:${FU_GM_HTTP_PORT:-8765}/health"
for attempt in {1..30}; do
  if curl -fsS --max-time 2 "$health_url"; then
    echo
    echo "FU-GM HTTP 服务已安装并启动：$SERVICE_LABEL"
    exit 0
  fi
  sleep 1
done

echo
echo "FU-GM HTTP 服务未能在 30 秒内通过健康检查：$health_url" >&2
exit 1
