#!/bin/bash
# T6: manager (tests/run-local.sh) + sm-agent from source on their own ports, then install_flow_test.py.
#   install-flow.sh [--keep | --cleanup]   --keep leaves everything running; --cleanup only removes a kept run
# env: PY, SM_RELEASE_SOURCE, REGISTRY, REG_USER, REG_PASSWORD_FILE, REG_CA (required)
#      SM_TEST_ROOT ($HOME/sm-t6: manager state, release store, agent state, logs)
#      SM_TEST_DATA_ROOT (/opt/sm-t6-data: pod folders; must be outside /root because templates mount the alarm
#        path at the same path inside containers that run as RUN_UID), SM_TEST_PORT (12992), AGENT_PORT (12993),
#      SM_TEST_PW (admin-test-pw), RELEASE_FOLDER (V1.0.0), NODE_IP (detected),
#      SERVICES_ROOT (folder holding software-manager/ and sm-agent/), T6_PODS, T6_MAX_PODS, T6_PORT_BASE
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SERVICES_ROOT=${SERVICES_ROOT:-$(cd "$HERE/../.." && pwd)}
export SM_TEST_ROOT=${SM_TEST_ROOT:-$HOME/sm-t6} SM_TEST_PORT=${SM_TEST_PORT:-12992} SM_TEST_PW=${SM_TEST_PW:-admin-test-pw}
export SM_TEST_DATA_ROOT=${SM_TEST_DATA_ROOT:-/opt/sm-t6-data}
AGENT_PORT=${AGENT_PORT:-12993}
ROOT=$SM_TEST_ROOT
DR=$SM_TEST_DATA_ROOT
MARK=.sm-install-flow-test

cleanup() {
  if [[ -f "$ROOT/created.txt" ]]; then
    while read -r c; do
      [[ -z "$c" ]] && continue
      f="$DR/$c/docker-compose.yml"
      if docker inspect "$c" >/dev/null 2>&1; then
        [[ -f "$f" ]] && docker compose -p "$c" -f "$f" down -t 10 >/dev/null 2>&1
        docker rm -f "$c" >/dev/null 2>&1
        echo "cleanup: removed leftover container $c"
      fi
    done < "$ROOT/created.txt"
  fi
  fuser -k "$SM_TEST_PORT/tcp" "$AGENT_PORT/tcp" 2>/dev/null
  rm -rf "$ROOT"
  [[ -f "$DR/$MARK" ]] && rm -rf "$DR"
}

case "${1:-}" in
  --cleanup) cleanup; exit 0 ;;
  --keep) keep=1 ;;
  *) keep=0 ;;
esac
: "${PY:?set PY}" "${SM_RELEASE_SOURCE:?set SM_RELEASE_SOURCE}" "${REGISTRY:?set REGISTRY}" "${REG_USER:?set REG_USER}"
: "${REG_PASSWORD_FILE:?set REG_PASSWORD_FILE}" "${REG_CA:?set REG_CA}"
NODE_IP=${NODE_IP:-$(ip route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')}

if [[ -d "$DR" && -n "$(ls -A "$DR")" && ! -f "$DR/$MARK" ]]; then
  echo "SM_TEST_DATA_ROOT $DR exists and was not created by this test; choose another folder" >&2
  exit 1
fi
[[ -f "$ROOT/created.txt" ]] && cleanup
fuser -k "$SM_TEST_PORT/tcp" "$AGENT_PORT/tcp" 2>/dev/null; sleep 1
rm -rf "$DR" && mkdir -p "$DR" && touch "$DR/$MARK"
bash "$HERE/run-local.sh" --fresh serve-only || exit 1

A=$ROOT/agent
mkdir -p "$A"
(cd "$SERVICES_ROOT/sm-agent/src" && env SERVICE_PORT="$AGENT_PORT" AC_DIR="$A" MODULE_NAME=sm-agent-t6 \
   SM_DATA_ROOT="$DR" NODE_IP="$NODE_IP" NODE_NAME=t6-node AGENT_HTTPS=true RECONCILE_SEC=5 \
   setsid nohup "$PY" -m app.serve > "$ROOT/agent.log" 2>&1 < /dev/null &)
for _ in $(seq 1 30); do curl -sk -m 2 "https://127.0.0.1:$AGENT_PORT/health" >/dev/null && break; sleep 1; done
curl -sk -m 2 "https://127.0.0.1:$AGENT_PORT/health" || { tail -20 "$ROOT/agent.log"; cleanup; exit 1; }
echo

env SM_URL="http://127.0.0.1:$SM_TEST_PORT" SM_PW="$SM_TEST_PW" NODE_IP="$NODE_IP" AGENT_PORT="$AGENT_PORT" \
  AGENT_TOKEN="$(cat "$A/data/node-token")" AGENT_LOG="$ROOT/agent.log" REGISTRY="$REGISTRY" REG_USER="$REG_USER" \
  REG_PASSWORD_FILE="$REG_PASSWORD_FILE" REG_CA="$REG_CA" RELEASE_FOLDER="${RELEASE_FOLDER:-V1.0.0}" \
  SM_RELEASE_STORE="$ROOT/releases" DATA_ROOT="$DR" T6_CREATED="$ROOT/created.txt" \
  "$PY" "$HERE/install_flow_test.py"
rc=$?
if [[ $rc -ne 0 ]]; then
  echo "---- agent log (tail)"; grep -v '"GET /api/pods HTTP' "$ROOT/agent.log" | tail -40
  echo "---- manager log (tail)"; grep -v 'httpx: HTTP Request' "$ROOT/sm.log" | tail -40
fi
[[ $keep -eq 0 ]] && cleanup
exit $rc
