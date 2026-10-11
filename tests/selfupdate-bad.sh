#!/bin/bash
# WSL lab: self-update rejection + rollback checks.
#   a) wrong repository name is rejected (422) before anything runs
#   b) a broken sm-logger image (exits at start) fails the job and the old logger keeps running
set -u
URL=https://127.0.0.1:2999
PW="$(cat /root/AlarmCenter/software-manager/data/state/initial-admin-password)"
PY=/root/smvenv/bin/python
T=/mnt/c/software-manager/tests

printf 'FROM localhost:5000/sm-logger:1.0.0\nENTRYPOINT ["sh", "-c", "echo broken build; exit 3"]\nCMD []\n' \
  | docker build -q -t localhost:5000/sm-logger:9.9.9-broken - >/dev/null && docker push -q localhost:5000/sm-logger:9.9.9-broken >/dev/null || exit 1

echo "== a) wrong image name (expect 422)"
$PY $T/selfupdate_test.py $URL "$PW" sm-logger localhost:5000/mqtt:1.0.0 2>&1 | grep -E '^start:' || true
echo "== b) broken sm-logger image (expect job failed, logger still healthy)"
$PY $T/selfupdate_test.py $URL "$PW" sm-logger localhost:5000/sm-logger:9.9.9-broken && echo "UNEXPECTED SUCCESS"
sleep 5
docker ps -a --format '{{.Names}} | {{.Image}} | {{.Status}}' | grep -E 'sm-logger'
echo "logger health: $(curl -s -m 3 http://127.0.0.1:2998/health)"
