# ROS 2 Jazzy + AgileX arm (`agx_arm_ros`) in the Isaac Sim image

Field notes for building a ROS 2 Jazzy environment on top of the Isaac Sim + MolmoSpaces
image and building AgileX's [`agx_arm_ros`](https://github.com/agilexrobotics/agx_arm_ros/tree/ros2)
(branch `ros2`) against it, plus opening the arm URDFs in Gazebo.

## Status (2026-09-15)

| Piece | State |
|---|---|
| Image `isaac-sim-molmo:ros2-jazzy` (Ubuntu 24.04, ROS 2 Jazzy) | Built and verified |
| ROS 2 checks: core packages, MoveIt, ros2_control, `pyAgxArm`, DDS pub/sub round-trip | Pass |
| `agx_arm_ros` workspace (4 packages) | Builds; `rosdep` reports all system deps satisfied |
| Workspace runtime check: driver executable, driver module import, `agx_arm_msgs` import | Pass |
| All 10 URDF/xacro variants (5 arms × with/without gripper) process with `xacro` | Pass |
| Gazebo Harmonic + `agx_arm_gazebo.launch.py` | See [Gazebo](#gazebo) |
| Arm URDF imported into Isaac Sim + ROS 2 bridge wiring | Not started |
| Virtual-CAN fake arm so the driver runs without hardware | Not started |

## Files (`molmospaces/scripts/docker/`)

| File | Purpose |
|---|---|
| `Dockerfile` | Original Isaac Sim 5.1 + MolmoSpaces image (`isaac-sim-molmo:latest`) |
| `setup_host.sh` | Host bring-up: driver, Docker, NVIDIA toolkit, X11, `isaac-sim` container |
| `Dockerfile.ros2` | ROS 2 Jazzy + `agx_arm_ros` deps + Gazebo, layered on the MolmoSpaces image |
| `setup_script.sh` | Builds and verifies that image, builds the workspace, optionally creates a container |
| `agx_arm_gazebo.launch.py` | Opens Gazebo with an `agx_arm_description` URDF |
| `ROS2_AGX_ARM.md` | This file |

## Quick start

Prerequisite: `setup_host.sh` has been run (driver, Docker, NVIDIA container toolkit, base image).

```bash
cd ~/S_ENG/molmospaces/scripts/docker
./setup_script.sh                     # build image, verify ROS 2, build workspace
CREATE_CONTAINER=1 ./setup_script.sh  # also create the isaac-sim-ros2 container
```

Every stage is idempotent. Knobs: `FORCE_REBUILD=1` (rebuild the image, e.g. after the base
image changed), `BUILD_WORKSPACE=0`, `BASE_IMAGE=…`, `AGX_REPO_PATH`, `PYAGXARM_PATH`, `AGX_WS_PATH`.

What it does:

1. **Preflight** — Docker, buildx, NVIDIA runtime, base image; clones `pyAgxArm` and
   `agx_arm_ros` (with submodules) if missing; fetches the URDF submodule if it has no URDFs.
2. **Build** `isaac-sim-molmo:ros2-jazzy` from `Dockerfile.ros2`.
3. **Verify** inside the image: ROS package prefixes, `pyAgxArm` import, a DDS pub/sub
   round-trip, Isaac Sim's bundled Jazzy bridge libs, and that `.bashrc` doesn't source system ROS.
4. **Build the workspace** into `~/S_ENG/agx_arm_ws` (`rosdep check`, then `colcon build`).
5. **Container** (opt-in) `isaac-sim-ros2` with the same mounts as `isaac-sim`, plus the workspace.

## Using it

Two separate shells in the container. Never source both in one shell.

```bash
# Shell 1 — ROS 2 nodes (agx_arm_ros)
docker exec -it isaac-sim-ros2 bash
source /opt/agx_env/ros2_env.sh

# Shell 2 — Isaac Sim with its bundled ROS 2 bridge
docker exec -it isaac-sim-ros2 bash
source /opt/agx_env/isaac_ros2_env.sh && /isaac-sim/isaac-sim.sh
```

## Design decisions

**A new image layer, not a rebuild of `./Dockerfile`.** The running `isaac-sim` container uses
`isaac-sim-molmo:with-molmospaces`, a `docker commit` from 2026-09-03 that predates the ROS lines in
`./Dockerfile`, so it has no `/opt/ros`. `isaac-sim-molmo:latest` (built 2026-09-14) does have Jazzy
but lacks the MolmoSpaces install baked into the commit. `Dockerfile.ros2` layers onto
`:with-molmospaces` (falling back to `:latest`, same as `setup_host.sh`), and the existing
`isaac-sim` container is never modified.

**Two Pythons that must not mix.** Isaac Sim runs Python 3.11 and reaches ROS 2 through the
`isaacsim.ros2.bridge` extension's own bundled Jazzy libs and `rclpy`. System ROS 2 Jazzy is
Python 3.12. Sourcing `/opt/ros/jazzy/setup.bash` puts 3.12 packages on `PYTHONPATH`, which the
3.11 interpreter then tries to import. So the image sources ROS nowhere globally (it also deletes
the `source /opt/ros/jazzy/setup.bash` line `./Dockerfile` appends to `.bashrc`) and ships two
per-shell scripts:

- `/opt/agx_env/ros2_env.sh` — system ROS 2 + the workspace, for `agx_arm_ros` nodes.
- `/opt/agx_env/isaac_ros2_env.sh` — `ROS_DISTRO`, `RMW_IMPLEMENTATION`, and `LD_LIBRARY_PATH`
  pointing at the bridge's bundled libs; it strips any system-ROS entries first.

**Isaac Sim's bundled ROS 2 is bridge-only.** `/isaac-sim/exts/isaacsim.ros2.bridge/{humble,jazzy}`
contain `rclpy`, the ROS CLI, launch, and RMW libs, but no `colcon`, `ros2_control`,
`controller_manager`, MoveIt, `robot_state_publisher`, `joint_state_publisher`, `rviz2`, or `xacro`.
It can exchange topics with external nodes but cannot build or run `agx_arm_ros`, which is why a
full Jazzy install is needed.

**Explicit dependency list.** The apt list mirrors the `<depend>` tags in `agx_arm_ros/src/*/package.xml`
plus `scripts/agx_arm_install_deps.sh`, instead of that script's `ros-jazzy-moveit*` /
`ros-jazzy-control*` globs, which pull in hundreds of unused packages. Two `package.xml` keys have
no Jazzy binary and are skipped:

- `rviz` — the ROS 1 name; `rviz2` is listed alongside it.
- `warehouse_ros_mongo` — not released for Jazzy; `warehouse_ros_sqlite` is installed instead.

**`pyAgxArm` in system Python, `--no-deps`.** `agx_arm_ctrl` imports AgileX's CAN SDK `pyAgxArm`
directly. It is installed into system Python 3.12 from the local checkout, bind-mounted via a BuildKit
named build context so the source isn't baked into a layer. Its only dependencies, `python-can` and
`typing-extensions`, come from apt; `--no-deps` stops pip from replacing apt-owned packages, which
`--break-system-packages` would otherwise allow.

**No `apt-get upgrade`** (unlike `./Dockerfile`): upgrading the NVIDIA base image wholesale can drift
libraries Isaac Sim was validated against.

**Workspace outside the git checkout.** Build output goes to `~/S_ENG/agx_arm_ws`; the
`agx_arm_ros` checkout is mounted read-only at `src/agx_arm_ros`, so building never dirties the repo.
`install/setup.bash` hardcodes absolute paths, so every container must mount the workspace at
exactly `/isaac-sim/agx_arm_ws`.

## Troubleshooting log

**`/opt/ros` missing in the running `isaac-sim` container.** Image history, not a broken install:
the container's image was committed before `./Dockerfile` gained its ROS lines (see above).

**Stage 4: `cd: /isaac-sim/agx_arm_ws: Permission denied`.** The workspace is built as the host UID
(1003) so build output belongs to the host user. But `/isaac-sim` is `drwxr-x--- isaac-sim:isaac-sim`
(1234), so UID 1003 is "other" and can't even traverse into the mount point. Fix: `--group-add` with
the group ID read from `/isaac-sim` in the image. Running the build as UID 1234 instead would work
but leave files on the host that the host user can't delete, and this host has no passwordless sudo.

**`git submodule status` shows `-` (uninitialized) for `agx_arm_urdf`, yet the directory is full.**
The submodule contents were cloned separately, so git's bookkeeping disagrees with the disk. The
script therefore checks for actual `.urdf` files rather than trusting `git submodule status`.

**Build log full of `Failed to resolve user 'systemd-network'` and `Failed to connect to socket
/run/dbus/system_bus_socket`.** Harmless: package post-install hooks expect systemd and D-Bus, which
containers don't run.

**Gazebo: `create` loops on `Requesting list of world names` forever; `gz service -l` lists nothing.**
Seen when Gazebo ran as a UID with no user entry in the container (`docker run --user 1003`, where
`whoami` fails with `cannot find name for user ID 1003`). The identical launch as the image's
`isaac-sim` user spawned the arm in 4 s. The likely mechanism is gz-transport naming its discovery
partition `<hostname>:<username>`, which can't resolve a username. Run Gazebo as `isaac-sim` (the
`isaac-sim-ros2` default); setting `GZ_PARTITION` explicitly is an untested alternative.

