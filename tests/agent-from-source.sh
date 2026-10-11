#!/bin/bash
# Start an extra sm-agent from source (test node).   agent-from-source.sh <port> <state dir> <node name>
#   env: PY (python with the agent requirements), SM_DATA_ROOT (default /opt/yellowmetal), NODE_IP (default: host route IP)
set -eu
PORT="$1"; DIR="$2"; NAME="$3"
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${AGENT_SRC:-$HERE/../../sm-agent/src}"
PY="${PY:-python3}"
NODE_IP="${NODE_IP:-$(ip route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')}"
mkdir -p "$DIR"
fuser -k "$PORT/tcp" 2>/dev/null || true
sleep 1
cd "$SRC"
env SERVICE_PORT="$PORT" AC_DIR="$DIR" MODULE_NAME="sm-agent-$NAME" SM_DATA_ROOT="${SM_DATA_ROOT:-/opt/yellowmetal}" \
  NODE_IP="$NODE_IP" NODE_NAME="$NAME" AGENT_HTTPS=true RECONCILE_SEC=10 \
  setsid nohup "$PY" -m app.serve > "$DIR/agent.log" 2>&1 < /dev/null &
for _ in $(seq 1 30); do curl -sk -m 2 "https://127.0.0.1:$PORT/health" >/dev/null && break; sleep 1; done
curl -sk -m 2 "https://127.0.0.1:$PORT/health" || { tail -20 "$DIR/agent.log"; exit 1; }
echo
