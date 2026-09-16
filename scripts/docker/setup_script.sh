#!/usr/bin/env bash
# Builds ROS 2 Jazzy (Ubuntu 24.04) on top of the Isaac Sim + MolmoSpaces image,
# verifies it, builds the AgileX agx_arm_ros workspace against it, and moves the
# single `isaac-sim` container onto that image.
#
# Why a new image (Dockerfile.ros2) instead of rebuilding ./Dockerfile:
#   isaac-sim-molmo:with-molmospaces is a `docker commit` that predates the ROS
#   lines in ./Dockerfile, so it has no /opt/ros. Rebuilding ./Dockerfile would
#   drop everything that commit baked in (the MolmoSpaces pip install — see
#   setup_host.sh Stage 7). Dockerfile.ros2 layers ROS on top of whichever base
#   image setup_host.sh uses.
#
# Inside the container:
#   - Isaac Sim's bundled ROS 2 bridge turns on whenever Isaac Sim starts, via
#     NVIDIA's setup_ros_env.sh (isaac-sim.sh, and python.sh through the hook in
#     Dockerfile.ros2).
#   - `ros2env` adds system ROS 2 + the agx_arm workspace to the current shell.
#
# Run setup_host.sh first (driver, Docker, NVIDIA toolkit, base image).
# Every stage is idempotent; rerun freely.
#
# Usage:
#   ./setup_script.sh                     # build image, verify, build workspace, update isaac-sim
#   FORCE_REBUILD=1 ./setup_script.sh     # rebuild image (e.g. after the base image changed)
#   BUILD_WORKSPACE=0 ./setup_script.sh   # skip the workspace build
#   SETUP_CONTAINER=0 ./setup_script.sh   # leave the isaac-sim container alone

set -euo pipefail

SETUP_DIR="$(dirname "$(readlink -f "$0")")"

# Same fallback as setup_host.sh. An explicitly set BASE_IMAGE must exist as-is.
if [[ -z "${BASE_IMAGE:-}" ]]; then
  BASE_IMAGE="isaac-sim-molmo:with-molmospaces"
  docker image inspect "$BASE_IMAGE" &>/dev/null || BASE_IMAGE="isaac-sim-molmo:latest"
fi
ROS_IMAGE="${ROS_IMAGE:-isaac-sim-molmo:ros2-jazzy}"
AGX_REPO_PATH="${AGX_REPO_PATH:-$HOME/S_ENG/agx_arm_ros}"
PYAGXARM_PATH="${PYAGXARM_PATH:-$HOME/S_ENG/pyAgxArm}"
# colcon build/install/log output. Kept outside the git checkout, which is
# mounted read-only into the build.
AGX_WS_PATH="${AGX_WS_PATH:-$HOME/S_ENG/agx_arm_ws}"
# install/setup.bash hardcodes absolute paths, so every container that uses the
# workspace must mount it at exactly this path.
WS_IN_CONTAINER="/isaac-sim/agx_arm_ws"
FORCE_REBUILD="${FORCE_REBUILD:-0}"
BUILD_WORKSPACE="${BUILD_WORKSPACE:-1}"
SETUP_CONTAINER="${SETUP_CONTAINER:-1}"
# Same name setup_host.sh uses: one container for Isaac Sim, MolmoSpaces and ROS 2.
CONTAINER_NAME="isaac-sim"
BACKUP_CONTAINER_NAME="${CONTAINER_NAME}-pre-ros2"
BRIDGE_LIB_DIR="/isaac-sim/exts/isaacsim.ros2.bridge/jazzy/lib"
# package.xml keys with no Jazzy binary (see Dockerfile.ros2).
ROSDEP_SKIP_KEYS="rviz warehouse_ros_mongo"
# Same host paths setup_host.sh mounts into `isaac-sim`.
MOLMO_REPO_PATH="${MOLMO_REPO_PATH:-$HOME/S_ENG/molmospaces}"
MOLMO_CACHE_PATH="${MOLMO_CACHE_PATH:-$HOME/S_ENG/molmospaces-cache}"
PIP_CACHE_PATH="${PIP_CACHE_PATH:-$HOME/S_ENG/molmospaces-cache-pip}"
OV_CACHE_PATH="${OV_CACHE_PATH:-$HOME/S_ENG/molmospaces-cache-ov}"
HOLODECK_PATH="${HOLODECK_PATH:-$HOME/S_ENG/Holodeck}"
OBJATHOR_PATH="${OBJATHOR_PATH:-$HOME/.objathor-assets}"

