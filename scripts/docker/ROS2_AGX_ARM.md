# ROS 2 Jazzy + AgileX arm (`agx_arm_ros`) in the Isaac Sim container

Field notes for running ROS 2 Jazzy inside the single `isaac-sim` container (Isaac Sim 5.1 +
MolmoSpaces), building AgileX's [`agx_arm_ros`](https://github.com/agilexrobotics/agx_arm_ros/tree/ros2)
(branch `ros2`) against it, and opening the arm URDFs in Gazebo.

## Status (2026-09-15)

| Piece | State |
|---|---|
| One `isaac-sim` container on `isaac-sim-molmo:ros2-jazzy` (Ubuntu 24.04, ROS 2 Jazzy) | Running; previous container kept stopped as `isaac-sim-pre-ros2` |
| System ROS 2 via `ros2env`: core packages, MoveIt, ros2_control, `pyAgxArm`, DDS pub/sub | Pass |
| `agx_arm_ros` workspace (4 packages): build, `rosdep`, driver executable and module import | Pass |
| All 10 URDF/xacro variants (5 arms × with/without gripper) process with `xacro` | Pass |
| Isaac Sim's bundled ROS 2 bridge enabled for `isaac-sim.sh` and `python.sh` | Environment verified; round-trip: see [Isaac Sim bridge](#isaac-sim-bridge) |
| MolmoSpaces after the change: `import molmo_spaces_isaac`, HTTPS and pip through `python.sh` | Pass |
| Gazebo Harmonic + `agx_arm_gazebo.launch.py` | Pass (see [Gazebo](#gazebo)) |
| Arm URDF imported into Isaac Sim + joint topics wired to the bridge | Not started |
| Virtual-CAN fake arm so the driver runs without hardware | Not started |

## Files (`molmospaces/scripts/docker/`)

| File | Purpose |
|---|---|
| `Dockerfile` | Original Isaac Sim 5.1 + MolmoSpaces image (`isaac-sim-molmo:latest`) |
| `setup_host.sh` | Host bring-up: driver, Docker, NVIDIA toolkit, X11, `isaac-sim` container. Prefers `:ros2-jazzy` when it exists and mounts the workspace |
| `Dockerfile.ros2` | ROS 2 Jazzy + `agx_arm_ros` deps + Gazebo, layered on the MolmoSpaces image |
| `setup_script.sh` | Builds and verifies that image, builds the workspace, moves `isaac-sim` onto the image |
| `agx_arm_gazebo.launch.py` | Opens Gazebo with an `agx_arm_description` URDF |
| `ROS2_AGX_ARM.md` | This file |

## Quick start

Prerequisite: `setup_host.sh` has been run (driver, Docker, NVIDIA container toolkit, base image).

```bash
cd ~/S_ENG/molmospaces/scripts/docker
./setup_script.sh
```

Every stage is idempotent. Knobs: `FORCE_REBUILD=1` (rebuild the image, e.g. after the base image
changed), `BUILD_WORKSPACE=0`, `SETUP_CONTAINER=0` (leave the container alone), `BASE_IMAGE=…`,
`AGX_REPO_PATH`, `PYAGXARM_PATH`, `AGX_WS_PATH`.

What it does:

1. **Preflight** — Docker, buildx, NVIDIA runtime, base image; clones `pyAgxArm` and
   `agx_arm_ros` (with submodules) if missing; fetches the URDF submodule if it has no URDFs.
2. **Build** `isaac-sim-molmo:ros2-jazzy` from `Dockerfile.ros2`.
3. **Verify** inside the image: no global `ROS_DISTRO`, bridge libs, or system-ROS `PYTHONPATH`;
   `OMNI_KIT_ACCEPT_EULA` set; `ros2env` alias present; `python.sh`'s environment enables the bridge;
   then, after `ros2env`, ROS package prefixes, `pyAgxArm`, and a DDS pub/sub round-trip.
4. **Build the workspace** into `~/S_ENG/agx_arm_ws` (`rosdep check`, then `colcon build`).
5. **Container**: if `isaac-sim` runs an older image, stop it, rename it to `isaac-sim-pre-ros2`, and
   create a new `isaac-sim` with the same mounts plus the workspace. If that backup name is already
   taken, the script stops and asks you to remove the old backup first.

## Using it

```bash
docker exec -it isaac-sim bash

# Isaac Sim (bridge on automatically) — from a shell where you have NOT run ros2env
/isaac-sim/isaac-sim.sh

# System ROS 2 + agx_arm_ros in this shell (driver, MoveIt, rviz, Gazebo)
ros2env
ros2 run agx_arm_ctrl agx_arm_ctrl_single --ros-args ...
```

Start Isaac Sim and MolmoSpaces Python from a shell where you have not run `ros2env`: it sets
`ROS_DISTRO` (so Isaac Sim skips its bundled bridge libs) and puts ROS's Python 3.12 packages on
`PYTHONPATH`. Open a new shell instead. To launch Isaac Sim without the bridge environment:
`/isaac-sim/isaac-sim.sh --no-ros-env`, or `NO_ROS_ENV=true /isaac-sim/python.sh …`.

## Design decisions

**One container, previous one kept.** The ROS 2 image is layered on `:with-molmospaces`, so MolmoSpaces,
Isaac Sim, and ROS 2 all live in `isaac-sim`. When the container moves to a new image, the old one is
stopped and renamed rather than deleted, because its writable layer can hold state that exists in no
image and no bind mount. Delete the backup with `docker rm isaac-sim-pre-ros2` once satisfied.

**A new image layer, not a rebuild of `./Dockerfile`.** `:with-molmospaces` is a `docker commit` from
2026-09-03 that predates the ROS lines in `./Dockerfile`, so it has no `/opt/ros`. `:latest` has Jazzy
but lacks the MolmoSpaces install baked into the commit.

**How each side gets ROS 2.** Isaac Sim (Python 3.11) talks ROS 2 through the `isaacsim.ros2.bridge`
extension's bundled Jazzy libs and `rclpy`; `agx_arm_ros` needs system ROS 2 Jazzy (Python 3.12).
They meet over DDS.

- NVIDIA's `/isaac-sim/setup_ros_env.sh`, sourced by `isaac-sim.sh` and the other GUI launchers, sets
  `ROS_DISTRO` from the Ubuntu version (24.04 → `jazzy`) and appends the bridge's `jazzy/lib` to
  `LD_LIBRARY_PATH`, **but only when `ROS_DISTRO` is unset**. So the image sets no global `ROS_DISTRO`.
- `python.sh` doesn't source that script, so `Dockerfile.ros2` appends a line to
  `/isaac-sim/setup_python_env.sh` (which `python.sh` sources) that does, unless `NO_ROS_ENV=true`.
- The bridge libs are not on the global `LD_LIBRARY_PATH`: `jazzy/lib` ships its own `libssl.so.3`,
  `libcrypto.so.3`, `libyaml`, `libspdlog`, and `libtinyxml2`, which would shadow the system copies
  for `git`, `curl`, and `apt`. Verified after the change: a plain shell has an empty
  `LD_LIBRARY_PATH` and `curl` / `git` over HTTPS work; through `python.sh`, with the bridge libs on
  the path, `ssl` reports OpenSSL 3.0.16, HTTPS to PyPI returns 200, and `pip download` works.
- System ROS 2 comes per shell from `ros2env` (alias for `/opt/agx_env/ros2_env.sh`). The image
  sources ROS nowhere globally and deletes the `source /opt/ros/jazzy/setup.bash` line `./Dockerfile`
  appends to `.bashrc`. `/opt/agx_env/isaac_ros2_env.sh` undoes `ros2env` in a shell if needed.

**Isaac Sim's bundled ROS 2 is bridge-only.** `/isaac-sim/exts/isaacsim.ros2.bridge/{humble,jazzy}`
contain `rclpy`, the ROS CLI, launch, and RMW libs, but no `colcon`, `ros2_control`,
`controller_manager`, MoveIt, `robot_state_publisher`, `joint_state_publisher`, `rviz2`, or `xacro`.
That is why a full Jazzy install is needed alongside it.

**EULA for headless Isaac Sim apps.** The image sets `OMNI_KIT_ACCEPT_EULA=YES`, the variable Kit
itself checks. See the troubleshooting entry below for why `ACCEPT_EULA=Y` alone isn't enough.

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
`install/setup.bash` hardcodes absolute paths, so the workspace must be mounted at exactly
`/isaac-sim/agx_arm_ws`.

## Isaac Sim bridge

Environment verified in the running container: `python.sh` processes get `ROS_DISTRO=jazzy` and the
bundled `jazzy/lib` on `LD_LIBRARY_PATH`, while a plain shell has neither.

Round-trip verified: a headless Isaac Sim app (`python.sh`, started from a plain shell) published
`/clock` through an OmniGraph `ROS2PublishClock` node, and `ros2 topic echo --once /clock` after
`ros2env` received it (`sec: 8`). Isaac Sim was publishing about 57 s after launch; its only warnings
were `carb.audio` finding no output device, which is expected in a container.

**Shared DDS domain.** The container uses `--network host` and the default `ROS_DOMAIN_ID` 0, so it
sees every ROS 2 graph on this host's network. During this test `ros2 topic list` also showed
`/arm_controller/*` and `/camera/armcam/*` topics that none of these scripts create. Before running
`agx_arm_ros` or Isaac Sim publishers next to another robot stack, pick a free domain and export it
in every shell involved, including the one that starts Isaac Sim, e.g. `export ROS_DOMAIN_ID=42`.

## Troubleshooting log

**Headless Isaac Sim exits after ~3 s: `Do you accept the EULA? (Yes/No): Unable to bootstrap inner
kit kernel: EOF when reading a line`.** Kit's own check (`/isaac-sim/kit/kit_app.py`) accepts only
`OMNI_KIT_ACCEPT_EULA` in `y` / `yes` / `1`, or an `EULA_ACCEPTED` file; otherwise it prompts on stdin,
and a background process gets EOF. `ACCEPT_EULA=Y` (set in `./Dockerfile`) is read only by
`/isaac-sim/license.sh`, which NVIDIA's entrypoint `runheadless.sh` runs — and these containers
bypass that entrypoint with `--entrypoint bash`. So the gap predates the ROS 2 work; it shows up with
any headless `python.sh` app. Fix: `ENV OMNI_KIT_ACCEPT_EULA=YES` in `Dockerfile.ros2`. Related:
`PRIVACY_CONSENT=Y` in `./Dockerfile` has no effect; NVIDIA's variable is `OMNI_ENV_PRIVACY_CONSENT`,
and setting it opts in to telemetry, so it is deliberately left unset.

**A global `ROS_DISTRO` silently disables Isaac Sim's bundled bridge libs.** An earlier version of
`Dockerfile.ros2` set `ENV ROS_DISTRO=jazzy`. `setup_ros_env.sh` then skipped adding `jazzy/lib` to
`LD_LIBRARY_PATH`, so the bridge could not load its libraries. Removed; Stage 3 now fails if it returns.

**`/opt/ros` missing in the old container.** Image history, not a broken install: its image was
committed before `./Dockerfile` gained its ROS lines.

**Stage 4: `cd: /isaac-sim/agx_arm_ws: Permission denied`.** The workspace is built as the host UID
(1003) so build output belongs to the host user. But `/isaac-sim` is `drwxr-x--- isaac-sim:isaac-sim`
(1234), so UID 1003 is "other" and can't even traverse into the mount point. Fix: `--group-add` with
the group ID read from `/isaac-sim` in the image. Running the build as UID 1234 instead would work
but leave files on the host that the host user can't delete, and this host has no passwordless sudo.

**Gazebo: `create` loops on `Requesting list of world names` forever; `gz service -l` lists nothing.**
Seen when Gazebo ran as a UID with no user entry in the container (`docker run --user 1003`, where
`whoami` fails with `cannot find name for user ID 1003`). The identical launch as the image's
`isaac-sim` user spawned the arm in 4 s. The likely mechanism is gz-transport naming its discovery
partition `<hostname>:<username>`, which can't resolve a username. Run Gazebo as `isaac-sim` (the
container default); setting `GZ_PARTITION` explicitly is an untested alternative.

**`import molmo_spaces.housegen.exporter` fails with `No module named 'prior'`.** Pre-existing, seen in
the old container before any ROS 2 change: `pyproject.toml` lists `prior>=1.0.3` in the housegen extra,
but it isn't installed in Isaac Sim's Python. `import molmo_spaces_isaac` works. Not fixed here.

**`git submodule status` shows `-` (uninitialized) for `agx_arm_urdf`, yet the directory is full.**
The submodule contents were cloned separately, so git's bookkeeping disagrees with the disk. The
script therefore checks for actual `.urdf` files rather than trusting `git submodule status`.

**Build log full of `Failed to resolve user 'systemd-network'` and `Failed to connect to socket
/run/dbus/system_bus_socket`.** Harmless: package post-install hooks expect systemd and D-Bus, which
containers don't run.

**`DISPLAY` inside the container.** `setup_script.sh` passes the creating shell's `DISPLAY` into
`isaac-sim` and warns if it is empty, rather than guessing `:0` (this host's display is `:1`). If a
container ends up with the wrong value, set it per exec: `docker exec -it -e DISPLAY=:1 isaac-sim bash`.

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

# In the container, after ros2env
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

Verified headless (`gui:=false`) in the single `isaac-sim` container: the arm spawned in about 4 s
(`Entity creation successful`) with no errors in the launch log, and all six
`/model/piper/joint/joint1…6/0/cmd_pos` topics exist. Earlier checks, from the two-container setup on
the same Gazebo packages: commanding `joint2` to 1.0 rad moved `link6` from z = 0.213 m to 0.125 m;
while settling, `link6` moved 0.2 mm and 0.004 rad over 5 s (the controllers are PD only,
`I_GAIN = 0`, so raise `I_GAIN` or `P_GAIN` in the launch file if exact holding matters); and the GUI
opened on display `:1`, rendering on the GPU (`gz sim gui` loaded NVIDIA's `libGLX_nvidia` /
`libEGL_nvidia` 580.178.04, listed by `nvidia-smi` as a graphics process). Its
`libEGL warning: egl: failed to create dri2 screen` lines come from Mesa's EGL, which glvnd tries
before selecting NVIDIA's; they are harmless.

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
| Container | `isaac-sim` on `isaac-sim-molmo:ros2-jazzy` (backup: `isaac-sim-pre-ros2` on `:with-molmospaces`) |
| Isaac Sim | 5.1.0 (Python 3.11), container OS Ubuntu 24.04.2 |
| ROS 2 / Gazebo | Jazzy / Harmonic |
| `agx_arm_ros` | `b9ad14d` (branch `ros2`), 4 packages: `agx_arm_msgs`, `agx_arm_ctrl`, `agx_arm_description`, `agx_arm_moveit` |
| `pyAgxArm` | `e7aef17` |
| Display | `:1` |
