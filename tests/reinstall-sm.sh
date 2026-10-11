#!/bin/bash
# WSL lab: rebuild the sm-agent + software-manager packages (option 2) and reinstall them in place.
# Settings and state stay in $HOME/AlarmCenter/<module>/ (.env, data/).
set -u
B=/mnt/c/platform/scripts/build-all-services.sh
bash "$B" 2 sm-agent software-manager > /root/sm-pkg.log 2>&1 || { grep -a -E '❌' /root/sm-pkg.log; exit 1; }
for m in sm-agent software-manager; do
  d=/mnt/c/$m/build/release/$m
  sed -i 's/\r$//' "$d/install.sh"
  (cd "$d" && bash install.sh | tail -1)
done
for p in 2997 2999; do
  for _ in $(seq 1 40); do curl -sk -m 2 "https://127.0.0.1:$p/health" >/dev/null && break; sleep 2; done
  echo "$p: $(curl -sk -m 2 https://127.0.0.1:$p/health)"
done
docker ps --format '{{.Names}} | {{.Image}} | {{.Status}}' | grep -E 'sm-|software-manager'
