"""Compose a Holodeck scene JSON into an Isaac-ready USD stage.

Reads a Holodeck scene directly (no MuJoCo/housegen step), references each
object's pre-converted USD asset at its Holodeck pose, and adds the shell the
raw JSON doesn't carry: floor, walls, a light, and a physics scene.

Needs only `pxr` (usd-core) -- no Isaac Sim runtime -- so it runs in well under
a second and can be iterated on quickly. Open the resulting scene.usda in Isaac
Sim when you want to look at it.

    /isaac-sim/python.sh scripts/assets/compose_holodeck_scene.py \
        --scene /isaac-sim/Holodeck/data/scenes/<dir>/<name>.json \
        --thor-dir /isaac-sim/.molmospaces/usd/objects/thor/20260128 \
        --obja-dir /isaac-sim/molmospaces/assets/isaac-usd/objects/objaverse \
        --out /isaac-sim/molmospaces/scratch/out/scene.usda

Assets that aren't on disk yet are logged and skipped, so this is useful before
the whole asset set has been fetched or converted.
"""

import argparse
import json
import re
from pathlib import Path

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdPhysics
from scipy.spatial.transform import Rotation as R

HEX32 = re.compile(r"^[0-9a-f]{32}$")

WALL_THICKNESS = 0.05
FLOOR_THICKNESS = 0.10


def unity_to_usd_pos(p: dict) -> Gf.Vec3d:
    return Gf.Vec3d(float(p["x"]), float(p["z"]), float(p["y"]))


def unity_to_usd_quat(rot: dict) -> Gf.Quatf:
    """Convert a Holodeck Euler rotation into a USD orientation.

    The converted USD assets keep their original Unity **Y-up** geometry -- the
    stage is Z-up, but the asset meshes are not re-axised (confirmed by bbox:
    Plate_27 is (0.292, 0.012, 0.292), thin in Y; Television_13 has its height
    in Y). So the rotation needs no axis permutation. Two steps only:

      1. Unity is left-handed and composes Euler angles Z, then X, then Y, so
         the same numeric angles in scipy's right-handed convention are negated.
      2. Rx(+90) stands the Y-up asset up into the Z-up stage. This is the same
         FIX_ROTATION the asset converter uses.

    Verified against the published holodeck-objaverse-train/train_0 scene: Unity
    y=90 gives quat (0.5, 0.5, -0.5, -0.5) and y=0 gives (0.707, 0.707, 0, 0),
    both exactly matching that scene's authored orientations.
    """
    rx, ry, rz = float(rot.get("x", 0)), float(rot.get("y", 0)), float(rot.get("z", 0))
    r = (
        R.from_euler("y", -ry, degrees=True)
        * R.from_euler("x", -rx, degrees=True)
        * R.from_euler("z", -rz, degrees=True)
    )
    q = (R.from_euler("x", 90, degrees=True) * r).as_quat()  # scipy returns x, y, z, w
    return Gf.Quatf(float(q[3]), Gf.Vec3f(float(q[0]), float(q[1]), float(q[2])))


def sanitize(name: str, idx: int) -> str:
    clean = re.sub(r"\W+", "_", name).strip("_")
    return f"{clean or 'obj'}_{idx}"


def resolve_asset(asset_id: str, thor_dir: Path, obja_dir: Path) -> Path | None:
    if HEX32.match(asset_id):
        path = obja_dir / f"obja_{asset_id}" / f"obja_{asset_id}.usda"
    else:
        path = thor_dir / asset_id / f"{asset_id}.usda"
    return path if path.is_file() else None


def define_box(stage, path, center, half_extents, rotation_z_deg=0.0):
    """A unit UsdGeom.Cube scaled to the given half extents, as a static collider."""
    xform = UsdGeom.Xform.Define(stage, path)
    xform.AddTranslateOp().Set(Gf.Vec3d(*center))
    if rotation_z_deg:
        xform.AddRotateZOp().Set(float(rotation_z_deg))
    xform.AddScaleOp().Set(Gf.Vec3f(*[float(h * 2) for h in half_extents]))

    cube = UsdGeom.Cube.Define(stage, path.AppendChild("Geom"))
    cube.GetSizeAttr().Set(1.0)
    # Collider with no RigidBodyAPI == immovable static geometry.
    UsdPhysics.CollisionAPI.Apply(cube.GetPrim())
    return xform


