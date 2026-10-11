#!/usr/bin/env bash
set -euo pipefail
CONTAINER_NAME="${1:-crowding_controller}"
ENV_FILE="$CONTAINER_NAME.env"
mkdir -p ./servicesdata/crowding_controller ./serviceslog/crowding_controller
cat > "${ENV_FILE}.tmp" <<ENVEOF
CONTAINER_NAME=$CONTAINER_NAME
TZ=UTC
ENVEOF
mv -f "${ENV_FILE}.tmp" "$ENV_FILE"
echo "created $(pwd)/$ENV_FILE"