stage() {
  printf '\n==================================================================\n'
  printf '%s\n' "$1"
  printf '==================================================================\n'
}
die() { echo "ERROR: $*" >&2; exit 1; }

stage "Stage 1: Preflight"
command -v docker &>/dev/null || die "docker not found — run setup_host.sh first."
docker buildx version &>/dev/null \
  || die "docker buildx missing — Dockerfile.ros2 needs BuildKit for --build-context (install docker-buildx-plugin)."
docker info --format '{{json .Runtimes}}' | grep -q nvidia \
  || die "NVIDIA container runtime not registered with Docker — run setup_host.sh (Stage 3)."
docker image inspect "$BASE_IMAGE" &>/dev/null \
  || die "Base image $BASE_IMAGE not found — run setup_host.sh first, or set BASE_IMAGE."
echo "Base image: $BASE_IMAGE"

if [[ ! -d "$PYAGXARM_PATH/.git" ]]; then
  git clone https://github.com/agilexrobotics/pyAgxArm.git "$PYAGXARM_PATH"
fi
if [[ ! -d "$AGX_REPO_PATH/.git" ]]; then
  git clone -b ros2 --recurse-submodules https://github.com/agilexrobotics/agx_arm_ros.git "$AGX_REPO_PATH"
fi
# Checked by content, not `git submodule status`: a checkout can hold a
# populated agx_arm_urdf that git still reports as uninitialized ("-" prefix).
URDF_DIR="$AGX_REPO_PATH/src/agx_arm_description/agx_arm_urdf"
if [[ -z "$(find "$URDF_DIR" -name '*.urdf' -print -quit 2>/dev/null)" ]]; then
  echo "agx_arm_urdf submodule has no URDFs — fetching it."
  git -C "$AGX_REPO_PATH" submodule update --init --recursive
fi
echo "agx_arm_ros: $AGX_REPO_PATH ($(git -C "$AGX_REPO_PATH" rev-parse --short HEAD))"
echo "pyAgxArm:    $PYAGXARM_PATH ($(git -C "$PYAGXARM_PATH" rev-parse --short HEAD))"

stage "Stage 2: Build $ROS_IMAGE"
if docker image inspect "$ROS_IMAGE" &>/dev/null && [[ "$FORCE_REBUILD" != "1" ]]; then
  echo "$ROS_IMAGE already exists — skipping. (FORCE_REBUILD=1 to rebuild.)"
else
  docker build \
    -f "$SETUP_DIR/Dockerfile.ros2" \
    --build-arg BASE_IMAGE="$BASE_IMAGE" \
    --build-context pyagxarm="$PYAGXARM_PATH" \
    -t "$ROS_IMAGE" \
    "$SETUP_DIR"
fi

