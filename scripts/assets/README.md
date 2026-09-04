# Holodeck → Isaac Sim asset pipeline

Turns a [Holodeck](https://github.com/allenai/Holodeck)-generated scene JSON into a USD stage
that opens directly in Isaac Sim, without going through `housegen`/MuJoCo.

```
Holodeck scene.json
        │
        ├─(1)─ fetch_holodeck_assets.py ──→ pre-converted USD from MolmoSpaces R2
        │                                   └─ missing_assets.json (what wasn't published)
        ├─(2)─ convert_missing_assets.py ─→ USD built locally from objathor .pkl.gz
        │
        └─(3)─ compose_holodeck_scene.py ─→ scene.usda
```

## When to use this instead of housegen

The [main scene pipeline](../../README.md#scene-generation-pipeline) (`housegen` → MJCF →
`molmo_spaces_isaac` → USD) is a *house* pipeline. It compiles THOR-format house JSON — rooms,
walls, doors, windows, materials — and physically settles the result over 20 simulated seconds.

It also hard-requires that structure. `house_converter.py` matches object body names against
`\w+_([0-9a-f]{32})_\d+_\d+_\d+` and reads a sibling `<house>_metadata.json`; bodies matching
neither that pattern nor `wall_`/`room_`/`ceiling_` are **silently dropped**. Point it at an
arbitrary MJCF and you get an empty stage, not an error.

| Use housegen when | Use this pipeline when |
|---|---|
| You need walls, doors, windows, materials authored properly | A single room with furniture is enough |
| You need physically settled object poses | Holodeck's authored poses are good enough (or you'll freeze them) |
| You're producing assets for the MolmoSpaces release format | You want a scene in Isaac Sim now |
| Articulated objects must keep working joints | Static geometry is fine |

