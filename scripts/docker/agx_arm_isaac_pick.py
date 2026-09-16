"""Import an AgileX arm URDF into Isaac Sim and pick up a cube.

The arm is driven in joint space and the cube is placed where the fingers
actually are: the script commands a pre-grasp pose, measures the finger midpoint
from the simulation, and spawns the cube there. Nothing is hardcoded from a
guess about the arm's reach.

Sequence: pre-grasp (fingers open) -> spawn cube at the measured midpoint ->
close fingers -> lift -> report whether the cube came up with the gripper.

The URDF must be a plain file with resolvable mesh paths. agx_arm_description
ships xacro with package:// URIs, and Isaac Sim's Python (3.11) can't run xacro
(ROS 2 Jazzy is Python 3.12), so prepare it from a ros2env shell first:

    ros2env
    share=$(ros2 pkg prefix agx_arm_description)/share/agx_arm_description
    xacro $share/agx_arm_urdf/piper/urdf/piper_with_gripper_description.xacro \
        | sed "s#package://agx_arm_description#$share#g" > /tmp/piper_gripper.urdf

Then, from a shell where you have NOT run ros2env:

    /isaac-sim/python.sh /isaac-sim/molmospaces/scripts/docker/agx_arm_isaac_pick.py
"""

import argparse
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--urdf", default="/tmp/piper_gripper.urdf")
parser.add_argument("--gui", action="store_true", help="show the Isaac Sim window")
# 4 cm: the jaws close from a 59 mm outer span to meeting at the midline, so a
# 3 cm cube leaves little margin for the jaw faces to bear on.
parser.add_argument("--cube", type=float, default=0.04, help="cube edge length (m)")
args = parser.parse_args()

from isaacsim import SimulationApp  # noqa: E402

app = SimulationApp({"headless": not args.gui})

import numpy as np  # noqa: E402
import omni.kit.commands  # noqa: E402
import omni.usd  # noqa: E402
from isaacsim.core.api import World  # noqa: E402
from isaacsim.core.api.objects import DynamicCuboid  # noqa: E402
from isaacsim.core.prims import Articulation  # noqa: E402
from isaacsim.core.utils.xforms import get_world_pose  # noqa: E402
from pxr import Sdf, UsdLux, UsdPhysics, UsdShade  # noqa: E402


def add_friction(prim_path, static=1.2, dynamic=1.1, material_path="/World/PhysicsMaterials/grip"):
    """Bind a high-friction physics material to the colliders under prim_path.

    The URDF importer marks each link's `collisions` prim instanceable, so the
    collision meshes inside are instance proxies: a plain stage.Traverse() never
    yields them (an earlier version of this bound 0 robot colliders while
    reporting success), and USD forbids authoring on a proxy. Bind on the
    instanceable wrapper instead — material bindings inherit to descendants.
    The cube is a normal prim and matches on CollisionAPI directly.
    """
    stage_ = omni.usd.get_context().get_stage()
    if not stage_.GetPrimAtPath(material_path):
        material = UsdShade.Material.Define(stage_, material_path)
        phys = UsdPhysics.MaterialAPI.Apply(material.GetPrim())
        phys.CreateStaticFrictionAttr().Set(static)
        phys.CreateDynamicFrictionAttr().Set(dynamic)
        phys.CreateRestitutionAttr().Set(0.0)
    material = UsdShade.Material.Get(stage_, material_path)
    bound = 0
    for prim in stage_.Traverse():
        path = str(prim.GetPath())
        if not path.startswith(prim_path):
            continue
        if not (prim.IsInstanceable() or prim.HasAPI(UsdPhysics.CollisionAPI)):
            continue
        UsdShade.MaterialBindingAPI.Apply(prim).Bind(
            material, UsdShade.Tokens.weakerThanDescendants, "physics"
        )
        bound += 1
    return bound

world = World(stage_units_in_meters=1.0)
world.scene.add_default_ground_plane()
stage = omni.usd.get_context().get_stage()
UsdLux.DistantLight.Define(stage, Sdf.Path("/DistantLight")).CreateIntensityAttr(1000)

status, import_config = omni.kit.commands.execute("URDFCreateImportConfig")
# Attribute form for the fields NVIDIA's own sample script uses; setter methods
# for the drive settings, because `import_config.default_drive_type = 1` raises
# TypeError — that property wants a UrdfJointTargetType enum, while the setter
# takes the int, which is how NVIDIA's own extension.py sets it.
#   fix_base: the arm is bolted down, not free-floating.
#   position drives: the URDF carries no drive information, so without stiffness
#     and damping the joints collapse under gravity.
#   parse_mimic off: gripper_joint1/2 mimic a `gripper` joint in the URDF;
#     driving both finger joints directly keeps the grasp under this script's
#     control (the `gripper` joint's child link imports with no mass and no
#     colliders, so it cannot hold anything anyway).
#   convex_decomp: fingers need collision shapes matching their real geometry.
# Collision meshes come from the URDF's own <collision> STLs, so there is no
# need to derive them from the visuals.
import_config.merge_fixed_joints = False
import_config.fix_base = True
import_config.import_inertia_tensor = True
import_config.distance_scale = 1.0
import_config.convex_decomp = True
import_config.set_parse_mimic(False)
import_config.set_default_drive_type(1)  # UrdfJointTargetType.JOINT_DRIVE_POSITION
import_config.set_default_drive_strength(1e4)
import_config.set_default_position_drive_damping(1e3)

