#!/bin/bash
# =============================================================================
# Installer for software-manager - Version: 1.0.0
# =============================================================================
# Settings live in $HOME/AlarmCenter/software-manager/.env (created on first start from the
# image defaults). Edit it, then: docker restart software-manager
# =============================================================================

cd "$(dirname "$0")" || exit 1

# Stop and remove container if exists
if docker ps -a --format '{{.Names}}' | grep -Eq "^software-manager$"; then
  echo "Stopping and removing existing container: software-manager"
  docker stop software-manager >/dev/null 2>&1
  docker rm software-manager >/dev/null 2>&1
fi

# Remove old images
echo "Checking for old images of software-manager"
old_images=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep "^software-manager:")
if [ -n "$old_images" ]; then
  echo "Removing old images:"
  echo "$old_images"
  docker rmi -f $old_images
fi

# Load docker image from tar
echo "Loading image from tar: software-manager.tar"
docker load -i software-manager.tar

# Ensure env path
echo "Creating env path folder: $HOME/AlarmCenter/software-manager"
mkdir -p $HOME/AlarmCenter/software-manager/logs $HOME/AlarmCenter/software-manager/data

SYS_IP_VAL=$(hostname -I | cut -d' ' -f1)
TZ_VAL=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo UTC)

# Run docker container
echo "Executing docker run for software-manager"
docker run -d \
  --network host \
  -e "TZ=${TZ_VAL}" \
  -e "HOME=$HOME" \
  -e "SYS_IP=${SYS_IP_VAL}" \
  -e "MODULE_NAME=software-manager" \
  -e "SERVICE_PORT=2999" \
  -v /etc/localtime:/etc/localtime:ro \
  --restart always \
  --mount type=bind,source=$HOME,target=$HOME \
  --mount type=bind,source=/mnt,target=/mnt,readonly \
  --user=$(id -u):$(id -g) \
  --name software-manager \
  software-manager:1.0.0

echo "✅ software-manager started (port 2999). Env: $HOME/AlarmCenter/software-manager/.env"
