#!/usr/bin/env python3
"""Verify Moonlit Lovers's IDX.DAT central index mirrors a container's PIDX0
leaf records exactly, and (with --dummy-roundtrip) prove the check would
actually catch a stale IDX.DAT after compressed blocks move/resize.

Ports galaxy_angel_audit_build.py / galaxy_angel_repair_central_idx.py /
galaxy_angel_audit_runtime_indexes.py's underlying "(file_id, offset, raw_size,
compressed_size) 16-byte record" reasoning to Moonlit Lovers's IDX.DAT, which
was confirmed (see STATUS.md) to share the exact same flat-table layout as
the 1st game, just with SCENARIO.DAT taking file_id 61 instead of GADAT001.

Two independent checks:

1. `verify_container` -- for a container's live PIDX0 leaf tuples
   (offset, raw_size, compressed_size), find the IDX.DAT flat-table records
   that carry the same file_id and confirm every leaf has a mirror with an
   *identical* tuple. This is a static structural check: it does not know
   which offsets "should" match, it discovers the file_id by finding the
   candidate that matches the most leaves, exactly as
   galaxy_angel_build.patch_central_idx_records does when repairing a build.

2. `--dummy-roundtrip` -- picks a handful of small SCENARIO.DAT blocks,
   appends throwaway Japanese filler text to their dialogue payload (never
   real translation), and runs moonlit_lovers_build.py against them with
   --force-recompress. This is expected to change at least one block's
   compressed size (proving the padding was not a no-op) and relocate/resize
   PIDX leaves. verify_container is then re-run against the *rebuilt* ISO: if
   the build's IDX.DAT mirroring is broken, the rebuilt ISO will fail
   verify_container the same way a stale-IDX bug would in production, so this
   is a real regression test for the mirroring logic, not just a smoke test.
"""

from __future__ import annotations

import argparse
import shutil
import struct
import sys
from pathlib import Path

import galaxy_angel_build as builder
import moonlit_lovers_build as mlbuild
import moonlit_lovers_resources as resources


def container_leaf_tuples(image: bytearray, files: dict[str, builder.IsoFile], stem: str):
    item = builder.resolve_iso_file(files, stem)
    begin = item.extent * builder.SECTOR
    # Fixed-LBA containers can advertise a multi-GB logical size so redirected
    # PIDX leaves reach a backing region elsewhere on disc. Use a zero-copy
    # view: the parser needs that logical address space, but must not duplicate
    # it in memory.
    container = memoryview(image)[begin : begin + item.size]
    recs = resources.pidx_record_map(container, stem)
    del container
    if not recs:
        raise SystemExit(f"{stem}: not a named PIDX0 container or no leaf records found")
    # recs: offset -> (record_pos, raw_size, compressed_size), including
    # stored 3;0 leaves and resources that are not 0x800-aligned.
    return recs


def find_idx_mirror(
    image: bytearray, files: dict[str, builder.IsoFile],
    leaf_tuples: dict[int, tuple[int, int, int]],
):
    """Read-only version of patch_central_idx_records's candidate search."""
    idx_file = builder.resolve_iso_file(files, "IDX")
    idx_begin = idx_file.extent * builder.SECTOR

    wanted = {(offset, raw_size, compressed_size) for offset, (_r, raw_size, compressed_size) in leaf_tuples.items()}
    candidates: dict[int, dict[tuple[int, int, int], int]] = {}
    for relative in range(0, idx_file.size - 15, 4):
        file_id, offset, raw_size, compressed_size = struct.unpack_from(
            "<IIII", image, idx_begin + relative
        )
        tup = (offset, raw_size, compressed_size)
        if tup in wanted:
            candidates.setdefault(file_id, {})[tup] = idx_begin + relative
    if not candidates:
        return None, {}
    ranked = sorted(candidates.items(), key=lambda kv: len(kv[1]), reverse=True)
    file_id, positions = ranked[0]
    ambiguous = len(ranked) > 1 and len(ranked[1][1]) == len(positions)
    return (file_id, ambiguous), positions


def verify_container(image: bytearray, files: dict[str, builder.IsoFile], stem: str) -> dict:
    leaf_tuples = container_leaf_tuples(image, files, stem)
    wanted = {(offset, raw_size, compressed_size) for offset, (_r, raw_size, compressed_size) in leaf_tuples.items()}
    (file_id, ambiguous), positions = find_idx_mirror(image, files, leaf_tuples)
    result = {
        "container": stem,
        "pidx_leaves": len(leaf_tuples),
        "file_id": file_id,
        "idx_matches": len(positions),
        "ambiguous_file_id": ambiguous,
        "complete": file_id is not None and not ambiguous and len(positions) == len(wanted),
    }
    return result


def report(result: dict) -> None:
    status = "OK" if result["complete"] else "FAIL"
    print(
        f"{status} {result['container']}: pidx_leaves={result['pidx_leaves']} "
        f"file_id={result['file_id']} idx_matches={result['idx_matches']} "
        f"ambiguous={result['ambiguous_file_id']}"
    )


DIALOGUE_LINE_RE_TEMPLATE = None


def make_dummy_scenario_files(original_scenario: Path, built_scenario: Path, count: int) -> list[str]:
    """Copy `count` small SCENARIO_DAT_*.txt files, inflating one dialogue line
    each with throwaway (non-translation) Japanese filler so the decompressed
    payload -- and therefore the recompressed size -- actually grows.
    """
    built_scenario.mkdir(parents=True, exist_ok=True)
    candidates = sorted(
        original_scenario.glob("SCENARIO_DAT_*.txt"),
        key=lambda p: p.stat().st_size,
    )
    touched = []
    filler = ("テスト" * 40).encode("cp932")
    made = 0
    for path in candidates:
        raw = path.read_bytes()
        # Insert filler right before the closing "@@)" of the first dialogue
        # block found, so it stays inside the dialogue payload the compressor
        # actually sees, without touching any command/anchor line.
        marker = raw.find(b"\n@@)")
        if marker < 0:
            continue
        new_raw = raw[:marker] + b"\n" + filler + raw[marker:]
        (built_scenario / path.name).write_bytes(new_raw)
        touched.append(path.name)
        made += 1
        if made >= count:
            break
    if not touched:
        raise SystemExit("no dialogue block found to inflate for dummy round-trip")
    return touched