status, robot_path = omni.kit.commands.execute(
    "URDFParseAndImportFile",
    urdf_path=args.urdf,
    import_config=import_config,
    get_articulation_root=True,
)
print(f"IMPORT: status={status} prim_path={robot_path}", flush=True)
if not robot_path:
    print("RESULT: FAIL (import)", flush=True)
    app.close()
    sys.exit(1)

# The importer returns the articulation root (/piper/root_joint), not the robot's
# top prim.
robot_root = robot_path.rsplit("/", 1)[0] or robot_path
FINGER_A = f"{robot_root}/gripper_link1"
FINGER_B = f"{robot_root}/gripper_link2"

world.reset()
for _ in range(60):
    world.step(render=False)

robot = Articulation(robot_root)
robot.initialize()
dof = list(robot.dof_names)
print(f"DOF NAMES ({len(dof)}): {dof}", flush=True)

print(f"FRICTION: bound to {add_friction(robot_root)} robot colliders", flush=True)
# The URDF gives the finger joints effort=10, and the jaws stalled at 0.0163 m
# against the cube instead of reaching the 0.002 command — not enough pinch to
# hold it through a lift. Raise force and stiffness on the two finger drives.
for finger in ("gripper_joint1", "gripper_joint2"):
    drive = UsdPhysics.DriveAPI.Get(stage.GetPrimAtPath(f"{robot_root}/joints/{finger}"), "linear")
    if drive:
        drive.GetMaxForceAttr().Set(500.0)
        drive.GetStiffnessAttr().Set(1e5)
        drive.GetDampingAttr().Set(1e3)
        print(f"  {finger}: maxForce=500 stiffness=1e5", flush=True)
    else:
        print(f"  {finger}: no linear DriveAPI at {robot_root}/joints/{finger}", flush=True)

FINGER_OPEN, FINGER_CLOSED = 0.035, 0.002


def command(arm_positions, finger_gap, settle=90):
    """Hold a joint-space pose; fingers are symmetric (joint2 travels negative)."""
    target = np.zeros((1, len(dof)))
    for name, value in arm_positions.items():
        target[0, dof.index(name)] = value
    target[0, dof.index("gripper_joint1")] = finger_gap
    target[0, dof.index("gripper_joint2")] = -finger_gap
    robot.set_joint_position_targets(target)
    for _ in range(settle):
        world.step(render=False)


def finger_midpoint():
    a, _ = get_world_pose(FINGER_A)
    b, _ = get_world_pose(FINGER_B)
    return (np.asarray(a) + np.asarray(b)) / 2.0


# Measured, not guessed. A joint2 sweep at joint3=-1.85 (scratchpad
# approach_probe.py) gave a family where reach stays ~0.593 m while height moves
# smoothly with joint2, so the hand descends straight onto the cube:
#   joint2=2.20 -> z=0.099   joint2=2.30 -> z=0.043   joint2=2.10 -> z=0.166
# Two assumptions cost earlier attempts: joint angles taken from the URDF limits
# left the fingers 0.24 m above the cube, and lowering joint2 moves the hand DOWN,
# not up. Avoid joint2 >= 2.40 here: the fingers reach the floor and z stops
# responding (the flat z~0.006 rows in the probe).
APPROACH = {"joint1": 0.0, "joint2": 2.20, "joint3": -1.85, "joint4": 0.0, "joint5": 0.60, "joint6": 0.0}
# joint2=2.30 (finger midpoint z=0.043) closed ABOVE the cube: measured jaw
# bodies span z[0.030,0.125] there, while a 3 cm cube tops out at z=0.030 — the
# jaws shut in clear air and only clipped its corner, shoving it 4 cm. 2.35
# interpolates toward the 2.40 row (z=0.007, fingers on the floor) to put the
# jaw underside across the cube instead of above it.
GRASP = dict(APPROACH, joint2=2.35)
LIFT = dict(APPROACH, joint2=2.10)

command(APPROACH, FINGER_OPEN, settle=150)
grasp_point = finger_midpoint()
print(f"FINGER A: {get_world_pose(FINGER_A)[0]}", flush=True)
print(f"FINGER B: {get_world_pose(FINGER_B)[0]}", flush=True)
print(f"GRASP POINT (finger midpoint): {grasp_point}", flush=True)

