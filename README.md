# CrazySwarm2 / Isaac Sim / MolmoSpaces Setup

Infrastructure and setup notes for running [MolmoSpaces](https://github.com/allenai/molmospaces) on
[Isaac Sim 5.1.0](https://docs.isaacsim.omniverse.nvidia.com/) inside Docker, for the CrazySwarm2
drone-swarm rig.

## Contents

- [`Dockerfile`](./Dockerfile) — builds `isaac-sim-molmo:latest` from NVIDIA's official Isaac Sim
  5.1.0 image, with every environment-level fix discovered while setting this up baked in.
- [`setup_host.sh`](./setup_host.sh) — staged, idempotent host bring-up script: driver → Docker
  Engine → NVIDIA Container Toolkit → X11 → repo checkout → container → pip installs.

## Requirements

- An NVIDIA GPU. This rig runs on a laptop RTX 4050 (6GB VRAM) — Isaac Sim itself is the
  primary consumer; budget accordingly if you also plan to run anything else on the same GPU
  (see [Holodeck / local LLMs](#holodeck-llm-scene-generation) below).
- Ubuntu (tested on 24.04 "noble").
- NVIDIA driver branch R580 confirmed working on this rig. Newer branches (R595) have caused
  `librtx.scenedb.plugin.so` segfaults here — pin to R580 unless you've confirmed otherwise.

## Quick Start

```bash
git clone <your-molmospaces-fork-url> ~/S_ENG/molmospaces
cd ~/S_ENG/isaac-sim-molmo-setup   # wherever this Dockerfile + setup_host.sh live
chmod +x setup_host.sh
MOLMO_REPO_URL=<your-molmospaces-fork-url> ./setup_host.sh
```

`setup_host.sh` is safe to re-run — every stage after the driver install checks whether it's
already done and skips ahead, including the pip installs in Stage 7 (checked by trying to
import `molmo_spaces_isaac` and `molmo_spaces.housegen.exporter` inside the container before
reinstalling anything). A rerun against an already-set-up rig is fast and doesn't touch the
network. The driver stage requires a manual reboot; the script stops and tells you when that's
needed rather than trying to push through unattended (a failed DKMS build or secure-boot
signing step during an unattended driver swap is a real way to lose your display session).

To force a clean reinstall of the MolmoSpaces packages specifically (skipping the driver/Docker/
container stages, which don't need it):
```bash
docker exec -u root -it isaac-sim rm -rf /isaac-sim/kit/python/lib/python3.11/site-packages/molmo_spaces*
./setup_host.sh
```

## Architecture

Three pieces, three different degrees of coupling to Isaac Sim — this distinction matters for
where each one should actually run:

| Piece | Depends on Isaac Sim's Python? | Where it runs |
|---|---|---|
| `molmo_spaces_isaac` | Yes — imports Isaac Sim/Kit modules directly | Inside the container, no alternative |
| `housegen` (root `molmo_spaces` package) | No — deps are just `open3d`, `p_tqdm`, `prior` | Inside the container currently, for convenience; not required |
| [Holodeck](https://github.com/allenai/Holodeck) | No — separate repo entirely, only output is a JSON file | Recommended: a separate host-side venv |

### Bind mounts

Four mounts — two required, two optional and only added when the host directory exists:

```bash
-v "$MOLMO_REPO_PATH":/isaac-sim/molmospaces         # git-tracked code
-v "$MOLMO_CACHE_PATH":/isaac-sim/.molmospaces        # ms-download's asset cache
-v "$HOLODECK_PATH":/isaac-sim/Holodeck:ro            # optional — Holodeck scene output
-v "$OBJATHOR_PATH":/isaac-sim/objathor-assets:ro     # optional — objathor source assets
```

`HOLODECK_PATH` (default `~/S_ENG/Holodeck`) and `OBJATHOR_PATH` (default `~/.objathor-assets`)
feed the [scene pipeline](#holodeck-scene--isaac-usd-pipeline) below — mounted read-only since
the container only ever reads from them.

Keeping the first two separate matters: `ms-download`'s asset cache can grow to many GB and has
no business anywhere near `git add`. It's also why deleting and recreating the container (`docker
rm`) doesn't lose downloaded assets — every mount above points at a real directory on the host, so
only things living in the container's own writable layer would be lost.

**That writable layer is not nothing, though — check what's actually installed there before
recreating a container.** `pip install -e .[dev,sim]` (Quick Start, Stage 7) writes into
`/isaac-sim/kit/python/lib/.../site-packages`, which is *not* a bind mount — it's the writable
layer, gone on `docker rm`. The base image built from this repo's `Dockerfile` (tagged `:latest`)
does **not** include that install; only a container that's actually had Stage 7 run in it does.
Before recreating such a container for any reason (fixing a mount, adding one, anything), commit
it first so the recreation starts from a state that still has the install:

```bash
docker commit isaac-sim isaac-sim-molmo:with-molmospaces
docker rm isaac-sim   # now safe — pip installs are preserved in the new tag
```

`setup_host.sh` picks `:with-molmospaces` automatically when present, falling back to `:latest`
otherwise — see Stage 6. This distinction is easy to lose track of: a `:latest`-based recreation
looks identical right up until the next `import molmo_spaces_isaac` fails.

### Always invoke Python through the wrapper

Isaac Sim's bundled Python (3.11) is not exposed as plain `python3`/`pip` — it must always be
invoked via `/isaac-sim/python.sh`, which sets extra `PYTHONPATH` entries (Kit's bundled
`extscache` packages) before launching the real interpreter. Console scripts installed under it
(like `ms-download`) have a shebang pointing at the *raw* interpreter, which bypasses that setup
if run directly — always invoke as:

```bash
docker exec -it isaac-sim bash -c "/isaac-sim/python.sh /isaac-sim/kit/python/bin/<script> ..."
```

not as a bare command, even with its directory on `PATH`.

## Scene Generation Pipeline

Traced from source, not guessed — three stages:

**Stage 1 — generation** (decides scene content, outputs a JSON):

| Source | Method | Needs an API key? | Room types |
|---|---|---|---|
| iTHOR | Hand-crafted, fixed set | No | Kitchen, LivingRoom, Bedroom, Bathroom (120 scenes) |
| ProcTHOR | Procedural algorithm | No | Same fixed 4 room types |
| Holodeck | LLM-driven (GPT-4o by default) | Yes — OpenAI, GPT-4o access | Arbitrary, from a free-text query |

**Stage 2 — `housegen`** (`molmo_spaces.housegen.exporter:main`): loads the JSON, builds it via
`MlSpacesSceneBuilder`, exports **MuJoCo XML (MJCF)**. Pure converter — no generation logic, no
LLM calls.

**Stage 3 — `molmo_spaces_isaac`**: converts that MJCF into **USD**, the format Isaac Sim loads.
Its own README states this directly: "Code for converting assets and scenes from MolmoSpaces in
`mjcf` format into `usd` format that can be loaded into IsaacSim."

The big `ms-download` bundles (`ithor`, `procthor-10k`, `procthor-objaverse`,
`holodeck-objaverse-{train,val}`) are this entire pipeline already run once by the Ai2 team and
packaged as ready USD — that's why they drag-and-drop straight into Isaac Sim. To get a scene
that *isn't* already in one of those pre-generated bundles (e.g. a specific "office" query), all
three stages need to run yourself.

### Downloading pre-built scenes

```bash
docker exec -it isaac-sim bash -c \
  "/isaac-sim/python.sh /isaac-sim/kit/python/bin/ms-download --help"
```
Check the real flag names before assuming — this session found both `--assets` and `--scenes`
referenced at different points; confirm against your installed version's `--help` output rather
than copying a command from memory. Exact dataset names (with engine + date stamp) are listed on
the [`allenai/molmospaces` Hugging Face dataset page](https://huggingface.co/datasets/allenai/molmospaces).

## Holodeck (LLM Scene Generation)

Holodeck **composes** scenes (floor plan, object selection and placement via an LLM) — it does
not generate 3D assets. Objects are retrieved from [Objaverse](https://objaverse.allenai.org/)
(~800K pre-existing 3D models), not modeled from scratch.

### Recommended: run it in its own venv, not the container

```bash
python3 -m venv ~/S_ENG/holodeck-env
source ~/S_ENG/holodeck-env/bin/activate
git clone <your-fork-of-Holodeck> ~/S_ENG/Holodeck
cd ~/S_ENG/Holodeck
pip install -r requirements.txt
python main.py --query "an office" --openai_api_key <OPENAI_API_KEY>
```

This produces a JSON file — that's the handoff point into `housegen` (Stage 2 above), which does
need to run inside the container. No Isaac Sim dependency exists on the Holodeck side at all.

### Model choice

Default is `gpt-4o-2024-05-13`, set in `ai2holodeck/constants.py`. Both `gpt-4o` and
`gpt-4o-mini` remain available via the OpenAI API (the 2026 ChatGPT-app retirements do not affect
API access). Current pricing per 1M tokens:

| Model | Input | Output |
|---|---|---|
| `gpt-4o` | \$2.50 | \$10.00 |
| `gpt-4o-mini` | \$0.15 | \$0.60 |

`gpt-4o-mini` is a real, ~16x cost reduction but an unofficial swap — the authors validated their
prompts against full `gpt-4o`; test output quality on your own queries before relying on it.

**Local (Ollama) is possible but not recommended on constrained hardware.** LangChain's
`langchain_ollama.OllamaLLM` is API-compatible as a drop-in for the `langchain.llms.OpenAI` class
Holodeck already uses. The real constraint is VRAM: a 6GB card fits a quantized 7B–8B model with
little headroom, which is a meaningful capability drop versus GPT-4o for structured spatial
reasoning. If attempting it, run Holodeck+Ollama as a standalone step with Isaac Sim closed
first — generation happens before anything touches Isaac Sim, so they don't need to run
concurrently on the same GPU.

## Holodeck Scene → Isaac USD Pipeline

The [Scene Generation Pipeline](#scene-generation-pipeline) above is the *house* pipeline —
`housegen` compiles THOR-format JSON (rooms, walls, doors, windows) into a settled MuJoCo scene,
which `molmo_spaces_isaac` then converts to USD. It hard-requires that house structure: object
body names must match a specific `\w+_[0-9a-f]{32}_\d+_\d+_\d+` pattern and a sibling
`_metadata.json`, or conversion silently drops the object.

For a Holodeck scene that's just furniture in a single room — no need for `housegen`'s wall/door/
window authoring or its 20-second physics settle — three scripts under
[`scripts/assets/`](./scripts/assets/) go straight from Holodeck's JSON to a loadable USD stage,
skipping the MuJoCo step entirely:

| Script | Stage | Input → Output |
|---|---|---|
| [`fetch_holodeck_assets.py`](./scripts/assets/fetch_holodeck_assets.py) | 1 | Scene JSON → pre-converted USD assets pulled from the MolmoSpaces R2 bucket |
| [`convert_missing_assets.py`](./scripts/assets/convert_missing_assets.py) | 2 (optional) | objathor `.pkl.gz` source → USD, for any asset step 1 didn't find |
| [`compose_holodeck_scene.py`](./scripts/assets/compose_holodeck_scene.py) | 3 | Scene JSON + the USD assets above → one `scene.usda` |

All three run inside the container via `/isaac-sim/python.sh`. Stage 3 needs only `pxr`
(`usd-core`), not the Isaac Sim runtime, so it's near-instant and safe to iterate on.

### 1. Fetch pre-converted assets

```bash
docker exec isaac-sim bash -c 'cd /isaac-sim/molmospaces && /isaac-sim/python.sh \
  scripts/assets/fetch_holodeck_assets.py \
  --scenes /isaac-sim/Holodeck/data/scenes/*/*.json \
  --cache-dir /isaac-sim/.molmospaces/isaac-thor-resources \
  --symlink-dir /isaac-sim/molmospaces/assets/isaac-usd'
```

Scans every scene's `objects`, splits ids into THOR names vs. 32-hex Objaverse UIDs, and for each
Objaverse UID calls `ResourceManager.index_lookup` / `install_packages` against the same R2
bucket `usda_downloader.py` targets. **`usda_downloader.py` itself does not run** — it's written
against an older `molmospaces_resources` API (`archives_with_substring`, `install_objects`, …)
that doesn't match what's actually installed (0.0.2, which has `index_lookup` /
`install_packages` instead). `fetch_holodeck_assets.py` is the same idea, rewired to the current
API. THOR assets aren't looked up this way — their index tokens are archive-based, not asset
names — so the script just checks presence against `--thor-dir` (get those via the normal
`ms-download --assets thor`).

Writes a `missing_assets.json` next to wherever you run it, listing every UID that isn't in the
published set — feeds directly into step 2. `--dry-run` resolves everything without downloading,
useful for a coverage check first.

### 2. Convert what's missing

Only needed if step 1 reported misses. Two sub-stages, matching the real MolmoSpaces release
pipeline:

```bash
docker exec isaac-sim bash -c 'cd /isaac-sim/molmospaces && /isaac-sim/python.sh \
  scripts/assets/convert_missing_assets.py \
  --missing-json missing_assets.json \
  --objathor-dir /isaac-sim/objathor-assets/2023_09_23/assets \
  --mjcf-out scratch/mjcf \
  --usd-out /isaac-sim/molmospaces/assets/isaac-usd/objects/objaverse \
  --max-workers 8 --skip-existing'
```

- **objathor → MJCF**: `molmo_spaces.housegen.utils.create_mujoco_model_from_objaverse`, called
  in-process per UID.
- **MJCF → USD**: `molmo_spaces_isaac.assets.asset_converter --mode convert-all --is-objaverse`,
  run once over the whole batch.

Needs `compress_json`, `msgpack`, `open3d`, `prior` — none shipped in the base image, only
`housegen`'s own dependency list (root `pyproject.toml`'s `[housegen]` extra) when actually
installed. `pip install compress_json msgpack open3d prior` inside the container if
`create_mujoco_model_from_objaverse` raises `ModuleNotFoundError`.

**Known data issue, worked around automatically:** the objathor `2023_09_23` release stores each
asset's texture paths as absolutes from the box that generated them (e.g.
`/root/processed_models/<uid>/albedo.jpg`), while the files sit locally as plain basenames. The
converter joins path-onto-directory and expects a basename, so an absolute path resolves to
nothing and the mesh write fails. `convert_missing_assets.py` stages a corrected copy of each
asset (paths rewritten to basenames) before conversion — your objathor store itself is never
touched.

### 3. Compose the scene

```bash
docker exec isaac-sim bash -c 'cd /isaac-sim/molmospaces && /isaac-sim/python.sh \
  scripts/assets/compose_holodeck_scene.py \
  --scene /isaac-sim/Holodeck/data/scenes/<dir>/<name>.json \
  --thor-dir /isaac-sim/.molmospaces/usd/objects/thor/20260128 \
  --obja-dir /isaac-sim/molmospaces/assets/isaac-usd/objects/objaverse \
  --out scratch/out/scene.usda'
```

Add `--dynamic` to let non-`kinematic` objects fall under gravity instead of freezing every
object as static geometry (the default). Prints a placed/skipped count; anything skipped means
its asset still isn't on disk anywhere — back to step 1/2.

If small objects are floating above the furniture they're supposed to sit on, add
`--snap-to-receptacle` — see the last bullet below before reaching for it.

Four things worth knowing if you're editing this script or debugging a scene it produced:

- **Position**: Holodeck/Unity is `(x, y-up, z)` in metres already — `(x, y, z) → (x, z, y)`, no
  unit scaling. (Confirmed independently against `usd_assets_metadata.json`'s `bbox_size`
  entries, e.g. `CD_1` is 0.112 — an 11cm CD.)
- **Rotation**: the converted USD assets keep their original **Y-up** mesh orientation — the
  *stage* is Z-up, the *assets* are not re-axised into it. Confirmed two independent ways: bbox
  shape (`Plate_27` is `(0.292, 0.012, 0.292)`, thin in Y) and by downloading a published
  `holodeck-objaverse-train/train_0` scene and reverse-engineering its authored quaternions,
  every one of which factors as `Rx(+90°) · Ry(θ)`. The conversion is `Rx(+90°)` applied to
  Holodeck's Euler angles negated for its left-handed convention — **not** an axis-permutation
  matrix, which was the first (wrong) approach tried here and looked plausible until checked
  against real data.
- **Static objects**: making an object immovable means `RemoveAPI("PhysicsRigidBodyAPI")`, not
  `kinematicEnabled = True` — the published scenes never use the latter. For an *articulated*
  asset (e.g. `Desk_313_2` has 6 `PhysicsPrismaticJoint`s for its drawers), every joint under it
  must also be deactivated, not just `PhysicsFixedJoint`s — PhysX errors ("no bodies defined at
  body0 and body1") the moment a joint references a body that just lost its rigid-body API.
- **Exact-position duplicates**: on by default, Holodeck occasionally places two+ objects at
  literally the same point (desk lamp ×4 at one position was observed in practice) — harmless in
  a frozen scene, but as dynamic rigid bodies the coincident collision generates enough contact
  force to launch them apart on the first physics step. Filtered at load (mirrors housegen's
  `filter_stage_holodeck_duplicates`, same tolerance); `--keep-duplicates` to disable.
- **Floating receptacle-relative objects**: small objects carry a compound id like
  `"computer monitor-0|office_desk-4 (office)"` — the part after `|` names the receptacle
  they're meant to rest on. Their authored height assumes Holodeck's internal size estimate for
  that `assetId` matches what we actually resolved; it doesn't always. Observed case: Holodeck
  mapped a "computer monitor" to THOR's `Television_13`, whose real bbox is 0.745m tall —
  TV-sized, not monitor-sized — so the authored Y put it ~0.5m above the desk. Confirmed this is
  a Holodeck-side data mismatch, not a placement bug in this script: desks and freestanding
  chairs land at `z ≈ 0` with the identical translate math, and the object is placed exactly
  where its own JSON says (`position.y = 1.57`, verified against the composed transform). Since
  these objects are `kinematic: true`, physics can't fix it even with `--dynamic`.

  `--snap-to-receptacle` rewrites just the Z of every compound-id object to rest its *already
  placed* bbox directly on the receptacle's *already placed* bbox — X/Y and orientation
  untouched, and it's a no-op if either side wasn't placed (missing asset). Off by default,
  since most objects don't need it — turn it on only after actually seeing floaters, since it
  fixes height, not the underlying size mismatch (that monitor still renders as a TV-sized
  object, just a grounded one). One-level only: a receptacle that's itself compound-id gets
  snapped using its own pre-snap bbox, not a fully resolved chain.

## Common Issues

| Symptom | Cause | Fix |
|---|---|---|
| `ms-download: command not found` | Bypassing the `/isaac-sim/python.sh` wrapper | Always invoke via the wrapper (see above) |
| `ModuleNotFoundError: molmospaces_resources` | Separate PyPI package, not an auto-installed dependency | `pip install molmospaces-resources` |
| `Failed building wheel for evdev` | Missing kernel headers matching the host's running kernel | `apt-get install -y linux-headers-$(uname -r)` as root inside the container; baked into the Dockerfile, but can go stale if the host kernel updates after the image was built |
| `PermissionError` writing into the mounted repo | UID mismatch between host user and container user | `sudo chmod -R a+rwX <mounted-path>` on the host |
| `fatal: detected dubious ownership in repository` | Git's CVE-2022-24765 safety check, same UID mismatch | `git config --system --add safe.directory /isaac-sim/molmospaces` (baked into the Dockerfile) |
| `Temporary failure in name resolution` during `pip install` | Container's DNS config stale relative to the host (`--network host` copies `/etc/resolv.conf` at container start, not continuously) | `docker restart isaac-sim` |
| `error: src refspec <name> does not match any` on `git push` | Branch name at push time doesn't match a local branch that actually exists | `git branch` to check real local branch names |
| `ModuleNotFoundError: compress_json` / `msgpack` / `open3d` / `prior` when running `create_mujoco_model_from_objaverse` | Base image only has `molmospaces_resources` — `housegen`'s own deps aren't preinstalled unless its `[housegen]` extra was actually run | `pip install compress_json msgpack open3d prior` inside the container |
| Small objects (monitors, lamps, decor) floating above the furniture they're placed on | Holodeck's authored height assumes a different-sized asset than the one actually resolved for that `assetId` | `--snap-to-receptacle` on `compose_holodeck_scene.py` — see [Holodeck Scene → Isaac USD Pipeline](#holodeck-scene--isaac-usd-pipeline) |
| PhysX `CreateJoint` error on scene load ("no bodies defined" / "cannot create a joint between static bodies") | An articulated asset's non-fixed joints (e.g. drawer slides) left active after its rigid bodies were stripped for a frozen/static scene | Fixed in `compose_holodeck_scene.py`'s `make_static()` — deactivates every joint type, not just `PhysicsFixedJoint` |
| `docker cp <container>:<path/under/a/bind/mount> ...` copies nothing | `docker cp` resolves a path under a bind mount to its *host* source, bypassing the container entirely — if that host directory is gone (see mount note above), this silently does nothing | `docker exec` a `cp -a` through a path that *is* a live bind mount instead, then move the result out on the host |

## Notes

- `docker stop` / a container exit preserves the writable layer fully — only `docker rm` destroys
  anything not in a bind mount. With both mounts above in place, there's nothing left to lose.
- Bind mounts can only be set at container *creation* (`docker run`) — not added to a running or
  already-created container. Changing mounts means recreating the container (safe, per the point
  above).
