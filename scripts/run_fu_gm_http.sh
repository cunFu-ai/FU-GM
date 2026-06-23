#!/bin/zsh
set -euo pipefail

PROJECT_DIR="/Users/example/Documents/New project"
RUNTIME_ENV="/Users/example/.fu-gm/fu_gm.env"
RUNTIME_SRC="/Users/example/.fu-gm/src"

if [[ -f "$RUNTIME_ENV" ]]; then
  set -a
  source "$RUNTIME_ENV"
  set +a
  export FU_GM_DOTENV_PATH="$RUNTIME_ENV"
elif [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a
  source "$PROJECT_DIR/.env"
  set +a
  export FU_GM_DOTENV_PATH="$PROJECT_DIR/.env"
fi

cd "$PROJECT_DIR"

if [[ -d "$RUNTIME_SRC/fu_gm" ]]; then
  export PYTHONPATH="$RUNTIME_SRC"
else
  export PYTHONPATH="$PROJECT_DIR/src"
fi
export PYTHONUNBUFFERED=1

exec /usr/bin/python3 -u -m fu_gm.http_server \
  --host "${FU_GM_HTTP_HOST:-127.0.0.1}" \
  --port "${FU_GM_HTTP_PORT:-8765}" \
  --data-root "${FU_GM_DATA_ROOT:-/Users/example/.fu-gm/data/campaigns}"
