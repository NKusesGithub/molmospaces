!/usr/bin/env bash
# Host bring-up script for the CrazySwarm2 / Isaac Sim / MolmoSpaces rig.
#
# STAGED, NOT ONE-SHOT. The driver step requires a reboot and can, in the
# worst case, break your display session if a DKMS build or secure-boot
# module-signing step fails — that's a real risk with NVIDIA driver swaps,
# not a hypothetical. This script deliberately stops after the driver step
# and makes you reboot + rerun by hand rather than trying to push through
# unattended. Every other stage is idempotent (safe to rerun).
#
# Usage: chmod +x setup_host.sh && ./setup_host.sh
# Keep this file version-controlled in your dotfiles/molmospaces repo — the
# TARGET_DRIVER_BRANCH line below is the single source of truth for which
# driver branch this rig needs.
 
set -euo pipefail
 
# ---- config — the one place that records "which driver branch this rig needs" ----
TARGET_DRIVER_BRANCH="580"   # R580 confirmed compatible; R595 causes librtx.scenedb.plugin.so
                              # segfaults on this rig (see Notion: Isaac-Sim Troubles)
MOLMO_REPO_PATH="${MOLMO_REPO_PATH:-$HOME/S_ENG/molmospaces}"
# Separate mount for the ms-download asset cache (/isaac-sim/.molmospaces inside
# the container). Kept OUT of the git-tracked repo dir on purpose — this can grow
# to many GB and has no business being anywhere near `git add`. Without this
# mount, downloaded assets live only in the container's writable layer and are
# lost on every `docker rm` — this is the fix for exactly that.
MOLMO_CACHE_PATH="${MOLMO_CACHE_PATH:-$HOME/S_ENG/molmospaces-cache}"
# Set this to your private repo's SSH or HTTPS URL to make Stage 5 auto-clone
# on a machine that doesn't have it yet. Left blank by default — auto-cloning
# a private repo means this script needs your git credentials available
# (an SSH agent with the right key, or a credential helper), which varies by
# machine, so this stays opt-in rather than assumed.
MOLMO_REPO_URL="${MOLMO_REPO_URL:-}"
CONTAINER_NAME="isaac-sim"
IMAGE_NAME="isaac-sim-molmo:latest"
DOCKERFILE_DIR="$(dirname "$(readlink -f "$0")")"
 
echo "=================================================================="
echo "Stage 1: NVIDIA driver"
echo "=================================================================="
current_version="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null || echo "")"
current_branch="${current_version%%.*}"
 
if [[ "$current_branch" == "$TARGET_DRIVER_BRANCH" ]]; then
  echo "Driver already on R${TARGET_DRIVER_BRANCH} branch (${current_version}) — skipping."
else
  echo "Current driver: '${current_version:-none detected}' — installing R${TARGET_DRIVER_BRANCH} instead."
  echo
  echo "!! This purges the existing NVIDIA driver and installs a different branch. !!"
  read -r -p "Continue? [y/N] " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
 
  sudo apt-get update
  sudo apt-get purge -y '^nvidia-.*' || true
  sudo add-apt-repository -y ppa:graphics-drivers/ppa
  sudo apt-get update
 
  # Package name may vary slightly (e.g. an "-open" variant) depending on what's
  # currently published to the PPA — check with `apt-cache search nvidia-driver-580`
  # if this exact name 404s.
  sudo apt-get install -y "nvidia-driver-${TARGET_DRIVER_BRANCH}"
 
  echo
  echo "=================================================================="
  echo "Driver installed. A REBOOT IS REQUIRED before continuing."
  echo "This script will NOT reboot for you. After rebooting, confirm with"
  echo "'nvidia-smi' that the driver is up and the branch matches, then"
  echo "rerun this script — it will detect the driver is current and skip"
  echo "straight to Stage 2."
  echo "=================================================================="
  exit 0
fi
 
echo "=================================================================="
echo "Stage 2: Docker Engine"
echo "=================================================================="
if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  echo "Added $USER to the docker group."
  echo "Log out and back in (or run 'newgrp docker' in this shell) before continuing,"
  echo "otherwise the docker commands below will fail with a permission error."
  read -r -p "Press Enter once you've done that (or Ctrl+C to handle it yourself and rerun): " _
