#!/bin/bash
# WSL lab: rebuild sm-agent + software-manager, push them as 1.0.1 and self-update through the live manager.
#   1) software-manager -> 1.0.1 (job driven by the currently running manager)
#   2) sm-agent on node-a -> 1.0.1 (manager waits for the updater helper's exit code)
#   3) software-manager -> 1.0.1 again (new manager resumes its own interrupted job after restart)
set -u
URL=https://127.0.0.1:2999
PW="$(cat /root/AlarmCenter/software-manager/data/state/initial-admin-password)"
PY=/root/smvenv/bin/python
T=/mnt/c/software-manager/tests

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  bash /mnt/c/platform/scripts/rebuild-push.sh sm-agent software-manager || exit 1
  for m in sm-agent software-manager; do
    docker tag "localhost:5000/$m:1.0.0" "localhost:5000/$m:1.0.1" && docker push -q "localhost:5000/$m:1.0.1" || exit 1
  done
fi

# agent B (from source) picks up the new agent code
pkill -f "SERVICE_PORT=2996" 2>/dev/null; fuser -k 2996/tcp 2>/dev/null; sleep 1
(cd /mnt/c/sm-agent/src && env SERVICE_PORT=2996 AC_DIR=/root/smtest/agent-b MODULE_NAME=sm-agent-b \
   SM_DATA_ROOT=/opt/yellowmetal NODE_IP="$(hostname -I | cut -d' ' -f1)" NODE_NAME=node-b AGENT_HTTPS=true \
   RECONCILE_SEC=10 setsid nohup $PY -m app.serve > /root/agent-b.log 2>&1 < /dev/null &)

NODE_A="$($PY -c "
import httpx,sys
c=httpx.Client(base_url='$URL',verify=False)
t=c.post('/api/auth/login',json={'username':'admin','password':'$PW'}).json()['token']
print(next(n['id'] for n in c.get('/api/nodes',headers={'Authorization':'Bearer '+t}).json() if n['name']=='node-a'))")"
echo "node-a id: $NODE_A"

rc=0
echo "== 1) software-manager self-update (old manager code)"
$PY $T/selfupdate_test.py $URL "$PW" software-manager localhost:5000/software-manager:1.0.1 || rc=1
sleep 20
echo "== 2) sm-agent self-update on node-a"
$PY $T/selfupdate_test.py $URL "$PW" sm-agent localhost:5000/sm-agent:1.0.1 "$NODE_A" || rc=1
echo "== 3) software-manager self-update (resume after own restart)"
$PY $T/selfupdate_test.py $URL "$PW" software-manager localhost:5000/software-manager:1.0.1 || rc=1
echo
docker ps --format '{{.Names}}  {{.Image}}  {{.Status}}' | grep -E 'sm-|software-manager'
echo "SELFUPDATE rc=$rc"