stage "Stage 3: Verify ROS 2 inside $ROS_IMAGE"
# Default bridge network, not host: keeps this smoke test's DDS traffic away from
# any ROS 2 graph already running on the host (CrazySwarm2, drone_reformation).
# No `set -u` inside: ROS setup.bash references unset variables.
docker run --rm --entrypoint bash -e BRIDGE_LIB_DIR="$BRIDGE_LIB_DIR" "$ROS_IMAGE" -c '
  set -eo pipefail
  echo "Default environment (every process):"
  [ -z "${ROS_DISTRO:-}" ] \
    || { echo "ROS_DISTRO preset to $ROS_DISTRO: Isaac Sim setup_ros_env.sh would skip its bundled bridge libs"; exit 1; }
  case ":${LD_LIBRARY_PATH:-}:" in
    *isaacsim.ros2.bridge*) echo "bridge libs on the global LD_LIBRARY_PATH: their libssl would shadow the system one"; exit 1 ;;
  esac
  case ":${PYTHONPATH:-}:" in
    *"/opt/ros/"*) echo "system ROS is on PYTHONPATH by default"; exit 1 ;;
  esac
  echo "  no global ROS_DISTRO, bridge libs or system-ROS PYTHONPATH: ok"
  case "${OMNI_KIT_ACCEPT_EULA:-}" in
    [Yy]|[Yy][Ee][Ss]|1) echo "  OMNI_KIT_ACCEPT_EULA set, so headless Isaac Sim apps skip the EULA prompt: ok" ;;
    *) echo "OMNI_KIT_ACCEPT_EULA not set: headless python.sh Isaac Sim apps block on the EULA prompt"; exit 1 ;;
  esac
  ! grep -q "/opt/ros" /isaac-sim/.bashrc \
    || { echo "/isaac-sim/.bashrc sources system ROS directly"; exit 1; }
  grep -q "^alias ros2env=" /isaac-sim/.bashrc \
    || { echo "ros2env alias missing from /isaac-sim/.bashrc"; exit 1; }
  echo "  .bashrc: ros2env alias present, no global ROS source: ok"

  # The environment python.sh builds (it sources setup_python_env.sh).
  (
    SCRIPT_DIR=/isaac-sim
    source /isaac-sim/setup_python_env.sh
    [ "$ROS_DISTRO" = jazzy ] || exit 1
    case ":$LD_LIBRARY_PATH:" in *":$BRIDGE_LIB_DIR:"*) ;; *) exit 1 ;; esac
  ) || { echo "python.sh environment does not enable the Isaac Sim ROS 2 bridge"; exit 1; }
  echo "  python.sh environment: ROS_DISTRO=jazzy + bundled bridge libs: ok"

  echo "After ros2env (system ROS 2):"
  source /opt/agx_env/ros2_env.sh
  for pkg in rclpy controller_manager joint_trajectory_controller robot_state_publisher xacro moveit_ros_move_group ros_gz_sim; do
    printf "  %-28s %s\n" "$pkg" "$(ros2 pkg prefix "$pkg")"
  done
  python3 -c "import pyAgxArm; print(\"  pyAgxArm (system python3):\", pyAgxArm.__file__)"

  timeout 30 ros2 topic echo --once /setup_smoke std_msgs/msg/String > /tmp/echo.log &
  sleep 3
  timeout 20 ros2 topic pub --once -w 1 /setup_smoke std_msgs/msg/String "{data: ok}" > /dev/null
  wait
  grep -q "data: ok" /tmp/echo.log || { echo "DDS pub/sub round-trip FAILED"; exit 1; }
  echo "  DDS pub/sub round-trip: ok"
'

if [[ "$BUILD_WORKSPACE" == "1" ]]; then
  stage "Stage 4: Build agx_arm_ros workspace -> $AGX_WS_PATH"
  # Pre-create the nested mountpoint as the host user; Docker would otherwise
  # create it root-owned inside the host directory.
  mkdir -p "$AGX_WS_PATH/src/agx_arm_ros"
  # Built as the host UID so build/install/log are owned by you, not the
  # container's isaac-sim user (UID 1234). They stay world-readable, so the
  # isaac-sim user can still source them.
  # /isaac-sim is drwxr-x--- isaac-sim:isaac-sim, so a foreign UID can't even
  # traverse into the mount point; joining that group grants the traversal.
  isaac_gid="$(docker run --rm --entrypoint stat "$ROS_IMAGE" -c '%g' /isaac-sim)"
  docker run --rm \
    --user "$(id -u):$(id -g)" --group-add "$isaac_gid" -e HOME=/tmp/home \
    -v "$AGX_WS_PATH":"$WS_IN_CONTAINER" \
    -v "$AGX_REPO_PATH":"$WS_IN_CONTAINER/src/agx_arm_ros":ro \
    --entrypoint bash "$ROS_IMAGE" -c "
      set -eo pipefail
      mkdir -p \"\$HOME\"
      source /opt/ros/jazzy/setup.bash
      cd $WS_IN_CONTAINER
      if rosdep update --rosdistro jazzy > /dev/null; then
        rosdep check --from-paths src --ignore-src --rosdistro jazzy --skip-keys '$ROSDEP_SKIP_KEYS' \
          || echo 'WARNING: rosdep lists missing system deps above — add them to Dockerfile.ros2.'
      else
        echo 'WARNING: rosdep update failed (network?) — skipping dependency check.'
      fi
      colcon build --event-handlers console_cohesion+
    "
  echo "Workspace built. Packages:"
  ls "$AGX_WS_PATH/install"
fi