else
  echo "Docker already installed — skipping."
fi
 
echo "=================================================================="
echo "Stage 3: NVIDIA Container Toolkit"
echo "=================================================================="
if ! dpkg -l 2>/dev/null | grep -q nvidia-container-toolkit; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
  sudo apt-get update
  sudo apt-get install -y nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
else
  echo "nvidia-container-toolkit already installed — skipping."
fi
 
echo "=================================================================="
echo "Stage 4: X11 access for the container"
echo "=================================================================="
xhost +local:docker
 
echo "=================================================================="
echo "Stage 5: MolmoSpaces checkout"
echo "=================================================================="
if [[ ! -d "$MOLMO_REPO_PATH/.git" ]]; then
  if [[ -n "$MOLMO_REPO_URL" ]]; then
    echo "No checkout found at $MOLMO_REPO_PATH — cloning from \$MOLMO_REPO_URL..."
    git clone "$MOLMO_REPO_URL" "$MOLMO_REPO_PATH"
  else
    echo "No git checkout found at $MOLMO_REPO_PATH, and MOLMO_REPO_URL isn't set."
    echo "Either clone it yourself first:"
    echo "  git clone <your-private-repo-url> $MOLMO_REPO_PATH"
    echo "or rerun this script with the URL set, e.g.:"
    echo "  MOLMO_REPO_URL=git@github.com:you/molmospaces.git ./setup_host.sh"
    exit 1
  fi
fi
sudo chmod -R a+rwX "$MOLMO_REPO_PATH"
echo "Permissions opened on $MOLMO_REPO_PATH so the container's non-root user can write into it."
echo "(Using sudo here — this path can contain files created by container processes"
echo "running as a different UID than your host user, e.g. anything copied via"
echo "'docker exec' without -u root. Plain chmod can only touch files you own.)"
 
mkdir -p "$MOLMO_CACHE_PATH"
sudo chmod -R a+rwX "$MOLMO_CACHE_PATH"
echo "Asset cache directory ready at $MOLMO_CACHE_PATH (created here, as your normal user,"
echo "so it isn't left root-owned by Docker auto-creating it — same UID trap as before)."
 
echo "=================================================================="
echo "Stage 6: Container image + container"
echo "=================================================================="
if ! docker image inspect "$IMAGE_NAME" &>/dev/null; then
  echo "Building $IMAGE_NAME from $DOCKERFILE_DIR/Dockerfile ..."
  docker build -t "$IMAGE_NAME" "$DOCKERFILE_DIR"
fi
 
if ! docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
  docker run --name "$CONTAINER_NAME" --entrypoint bash -d --runtime=nvidia --gpus all \
    -e "DISPLAY=$DISPLAY" -v /tmp/.X11-unix:/tmp/.X11-unix \
    -v "$MOLMO_REPO_PATH":/isaac-sim/molmospaces \
    -v "$MOLMO_CACHE_PATH":/isaac-sim/.molmospaces \
    --network host \
    "$IMAGE_NAME" -c "tail -f /dev/null"
  echo "Container '$CONTAINER_NAME' started."
elif [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME")" != "true" ]]; then
  docker start "$CONTAINER_NAME"
fi
 
echo "=================================================================="
echo "Stage 7: MolmoSpaces pip install (inside the container)"
echo "=================================================================="
# molmo_spaces_isaac (Isaac Sim integration) and the root molmo_spaces package
# (housegen extra) are separate installs from separate directories.
docker exec "$CONTAINER_NAME" bash -c \
  "cd /isaac-sim/molmospaces/molmo_spaces_isaac && /isaac-sim/python.sh -m pip install -e .[dev,sim]"
docker exec "$CONTAINER_NAME" bash -c \
  "cd /isaac-sim/molmospaces && /isaac-sim/python.sh -m pip install -e .[housegen]"
 
echo "=================================================================="
echo "Done. Enter the container with: docker exec -it $CONTAINER_NAME bash"
echo "=================================================================="
 

