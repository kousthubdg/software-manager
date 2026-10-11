#!/bin/bash
# WSL lab: wipe e2e state (pods, pod data, manager DB/releases/backups, agent pod state). Keeps users' passwords/tokens.
set -u
for p in postgres-database mqtt configuration alarm; do docker rm -f "$p" >/dev/null 2>&1; done
rm -rf /opt/yellowmetal/postgres-database /opt/yellowmetal/mqtt /opt/yellowmetal/configuration /opt/yellowmetal/alarm /opt/yellowmetal/alarms
M=/root/AlarmCenter/software-manager/data
docker stop software-manager >/dev/null 2>&1
rm -f "$M"/state/software-manager.db*
rm -rf "$M"/releases/* "$M"/releases/.??* "$M"/backups/*
python3 - <<'EOF'
import json, pathlib
for f in ("/root/AlarmCenter/sm-agent/data/state.json", "/root/smtest/agent-b/data/state.json"):
    p = pathlib.Path(f)
    if p.exists():
        d = json.loads(p.read_text()); d["pods"] = {}; p.write_text(json.dumps(d))
EOF
docker restart sm-agent >/dev/null
docker start software-manager >/dev/null
for _ in $(seq 1 40); do curl -sk -m 2 https://127.0.0.1:2999/health >/dev/null && break; sleep 1; done
echo "reset done"