if [[ "$SETUP_CONTAINER" == "1" ]]; then
  stage "Stage 5: Container $CONTAINER_NAME on $ROS_IMAGE"
  want_image_id="$(docker image inspect -f '{{.Id}}' "$ROS_IMAGE")"
  have_image_id="$(docker inspect -f '{{.Image}}' "$CONTAINER_NAME" 2>/dev/null || true)"

  if [[ "$have_image_id" == "$want_image_id" ]]; then
    echo "$CONTAINER_NAME already runs $ROS_IMAGE — leaving it alone."
    if [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME")" != "true" ]]; then
      docker start "$CONTAINER_NAME"
    fi
  else
    if [[ -n "$have_image_id" ]]; then
      # Renamed and stopped, not deleted: the old container's writable layer can
      # hold state that exists in no image and no bind mount.
      if docker ps -a --format '{{.Names}}' | grep -qx "$BACKUP_CONTAINER_NAME"; then
        die "$CONTAINER_NAME must be recreated on the new image, but the backup name" \
            "$BACKUP_CONTAINER_NAME is taken. Once it's no longer needed: docker rm $BACKUP_CONTAINER_NAME"
      fi
      docker stop "$CONTAINER_NAME" > /dev/null
      docker rename "$CONTAINER_NAME" "$BACKUP_CONTAINER_NAME"
      echo "Previous $CONTAINER_NAME kept, stopped, as $BACKUP_CONTAINER_NAME."
      echo "Remove it once the new container works for you: docker rm $BACKUP_CONTAINER_NAME"
    fi

    mounts=()
    [[ -d "$MOLMO_REPO_PATH" ]]  && mounts+=(-v "$MOLMO_REPO_PATH":/isaac-sim/molmospaces)
    [[ -d "$MOLMO_CACHE_PATH" ]] && mounts+=(-v "$MOLMO_CACHE_PATH":/isaac-sim/.molmospaces)
    [[ -d "$PIP_CACHE_PATH" ]]   && mounts+=(-v "$PIP_CACHE_PATH":/isaac-sim/.cache/pip)
    [[ -d "$OV_CACHE_PATH" ]]    && mounts+=(-v "$OV_CACHE_PATH":/isaac-sim/.cache/ov)
    [[ -d "$HOLODECK_PATH" ]]    && mounts+=(-v "$HOLODECK_PATH":/isaac-sim/Holodeck:ro)
    [[ -d "$OBJATHOR_PATH" ]]    && mounts+=(-v "$OBJATHOR_PATH":/isaac-sim/objathor-assets:ro)
    mkdir -p "$AGX_WS_PATH/src/agx_arm_ros"
    mounts+=(-v "$AGX_WS_PATH":"$WS_IN_CONTAINER")
    mounts+=(-v "$AGX_REPO_PATH":"$WS_IN_CONTAINER/src/agx_arm_ros":ro)

    # No guessed fallback: this host's display is :1, and a silent :0 bakes a
    # wrong value into the container for good.
    if [[ -z "${DISPLAY:-}" ]]; then
      echo "WARNING: DISPLAY is unset (not run from a desktop terminal?). GUI apps in the"
      echo "container will need it per exec, e.g.: docker exec -it -e DISPLAY=:1 $CONTAINER_NAME bash"
    fi
    # --network host: agx_arm_ros talks to CAN interfaces (can0 / vcan0) that live
    # in the host's network namespace, and DDS discovery with host-side ROS tools.
    docker run --name "$CONTAINER_NAME" --entrypoint bash -d --runtime=nvidia --gpus all \
      -e "DISPLAY=${DISPLAY:-}" -v /tmp/.X11-unix:/tmp/.X11-unix \
      "${mounts[@]}" \
      --network host \
      "$ROS_IMAGE" -c "tail -f /dev/null"
    echo "Container '$CONTAINER_NAME' started on $ROS_IMAGE."
  fi
fi

stage "Done"
cat <<EOF
Image: $ROS_IMAGE   Container: $CONTAINER_NAME
Workspace: $AGX_WS_PATH (mounted at $WS_IN_CONTAINER)

  docker exec -it $CONTAINER_NAME bash

Isaac Sim's ROS 2 bridge (Jazzy, bundled libs) turns on by itself when Isaac Sim
starts, from isaac-sim.sh or python.sh:
  /isaac-sim/isaac-sim.sh

System ROS 2 + agx_arm_ros in the current shell (driver, MoveIt, rviz, Gazebo):
  ros2env
Start Isaac Sim and MolmoSpaces Python from a shell where you have NOT run ros2env.
ros2env sets ROS_DISTRO (so Isaac Sim skips its bundled bridge libs) and puts ROS's
Python 3.12 packages on PYTHONPATH. Open a new shell instead.

Gazebo Harmonic with an agx_arm URDF, after ros2env
(on the host first, so the container may open windows: xhost +local:docker):
  ros2 launch /isaac-sim/molmospaces/scripts/docker/agx_arm_gazebo.launch.py arm_type:=piper

No physical arm? Virtual CAN has to be created on the HOST (containers share its
kernel and, with --network host, its interfaces):
  sudo modprobe vcan
  sudo ip link add dev can0 type vcan && sudo ip link set up can0
EOF
