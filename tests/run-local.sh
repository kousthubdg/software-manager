#!/bin/bash
# Run Software Manager from source (HTTP, throwaway state) and optionally exercise the release flow.
#   run-local.sh [--fresh] [serve-only]
# env: SM_TEST_ROOT (state, default $HOME/sm-test), SM_TEST_PORT (12999), SM_TEST_PW, PY (python with requirements),
#      SM_RELEASE_SOURCE (release path, read only), REGISTRY / REG_USER / REG_PASSWORD_FILE / REG_CA (non serve-only)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SM_SRC="$(cd "$HERE/../src" && pwd)"
ROOT=${SM_TEST_ROOT:-$HOME/sm-test}
PORT=${SM_TEST_PORT:-12999}
PY=${PY:-python3}
fresh=0; serve_only=0
for a in "$@"; do
  [[ "$a" == "--fresh" ]] && fresh=1
  [[ "$a" == "serve-only" ]] && serve_only=1
done
[[ $fresh -eq 1 ]] && rm -rf "$ROOT"
mkdir -p "$ROOT"
fuser -k "$PORT/tcp" 2>/dev/null
sleep 1
cd "$SM_SRC" || exit 1
export SM_STATE_DIR=$ROOT/state SM_RELEASE_STORE=$ROOT/releases SM_BACKUP_DIR=$ROOT/backups SM_HTTPS=false \
  SERVICE_PORT=$PORT SYS_IP=${SM_TEST_IP:-127.0.0.1} SM_ADMIN_PASSWORD=${SM_TEST_PW:-admin-test-pw} SM_MIN_FREE_GB=1 \
  SM_DATA_ROOT=${SM_TEST_DATA_ROOT:-$ROOT/data-root} BRANCH_NAME=test SM_POLL_SEC=5 \
  SM_LOGGER_URL=${SM_TEST_LOGGER:-http://127.0.0.1:12998} AC_DIR=$ROOT/ac \
  SM_STATIC_DIR="$SM_SRC/frontend/dist"
nohup "$PY" -m app.serve > "$ROOT/sm.log" 2>&1 &
for _ in $(seq 1 30); do curl -sf -m 2 "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 1; done
curl -sf -m 2 "http://127.0.0.1:$PORT/health" || { tail -30 "$ROOT/sm.log"; exit 1; }
echo
[[ $serve_only -eq 1 ]] && exit 0
: "${REGISTRY:?set REGISTRY}" "${REG_USER:?set REG_USER}" "${REG_PASSWORD_FILE:?set REG_PASSWORD_FILE}" "${REG_CA:?set REG_CA}"
"$PY" "$HERE/local_release_test.py" "http://127.0.0.1:$PORT" "${SM_TEST_PW:-admin-test-pw}" \
  "${MANIFEST:?set MANIFEST (upload YML)}" "$REGISTRY" "$REG_USER" "$REG_PASSWORD_FILE" "$REG_CA"
