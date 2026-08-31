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

Two separate mounts, on purpose:

```bash
-v "$MOLMO_REPO_PATH":/isaac-sim/molmospaces        # git-tracked code
-v "$MOLMO_CACHE_PATH":/isaac-sim/.molmospaces       # ms-download's asset cache
```

Keeping these separate matters: `ms-download`'s asset cache can grow to many GB and has no
business anywhere near `git add`. It's also why deleting and recreating the container (`docker
rm`) doesn't lose downloaded assets — both mounts point at real directories on the host, so only
things living in the container's own writable layer (nothing, if you stick to these two paths)
would be lost.

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

## Notes

- `docker stop` / a container exit preserves the writable layer fully — only `docker rm` destroys
  anything not in a bind mount. With both mounts above in place, there's nothing left to lose.
- Bind mounts can only be set at container *creation* (`docker run`) — not added to a running or
  already-created container. Changing mounts means recreating the container (safe, per the point
  above).
