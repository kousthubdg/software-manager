#!/bin/bash
# =============================================================================
# software-manager Build & Installer Generator Script
# =============================================================================
# This script supports:
#   1. Building a Docker image
#   2. Building & saving Docker image into a release folder
#   3. Clearing the release folder
#   4. Clear + Build + Save Docker image + Generate installer
# Works in both CLI (./build.sh 4) and Interactive mode.
# Output: build/release/<moduleName>/<datafilename> + install.sh
# =============================================================================

cd "$(dirname "$0")" || exit 1
CONFIG_FILE="buildConfig.yml"

# -------------------------------
# Default values (used as fallback)
# -------------------------------
DEFAULT_VERSION="1.0.0"
DEFAULT_IMAGE_NAME="software-manager"
DEFAULT_MODULE_NAME="software-manager"
DEFAULT_BUILD_IMAGE_NAME="software-manager"
DEFAULT_BUILD_IMAGE_VERSION="build"
DEFAULT_DATA_FILENAME="software-manager.tar"
DEFAULT_DATA_FILE_VERSION="1.0.0"
DEFAULT_HOST_PORT="2999"
DEFAULT_CONTAINER_PORT="2999"

# -------------------------------
# Function to read key from buildConfig.yml
# -------------------------------
get_config_value() {
  local key=$1
  if [[ -f "$CONFIG_FILE" ]]; then
    grep -E "^${key}:" "$CONFIG_FILE" | awk -F': ' '{print $2}' | tr -d '[:space:]'
  fi
}

# -------------------------------
# Load config (or fallback to defaults)
# -------------------------------
if [[ -f $CONFIG_FILE && -s $CONFIG_FILE ]]; then
  echo "📖 Reading config from $CONFIG_FILE"
  MODULE_NAME=$(get_config_value "moduleName")
  IMAGE_NAME=$(get_config_value "imageName")
  VERSION=$(get_config_value "version")
  BUILD_IMAGE_NAME=$(get_config_value "buildImageName")
  BUILD_IMAGE_VERSION=$(get_config_value "buildImageversion")
  DATA_FILENAME=$(get_config_value "datafilename")
  DATA_FILE_VERSION=$(get_config_value "datafileversion")
  HOST_PORT=$(get_config_value "hostPort")
  CONTAINER_PORT=$(get_config_value "containerPort")
else
  echo "⚠️ $CONFIG_FILE not found or empty. Using defaults."
fi
VERSION=${VERSION:-$DEFAULT_VERSION}
IMAGE_NAME=${IMAGE_NAME:-$DEFAULT_IMAGE_NAME}
MODULE_NAME=${MODULE_NAME:-$DEFAULT_MODULE_NAME}
BUILD_IMAGE_NAME=${BUILD_IMAGE_NAME:-$DEFAULT_BUILD_IMAGE_NAME}
BUILD_IMAGE_VERSION=${BUILD_IMAGE_VERSION:-$DEFAULT_BUILD_IMAGE_VERSION}
DATA_FILENAME=${DATA_FILENAME:-$DEFAULT_DATA_FILENAME}
DATA_FILE_VERSION=${DATA_FILE_VERSION:-$DEFAULT_DATA_FILE_VERSION}
HOST_PORT=${HOST_PORT:-$DEFAULT_HOST_PORT}
CONTAINER_PORT=${CONTAINER_PORT:-$DEFAULT_CONTAINER_PORT}

# -------------------------------
# Paths
# -------------------------------
build_folder=$(pwd)/release/${MODULE_NAME}

# -------------------------------
# Utility functions
# -------------------------------

# Remove existing container if running/stopped (control-plane services keep theirs: KEEP_CONTAINER=1)
remove_existing_container() {
  if [[ "${KEEP_CONTAINER:-1}" == "1" ]]; then
    echo "ℹ️  keeping running ${MODULE_NAME} (update it with install.sh or Software Manager self-update)"
    return
  fi
  if docker ps -a --format '{{.Names}}' | grep -Eq "^${MODULE_NAME}\$"; then
    echo "🛑 Stopping and removing existing container: ${MODULE_NAME}"
    docker stop ${MODULE_NAME} >/dev/null 2>&1
    docker rm ${MODULE_NAME} >/dev/null 2>&1
  fi
}

