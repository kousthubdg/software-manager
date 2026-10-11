#!/bin/bash
# WSL lab e2e: manager + agent A + logger from prereq.sh (containers), agent B from source on :2996.
#   bash e2e-wsl.sh [--keep]
set -u
R=/root/AlarmCenter/docker-registry/data
REG_PW="$(cat "$R/auth/initial-password")"
NODE_IP="$(ip route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
printf '%s' "$REG_PW" | docker login localhost:5000 -u ym --password-stdin >/dev/null

# test tags: configuration 1.0.1 (same image, new version) and a broken alarm 1.0.2 (mqtt image: no /health)
docker tag localhost:5000/configuration:1.0.0 localhost:5000/configuration:1.0.1 2>/dev/null \
  || { docker pull -q localhost:5000/configuration:1.0.0 && docker tag localhost:5000/configuration:1.0.0 localhost:5000/configuration:1.0.1; }
docker push -q localhost:5000/configuration:1.0.1 >/dev/null
docker pull -q localhost:5000/mqtt:1.0.0 >/dev/null 2>&1
docker tag localhost:5000/mqtt:1.0.0 localhost:5000/alarm:1.0.2 && docker push -q localhost:5000/alarm:1.0.2 >/dev/null

# agent B from source
B=/root/smtest/agent-b
mkdir -p "$B"
pkill -f "SERVICE_PORT=2996" 2>/dev/null; fuser -k 2996/tcp 2>/dev/null; sleep 1
/root/smvenv/bin/pip install -q -r /mnt/c/sm-agent/src/requirements.txt 2>&1 | tail -1
(cd /mnt/c/sm-agent/src && env SERVICE_PORT=2996 AC_DIR="$B" MODULE_NAME=sm-agent-b SM_DATA_ROOT=/opt/yellowmetal \
   NODE_IP="$NODE_IP" NODE_NAME=node-b AGENT_HTTPS=true RECONCILE_SEC=10 \
   setsid nohup /root/smvenv/bin/python -m app.serve > /root/agent-b.log 2>&1 < /dev/null &)
for _ in $(seq 1 30); do curl -sk -m 2 https://127.0.0.1:2996/health >/dev/null && break; sleep 1; done
curl -sk -m 2 https://127.0.0.1:2996/health || { tail -20 /root/agent-b.log; exit 1; }
echo

SCRIPTS=/root/e2e-scripts
mkdir -p "$SCRIPTS"
for s in postgres-database mqtt configuration alarm; do
  f=$(ls /mnt/c/*/build/release/$s/${s}_scripts.tar | head -1); cp "$f" "$SCRIPTS/"
done

env SM_URL="https://127.0.0.1:2999" SM_PW="$(cat /root/AlarmCenter/software-manager/data/state/initial-admin-password)" \
  NODE_IP="$NODE_IP" PORT_A=2997 PORT_B=2996 TOKEN_A="$(cat /root/AlarmCenter/sm-agent/data/node-token)" \
  TOKEN_B="$(cat $B/data/node-token)" REG_PW="$REG_PW" REG_CA="$R/certs/ca.crt" SCRIPTS="$SCRIPTS" \
  /root/smvenv/bin/python /mnt/c/software-manager/tests/e2e.py "$@"
