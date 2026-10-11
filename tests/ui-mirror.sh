#!/bin/bash
# Serve the UI over HTTP on 12999 from a copy of the live manager state (read-only check in a browser).
set -u
SRC=${SM_LIVE_STATE:-/root/AlarmCenter/software-manager/data}
DST=/root/smui
rm -rf "$DST"; mkdir -p "$DST"
cp -a "$SRC/state" "$DST/state"
[[ -d "$SRC/releases" ]] && cp -a "$SRC/releases" "$DST/releases"
[[ -d "$SRC/backups" ]] && cp -a "$SRC/backups" "$DST/backups"
export LOGGER_TOKEN="$(grep -E '^LOGGER_TOKEN=' /root/AlarmCenter/software-manager/.env | cut -d= -f2-)"
SM_TEST_ROOT=$DST SM_TEST_LOGGER=${SM_TEST_LOGGER:-http://127.0.0.1:2998} \
  bash /mnt/c/software-manager/tests/run-local.sh keep serve-only