**`DISPLAY` inside the container.** `setup_script.sh` passes the creating shell's `DISPLAY` into
`isaac-sim-ros2` and warns if it is empty, rather than guessing `:0` (this host's display is `:1`).
If a container ends up with the wrong value, set it per exec:
`docker exec -it -e DISPLAY=:1 isaac-sim-ros2 bash`.

**Don't edit `setup_script.sh` while it runs.** Bash reads a running script from the file as it
executes, so an in-place edit can make it resume mid-line in the new content.

## Virtual CAN (no physical arm)

The driver talks CAN through `pyAgxArm`. Interfaces live in the host kernel; with `--network host`
the container sees them.

```bash
# On the host (needs root):
sudo modprobe vcan
sudo ip link add dev can0 type vcan && sudo ip link set up can0
```

Without passwordless sudo, a privileged container can do the same, since it shares the host kernel:

```bash
docker run --rm -u root --privileged --network host -v /lib/modules:/lib/modules:ro \
  --entrypoint bash isaac-sim-molmo:ros2-jazzy \
  -c "modprobe vcan && ip link add dev can0 type vcan && ip link set up can0"
```

A bare `vcan0` is not enough: the driver's `connect()` waits for firmware and joint feedback before
accepting commands. `pyAgxArm/tests/slaves/piper_can_slave.py` (and the `nero` / gripper / Revo2
slaves) emulate those responses and are the starting point for a standalone fake arm.

## Gazebo

`Dockerfile.ros2` installs `ros-jazzy-ros-gz` (Gazebo Harmonic, the release paired with Jazzy).
`agx_arm_gazebo.launch.py` loads an `agx_arm_description` URDF, runs `robot_state_publisher`, starts
Gazebo, and spawns the arm.

The upstream URDFs carry no `<gazebo>` or `ros2_control` tags, so every joint would go limp under
gravity. The launch file adds a Gazebo `JointPositionController` per movable joint at spawn time,
holding a start pose inside the joint's limits, without editing the repo's files. Mesh URIs are
`package://agx_arm_description/...`; Gazebo resolves them through `GZ_SIM_RESOURCE_PATH`, which the
launch file points at the package's `share` directory.

```bash
# On the host, once per login, so the container may open windows on display :1
xhost +local:docker

# In the container, from a ROS 2 shell (source /opt/agx_env/ros2_env.sh)
ros2 launch /isaac-sim/molmospaces/scripts/docker/agx_arm_gazebo.launch.py arm_type:=piper
#   arm_type: nero | piper | piper_h | piper_l | piper_x
#   effector_type: none | agx_gripper
#   gui: true | false (false = server only, no window)

# Move a joint
gz topic -t /model/piper/joint/joint2/0/cmd_pos -m gz.msgs.Double -p "data: 1.0"
```

The arm is about 0.6 m tall and Gazebo's default camera starts several meters away, so it first
appears as a speck. Right-click → "Move to" only framed it slightly closer in testing; this sets a
close view:

```bash
gz service -s /gui/move_to/pose --reqtype gz.msgs.GUICamera --reptype gz.msgs.Boolean --timeout 5000 \
  -r "pose: {position: {x: 0.9, y: -0.9, z: 0.7}, orientation: {w: 0.37717, x: -0.15621, y: 0.06470, z: 0.91058}}"
```

Verified headless (`gui:=false`) inside `isaac-sim-ros2` with `arm_type:=piper`:

- The arm spawned about 4 s after launch (`Entity creation successful`), with no errors in the launch
  log, so the meshes resolved.
- All six position-controller topics exist (`/model/piper/joint/joint1…6/0/cmd_pos`).
- Commanding `joint2` to 1.0 rad moved `link6` from z = 0.213 m to z = 0.125 m.
- Holding: between two samples 5 s apart, `link6` moved 0.2 mm and 0.004 rad while settling. The
  controllers are PD only (`I_GAIN = 0`), which can leave an offset under gravity; raise `I_GAIN` or
  `P_GAIN` in the launch file if exact holding matters.

GUI verified on display `:1` (after `xhost +local:docker`): the `Gazebo Sim` window opened with the arm
spawned, rendering on the GPU. The `gz sim gui` process has NVIDIA's `libGLX_nvidia` / `libEGL_nvidia`
580.178.04 loaded, and `nvidia-smi` lists it as a graphics process (275 MiB). The launch log's
`libEGL warning: egl: failed to create dri2 screen` lines come from Mesa's EGL, which the GL vendor
loader (glvnd) tries before selecting NVIDIA's; they are harmless.

## Next steps toward Isaac Sim

1. Import an arm URDF (e.g. `piper/urdf/piper_description.urdf`) with the Isaac Sim URDF importer;
   resolve the `package://agx_arm_description/` mesh paths against
   `/isaac-sim/agx_arm_ws/install/agx_arm_description/share`.
2. Wire the ROS 2 bridge (OmniGraph): subscribe to `control/joint_states` and drive the articulation;
   publish `feedback/joint_states` from the simulated joints. These are the topics `agx_arm_ctrl` uses.
3. Decide whether Isaac Sim replaces the driver (sim-only) or a vCAN fake arm feeds the real driver.

## Environment reference

| | |
|---|---|
| Host | `hp-z8-fury-g5-03`, user `nerissa`, kernel 6.8.0-138 (Ubuntu 22.04 HWE) |
| GPU | NVIDIA RTX 6000 Ada, 49 GB, driver 580.178.04 |
| Isaac Sim | 5.1.0 (Python 3.11), container OS Ubuntu 24.04.2 |
| ROS 2 / Gazebo | Jazzy / Harmonic |
| `agx_arm_ros` | `b9ad14d` (branch `ros2`), 4 packages: `agx_arm_msgs`, `agx_arm_ctrl`, `agx_arm_description`, `agx_arm_moveit` |
| `pyAgxArm` | `e7aef17` |
| Display | `:1` |
