"""Download the USD assets a Holodeck-generated scene needs.

Reads one or more Holodeck scene .json files, collects every distinct assetId
they reference, and pulls the matching pre-converted USD asset out of the
MolmoSpaces R2 bucket.

This is the same job `install_objaverse_uid` in usda_downloader.py does, but
written against the molmospaces_resources API that is actually installed
(0.0.2), and driven from a scene file instead of a dataset index.

Usage (inside the isaac-sim container):

    /isaac-sim/python.sh scripts/assets/fetch_holodeck_assets.py \
        --scenes scratch/holodeck_scenes/*.json \
        --cache-dir /isaac-sim/.molmospaces/isaac-thor-resources \
        --symlink-dir /isaac-sim/molmospaces/assets/isaac-usd

Assets that aren't in the bucket are listed at the end and written to
missing_assets.json; those are the ones needing local conversion from the
objathor .pkl.gz source instead.
"""

import argparse
import json
import re
from pathlib import Path

from molmospaces_resources import R2RemoteStorage, ResourceManager, setup_resource_manager

# Matches an Objaverse UID; anything else in a Holodeck scene is a THOR asset name.
HEX32 = re.compile(r"^[0-9a-f]{32}$")

# Which published version of each object set to pull from. Mirrors
# ISAAC_DATA_TYPE_TO_SOURCE_TO_VERSION in usda_downloader.py.
VERSIONS = {
    "objects": {
        "thor": "20260128",
        "objaverse": "20260128",
    },
}


def collect_asset_ids(scene_paths: list[Path]) -> dict[str, set[str]]:
    """Return {"objaverse": {uid, ...}, "thor": {asset_id, ...}} across all scenes."""
    found: dict[str, set[str]] = {"objaverse": set(), "thor": set()}

    for path in scene_paths:
        house = json.loads(path.read_text())
        # Holodeck keeps the final placement list in "objects"; "structuralObjects"
        # is normally absent but is cheap to include.
        entries = house.get("objects", []) + house.get("structuralObjects", [])

        def walk(objs):
            for obj in objs:
                asset_id = obj.get("assetId", "")
                if not asset_id:
                    continue
                source = "objaverse" if HEX32.match(asset_id) else "thor"
                found[source].add(asset_id)
                # Holodeck output is normally flat, but THOR houses nest here.
                walk(obj.get("children", []))

        walk(entries)

    return found


def fetch_objaverse(manager: ResourceManager, asset_ids: set[str], dry_run: bool):
    """Install each Objaverse asset, one at a time so one bad id can't sink the batch."""
    ok: list[str] = []
    missing: list[str] = []

    for i, asset_id in enumerate(sorted(asset_ids), 1):
        try:
            # index_lookup maps a token (the UID) to the archive(s) holding it.
            # Empty result means this asset isn't published in MolmoSpaces' set.
            packages = manager.index_lookup("objects", "objaverse", asset_id)
            if not packages:
                raise KeyError("not present in index")

            if not dry_run:
                manager.install_packages("objects", {"objaverse": list(packages)})

            ok.append(asset_id)
            print(f"  [{i}/{len(asset_ids)}] OK   {asset_id}")
        except Exception as exc:  # noqa: BLE001 - want the whole batch to finish
            missing.append(asset_id)
            print(f"  [{i}/{len(asset_ids)}] MISS {asset_id}  ({type(exc).__name__}: {exc})")

    return ok, missing


def check_thor(asset_ids: set[str], thor_dir: Path):
    """THOR assets come from the bulk `ms-download --assets thor` set, not the
    per-asset index (their index tokens are archive-based, not asset names), so
    just verify each one is present on disk."""
    ok = [a for a in sorted(asset_ids) if (thor_dir / a).is_dir()]
    missing = [a for a in sorted(asset_ids) if not (thor_dir / a).is_dir()]
    for a in ok:
        print(f"  OK   {a}")
    for a in missing:
        print(f"  MISS {a}  (not in {thor_dir})")
    return ok, missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenes", type=Path, nargs="+", required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--symlink-dir", type=Path, required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve every asset against the index but download nothing",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Only process the first N assets per source"
    )
    parser.add_argument("--out", type=Path, default=Path("missing_assets.json"))
    parser.add_argument(
        "--thor-dir",
        type=Path,
        default=Path("/isaac-sim/.molmospaces/usd/objects/thor/20260128"),
        help="Where `ms-download --assets thor` put the THOR USD assets",
    )
    args = parser.parse_args()

    scene_paths = [p for p in args.scenes if p.is_file()]
    if not scene_paths:
        print("No readable scene files given")
        return 1

    found = collect_asset_ids(scene_paths)
    print(f"Scanned {len(scene_paths)} scene(s)")
    for source, ids in found.items():
        print(f"  {source:<10} {len(ids)} unique assets")
    print()

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.symlink_dir.mkdir(parents=True, exist_ok=True)

    manager = setup_resource_manager(
        R2RemoteStorage("isaac-thor-resources"),
        symlink_dir=args.symlink_dir,
        versions=VERSIONS,
        cache_dir=args.cache_dir,
    )

    results: dict[str, dict[str, list[str]]] = {}

    if found["objaverse"]:
        ids = found["objaverse"]
        subset = set(sorted(ids)[: args.limit]) if args.limit else ids
        print(f"objaverse: fetching {len(subset)}{' (dry run)' if args.dry_run else ''}")
        ok, missing = fetch_objaverse(manager, subset, args.dry_run)
        results["objaverse"] = {"ok": ok, "missing": missing}
        print()

    if found["thor"]:
        print(f"thor: checking {len(found['thor'])} against {args.thor_dir}")
        ok, missing = check_thor(found["thor"], args.thor_dir)
        results["thor"] = {"ok": ok, "missing": missing}
        print()

    print("=" * 60)
    total_missing = 0
    for source, res in results.items():
        print(f"{source:<10} {len(res['ok'])} ok, {len(res['missing'])} missing")
        total_missing += len(res["missing"])

    if total_missing:
        args.out.write_text(json.dumps(results, indent=2))
        print(f"\nWrote per-source results to {args.out}")
        print("Assets under 'missing' need local conversion from their objathor .pkl.gz source.")

    print(f"\nAssets installed under: {args.symlink_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
