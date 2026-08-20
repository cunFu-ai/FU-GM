#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
WORKSPACE_DIR="${FU_GM_WORKSPACE_DIR:-${SCRIPT_DIR:h}}"
RUNTIME_HOME="${FU_GM_RUNTIME_HOME:-$HOME/.fu-gm}"
RUNTIME_ENV="$RUNTIME_HOME/fu_gm.env"
RUNTIME_SRC="$RUNTIME_HOME/src"

if [[ -f "$RUNTIME_ENV" ]]; then
  set -a
  source "$RUNTIME_ENV"
  set +a
  export FU_GM_DOTENV_PATH="$RUNTIME_ENV"
elif [[ -f "$WORKSPACE_DIR/.env" ]]; then
  set -a
  source "$WORKSPACE_DIR/.env"
  set +a
  export FU_GM_DOTENV_PATH="$WORKSPACE_DIR/.env"
fi

# 一键入口必须运行当前检出的源码，不能被部署环境中的旧值覆盖。
if [[ "${FU_GM_FORCE_WORKSPACE_SOURCE:-0}" == "1" ]]; then
  export FU_GM_USE_WORKSPACE_SOURCE=1
fi

# LaunchAgent processes may be unable to import code from macOS-protected
# folders such as Documents. Keep code, Java, Nortantis and map assets under
# one deploy root; direct development runs can opt into the workspace source.
if [[ "${FU_GM_USE_WORKSPACE_SOURCE:-0}" == "1" && -d "$WORKSPACE_DIR/src/fu_gm" ]]; then
  export FU_GM_PROJECT_DIR="$WORKSPACE_DIR"
  export PYTHONPATH="$WORKSPACE_DIR/src"
  cd "$WORKSPACE_DIR"
elif [[ -d "$RUNTIME_SRC/fu_gm" ]]; then
  export FU_GM_PROJECT_DIR="$RUNTIME_HOME"
  export FU_GM_NORTANTIS_OUTPUT_DIR="${FU_GM_NORTANTIS_OUTPUT_DIR:-$RUNTIME_HOME/data/nortantis_maps}"
  export PYTHONPATH="$RUNTIME_SRC"
  cd "$RUNTIME_HOME"
elif [[ -d "$WORKSPACE_DIR/src/fu_gm" ]]; then
  export FU_GM_PROJECT_DIR="$WORKSPACE_DIR"
  export PYTHONPATH="$WORKSPACE_DIR/src"
  cd "$WORKSPACE_DIR"
else
  echo "FU-GM source not found under $WORKSPACE_DIR/src or $RUNTIME_SRC" >&2
  exit 1
fi
export PYTHONUNBUFFERED=1

SERVER_ARGS=(
  --host "${FU_GM_HTTP_HOST:-127.0.0.1}"
  --port "${FU_GM_HTTP_PORT:-8765}"
  --data-root "${FU_GM_DATA_ROOT:-$RUNTIME_HOME/data/campaigns}"
)
if [[ "${FU_GM_OFFLINE:-0}" == "1" ]]; then
  SERVER_ARGS+=(--offline)
fi

exec "${FU_GM_PYTHON:-/usr/bin/python3}" -u -m fu_gm.http_server "${SERVER_ARGS[@]}"