# Remove old images (all versions of IMAGE_NAME)
remove_old_images() {
  echo "🔍 Checking for old images of ${IMAGE_NAME}"
  old_images=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep "^${IMAGE_NAME}:")
  if [ -n "$old_images" ]; then
    echo "🗑 Removing old images:"
    echo "$old_images"
    docker rmi -f $old_images
  fi
}

# -------------------------------
# Build functions
# -------------------------------

# Build Docker image only
build_docker_image() {
  remove_existing_container
  remove_old_images

  echo "🐳 Building ${IMAGE_NAME} docker image with version $VERSION"
  docker build -t ${IMAGE_NAME}:$VERSION -f Dockerfile ../ || { echo "❌ docker build failed"; exit 1; }

  echo "✅ Build process completed successfully."
}

# Build + Save image to tar + generate installer
build_and_save_docker_image() {
  echo "📂 Creating build folder: $build_folder"
  mkdir -p $build_folder

  remove_existing_container
  remove_old_images

  echo "🐳 Building ${IMAGE_NAME} docker image with version $VERSION"
  docker build -t ${IMAGE_NAME}:$VERSION -f Dockerfile ../ || { echo "❌ docker build failed"; exit 1; }

  echo "💾 Saving docker image -> ${DATA_FILENAME}"
  docker save -o ${DATA_FILENAME} ${IMAGE_NAME}:$VERSION || { echo "❌ docker save failed"; exit 1; }
  chmod 644 ${DATA_FILENAME}

  echo "📦 Moving ${DATA_FILENAME} to build folder"
  mv ${DATA_FILENAME} $build_folder
  if [[ -f prereq.sh ]]; then
    sed 's/\r$//' prereq.sh > "$build_folder/prereq.sh"
  fi

  chmod -R 755 $build_folder

  echo "🛠 Generating Installer script"
  generate_install_script

  echo "✅ Build process completed successfully."
}

# Clear release folder
clear_release_folder() {
  echo "🗑 Clearing release folder..."
  rm -rf release/
  echo "✅ Release folder cleared."
}

# -------------------------------
# Generate install.sh inside release
# -------------------------------
generate_install_script() {
  cd "$build_folder" || exit 1

  cat <<EOL > install.sh
#!/bin/bash
# =============================================================================
# Installer for ${MODULE_NAME} - Version: $VERSION
# =============================================================================
# Settings live in \$HOME/AlarmCenter/${MODULE_NAME}/.env (created on first start from the
# image defaults). Edit it, then: docker restart ${MODULE_NAME}
# =============================================================================

cd "\$(dirname "\$0")" || exit 1

# Stop and remove container if exists
if docker ps -a --format '{{.Names}}' | grep -Eq "^${MODULE_NAME}\$"; then
  echo "Stopping and removing existing container: ${MODULE_NAME}"
  docker stop ${MODULE_NAME} >/dev/null 2>&1
  docker rm ${MODULE_NAME} >/dev/null 2>&1
fi

# Remove old images
echo "Checking for old images of ${IMAGE_NAME}"
old_images=\$(docker images --format '{{.Repository}}:{{.Tag}}' | grep "^${IMAGE_NAME}:")
if [ -n "\$old_images" ]; then
  echo "Removing old images:"
  echo "\$old_images"
  docker rmi -f \$old_images
fi

# Load docker image from tar
echo "Loading image from tar: ${DATA_FILENAME}"
docker load -i ${DATA_FILENAME}

# Ensure env path
echo "Creating env path folder: \$HOME/AlarmCenter/${MODULE_NAME}"
mkdir -p \$HOME/AlarmCenter/${MODULE_NAME}/logs \$HOME/AlarmCenter/${MODULE_NAME}/data

SYS_IP_VAL=\$(hostname -I | cut -d' ' -f1)
TZ_VAL=\$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo UTC)