This pipeline is **lossy by comparison** — see [Known limitations](#known-limitations).

## Prerequisites

Everything runs inside the `isaac-sim` container. Stage 3 is the exception: it needs only
`numpy`, `scipy` and `pxr` — no Isaac runtime — so it also runs host-side. The `holodeck` conda
env (`~/S_ENG/miniconda3/envs/holodeck`, Python 3.10) already has numpy and scipy, so
`pip install usd-core` there is enough to run stage 3 next to where scenes are generated.

**Mounts** (added by `setup_host.sh`, both optional and skipped if the host dir is absent):

```
~/S_ENG/Holodeck    → /isaac-sim/Holodeck        (ro)   scene JSONs
~/.objathor-assets  → /isaac-sim/objathor-assets (ro)   objathor source for stage 2
```

**Python deps.** The base image ships only `molmospaces_resources`. Stage 2 additionally needs
`housegen`'s dependencies, which are *not* installed unless the `[housegen]` extra actually ran:

```bash
docker exec isaac-sim /isaac-sim/python.sh -m pip install compress_json msgpack open3d prior
```

Versions confirmed working: `compress_json` 1.1.1, `msgpack` 1.2.2, `open3d` 0.19.0, `prior` 1.0.3.

> These land in the container's **writable layer**, not a bind mount. `docker rm` destroys them.
> Re-commit the image (`docker commit isaac-sim isaac-sim-molmo:with-molmospaces`) after
> installing, or you'll reinstall on every container recreation.

**THOR assets.** Fetched separately via the normal downloader, not by stage 1:

```bash
docker exec isaac-sim /isaac-sim/python.sh \
  /isaac-sim/kit/python/bin/ms-download --type usd --install-dir assets/usd --assets thor
```

## Quick start

End to end, for one scene:

```bash
cd /isaac-sim/molmospaces            # inside the container
SCENE=/isaac-sim/Holodeck/data/scenes/<dir>/<name>.json
OBJA=/isaac-sim/molmospaces/assets/isaac-usd/objects/objaverse
THOR=/isaac-sim/.molmospaces/usd/objects/thor/20260128

# 1. pull what MolmoSpaces already publishes
/isaac-sim/python.sh scripts/assets/fetch_holodeck_assets.py \
  --scenes "$SCENE" \
  --cache-dir /isaac-sim/.molmospaces/isaac-thor-resources \
  --symlink-dir /isaac-sim/molmospaces/assets/isaac-usd \
  --out scratch/missing_assets.json

# 2. build whatever step 1 couldn't find
/isaac-sim/python.sh scripts/assets/convert_missing_assets.py \
  --missing-json scratch/missing_assets.json \
  --objathor-dir /isaac-sim/objathor-assets/2023_09_23/assets \
  --mjcf-out scratch/mjcf \
  --usd-out "$(readlink -f $OBJA)" \
  --max-workers 8 --skip-existing

# 3. compose
/isaac-sim/python.sh scripts/assets/compose_holodeck_scene.py \
  --scene "$SCENE" --thor-dir "$THOR" --obja-dir "$OBJA" \
  --out scratch/out/scene.usda
```

Then open `scratch/out/scene.usda` in Isaac Sim (drag-and-drop, or File → Open).

Expect `placed: N / N objects`. Any `skipped` count means an asset still isn't on disk — rerun
stages 1–2, or check the [troubleshooting table](#troubleshooting).

---

## Stage 1 — `fetch_holodeck_assets.py`

Scans scene JSONs, collects every distinct `assetId`, and downloads the pre-converted USD for
each from the MolmoSpaces R2 bucket.

Asset ids come in two flavours and are handled differently:

| Kind | Looks like | How it's resolved |
|---|---|---|
| Objaverse | 32 hex chars (`43f15bff…`) | `index_lookup` → `install_packages` against R2 |
| THOR | a name (`Desk_313_2`) | **Presence check only** against `--thor-dir` |

THOR assets aren't fetched here because their index tokens are archive-based rather than asset
names — `index_lookup("thor", "Desk_313_2")` finds nothing. Get them with `ms-download` instead.

### Options

| Flag | Required | Default | Notes |
|---|---|---|---|
| `--scenes` | yes | — | One or more scene JSONs; globs fine (`.../scenes/*/*.json`) |
| `--cache-dir` | yes | — | Where the resource manager unpacks archives |
| `--symlink-dir` | yes | — | Where the versioned symlink tree is built |
| `--thor-dir` | no | `/isaac-sim/.molmospaces/usd/objects/thor/20260128` | Checked for THOR presence |
| `--out` | no | `missing_assets.json` | Report of what couldn't be fetched |
| `--dry-run` | no | off | Resolve against the index, download nothing |
| `--limit N` | no | all | First N assets per source; for smoke tests |

### Output

Prints per-asset `OK`/`MISS` plus a summary, and writes `--out`:

```json
{"objaverse": {"ok": ["…"], "missing": ["…"]}, "thor": {"ok": ["…"], "missing": []}}
```

`--dry-run` first is worth it: it gives you the coverage number without committing to a download.

### Why not `usda_downloader.py`?

That script targets an older `molmospaces_resources` API and **does not run** against 0.0.2:

| It calls | 0.0.2 provides |
|---|---|
| `archives_with_substring`, `archives_with_number` | `index_lookup` |
| `install_objects`, `install_scenes` | `install_packages` |
| `archives_for_paths` | `find_archives` |
| `scene_root_and_archive_paths` | `source_info` |
| `object_dirs`, `cache_dir` | `source_dir`, `cache_path` |

It also imports `molmo_spaces.molmo_spaces_constants`, which fails on a missing `compress_json`.
`fetch_holodeck_assets.py` is the same idea rewired to the installed API, and driven by a scene
file rather than a dataset index.

---

## Stage 2 — `convert_missing_assets.py`

Builds USD for Objaverse assets MolmoSpaces doesn't publish, from the objathor source you already
have locally. Two sub-stages, mirroring the real MolmoSpaces release pipeline:

1. **objathor `.pkl.gz` → MJCF** — `create_mujoco_model_from_objaverse`, in-process, per UID
2. **MJCF → USD** — `molmo_spaces_isaac.assets.asset_converter --mode convert-all --is-objaverse`,
   one batch call over the whole folder

Output lands as `obja_<uid>/obja_<uid>.usda`, exactly where stage 3's resolver looks.

### Options

| Flag | Required | Default | Notes |
|---|---|---|---|
| `--missing-json` | one of these | — | The report from stage 1 |
| `--uids` | one of these | — | Explicit UID list; good for testing one asset |
| `--objathor-dir` | yes | — | e.g. `/isaac-sim/objathor-assets/2023_09_23/assets` |
| `--mjcf-out` | yes | — | Intermediate MJCF; safe to delete afterwards |
| `--usd-out` | yes | — | Pass the **resolved** path (`readlink -f`), not the symlink |
| `--max-workers` | no | 4 | Stage 2 parallelism (stage 1 is serial) |
| `--skip-existing` | no | off | Skip UIDs already converted — makes reruns cheap |

### The texture-path workaround

objathor `2023_09_23` stores texture paths as absolutes from the machine that generated them:

```
albedoTexturePath: '/root/processed_models/<uid>/albedo.jpg'
```

while the files sit locally as plain basenames (`albedo.jpg`). The converter does
`objaverse_dir / uid / <that path>` and an absolute path wins that join, resolving to a
nonexistent file — the mesh write then fails with `Error opening file '<uid>_visual_0.png'`.

The script stages a corrected copy of each asset (paths rewritten to basenames) into
`<mjcf-out>_staging/` before converting. **Your objathor store is never modified.**

Staging deliberately lives *beside* `--mjcf-out`, not inside it: stage 2 globs every directory
under `--mjcf-out` as a candidate asset and would otherwise try to convert `_staging` itself.

---

## Stage 3 — `compose_holodeck_scene.py`

Builds the USD stage: references each object's asset at its Holodeck pose, and adds what the raw
JSON has no concept of — a floor, walls, a distant light, and a `UsdPhysics.Scene`.

Pure `pxr`. No Isaac runtime, sub-second, safe to run in a tight edit loop.

### Options

| Flag | Required | Default | Notes |
|---|---|---|---|
| `--scene` | yes | — | Holodeck scene JSON |
| `--thor-dir` | yes | — | THOR USD assets |
| `--obja-dir` | yes | — | Objaverse USD assets |
| `--out` | yes | — | Output `.usda` (parent dirs created) |
| `--no-walls` | no | off | Skip wall generation; floor still emitted |
| `--dynamic` | no | off | Let non-`kinematic` objects fall under gravity |
| `--keep-duplicates` | no | off | Disable the exact-position duplicate filter |
| `--snap-to-receptacle` | no | off | Rest `X\|Y`-id objects on Y's actual bbox |

### What it emits

```
/World                    (defaultPrim, Z-up, metres)
├── PhysicsScene          gravity -Z 9.81
├── Light                 DistantLight, intensity 3000
├── Floor                 one slab spanning all rooms' floorPolygon bbox
├── Walls/Wall_N          thin rotated boxes from each wall polygon
└── <object>_N            Xform → reference to the asset USD
```

Floor and walls are colliders with no `RigidBodyAPI` — i.e. static geometry.

---

## How it works

Four things took real investigation. If you're modifying stage 3, read these first.

### Position

Holodeck/Unity is `(x, y-up, z)` and **already in metres** — no unit conversion:

```python
(x, y, z)_unity → (x, z, y)_usd
```

Verified against `usd_assets_metadata.json`: `CD_1` has `bbox_size` 0.112, an 11cm CD.

> Careful: the per-object `vertices` field *is* in centimetres (`1128.78` = 11.29m). It's a 2D
> floorplan footprint, not placement data. Don't mix them up.

### Rotation

The converted USD assets keep their original **Y-up** mesh orientation. The *stage* is Z-up; the
*assets* are not re-axised into it. So the conversion is a stand-up rotation, **not** an axis
permutation:

```python
R = Rx(+90°) · Ry(-ry) · Rx(-rx) · Rz(-rz)     # Unity composes Z, then X, then Y
```

The negations account for Unity's left-handedness; `Rx(+90°)` is the same `FIX_ROTATION` the
asset converter applies.

Confirmed two independent ways:

1. **Bbox shape** — `Plate_27` is `(0.292, 0.012, 0.292)`, thin in **Y**; `Television_13` is
   `(1.051, 0.745, 0.344)` with its height in **Y**.
2. **A published scene** — downloading `holodeck-objaverse-train/train_0` and factoring its
   authored quaternions gives `Rx(90°)·Ry(θ)` for all 26 objects. The formula reproduces them
   exactly: Unity `y=90` → `(0.5, 0.5, -0.5, -0.5)`, Unity `y=0` → `(0.707, 0.707, 0, 0)`.

An axis-permutation matrix was the first approach tried here. It looked plausible and produced a
scene that was wrong in a way you'd only catch by measuring.

### Static objects and joints

Making an object immovable is **not** `kinematicEnabled = True` — the published scenes never use
it. It's removing the rigid body outright:

```python
prim.RemoveAPI("PhysicsRigidBodyAPI")     # published train_0: 24 of these, 0 kinematicEnabled
```

And **every joint underneath must be deactivated**, not just `PhysicsFixedJoint`. `Desk_313_2`
carries 6 `PhysicsPrismaticJoint`s for its drawers; leaving those active after stripping their
bodies produces one PhysX error per joint on load:

```
CreateJoint - no bodies defined at body0 and body1
CreateJoint - cannot create a joint between static bodies
```

`house_converter.py`'s `make_non_articulated_static()` only handles fixed joints — the name is
the warning. Stage 3 generalises it to every joint type.

### Duplicate filtering

Holodeck's LLM placement sometimes emits objects at byte-identical positions — 4 copies of one
desk lamp at a single point was observed in practice. Frozen, they merely overlap; as dynamic
rigid bodies the coincident collision generates enough contact force to launch them apart on the
first physics step.

Filtered by default at `atol=1e-4`, mirroring housegen's `filter_stage_holodeck_duplicates`.
`--keep-duplicates` disables it.

One caveat worth knowing: one observed cluster was 6 *distinct* items (notebook, folders, books)
piled at a single point. Dedup keeps the first and drops the rest, exactly as housegen does —
but housegen then spreads survivors via its settle pass, which this pipeline has no equivalent
of.

### Receptacle snapping (`--snap-to-receptacle`)

Small objects carry a compound id naming what they sit on:

```
"computer monitor-0|office_desk-4 (office)"
                   └── receptacle id
```

Their authored height assumes Holodeck's internal size estimate for that `assetId` matches the
asset actually resolved. Sometimes it doesn't — Holodeck mapped a "computer monitor" to THOR's
`Television_13`, whose real bbox is **0.745m tall** (TV-sized), putting it ~0.5m above the desk.

This is a Holodeck-side data mismatch, not a placement bug: desks and freestanding chairs land at
`z ≈ 0` with the identical maths, and the object sits exactly where its own JSON says
(`position.y = 1.57`, matching the composed transform to 3 decimals). Because these objects are
`kinematic: true`, physics can't correct it even with `--dynamic`.

The flag rewrites **only Z**, resting the object's already-placed bbox on the receptacle's
already-placed bbox. X/Y and orientation untouched; a no-op if either prim wasn't placed. It
fixes height, **not** the size mismatch — that monitor still renders TV-sized, just grounded.

One-level only: a receptacle that is itself compound-id is snapped using its own pre-snap bbox.

---

## Verifying a composed scene

Numbers beat eyeballing. All of these run inside the container.

**Everything resolved, nothing below the floor, no live joints:**

```bash
/isaac-sim/python.sh -c '
from pxr import Usd, UsdGeom, UsdPhysics
s = Usd.Stage.Open("scratch/out/scene.usda"); s.Load()
bc = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
objs = [p for p in s.GetDefaultPrim().GetChildren()
        if p.GetName() not in ("PhysicsScene","Light","Floor","Walls")]
b = bc.ComputeWorldBound(s.GetDefaultPrim()).ComputeAlignedRange()
below = sum(1 for p in objs
            if not bc.ComputeWorldBound(p).ComputeAlignedRange().IsEmpty()
            and bc.ComputeWorldBound(p).ComputeAlignedRange().GetMin()[2] < -0.05)
joints = sum(1 for p in s.Traverse()
             if p.GetTypeName().endswith("Joint") and p.IsActive())
print("objects:", len(objs))
print("bounds :", [round(v,2) for v in b.GetMin()], "→", [round(v,2) for v in b.GetMax()])
print("below floor:", below, " active joints:", joints)'
```

Healthy output for a 12×12m room with 2.7m walls:

```
objects: 117
bounds : [-0.03, -0.03, -0.1] → [12.03, 12.03, 2.7]
below floor: 0  active joints: 0
```

**Bounds are the strongest single signal.** If they exceed the room footprint, or the height
isn't the wall height, the rotation or axis mapping is wrong — that's how the axis-permutation
bug was caught (Y overshot to 12.16 instead of 12.03).

**Find floaters** (objects whose bbox sits well above the floor):

```bash
/isaac-sim/python.sh -c '
from pxr import Usd, UsdGeom
s = Usd.Stage.Open("scratch/out/scene.usda"); s.Load()
bc = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
rows = []
for p in s.GetDefaultPrim().GetChildren():
    if p.GetName() in ("PhysicsScene","Light","Floor","Walls"): continue
    r = bc.ComputeWorldBound(p).ComputeAlignedRange()
    if not r.IsEmpty() and r.GetMin()[2] > 0.15: rows.append((r.GetMin()[2], p.GetName()))
for z, n in sorted(rows, reverse=True)[:20]: print(f"{z:7.3f}  {n}")'
```

Wall-mounted items (clocks, whiteboards, TVs) legitimately appear here. Compare a suspect against
the top of whatever it should rest on before assuming a bug.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: compress_json` / `msgpack` / `open3d` / `prior` | Base image lacks housegen's deps | `pip install compress_json msgpack open3d prior` in the container |
| `AttributeError` from `usda_downloader.py` | Written against an older `molmospaces_resources` | Use `fetch_holodeck_assets.py` |
| Stage 2: `Error opening file '<uid>_visual_0.png'` | objathor absolute texture paths | Handled by staging; if it still fires, confirm `albedo.jpg` exists in the asset dir |
| Stage 2 logs an error for `_staging/_staging.xml` | Staging dir was created inside `--mjcf-out` | Fixed — staging is now a sibling directory |
| Stage 3: `skipped: N` | Asset not on disk | Rerun stages 1–2; check the UID appears under `--obja-dir` |
| Isaac: `CreateJoint - no bodies defined` | Active joints on an object whose bodies were stripped | Fixed in `make_static()`; regenerate the scene |
| Objects explode on Play | Coincident duplicates | On by default; don't pass `--keep-duplicates` with `--dynamic` |
| Small objects float above furniture | Holodeck size estimate ≠ resolved asset | `--snap-to-receptacle` |
| Objects float with *nothing* underneath | The receptacle itself was skipped | Convert the missing receptacle asset (stage 2) |
| Nothing moves on Play | Default is fully static | `--dynamic` |
| Scene bounds exceed the room | Rotation/axis mapping wrong | See [Rotation](#rotation) |

---

## Known limitations

Relative to the real `housegen` pipeline, this trades fidelity for directness:

- **No physics settle.** housegen steps 20 simulated seconds and writes resting poses back.
  Holodeck's raw poses are LLM-proposed and never physically resolved, so interpenetration
  survives into the output. Freezing (the default) hides it; `--dynamic` exposes it.
- **No materials.** Walls and floor are untextured boxes. housegen authors real materials from
  the scene's `floorMaterial` / `wallMaterial` / skybox fields.
- **No doors or windows.** housegen cuts apertures out of wall meshes and rebuilds each wall as
  3–4 convex colliders. Here, walls are solid boxes and door/window entries are ignored.
- **Articulation is dropped for static objects.** Frozen desks don't have working drawers.
- **Asset size mismatches aren't corrected.** `--snap-to-receptacle` fixes placement height, not
  an asset that's simply the wrong size for its semantic role.
- **Ceilings, lights from the scene JSON, and `proceduralParameters` are ignored** — one
  `DistantLight` is emitted instead.

If you need any of those, use `housegen` and accept its input requirements.

## Reference

**Asset layout on disk**

```
<obja-dir>/obja_<uid>/obja_<uid>.usda      Objaverse (fetched or locally converted)
<thor-dir>/<AssetId>/<AssetId>.usda        THOR (via ms-download)
```

Both carry their own `Textures/` and embedded physics; stage 3 references the `.usda` directly
and never re-authors physics APIs on it.

**Scene JSON fields consumed** — `objects[]` and `structuralObjects[]` (`assetId`, `id`,
`position`, `rotation`, `kinematic`, `children`), `rooms[].floorPolygon`, `walls[].polygon`.
Everything else in the file is ignored by stage 3.
