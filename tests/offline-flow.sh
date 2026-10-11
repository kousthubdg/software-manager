#!/bin/bash
# Release path (read only) -> Upload (check + store scripts and images) -> prerequisites package ->
# a second, empty Software Manager (the offline site) starts with the packaged releases.
#   offline-flow.sh <release folder, e.g. V1.0.0> <prerequisites version, e.g. 1.0.0>
# env (required): SM_RELEASE_SOURCE, REGISTRY, REG_USER, REG_PASSWORD_FILE, REG_CA
# env (optional): PY (python with Software Manager requirements), SERVICES_ROOT, WORK (default $HOME/sm-offline-test)
set -eu
FOLDER="${1:?release folder}"
PREREQ_VER="${2:?prerequisites version}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SERVICES_ROOT="${SERVICES_ROOT:-$(cd "$HERE/../.." && pwd)}"
PY="${PY:-python3}"
WORK="${WORK:-$HOME/sm-offline-test}"
: "${SM_RELEASE_SOURCE:?}" "${REGISTRY:?}" "${REG_USER:?}" "${REG_PASSWORD_FILE:?}" "${REG_CA:?}"
export SM_RELEASE_SOURCE PY
REL="$SM_RELEASE_SOURCE/$FOLDER"
mkdir -p "$WORK"

echo "== 1. engineer step: prerequisites tar into the release path"
SERVICES_ROOT="$SERVICES_ROOT" python3 "$SERVICES_ROOT/platform/scripts/make_prerequisites.py" "$PREREQ_VER" \
  --out "$SM_RELEASE_SOURCE" --force | tail -1
before="$(find "$REL" -type f -printf '%p %s %T@\n' | sort | md5sum)"

echo "== 2. central Software Manager: upload $FOLDER from the release path, build the package"
SM_TEST_ROOT="$WORK/central" SM_TEST_PORT=12999 bash "$HERE/run-local.sh" --fresh serve-only >/dev/null
"$PY" "$HERE/offline_flow_test.py" http://127.0.0.1:12999 admin-test-pw "$FOLDER" \
  "$PREREQ_VER/sm_pre_requisites.tar" "$REGISTRY" "$REG_USER" "$REG_PASSWORD_FILE" "$REG_CA" "$WORK/pkgname"
after="$(find "$REL" -type f -printf '%p %s %T@\n' | sort | md5sum)"
[[ "$before" == "$after" ]] && echo "release path unchanged: yes" || echo "release path unchanged: NO"
VER="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$WORK/version.json")"
echo "store $WORK/central/releases/$VER:"; ls "$WORK/central/releases/$VER" | head -5
PKG="$WORK/central/ac/data/bundles/$(cat "$WORK/pkgname")"

echo "== 3. offline site: untar the package, hand releases to a fresh Software Manager, docker load one image"
WORK="$WORK" bash "$HERE/offline-site.sh" "$PKG"