def add_floor(stage, house, root):
    """One slab covering the bounding box of every room's floor polygon."""
    pts = [
        (float(v["x"]), float(v["z"]))
        for room in house.get("rooms", [])
        for v in room.get("floorPolygon", [])
    ]
    if not pts:
        return
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    hx, hy = (max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 2
    define_box(
        stage,
        root.AppendChild("Floor"),
        (cx, cy, -FLOOR_THICKNESS / 2),
        (hx, hy, FLOOR_THICKNESS / 2),
    )


def add_walls(stage, house, root):
    """Each wall polygon is a vertical quad; rebuild it as a thin rotated box."""
    scope = UsdGeom.Scope.Define(stage, root.AppendChild("Walls")).GetPrim().GetPath()
    count = 0
    for i, wall in enumerate(house.get("walls", [])):
        poly = wall.get("polygon", [])
        if len(poly) < 4:
            continue
        # Floor-plane footprint: the two distinct (x, z) corners.
        xz = sorted({(round(float(v["x"]), 4), round(float(v["z"]), 4)) for v in poly})
        if len(xz) < 2:
            continue
        (x0, z0), (x1, z1) = xz[0], xz[-1]
        height = max(float(v["y"]) for v in poly)
        if height <= 0:
            continue

        length = float(np.hypot(x1 - x0, z1 - z0))
        if length <= 0:
            continue
        yaw = float(np.degrees(np.arctan2(z1 - z0, x1 - x0)))

        define_box(
            stage,
            scope.AppendChild(f"Wall_{i}"),
            ((x0 + x1) / 2, (z0 + z1) / 2, height / 2),
            (length / 2, WALL_THICKNESS / 2, height / 2),
            rotation_z_deg=yaw,
        )
        count += 1
    return count


def make_static(prim: Usd.Prim):
    """Turn an object into immovable static geometry.

    Based on make_non_articulated_static() in molmo_spaces_isaac's house_converter,
    but generalized: that helper is for non-articulated objects and only kills
    PhysicsFixedJoints. Articulated assets (e.g. Desk_313_2 has 6 sliding-drawer
    PhysicsPrismaticJoints) also carry non-fixed joints, so we deactivate EVERY
    joint. Two parts, and both matter:

      1. REMOVE RigidBodyAPI rather than setting kinematicEnabled. A collider
         with no rigid body is static; the published train_0 scene has 24
         `delete apiSchemas = ["PhysicsRigidBodyAPI"]` and zero kinematicEnabled.
      2. Deactivate every joint prim. Once the bodies a joint connects lose their
         RigidBodyAPI, an active joint references nothing -- that is the "no
         bodies defined at body0 and body1" error (and, when both ends resolve to
         static geometry, "cannot create a joint between static bodies"). A frozen
         desk doesn't need working drawers, so dropping all its joints is correct.
    """
    if prim.HasAPI("PhysicsRigidBodyAPI"):
        prim.RemoveAPI("PhysicsRigidBodyAPI")
    for child in prim.GetChildren():
        if child.GetTypeName().endswith("Joint"):
            child.SetActive(False)
        else:
            make_static(child)


def snap_to_receptacles(objects: list[dict], id_to_prim: dict) -> int:
    """Opt-in fix for objects placed at a Holodeck-authored height that doesn't
    match the actual size of the asset we rendered.

    Small objects get a compound id like "computer monitor-0|office_desk-4
    (office)" -- the part after "|" names the receptacle they're supposed to sit
    on. Trusting Holodeck's authored Y for these assumes Holodeck's internal size
    estimate for that assetId matches ours; it doesn't always (observed case: a
    "computer monitor" mapped to THOR's Television_13, whose real bbox is
    0.745m tall -- TV-sized, not monitor-sized -- so the authored height put it
    ~0.5m above the desk it's meant to rest on. Verified this is a data issue,
    not a placement bug: desks and freestanding chairs land at z=~0 with the
    exact same math). Since these objects are typically kinematic, physics can't
    correct this even in --dynamic mode.

    Rewrites just the Z of the object's translate op to rest its already-placed
    bbox directly on top of the receptacle's already-placed bbox, keeping X/Y
    and orientation untouched. Silently skipped if either prim didn't get placed
    (missing asset) or isn't a compound id -- never invents a position.

    One-level only: if a receptacle is itself compound-id (sitting on another
    receptacle), it's snapped using its OWN pre-snap bbox, since dict iteration
    order isn't guaranteed to process it first. Rare enough in practice not to
    warrant a topological sort here.
    """
    bc = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    count = 0
    for obj in objects:
        oid = obj.get("id", "")
        if "|" not in oid:
            continue
        prim = id_to_prim.get(oid)
        recep_prim = id_to_prim.get(oid.split("|", 1)[1])
        if prim is None or recep_prim is None:
            continue

        recep_range = bc.ComputeWorldBound(recep_prim).ComputeAlignedRange()
        obj_range = bc.ComputeWorldBound(prim).ComputeAlignedRange()
        if recep_range.IsEmpty() or obj_range.IsEmpty():
            continue

        half_height = (obj_range.GetMax()[2] - obj_range.GetMin()[2]) / 2.0
        target_z = recep_range.GetMax()[2] + half_height

        t = prim.GetAttribute("xformOp:translate").Get()
        if t is None:
            continue
        prim.GetAttribute("xformOp:translate").Set(Gf.Vec3d(t[0], t[1], target_z))
        count += 1
    return count


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scene", type=Path, required=True)
    ap.add_argument("--thor-dir", type=Path, required=True)
    ap.add_argument("--obja-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--no-walls", action="store_true")
    ap.add_argument(
        "--keep-duplicates",
        action="store_true",
        help="Keep objects at identical positions. They explode as dynamic rigid "
        "bodies, but in a static scene they only overlap -- keep them for a fuller "
        "render. Off by default (dedup on), matching housegen.",
    )
    ap.add_argument(
        "--dynamic",
        action="store_true",
        help="Let objects fall under gravity. Off by default: raw Holodeck poses "
        "overlap, so a dynamic first frame explodes.",
    )
    ap.add_argument(
        "--snap-to-receptacle",
        action="store_true",
        help="Rest each receptacle-relative object (id like 'X|Y') directly on "
        "top of Y's actual placed bbox, instead of trusting Holodeck's authored "
        "height. Fixes floaters caused by Holodeck assuming a different-sized "
        "asset than the one actually resolved. Off by default -- most objects "
        "don't need it; only turn it on if you see floaters after inspecting.",
    )
    args = ap.parse_args()

    house = json.loads(args.scene.read_text())
    args.out.parent.mkdir(parents=True, exist_ok=True)

    stage = Usd.Stage.CreateNew(str(args.out))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    root = Sdf.Path("/World")
    world = UsdGeom.Xform.Define(stage, root)
    stage.SetDefaultPrim(world.GetPrim())

    scene = UsdPhysics.Scene.Define(stage, root.AppendChild("PhysicsScene"))
    scene.CreateGravityDirectionAttr(Gf.Vec3f(0, 0, -1))
    scene.CreateGravityMagnitudeAttr(9.81)

    light = UsdLux.DistantLight.Define(stage, root.AppendChild("Light"))
    light.CreateIntensityAttr(3000.0)
    light.CreateAngleAttr(0.53)

    add_floor(stage, house, root)
    n_walls = 0 if args.no_walls else add_walls(stage, house, root)

    objects = house.get("objects", []) + house.get("structuralObjects", [])

    # Drop exact positional duplicates. Holodeck's LLM placement sometimes emits
    # two objects at the same point; as dynamic rigid bodies they generate a huge
    # contact force and launch apart. Mirrors filter_stage_holodeck_duplicates in
    # housegen (builder.py), which deletes root bodies sharing a position at
    # atol=1e-4. Tight tolerance so genuinely-adjacent objects are kept.
    dups = 0
    if not args.keep_duplicates:
        deduped, seen = [], []
        for obj in objects:
            p = obj.get("position", {})
            v = (round(float(p.get("x", 0)), 4), round(float(p.get("y", 0)), 4), round(float(p.get("z", 0)), 4))
            if any(abs(v[0] - s[0]) < 1e-4 and abs(v[1] - s[1]) < 1e-4 and abs(v[2] - s[2]) < 1e-4 for s in seen):
                dups += 1
                continue
            seen.append(v)
            deduped.append(obj)
        objects = deduped

    placed, skipped = 0, []
    id_to_prim: dict[str, Usd.Prim] = {}

    def place(obj: dict, parent: Sdf.Path, idx: int):
        nonlocal placed
        asset_id = obj.get("assetId", "")
        usd = resolve_asset(asset_id, args.thor_dir, args.obja_dir)
        if usd is None:
            skipped.append(asset_id)
            return

        path = parent.AppendChild(sanitize(obj.get("id", asset_id), idx))
        xform = UsdGeom.Xform.Define(stage, path)
        xform.GetPrim().GetReferences().AddReference(str(usd))

        xform.AddTranslateOp().Set(unity_to_usd_pos(obj["position"]))
        xform.AddOrientOp().Set(unity_to_usd_quat(obj.get("rotation", {})))

        if not args.dynamic or obj.get("kinematic", False):
            make_static(xform.GetPrim())

        placed += 1
        if oid := obj.get("id"):
            id_to_prim[oid] = xform.GetPrim()
        for j, child in enumerate(obj.get("children", [])):
            place(child, path, j)

    for i, obj in enumerate(objects):
        place(obj, root, i)

    snapped = snap_to_receptacles(objects, id_to_prim) if args.snap_to_receptacle else 0

    stage.Save()

    print(f"scene:   {args.scene.name}")
    print(f"floor:   1 slab, walls: {n_walls}")
    print(f"deduped: {dups} exact-duplicate objects removed")
    print(f"placed:  {placed} / {len(objects)} objects")
    if args.snap_to_receptacle:
        print(f"snapped: {snapped} receptacle-relative objects onto their surface")
    if skipped:
        uniq = sorted(set(skipped))
        print(f"skipped: {len(skipped)} instances, {len(uniq)} unique assets not on disk")
        for a in uniq[:10]:
            print(f"           {a}")
        if len(uniq) > 10:
            print(f"           ... and {len(uniq) - 10} more")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
