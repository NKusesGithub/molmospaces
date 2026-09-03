"""Convert the Objaverse assets a scene needs but MolmoSpaces doesn't publish.

Some Holodeck assets aren't in the pre-converted USD set fetched by
fetch_holodeck_assets.py. This builds them locally from their objathor source in
two stages, the same pipeline the MolmoSpaces asset release uses:

  stage 1  objathor .pkl.gz  ->  MJCF  (create_mujoco_model_from_objaverse)
  stage 2  MJCF              ->  USD   (molmo_spaces_isaac.assets.asset_converter)

The USD lands in --usd-out as obja_<uid>/obja_<uid>.usda, exactly where
compose_holodeck_scene.py's resolver looks, so the composed scene picks them up
with no further wiring.

Feed it the missing_assets.json written by fetch_holodeck_assets.py:

    /isaac-sim/python.sh scripts/assets/convert_missing_assets.py \
        --missing-json scratch/missing_assets.json \
        --objathor-dir /isaac-sim/objathor-assets/2023_09_23/assets \
        --mjcf-out scratch/mjcf \
        --usd-out $(readlink -f assets/isaac-usd/objects/objaverse) \
        --max-workers 8
"""

import argparse
import gzip
import json
import pickle
import shutil
import subprocess
import sys
from pathlib import Path

from molmo_spaces.housegen.utils import create_mujoco_model_from_objaverse

# The objathor 2023_09_23 pkl.gz files store texture paths as absolute paths from
# the original processing box (e.g. /root/processed_models/<uid>/albedo.jpg). The
# MolmoSpaces converter joins them onto the asset dir and expects a basename, so
# an absolute path resolves to a nonexistent file. We stage a corrected copy.
TEXTURE_PATH_KEYS = ("albedoTexturePath", "normalTexturePath", "emissionTexturePath")


def stage_asset(uid: str, objathor_dir: Path, staging_dir: Path) -> Path | None:
    """Copy one asset dir into staging with its texture paths rewritten to
    basenames. Returns the staging root to hand the converter, or None on miss."""
    src = objathor_dir / uid
    pkl = src / f"{uid}.pkl.gz"
    if not pkl.is_file():
        return None

    dst = staging_dir / uid
    dst.mkdir(parents=True, exist_ok=True)

    # Copy the real texture/aux files (skip AppleDouble ._ sidecars).
    for f in src.iterdir():
        if f.is_file() and not f.name.startswith("._") and f.name != f"{uid}.pkl.gz":
            shutil.copy(f, dst / f.name)

    with gzip.open(pkl, "rb") as fh:
        data = pickle.load(fh)
    for key in TEXTURE_PATH_KEYS:
        if key in data and data[key]:
            data[key] = Path(data[key]).name  # absolute -> basename
    with gzip.open(dst / f"{uid}.pkl.gz", "wb") as fh:
        pickle.dump(data, fh)

    return staging_dir


def load_missing_uids(args) -> list[str]:
    if args.missing_json:
        data = json.loads(Path(args.missing_json).read_text())
        return list(data.get("objaverse", {}).get("missing", []))
    if args.uids:
        return list(args.uids)
    raise SystemExit("Provide --missing-json or --uids")


def build_mjcf(uids: list[str], objathor_dir: Path, mjcf_out: Path) -> list[str]:
    """Stage 1: write mjcf_out/<uid>/<uid>.xml plus its meshes/textures.

    The <uid>/<uid>.xml layout is what asset_converter's --is-objaverse mode
    globs for. Returns the uids whose MJCF built cleanly.
    """
    # Sibling of mjcf_out, not inside it -- stage 2 globs every dir under mjcf_out
    # as a candidate asset, and would otherwise pick up "_staging" itself as a
    # bogus extra one (surfaces as a spurious "<mjcf_out>/_staging/_staging.xml
    # doesn't exist" entry in the errors file).
    staging = mjcf_out.parent / f"{mjcf_out.name}_staging"
    ok: list[str] = []
    for i, uid in enumerate(uids, 1):
        staged_dir = stage_asset(uid, objathor_dir, staging)
        if staged_dir is None:
            print(f"  [{i}/{len(uids)}] MISS {uid}  (no source pkl.gz)")
            continue
        save_folder = mjcf_out / uid
        save_folder.mkdir(parents=True, exist_ok=True)
        try:
            create_mujoco_model_from_objaverse(uid, staged_dir, save_folder)
            ok.append(uid)
            print(f"  [{i}/{len(uids)}] OK   {uid}")
        except Exception as exc:  # noqa: BLE001 - one bad asset shouldn't stop the batch
            print(f"  [{i}/{len(uids)}] FAIL {uid}  ({type(exc).__name__}: {exc})")
    return ok


def build_usd(mjcf_out: Path, usd_out: Path, workers: int) -> int:
    """Stage 2: run the asset converter over the whole MJCF folder at once.

    convert-all + --is-objaverse globs <folder>/<uid>/<uid>.xml and writes
    <output_dir>/obja_<uid>/obja_<uid>.usda.
    """
    usd_out.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "molmo_spaces_isaac.assets.asset_converter",
        "--mode",
        "convert-all",
        "--is-objaverse",
        "--folder-path",
        str(mjcf_out),
        "--output-dir",
        str(usd_out),
        "--max-workers",
        str(workers),
    ]
    print("  " + " ".join(cmd))
    return subprocess.call(cmd)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--missing-json", type=Path)
    ap.add_argument("--uids", nargs="+")
    ap.add_argument("--objathor-dir", type=Path, required=True)
    ap.add_argument("--mjcf-out", type=Path, required=True)
    ap.add_argument("--usd-out", type=Path, required=True)
    ap.add_argument("--max-workers", type=int, default=4)
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip UIDs whose obja_<uid>.usda is already in --usd-out",
    )
    args = ap.parse_args()

    uids = load_missing_uids(args)
    if args.skip_existing:
        before = len(uids)
        uids = [u for u in uids if not (args.usd_out / f"obja_{u}" / f"obja_{u}.usda").is_file()]
        print(f"skip-existing: {before - len(uids)} already converted, {len(uids)} to do")

    if not uids:
        print("nothing to convert")
        return 0

    print(f"stage 1: MJCF for {len(uids)} asset(s)")
    built = build_mjcf(uids, args.objathor_dir, args.mjcf_out)
    print(f"  -> {len(built)}/{len(uids)} MJCFs built\n")
    if not built:
        return 1

    print(f"stage 2: USD conversion")
    rc = build_usd(args.mjcf_out, args.usd_out, args.max_workers)
    print()

    done = [u for u in built if (args.usd_out / f"obja_{u}" / f"obja_{u}.usda").is_file()]
    print("=" * 60)
    print(f"USD assets produced: {len(done)}/{len(uids)}")
    print(f"output: {args.usd_out}")
    failed = [u for u in uids if u not in done]
    if failed:
        print(f"failed ({len(failed)}): {', '.join(u[:12] for u in failed[:10])}")
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(main())