# Run docker container
echo "Executing docker run for ${MODULE_NAME}"
docker run -d \\
  --network host \\
  -e "TZ=\${TZ_VAL}" \\
  -e "HOME=\$HOME" \\
  -e "SYS_IP=\${SYS_IP_VAL}" \\
  -e "MODULE_NAME=${MODULE_NAME}" \\
  -e "SERVICE_PORT=${CONTAINER_PORT}" \\
  -v /etc/localtime:/etc/localtime:ro \\
  --restart always \\
  --mount type=bind,source=\$HOME,target=\$HOME \\
  --mount type=bind,source=/mnt,target=/mnt,readonly \\
  --user=\$(id -u):\$(id -g) \\
  --name ${MODULE_NAME} \\
  ${IMAGE_NAME}:$VERSION

echo "✅ ${MODULE_NAME} started (port ${HOST_PORT}). Env: \$HOME/AlarmCenter/${MODULE_NAME}/.env"
EOL

  chmod +x install.sh
}

# Push image to the private registry: REGISTRY (env) or `registry:` in buildConfig.yml.
# Optional REGISTRY_USER / REGISTRY_PASSWORD (env) for docker login.
push_docker_image() {
  local reg="${REGISTRY:-$(get_config_value registry)}"
  [[ -n "$reg" ]] || { echo "❌ Set REGISTRY (env) or registry: in $CONFIG_FILE"; exit 1; }
  docker image inspect ${IMAGE_NAME}:$VERSION >/dev/null 2>&1 || build_docker_image
  if [[ -n "${REGISTRY_USER:-}" && -n "${REGISTRY_PASSWORD:-}" ]]; then
    printf '%s' "$REGISTRY_PASSWORD" | docker login "$reg" -u "$REGISTRY_USER" --password-stdin >/dev/null \
      || { echo "❌ docker login $reg failed"; exit 1; }
  fi
  docker tag ${IMAGE_NAME}:$VERSION $reg/${IMAGE_NAME}:$VERSION
  docker push $reg/${IMAGE_NAME}:$VERSION || { echo "❌ docker push failed"; exit 1; }
  local digest
  digest=$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' $reg/${IMAGE_NAME}:$VERSION | grep "^$reg/" | head -n1)
  echo "✅ pushed $reg/${IMAGE_NAME}:$VERSION ${digest#*@}"
}

# Scripts tar for Software Manager: exactly composetemplate.yml + prestart.sh (from build/sm/).
build_scripts_tar() {
  [[ -f sm/composetemplate.yml && -f sm/prestart.sh ]] || { echo "❌ build/sm templates missing"; exit 1; }
  mkdir -p "$build_folder"
  sed -i 's/\r$//' sm/prestart.sh sm/composetemplate.yml
  bash -n sm/prestart.sh || { echo "❌ prestart.sh syntax error"; exit 1; }
  chmod 755 sm/prestart.sh
  local scripts_ver
  scripts_ver=$(get_config_value scriptsVersion)
  tar --owner=0 --group=0 -C sm -cf "$build_folder/${MODULE_NAME}_scripts.tar" composetemplate.yml prestart.sh
  echo "✅ $build_folder/${MODULE_NAME}_scripts.tar (scripts ${scripts_ver:-$VERSION})"
}

# -------------------------------
# Interactive vs CLI mode
# -------------------------------
if [ -z "$1" ]; then
  echo ""
  echo "Select an option:"
  echo "1. Build Docker image"
  echo "2. Build Docker image and save Tar"
  echo "3. Clear release folder"
  echo "4. Clear release folder, build and save Docker image"
  echo "5. Push Docker image to registry (REGISTRY)"
  echo "6. Build Software Manager scripts tar"
  read -p "Enter your choice: " choice
else
  choice=$1
fi

case $choice in
  1) build_docker_image ;;
  2) build_and_save_docker_image ;;
  3) clear_release_folder ;;
  4) clear_release_folder; build_and_save_docker_image; if [[ -d sm ]]; then build_scripts_tar; fi ;;
  5) push_docker_image ;;
  6) build_scripts_tar ;;
  *) echo "❌ Invalid choice. Exiting." ; exit 1 ;;
esac
