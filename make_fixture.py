#!/usr/bin/env python3
"""Build a synthetic image tree from the ACDSee database for testing remap_assets.

The remapper only ever reads three things from the filesystem: directory
entry names, file sizes, and JPEG EXIF headers.  It never touches pixel
data.  That means a faithful test fixture costs ~800 bytes per asset
instead of the 1.17 TB the real library occupies -- the recorded sizes are
reproduced with sparse files, so a full 165k-asset tree needs well under a
gigabyte of actual disk.

Because the tree is generated from the database, we know the correct answer
for every asset up front.  `build` writes that answer to a ground-truth
manifest; `verify` compares a remap run's mappings JSON against it and
reports per-level recall plus, more importantly, false positives -- assets
the remapper claimed to resolve to the wrong file.

Usage:
    python make_fixture.py build acdsee_source/Default /tmp/fixture --limit 2000
    python remap_assets.py acdsee_source/Default /tmp/fixture --level 3 --dry-run
    python make_fixture.py verify /tmp/fixture/fixture_manifest.json remap_mappings_*.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import io
import json
import logging
import os
import random
import sys
from typing import Any, Optional

from remap_assets import AcdDatabase

log = logging.getLogger("fixture")

MANIFEST_NAME = "fixture_manifest.json"
EXIF_MANIFEST_NAME = "fixture_exif_manifest.json"
MOVED_DIR = "_relocated"     # destination for the "moved" bucket
RENAME_PREFIX = "RN_"

# Smallest JPEG we can emit that still carries a readable EXIF block.
# Assets recorded smaller than this cannot have both an exact size and
# valid EXIF; size wins and they are marked has_exif=False.
_MIN_JPEG_BYTES = 1024


# ---------------------------------------------------------------------------
# Path handling
# ---------------------------------------------------------------------------

def classify_root(db_path: str) -> str:
    """Return 'drive' for X:/... paths, 'unc' for \\\\server/... paths."""
    return "drive" if ":" in db_path.split("/")[0] else "unc"


def split_root(db_path: str) -> tuple[str, str]:
    """Split a database path into (volume_token, path_below_volume).

    'E:/Photos/a.jpg'        -> ('E', 'Photos/a.jpg')
    '\\\\Miracle/pics/a.jpg' -> ('Miracle', 'pics/a.jpg')

    Each volume becomes its own directory under the image root, mirroring a
    migration where every old drive lands as a separate share.  Merging them
    into one tree would overlay same-named files from different volumes and
    manufacture ambiguity that does not exist on the real storage.
    """
    p = db_path.replace("\\", "/")
    head = p.split("/")[0]
    if ":" in head:
        vol, rest = p.split(":", 1)
        return vol.strip("/") or "_root", rest.lstrip("/")
    parts = [s for s in p.split("/") if s]
    if not parts:
        return "_root", ""
    return parts[0], "/".join(parts[1:])


def sanitize(rel: str) -> Optional[str]:
    """Make a database-derived relative path safe to create locally."""
    parts = []
    for seg in rel.split("/"):
        seg = seg.replace("\x00", "").strip().rstrip(".")
        if seg in ("", ".", ".."):
            continue
        parts.append(seg[:200])
    if not parts:
        return None
    out = "/".join(parts)
    return out if len(out) < 3500 else None


# ---------------------------------------------------------------------------
# Synthetic image construction
# ---------------------------------------------------------------------------

def _exif_datetime(meta: dict, asset: dict) -> str:
    """Best available capture timestamp, in EXIF's 'YYYY:MM:DD HH:MM:SS' form.

    The database exposes this as Asset.EXIFDATE (surfaced by
    get_asset_metadata as 'exifdate'); the 'date_time_original' key it also
    returns is always empty because no such column exists in this schema.
    A real photo on disk carries DateTimeOriginal regardless, so the fixture
    writes one.
    """
    for val in (meta.get("date_time_original"), meta.get("exifdate")):
        if not val:
            continue
        if isinstance(val, datetime.datetime):
            return val.strftime("%Y:%m:%d %H:%M:%S")
        text = str(val).strip()
        if text:
            return text.replace("-", ":")[:19]
    return ""


def build_jpeg(meta: dict, asset: dict) -> tuple[bytes, bool]:
    """Return (jpeg_bytes, carries_exif) for one asset's stand-in image."""
    from PIL import Image

    exif = Image.Exif()
    carries = False

    make = (meta.get("camera_make") or "").strip()
    model = (meta.get("camera_model") or "").strip()
    if make:
        exif[0x010F] = make
        carries = True
    if model:
        exif[0x0110] = model
        carries = True

    sub = exif.get_ifd(0x8769)
    dto = _exif_datetime(meta, asset)
    if dto:
        sub[0x9003] = dto
        carries = True
    lens = (meta.get("lens_model") or "").strip()
    if lens:
        sub[0xA434] = lens
        carries = True
    for key, tag in (("focal_length", 0x920A), ("fnumber", 0x829D)):
        raw = meta.get(key)
        if raw in (None, ""):
            continue
        try:
            sub[tag] = float(raw)
            carries = True
        except (TypeError, ValueError):
            pass

    img = Image.new("RGB", (8, 8), (128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif, quality=40)
    return buf.getvalue(), carries


def write_asset_file(path: str, blob: bytes, target_size: int) -> Optional[bool]:
    """Write *blob* at *path*, sparsely padded to *target_size*.

    Returns True if the file carries a readable EXIF header, False if size
    fidelity forced the EXIF out (the remapper checks size before EXIF), or
    None if the path could not be created at all.

    ACDSee indexes the contents of archives, so the database contains assets
    whose parent "folder" is itself a file asset (`foo.zip/bar.jpg`).  Those
    two cannot coexist on a real filesystem; such assets are skipped and
    excluded from ground truth rather than being recorded as placeable.
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if target_size and target_size < len(blob):
            with open(path, "wb") as fh:
                fh.write(b"\xff\xd8" + b"\0" * max(0, target_size - 2))
            os.truncate(path, target_size)
            return False
        with open(path, "wb") as fh:
            fh.write(blob)
            if target_size > len(blob):
                os.truncate(fh.fileno(), target_size)
        return True
    except (FileExistsError, NotADirectoryError, IsADirectoryError,
            OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def assign_buckets(n: int, rng: random.Random,
                   moved: float, renamed: float, deleted: float) -> list[str]:
    """Deterministically partition n assets into perturbation buckets."""
    buckets = ["pristine"] * n
    idx = list(range(n))
    rng.shuffle(idx)
    cut_m = int(n * moved)
    cut_r = cut_m + int(n * renamed)
    cut_d = cut_r + int(n * deleted)
    for i in idx[:cut_m]:
        buckets[i] = "moved"
    for i in idx[cut_m:cut_r]:
        buckets[i] = "renamed"
    for i in idx[cut_r:cut_d]:
        buckets[i] = "deleted"
    return buckets


def expected_level(bucket: str, root_kind: str, has_exif: bool) -> Optional[int]:
    """The resolution level a correct remapper should reach for this asset."""
    if bucket == "deleted":
        return None
    if bucket == "moved":
        return 2          # same filename elsewhere, size still matches
    if bucket == "renamed":
        return 3 if has_exif else None   # only EXIF can find a renamed file
    return 1                             # pristine: a volume-prefix swap


def build(db_dir: str, out_dir: str, limit: int, seed: int,
          moved: float, renamed: float, deleted: float,
          workers: int) -> str:
    rng = random.Random(seed)
    db = AcdDatabase(db_dir)
    db.load()

    file_assets = [a for a in db.assets if int(float(a.get("SIZE", 0) or 0)) > 0]
    log.info("%d file assets in database", len(file_assets))
    if limit and limit < len(file_assets):
        file_assets = rng.sample(file_assets, limit)
        log.info("Sampled %d assets (seed=%d)", len(file_assets), seed)

    buckets = assign_buckets(len(file_assets), rng, moved, renamed, deleted)
    root = os.path.abspath(out_dir)

    jobs: list[tuple[dict, dict, str, str, str]] = []  # meta, asset, bucket, vol, rel
    skipped = 0
    for asset, bucket in zip(file_assets, buckets):
        meta = db.get_asset_metadata(asset)
        db_path = db.build_asset_path(asset)
        vol, below = split_root(db_path)
        vol = sanitize(vol)
        rel = sanitize(below)
        if not rel or not vol:
            skipped += 1
            continue
        meta["original_path"] = db_path
        meta["root_kind"] = classify_root(db_path)
        jobs.append((meta, asset, bucket, vol, rel))
    if skipped:
        log.warning("Skipped %d assets with unusable paths", skipped)

    entries: list[dict] = []
    taken: set[str] = set()
    collisions = 0

    def placement(bucket: str, vol: str, rel: str, name: str) -> Optional[str]:
        base = os.path.join(root, vol)
        if bucket == "deleted":
            return None
        if bucket == "moved":
            return os.path.join(base, MOVED_DIR, name)
        if bucket == "renamed":
            return os.path.join(base, os.path.dirname(rel), RENAME_PREFIX + name)
        return os.path.join(base, rel)

    for meta, asset, bucket, vol, rel in jobs:
        name = os.path.basename(rel)
        dest = placement(bucket, vol, rel, name)
        if dest is not None:
            key = dest.lower()
            if key in taken:
                collisions += 1
                continue
            taken.add(key)
        entries.append({
            "asset_id": meta["asset_id"],
            "name": meta.get("name") or name,
            "original_path": meta["original_path"],
            "root_kind": meta["root_kind"],
            "volume": vol,
            "bucket": bucket,
            "size": meta["size"],
            "fixture_path": dest,
            "_meta": meta,
            "_asset": asset,
        })
    if collisions:
        log.warning("Skipped %d assets whose fixture paths collided", collisions)

    log.info("Materialising %d files under %s ...", len(entries), root)
    written = [0]

    def make_one(entry: dict) -> tuple[int, Optional[bool]]:
        aid = entry["asset_id"]
        dest = entry["fixture_path"]
        if dest is None:            # deleted bucket: intentionally absent
            return aid, False
        try:
            blob, carries = build_jpeg(entry["_meta"], entry["_asset"])
        except Exception:
            return aid, None
        ok = write_asset_file(dest, blob, entry["size"])
        if ok is None:
            return aid, None
        return aid, (carries and ok)

    exif_flags: dict[int, Optional[bool]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for aid, has_exif in ex.map(make_one, entries):
            exif_flags[aid] = has_exif
            written[0] += 1
            if written[0] % 20000 == 0:
                log.info("  %d / %d", written[0], len(entries))

    unplaceable = [e for e in entries if exif_flags.get(e["asset_id"]) is None]
    if unplaceable:
        log.warning("Skipped %d assets that could not be materialised "
                    "(path occupied by a file, e.g. archive contents)",
                    len(unplaceable))
    entries = [e for e in entries if exif_flags.get(e["asset_id"]) is not None]

    for entry in entries:
        entry["has_exif"] = bool(exif_flags.get(entry["asset_id"]))
        entry["expected_level"] = expected_level(
            entry["bucket"], entry["root_kind"], entry["has_exif"])
        entry.pop("_meta", None)
        entry.pop("_asset", None)

    manifest = {
        "generated": datetime.datetime.now().isoformat(),
        "db_dir": os.path.abspath(db_dir),
        "image_root": root,
        "volumes": sorted({e["volume"] for e in entries}),
        "sampled": bool(limit),
        # Assets with no file on disk for reasons unrelated to the test design.
        # verify() discounts claims against these instead of scoring them.
        "excluded_asset_ids": sorted(e["asset_id"] for e in unplaceable),
        "seed": seed,
        "fractions": {"moved": moved, "renamed": renamed, "deleted": deleted},
        "counts": _bucket_counts(entries),
        "entries": entries,
    }
    manifest_path = os.path.join(os.path.abspath(out_dir), MANIFEST_NAME)
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh, indent=1, default=str)

    _write_exif_manifest(entries, root, out_dir)

    _report_build(manifest, out_dir, manifest_path)
    return manifest_path


def _write_exif_manifest(entries: list[dict], root: str, out_dir: str) -> str:
    """Emit an exiftool-shaped manifest of the fixture, for --manifest testing.

    Reads the files that were just written rather than reusing the values
    used to write them, so the manifest is an independent observation of the
    tree -- the same relationship a real exiftool run has to real storage.
    """
    from PIL import Image
    from PIL.ExifTags import TAGS

    WANT = ("DateTimeOriginal", "CreateDate", "Make", "Model", "LensModel",
            "FocalLength", "FNumber", "ImageWidth", "ImageHeight")
    out = []
    for entry in entries:
        path = entry["fixture_path"]
        if not path:
            continue
        try:
            rec: dict[str, Any] = {"SourceFile": path,
                                   "FileSize": os.path.getsize(path)}
        except OSError:
            continue
        if entry.get("has_exif"):
            try:
                img = Image.open(path)
                raw = img._getexif() or {}
                img.close()
                named = {TAGS.get(k, k): v for k, v in raw.items()}
                for key in WANT:
                    if named.get(key) not in (None, ""):
                        rec[key] = named[key]
            except Exception:
                pass
        out.append(rec)

    path = os.path.join(os.path.abspath(out_dir), EXIF_MANIFEST_NAME)
    with open(path, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    log.info("  exif manifest     %s (%d files)", path, len(out))
    return path


def _bucket_counts(entries: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["bucket"]] = counts.get(e["bucket"], 0) + 1
        lvl = e["expected_level"]
        key = f"expect_L{lvl}" if lvl else "expect_unresolved"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _du(path: str) -> tuple[int, int]:
    """Return (apparent_bytes, allocated_bytes) for a tree."""
    apparent = allocated = 0
    for dirpath, _dirs, files in os.walk(path):
        for fn in files:
            try:
                st = os.lstat(os.path.join(dirpath, fn))
            except OSError:
                continue
            apparent += st.st_size
            allocated += st.st_blocks * 512
    return apparent, allocated


def _report_build(manifest: dict, out_dir: str, manifest_path: str) -> None:
    apparent, allocated = _du(manifest["image_root"])
    log.info("=" * 60)
    log.info("Fixture built: %s", out_dir)
    log.info("  volumes           %s", ", ".join(manifest["volumes"]))
    for key in sorted(manifest["counts"]):
        log.info("  %-18s %d", key, manifest["counts"][key])
    log.info("  apparent size     %.2f GB", apparent / 1e9)
    log.info("  actual disk use   %.2f GB", allocated / 1e9)
    log.info("  manifest          %s", manifest_path)
    log.info("=" * 60)


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def verify(manifest_path: str, mappings_path: str) -> int:
    """Compare a remap run against ground truth.  Returns a process exit code."""
    with open(manifest_path) as fh:
        manifest = json.load(fh)
    with open(mappings_path) as fh:
        mappings = json.load(fh)

    truth = {e["asset_id"]: e for e in manifest["entries"]}
    got: dict[int, dict] = {}
    for res in mappings.get("resolutions", []):
        aid = res.get("asset_id")
        if aid is not None:
            got[aid] = res

    stats: dict[str, int] = {}
    def bump(k: str) -> None:
        stats[k] = stats.get(k, 0) + 1

    false_positives: list[tuple[dict, dict]] = []
    wrong_level: list[tuple[dict, dict]] = []
    missed: list[dict] = []

    # Claims for assets outside the manifest point at some other asset's file,
    # since only manifest assets were materialised.  In a FULL fixture that is
    # an unambiguous false positive.  In a SAMPLED fixture the count is
    # inflated: those assets would have had their own files and would mostly
    # have resolved at level 1 before level 2 ever ran.  Report the mechanism,
    # but do not present the magnitude as a precision measurement.
    sampled = manifest.get("sampled", True)
    excluded = set(manifest.get("excluded_asset_ids", []))
    orphaned_claims = [r for aid, r in got.items()
                       if aid not in truth and aid not in excluded]
    if excluded:
        stats["excluded_from_scoring"] = len(excluded)
    label = "CROSS_ASSET_sampling_artifact" if sampled else "FALSE_POSITIVE_unknown_asset"
    for res in orphaned_claims:
        bump(f"{label}_L{res.get('resolution_level')}")

    for aid, exp in truth.items():
        res = got.get(aid)
        want = exp["expected_level"]
        if res is None:
            if want is None:
                bump("correctly_unresolved")
            else:
                bump(f"missed_L{want}")
                missed.append(exp)
            continue

        actual_path = os.path.realpath(res.get("new_path", ""))
        wanted_path = exp["fixture_path"]
        if wanted_path is None:
            bump("FALSE_POSITIVE_deleted")
            false_positives.append((exp, res))
            continue
        if actual_path != os.path.realpath(wanted_path):
            bump("FALSE_POSITIVE_wrong_file")
            false_positives.append((exp, res))
            continue

        lvl = res.get("resolution_level")
        if lvl == want:
            bump(f"correct_L{lvl}")
        else:
            bump(f"right_file_L{lvl}_expected_L{want}")
            wrong_level.append((exp, res))

    print("=" * 68)
    print(f"Fixture verification: {os.path.basename(mappings_path)}")
    print(f"  assets in manifest: {len(truth)}")
    print(f"  resolutions claimed: {len(got)}")
    print("-" * 68)
    for key in sorted(stats):
        flag = "  <-- WRONG" if key.startswith("FALSE_POSITIVE") else ""
        if key.startswith("CROSS_ASSET"):
            flag = "  <-- see note"
        print(f"  {key:<38} {stats[key]:>7}{flag}")
    print("-" * 68)

    if false_positives:
        print(f"\n{len(false_positives)} FALSE POSITIVES "
              f"(remapper would corrupt these records):")
        for exp, res in false_positives[:10]:
            print(f"  asset {exp['asset_id']} [{exp['bucket']}] "
                  f"{exp['original_path']}")
            print(f"      claimed L{res.get('resolution_level')} -> "
                  f"{res.get('new_path')}")
            print(f"      truth: {exp['fixture_path']}")
        if len(false_positives) > 10:
            print(f"  ... and {len(false_positives) - 10} more")

    if orphaned_claims:
        if sampled:
            print(f"\n{len(orphaned_claims)} claims for assets outside the sample "
                  f"(each points at another asset's photo).\n"
                  f"  NOTE: inflated by sampling -- rebuild with --limit 0 for a "
                  f"true precision figure.")
        else:
            print(f"\n{len(orphaned_claims)} claims for assets with NO file "
                  f"(each points at another asset's photo):")
        for res in orphaned_claims[:5]:
            print(f"  asset {res.get('asset_id')} L{res.get('resolution_level')}"
                  f"  {res.get('original_path')}")
            print(f"      -> {res.get('new_path')}")
        if len(orphaned_claims) > 5:
            print(f"  ... and {len(orphaned_claims) - 5} more")

    if missed:
        by_level: dict[Any, int] = {}
        for exp in missed:
            by_level[exp["expected_level"]] = by_level.get(exp["expected_level"], 0) + 1
        print(f"\n{len(missed)} MISSED (resolvable but not found): "
              + ", ".join(f"L{k}={v}" for k, v in sorted(by_level.items())))
        for exp in missed[:5]:
            print(f"  asset {exp['asset_id']} [{exp['bucket']}] "
                  f"expect L{exp['expected_level']}  {exp['original_path']}")

    print()
    return 1 if (false_positives or (orphaned_claims and not sampled)) else 0


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

def self_check(manifest_path: str, sample: int = 20) -> int:
    """Confirm the generated files really carry the metadata we intended.

    Uses a correct EXIF reader (PIL.ExifTags.TAGS) rather than the one in
    remap_assets, so a fault in the fixture is distinguishable from a fault
    in the remapper.
    """
    from PIL import Image
    from PIL.ExifTags import TAGS

    with open(manifest_path) as fh:
        manifest = json.load(fh)
    entries = [e for e in manifest["entries"]
               if e["fixture_path"] and e["has_exif"]]
    if not entries:
        print("self-check: no EXIF-bearing entries in manifest")
        return 1
    picks = random.Random(0).sample(entries, min(sample, len(entries)))

    bad = 0
    for e in picks:
        path = e["fixture_path"]
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            print(f"  MISSING {path}: {exc}")
            bad += 1
            continue
        if size != e["size"]:
            print(f"  SIZE MISMATCH {path}: on disk {size}, manifest {e['size']}")
            bad += 1
        img = Image.open(path)
        raw = img._getexif() or {}
        img.close()
        named = {TAGS.get(k, k): v for k, v in raw.items()}
        if not named:
            print(f"  NO EXIF {path}")
            bad += 1
            continue
        keys = [k for k in ("Make", "Model", "DateTimeOriginal") if named.get(k)]
        print(f"  ok  {os.path.basename(path):<44} size={size:<10} {keys}")
    print(f"\nself-check: {len(picks) - bad}/{len(picks)} sampled files correct")
    return 1 if bad else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="generate the fixture tree + manifest")
    b.add_argument("db_dir")
    b.add_argument("out_dir")
    b.add_argument("--limit", type=int, default=2000,
                   help="assets to include; 0 = all (default: 2000)")
    b.add_argument("--seed", type=int, default=1)
    b.add_argument("--moved", type=float, default=0.10)
    b.add_argument("--renamed", type=float, default=0.05)
    b.add_argument("--deleted", type=float, default=0.03)
    b.add_argument("--workers", type=int, default=8)

    v = sub.add_parser("verify", help="score a remap run against ground truth")
    v.add_argument("manifest")
    v.add_argument("mappings")

    s = sub.add_parser("self-check", help="confirm the fixture itself is sound")
    s.add_argument("manifest")
    s.add_argument("--sample", type=int, default=20)

    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "build":
        total = args.moved + args.renamed + args.deleted
        if total >= 1.0:
            ap.error(f"bucket fractions sum to {total}, must be < 1.0")
        build(args.db_dir, args.out_dir, args.limit, args.seed,
              args.moved, args.renamed, args.deleted, args.workers)
    elif args.cmd == "verify":
        sys.exit(verify(args.manifest, args.mappings))
    elif args.cmd == "self-check":
        sys.exit(self_check(args.manifest, args.sample))


if __name__ == "__main__":
    main()
