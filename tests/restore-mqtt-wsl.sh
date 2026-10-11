#!/bin/bash
# WSL lab: run restore_mqtt_test.py against the live manager and the kept e2e deployment.
/root/smvenv/bin/python /mnt/c/software-manager/tests/restore_mqtt_test.py https://127.0.0.1:2999 \
  "$(cat /root/AlarmCenter/software-manager/data/state/initial-admin-password)" "${1:-e2e}"