if grasp_point[2] < args.cube / 2:
    print(f"RESULT: FAIL (grasp point z={grasp_point[2]:.3f} is below the cube's half height)", flush=True)
    app.close()
    sys.exit(1)

# The cube rests on the ground, directly under the approach pose's fingers, so
# the GRASP pose closes around it.
cube = world.scene.add(
    DynamicCuboid(
        prim_path="/World/target_cube",
        name="target_cube",
        position=np.array([grasp_point[0], grasp_point[1], args.cube / 2]),
        scale=np.array([args.cube] * 3),
        color=np.array([0.9, 0.2, 0.2]),
        mass=0.05,
    )
)
for _ in range(60):
    world.step(render=False)
# Both surfaces need the high-friction material; a grippy finger against a
# slippery cube still slides.
print(f"FRICTION: bound to {add_friction('/World/target_cube')} cube colliders", flush=True)
cube_before = cube.get_world_pose()[0]
print(f"CUBE spawned at rest: {cube_before}", flush=True)

# Lower onto the cube, close, then lift.
command(GRASP, FINGER_OPEN, settle=150)
print(f"GRASP POSE finger midpoint: {finger_midpoint()}", flush=True)
command(GRASP, FINGER_CLOSED, settle=150)
grasped_gap = float(robot.get_joint_positions()[0][dof.index("gripper_joint1")])
print(f"FINGER GAP after closing: {grasped_gap:.4f} m (open={FINGER_OPEN}, commanded={FINGER_CLOSED})", flush=True)

command(LIFT, FINGER_CLOSED, settle=200)
cube_after = cube.get_world_pose()[0]
lifted = float(cube_after[2] - cube_before[2])
held = float(np.linalg.norm(np.asarray(cube_after) - finger_midpoint()))
print(f"CUBE after lift: {cube_after}", flush=True)
print(f"LIFT HEIGHT: {lifted:+.4f} m   cube-to-finger distance: {held:.4f} m", flush=True)

# A cube left on the ground scores ~0 lift; one squeezed out sideways ends up far
# from the fingers even if it moved.
# "Within 8 cm of the finger midpoint" is also true of a cube that merely sits
# next to the fingers, and the first capture was ambiguous (the cube looked like
# it floated beside a dark object). Swing joint1: a held cube travels with the
# gripper, a free one stays where it was.
SWEEP = dict(LIFT, joint1=0.35)
cube_pre_sweep = np.asarray(cube.get_world_pose()[0])
command(SWEEP, FINGER_CLOSED, settle=250)
cube_post_sweep = np.asarray(cube.get_world_pose()[0])
mid_post = finger_midpoint()
travelled = float(np.linalg.norm(cube_post_sweep[:2] - cube_pre_sweep[:2]))
still_held = float(np.linalg.norm(cube_post_sweep - mid_post))
print(f"SWEEP: cube travelled {travelled:.4f} m laterally, now {still_held:.4f} m from the fingers", flush=True)

# If the articulation ever came apart, the chain's world positions show it.
print("LINK CHAIN (world positions):", flush=True)
for link in ("base_link", "link1", "link3", "link5", "link6", "gripper_base", "gripper_link1", "gripper_link2"):
    p, _ = get_world_pose(f"{robot_root}/{link}")
    print(f"  {link:14s} [{p[0]:+.3f} {p[1]:+.3f} {p[2]:+.3f}]", flush=True)

if lifted > 0.02 and held < 0.08 and travelled > 0.05 and still_held < 0.08:
    print("RESULT: PASS (cube lifted and carried through a sideways move)", flush=True)
else:
    print("RESULT: FAIL (cube not held)", flush=True)

# Viewport capture framed on the cube itself. Works headless: the render product
# exists without a window, so this needs no X11.
SHOT = "/tmp/agx_arm_pick.png"
try:
    from isaacsim.core.utils.viewports import set_camera_view
    from omni.kit.viewport.utility import capture_viewport_to_file, get_active_viewport

    # Look back along the arm, from beyond the cube towards the base: the jaws
    # approach from the base side, so a camera on the far side has the cube
    # itself between it and the fingers (the first captures looked like a cube
    # floating next to a dark shape for exactly that reason).
    target = [float(cube_post_sweep[0]), float(cube_post_sweep[1]), float(cube_post_sweep[2])]
    reach = float(np.hypot(target[0], target[1])) or 1.0
    eye = [
        target[0] + 0.42 * target[0] / reach,
        target[1] + 0.42 * target[1] / reach,
        target[2] + 0.16,
    ]
    set_camera_view(eye=eye, target=target)
    for _ in range(30):
        world.step(render=True)
    capture_viewport_to_file(get_active_viewport(), SHOT)
    # capture_viewport_to_file completes asynchronously; let the app pump frames.
    for _ in range(60):
        app.update()
    print(f"SCREENSHOT: {SHOT}", flush=True)
except Exception as exc:  # a failed screenshot must not fail the pick result
    print(f"SCREENSHOT FAILED: {exc}", flush=True)

app.close()
