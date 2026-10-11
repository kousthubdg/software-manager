#!/bin/bash
# Office -> branch release catalog test with two throwaway managers on this host.
#   central-test.sh <release folder> [office port] [branch port]
# env: as ui-setup.sh (SM_RELEASE_SOURCE, REGISTRY, REG_USER, REG_PASSWORD_FILE, REG_CA, PY), WORK (default $HOME/sm-central)
set -eu
FOLDER="${1:?release folder}"
OPORT="${2:-12991}"
BPORT="${3:-12992}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-python3}"
WORK="${WORK:-$HOME/sm-central}"
export PY
WORK="$WORK/office" bash "$HERE/ui-setup.sh" "$FOLDER" "$OPORT"
SM_TEST_ROOT="$WORK/branch" SM_TEST_PORT="$BPORT" bash "$HERE/run-local.sh" --fresh serve-only >/dev/null
"$PY" "$HERE/central_test.py" "http://127.0.0.1:$OPORT" "http://127.0.0.1:$BPORT" "$WORK/office/manager/releases"
