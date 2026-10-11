#!/bin/bash
# WSL lab: install the SM stack packages from a release folder's prerequisites/ (settings stay in $HOME/AlarmCenter).
#   bash install-from-release.sh [/mnt/releases/V1.0.0] [module ...]
set -u
REL="${1:-/mnt/releases/V1.0.0}"
shift || true
MODS=("$@")
[[ ${#MODS[@]} -eq 0 ]] && MODS=(sm-agent software-manager)
for m in "${MODS[@]}"; do
  (cd "$REL/prerequisites/$m" && bash install.sh | tail -1)
done
for u in https://127.0.0.1:2997/health https://127.0.0.1:2999/health http://127.0.0.1:2998/health; do
  for _ in $(seq 1 40); do curl -sk -m 2 "$u" >/dev/null && break; sleep 2; done
  echo "$u: $(curl -sk -m 2 "$u")"
done
docker ps --format '{{.Names}} | {{.Image}} | {{.Status}}'
