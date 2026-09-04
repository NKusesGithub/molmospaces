# CrazySwarm2 / Isaac Sim / MolmoSpaces Setup

Infrastructure and setup notes for running [MolmoSpaces](https://github.com/allenai/molmospaces) on
[Isaac Sim 5.1.0](https://docs.isaacsim.omniverse.nvidia.com/) inside Docker, for the CrazySwarm2
drone-swarm rig.

## Contents

- [`Dockerfile`](./molmo-isaac-docker-setup/Dockerfile) — builds `isaac-sim-molmo:latest` from NVIDIA's official Isaac Sim
  5.1.0 image, with every environment-level fix discovered while setting this up baked in.
- [`setup_host.sh`](./molmo-isaac-docker-setup/setup_host.sh) — staged, idempotent host bring-up script: driver → Docker
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
| [Holodeck](https://github.com/allenai/Holodeck) | No — separate repo entirely, only output is a JSON file | Recommended: its own conda env (Python 3.10) |

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

### Recommended: its own conda env, not the container

Holodeck's own README specifies conda with a **pinned Python 3.10** — this matters, since the
default `python3` on this rig is 3.14 and Holodeck won't run on it. The `ai2thor` install is a
separate step against a custom index and a pinned commit; `requirements.txt` alone is not enough.

```bash
git clone <your-fork-of-Holodeck> ~/S_ENG/Holodeck
cd ~/S_ENG/Holodeck

conda create --name holodeck python=3.10
conda activate holodeck
pip install -r requirements.txt
pip install --extra-index-url https://ai2thor-pypi.allenai.org \
  ai2thor==0+8524eadda94df0ab2dbb2ef5a577e4d37c712897

python main.py --query "an office" --openai_api_key <OPENAI_API_KEY>
```

On this rig that env lives at `~/S_ENG/miniconda3/envs/holodeck` (Python 3.10.21). Generated
scenes land in `~/S_ENG/Holodeck/data/scenes/<query>-<timestamp>/<query>.json`.

That JSON is the handoff point — either into `housegen` (Stage 2 above, inside the container), or
into the [direct USD pipeline](#holodeck-scene--isaac-usd-pipeline) below. No Isaac Sim dependency
exists on the Holodeck side at all.

The env already carries `numpy` and `scipy`, so it's also a viable home for
`compose_holodeck_scene.py`, which needs only those plus `pxr` (`pip install usd-core`) — no
Isaac runtime. That keeps scene generation and scene composition in one place.

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

**→ Full reference: [`scripts/assets/README.md`](./scripts/assets/README.md)**

The [Scene Generation Pipeline](#scene-generation-pipeline) above is the *house* pipeline —
`housegen` compiles THOR-format JSON (rooms, walls, doors, windows) into a settled MuJoCo scene,
which `molmo_spaces_isaac` converts to USD. It hard-requires that house structure: object body
names must match `\w+_[0-9a-f]{32}_\d+_\d+_\d+` and have a sibling `_metadata.json`, or
conversion silently drops the object.

For a Holodeck scene that's just furniture in a single room, three scripts under
[`scripts/assets/`](./scripts/assets/) go straight from Holodeck's JSON to a loadable USD stage,
skipping MuJoCo entirely:

| Script | Stage | Input → Output |
|---|---|---|
| [`fetch_holodeck_assets.py`](./scripts/assets/fetch_holodeck_assets.py) | 1 | Scene JSON → pre-converted USD assets from the MolmoSpaces R2 bucket |
| [`convert_missing_assets.py`](./scripts/assets/convert_missing_assets.py) | 2 (optional) | objathor `.pkl.gz` → USD, for anything step 1 didn't find |
| [`compose_holodeck_scene.py`](./scripts/assets/compose_holodeck_scene.py) | 3 | Scene JSON + those assets → one `scene.usda` |

```bash
cd /isaac-sim/molmospaces            # inside the container
SCENE=/isaac-sim/Holodeck/data/scenes/<dir>/<name>.json

/isaac-sim/python.sh scripts/assets/fetch_holodeck_assets.py --scenes "$SCENE" \
  --cache-dir /isaac-sim/.molmospaces/isaac-thor-resources \
  --symlink-dir /isaac-sim/molmospaces/assets/isaac-usd \
  --out scratch/missing_assets.json

/isaac-sim/python.sh scripts/assets/convert_missing_assets.py \
  --missing-json scratch/missing_assets.json \
  --objathor-dir /isaac-sim/objathor-assets/2023_09_23/assets \
  --mjcf-out scratch/mjcf --max-workers 8 --skip-existing \
  --usd-out "$(readlink -f assets/isaac-usd/objects/objaverse)"

/isaac-sim/python.sh scripts/assets/compose_holodeck_scene.py --scene "$SCENE" \
  --thor-dir /isaac-sim/.molmospaces/usd/objects/thor/20260128 \
  --obja-dir /isaac-sim/molmospaces/assets/isaac-usd/objects/objaverse \
  --out scratch/out/scene.usda
```

Stage 3 needs only `pxr` (`usd-core`), not the Isaac runtime, so it runs in under a second.
Useful flags: `--dynamic` (let non-`kinematic` objects fall), `--snap-to-receptacle` (fix small
objects floating above furniture), `--no-walls`, `--keep-duplicates`.

Stage 2 needs `housegen`'s deps, which the base image doesn't carry:
`pip install compress_json msgpack open3d prior` inside the container.

The pipeline README covers the coordinate/rotation conventions (assets are **Y-up** inside a Z-up
stage), why static objects need `RemoveAPI("PhysicsRigidBodyAPI")` plus deactivating *every*
joint type, the duplicate and receptacle-snapping heuristics, verification recipes, and what this
pipeline gives up versus `housegen` (no settle, no materials, no doors/windows).

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