def dummy_roundtrip(
    original_iso: Path, original_scenario: Path, lz_script: Path, work_dir: Path, count: int,
) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    built_scenario = work_dir / "built_scenario_dummy"
    if built_scenario.exists():
        shutil.rmtree(built_scenario)
    touched = make_dummy_scenario_files(original_scenario, built_scenario, count)
    print(f"dummy round-trip: inflated {len(touched)} scenario blocks: {touched}")

    output_iso = work_dir / "dummy_roundtrip.iso"
    mlbuild.build(
        original_iso, output_iso, original_scenario, built_scenario,
        lz_script, ["SCENARIO"], force_recompress=False,
    )

    # Compare compressed sizes before/after for the touched blocks specifically.
    with original_iso.open("rb") as f:
        original_image = bytearray(f.read())
    with output_iso.open("rb") as f:
        rebuilt_image = bytearray(f.read())
    original_files = builder.iso_files(original_image)
    rebuilt_files = builder.iso_files(rebuilt_image)
    original_recs = container_leaf_tuples(original_image, original_files, "SCENARIO")
    rebuilt_recs = container_leaf_tuples(rebuilt_image, rebuilt_files, "SCENARIO")

    # A leaf's *record position* inside the PIDX table never moves (only the
    # data offset it points at can); match before/after by record position,
    # not by data offset, since an oversized block gets relocated.
    rebuilt_by_record = {record: (offset, raw, compressed) for offset, (record, raw, compressed) in rebuilt_recs.items()}
    size_changed = 0
    for name in touched:
        offset = int(name.rsplit("_", 1)[1].removesuffix(".txt"), 16)
        record, _raw, old_compressed = original_recs[offset]
        new_offset, _new_raw, new_compressed = rebuilt_by_record[record]
        moved = new_offset != offset
        grew = new_compressed != old_compressed
        print(
            f"  {name}: compressed {old_compressed}->{new_compressed} "
            f"({'grew' if grew else 'unchanged'}), offset {offset:#x}->{new_offset:#x} "
            f"({'moved' if moved else 'same slot'})"
        )
        if grew:
            size_changed += 1
    print(f"dummy round-trip: {size_changed}/{len(touched)} touched blocks changed compressed size")
    if size_changed == 0:
        raise SystemExit(
            "dummy round-trip did not actually change any compressed size -- "
            "the round-trip test proves nothing; padding was a no-op"
        )

    original_result = verify_container(original_image, original_files, "SCENARIO")
    rebuilt_result = verify_container(rebuilt_image, rebuilt_files, "SCENARIO")
    report(original_result)
    report(rebuilt_result)
    if not rebuilt_result["complete"]:
        raise SystemExit("dummy round-trip FAILED: IDX.DAT mirror is stale after rebuild")

    # Source fidelity: confirm every ISO file NOT touched by the build kept
    # its original bytes (skill rule: only what changed should change).
    import hashlib

    mismatches = []
    for path, item in original_files.items():
        if path.upper() in ("SCENARIO.DAT", "IDX.DAT"):
            continue
        rebuilt_item = rebuilt_files.get(path)
        if rebuilt_item is None:
            mismatches.append((path, "missing in rebuilt ISO"))
            continue
        original_bytes = bytes(
            original_image[item.extent * builder.SECTOR : item.extent * builder.SECTOR + item.size]
        )
        rebuilt_bytes = bytes(
            rebuilt_image[
                rebuilt_item.extent * builder.SECTOR
                : rebuilt_item.extent * builder.SECTOR + rebuilt_item.size
            ]
        )
        if hashlib.sha256(original_bytes).digest() != hashlib.sha256(rebuilt_bytes).digest():
            mismatches.append((path, "content changed"))
    print(f"dummy round-trip: untouched-file fidelity check: {len(mismatches)} mismatches")
    if mismatches:
        for path, why in mismatches[:10]:
            print(f"  MISMATCH {path}: {why}")
        raise SystemExit("dummy round-trip FAILED: untouched files changed")

    print("dummy round-trip PASSED: compressed size changed, IDX.DAT stayed mirrored, "
          "and every untouched file kept its original bytes.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--container", action="append", dest="containers")
    parser.add_argument("--dummy-roundtrip", action="store_true")
    parser.add_argument("--original-scenario", type=Path)
    parser.add_argument("--lz-script", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--dummy-count", type=int, default=3)
    args = parser.parse_args()

    with args.iso.open("rb") as f:
        image = bytearray(f.read())
    files = builder.iso_files(image)
    containers = args.containers or ["SCENARIO"]
    failed = False
    for stem in containers:
        result = verify_container(image, files, stem)
        report(result)
        failed = failed or not result["complete"]

    if args.dummy_roundtrip:
        if not args.original_scenario or not args.lz_script or not args.work_dir:
            raise SystemExit("--dummy-roundtrip requires --original-scenario --lz-script --work-dir")
        dummy_roundtrip(args.iso, args.original_scenario, args.lz_script, args.work_dir, args.dummy_count)

    if failed:
        raise SystemExit("structure verification FAILED")


if __name__ == "__main__":
    main()
