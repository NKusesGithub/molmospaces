#!/usr/bin/env bash
# Builds ROS 2 Jazzy (Ubuntu 24.04) on top of the Isaac Sim + MolmoSpaces image,
# verifies it, then builds the AgileX agx_arm_ros workspace against it.
#
# Why a new image (Dockerfile.ros2) instead of rebuilding ./Dockerfile:
#   The `isaac-sim` container runs isaac-sim-molmo:with-molmospaces, a
#   `docker commit` that predates the ROS lines in ./Dockerfile, so it has no
#   /opt/ros. Rebuilding ./Dockerfile would drop everything that commit baked in
#   (the MolmoSpaces pip install — see setup_host.sh Stage 7). Dockerfile.ros2
#   layers ROS on top of whichever base image setup_host.sh uses, and the
#   existing `isaac-sim` container is never touched.
#
# Run setup_host.sh first (driver, Docker, NVIDIA toolkit, base image).
# Every stage is idempotent; rerun freely.
#
# Usage:
#   ./setup_script.sh                     # build image, verify, build workspace
#   FORCE_REBUILD=1 ./setup_script.sh     # rebuild image (e.g. after the base image changed)
#   BUILD_WORKSPACE=0 ./setup_script.sh   # image + verification only
#   CREATE_CONTAINER=1 ./setup_script.sh  # also create an `isaac-sim-ros2` container

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
CREATE_CONTAINER="${CREATE_CONTAINER:-0}"
ROS_CONTAINER_NAME="${ROS_CONTAINER_NAME:-isaac-sim-ros2}"
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
docker run --rm --entrypoint bash "$ROS_IMAGE" -c '
  set -eo pipefail
  source /opt/agx_env/ros2_env.sh
  echo "ROS_DISTRO=$ROS_DISTRO  RMW_IMPLEMENTATION=$RMW_IMPLEMENTATION"
  for pkg in rclpy controller_manager joint_trajectory_controller robot_state_publisher xacro moveit_ros_move_group; do
    printf "  %-28s %s\n" "$pkg" "$(ros2 pkg prefix "$pkg")"
  done
  python3 -c "import pyAgxArm; print(\"  pyAgxArm (system python3):\", pyAgxArm.__file__)"

  timeout 30 ros2 topic echo --once /setup_smoke std_msgs/msg/String > /tmp/echo.log &
  sleep 3
  timeout 20 ros2 topic pub --once -w 1 /setup_smoke std_msgs/msg/String "{data: ok}" > /dev/null
  wait
  grep -q "data: ok" /tmp/echo.log || { echo "DDS pub/sub round-trip FAILED"; exit 1; }
  echo "  DDS pub/sub round-trip: ok"

  test -d /isaac-sim/exts/isaacsim.ros2.bridge/jazzy/lib \
    || { echo "Isaac Sim bundled Jazzy bridge libs missing"; exit 1; }
  echo "  Isaac Sim bundled Jazzy bridge libs: present"
  ! grep -q "/opt/ros" /isaac-sim/.bashrc \
    || { echo "/isaac-sim/.bashrc still sources system ROS"; exit 1; }
  echo "  /isaac-sim/.bashrc does not source system ROS: ok"
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

if [[ "$CREATE_CONTAINER" == "1" ]]; then
  stage "Stage 5: Container $ROS_CONTAINER_NAME"
  if docker ps -a --format '{{.Names}}' | grep -qx "$ROS_CONTAINER_NAME"; then
    echo "Container $ROS_CONTAINER_NAME already exists — leaving it alone."
  else
    mounts=()
    [[ -d "$MOLMO_REPO_PATH" ]]  && mounts+=(-v "$MOLMO_REPO_PATH":/isaac-sim/molmospaces)
    [[ -d "$MOLMO_CACHE_PATH" ]] && mounts+=(-v "$MOLMO_CACHE_PATH":/isaac-sim/.molmospaces)
    [[ -d "$PIP_CACHE_PATH" ]]   && mounts+=(-v "$PIP_CACHE_PATH":/isaac-sim/.cache/pip)
    [[ -d "$OV_CACHE_PATH" ]]    && mounts+=(-v "$OV_CACHE_PATH":/isaac-sim/.cache/ov)
    [[ -d "$HOLODECK_PATH" ]]    && mounts+=(-v "$HOLODECK_PATH":/isaac-sim/Holodeck:ro)
    [[ -d "$OBJATHOR_PATH" ]]    && mounts+=(-v "$OBJATHOR_PATH":/isaac-sim/objathor-assets:ro)

    # No guessed fallback: this host's display is :1, and a silent :0 bakes a wrong
    # value into the container for good.
    if [[ -z "${DISPLAY:-}" ]]; then
      echo "WARNING: DISPLAY is unset (not run from a desktop terminal?). GUI apps in the"
      echo "container will need it per exec, e.g.: docker exec -it -e DISPLAY=:1 $ROS_CONTAINER_NAME bash"
    fi
    # --network host: agx_arm_ros talks to CAN interfaces (can0 / vcan0) that live
    # in the host's network namespace, and DDS discovery with host-side ROS tools.
    docker run --name "$ROS_CONTAINER_NAME" --entrypoint bash -d --runtime=nvidia --gpus all \
      -e "DISPLAY=${DISPLAY:-}" -v /tmp/.X11-unix:/tmp/.X11-unix \
      "${mounts[@]}" \
      -v "$AGX_WS_PATH":"$WS_IN_CONTAINER" \
      -v "$AGX_REPO_PATH":"$WS_IN_CONTAINER/src/agx_arm_ros":ro \
      --network host \
      "$ROS_IMAGE" -c "tail -f /dev/null"
    echo "Container '$ROS_CONTAINER_NAME' started."
  fi
fi

stage "Done"
cat <<EOF
Image: $ROS_IMAGE   Workspace: $AGX_WS_PATH (mounted at $WS_IN_CONTAINER)

Use two separate shells in the container — never mix them:

  # ROS 2 nodes (agx_arm_ros):
  docker exec -it $ROS_CONTAINER_NAME bash
  source /opt/agx_env/ros2_env.sh

  # Isaac Sim with its bundled ROS 2 bridge:
  docker exec -it $ROS_CONTAINER_NAME bash
  source /opt/agx_env/isaac_ros2_env.sh && /isaac-sim/isaac-sim.sh

  # Gazebo Harmonic with an agx_arm URDF (from the ROS 2 shell above).
  # On the host first, so the container may open windows: xhost +local:docker
  ros2 launch /isaac-sim/molmospaces/scripts/docker/agx_arm_gazebo.launch.py arm_type:=piper

No container yet? Rerun with CREATE_CONTAINER=1.
No physical arm? Virtual CAN has to be created on the HOST (containers share its
kernel and, with --network host, its interfaces):
  sudo modprobe vcan
  sudo ip link add dev can0 type vcan && sudo ip link set up can0
EOF
