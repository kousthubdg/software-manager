#!/bin/bash
# Offline-site half of offline-flow.sh: untar a prerequisites package, hand its releases to a fresh Software Manager.
#   offline-site.sh <package.tar>      env: PY, WORK (default $HOME/sm-offline-test)
set -eu
PKG="${1:?package tar}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-python3}"
WORK="${WORK:-$HOME/sm-offline-test}"
export PY
rm -rf "$WORK/sitepkg" "$WORK/site" && mkdir -p "$WORK/sitepkg" "$WORK/site/ac/data/import"
tar -xf "$PKG" -C "$WORK/sitepkg"
cp -a "$WORK"/sitepkg/*/releases/. "$WORK/site/ac/data/import/"
SM_TEST_ROOT="$WORK/site" SM_TEST_PORT=12997 bash "$HERE/run-local.sh" serve-only >/dev/null
"$PY" - <<'EOF'
import httpx
c = httpx.Client(base_url="http://127.0.0.1:12997", timeout=30)
c.headers["Authorization"] = "Bearer " + c.post("/api/auth/login", json={"username": "admin", "password": "admin-test-pw"}).json()["token"]
for r in c.get("/api/releases").json():
    print("site release:", r["software_name"], r["version"], r["status"])
EOF
echo "import folder left over: $(ls -A "$WORK/site/ac/data/import" | wc -l)"
IMG="$(ls "$WORK"/site/releases/*/*/image.tar | head -1)"
echo "docker load $IMG"
docker load -i "$IMG"
