#!/bin/sh
# start:prod
#   standalone (install.sh): seed/refresh $HOME/AlarmCenter/<module>/.env (generateenv + env.defaults), then run.
#   SM_MANAGED=1 (Software Manager): env comes only from compose env_file (values.env); no .env seeding.
# In both modes X_FILE=/run/secrets/X is read into X (secrets never sit in values.env / docker inspect).
set -e
MODULE="${MODULE_NAME:-software-manager}"
export MODULE_NAME="$MODULE"
AC="${AC_DIR:-$HOME/AlarmCenter/$MODULE}"
mkdir -p "$AC/logs" "$AC/data" 2>/dev/null || true
export SYS_IP="${SYS_IP:-127.0.0.1}"

load_env() {
  [ -f "$1" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|\#*) continue ;; esac
    key="${line%%=*}"
    val="${line#*=}"
    if [ -n "$val" ]; then export "$key=$val"; fi
  done < "$1"
  return 0
}
if [ "${SM_MANAGED:-0}" != "1" ]; then
  cp /app/generateenv.sh /app/env.defaults "$AC/" 2>/dev/null || true
  (cd "$AC" && bash ./generateenv.sh --non-interactive) || echo "WARN: generateenv failed — using existing $AC/.env"
  load_env "$AC/.env"
fi

for var in $(env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)_FILE=.*/\1/p'); do
  eval "f=\${${var}_FILE}"
  if [ -r "$f" ]; then
    export "$var=$(cat "$f")"
  fi
done

cd /app
exec python -m app.serve
