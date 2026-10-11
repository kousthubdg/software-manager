#!/bin/bash
# Start a test manager for UI checks with one release stored from the release path.
#   ui-setup.sh <release folder> [port]   env: as offline-flow.sh (SM_RELEASE_SOURCE, REGISTRY, REG_*, PY, WORK)
set -eu
FOLDER="${1:?release folder}"
PORT="${2:-12994}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-python3}"
WORK="${WORK:-$HOME/sm-ui-test}"
: "${SM_RELEASE_SOURCE:?}" "${REGISTRY:?}" "${REG_USER:?}" "${REG_PASSWORD_FILE:?}" "${REG_CA:?}"
export SM_RELEASE_SOURCE PY
mkdir -p "$WORK"
SM_TEST_ROOT="$WORK/manager" SM_TEST_PORT="$PORT" bash "$HERE/run-local.sh" --fresh serve-only >/dev/null
"$PY" - "$PORT" "$FOLDER" "$REGISTRY" "$REG_USER" "$REG_PASSWORD_FILE" "$REG_CA" <<'EOF'
import sys, time, httpx
port, folder, reg, user, pwf, ca = sys.argv[1:7]
c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60)
c.headers["Authorization"] = "Bearer " + c.post("/api/auth/login", json={"username": "admin", "password": "admin-test-pw"}).json()["token"]
c.put("/api/registries", json={"host": reg, "username": user, "password": open(pwf).read().strip(), "ca_pem": open(ca).read()}).raise_for_status()
c.post("/api/users", json={"username": "viewer1", "password": "viewer-test-pw", "role": "viewer"})
ver = c.post(f"/api/release-source/{folder}/import", json={"download": True}).json()["version"]
while (s := c.get(f"/api/releases/{ver}").json()["status"]) not in ("downloaded", "scan_failed", "download_failed"):
    time.sleep(3)
print("release", ver, s)
EOF
echo "UI test manager: http://127.0.0.1:$PORT  (admin / admin-test-pw, viewer1 / viewer-test-pw)"
