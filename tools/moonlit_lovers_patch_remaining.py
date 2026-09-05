#!/usr/bin/env python3
"""Patch Moonlit Lovers non-scenario text using a small translation overlay.

`remaining_candidates.json` is an immutable extraction/index.  Human/model
translation work lives in `remaining_translations.json`.  This adapter validates
that every overlay ID still names the exact extracted source string, then feeds
only those approved entries to the generic PIDX/FSTS repacker already proven by
Eternal Lovers.

This tool does not create translations.  `--dry-run` is safe for structure and
capacity testing and does not modify the ISO.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import shutil
from pathlib import Path

import eternal_lovers_backing
import eternal_lovers_patch_remaining as engine
import galaxy_angel_build as builder
import galaxy_angel_translation as translation
import moonlit_lovers_resources as resources


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_tbi_com_payload(
    overlay_path: Path,
    manifest_path: Path,
    raw_root: Path,
) -> dict:
    """Build exact SCENARIO TBI ``COM_n`` targets from the extracted resources.

    The original remaining-text scanner intentionally classifies SCENARIO resources as
    scenario-owned, but Moonlit's ship-movement ``tbi_*.tbl`` files contain separate
    user-visible room descriptions that are not represented by ``@0/@1`` scenario units.
    Keep the immutable remaining index untouched and derive these targets directly from
    the full-extraction manifest plus the compact translation overlay.
    """

    overlay = load_json(overlay_path)
    entries = overlay.get("entries", [])
    if not entries:
        return {"candidates": []}
    by_original: dict[str, dict] = {}
    for entry in entries:
        original = str(entry["original"])
        if original in by_original:
            raise SystemExit(f"duplicate TBI original in overlay: {original!r}")
        by_original[original] = entry

    manifest = load_json(manifest_path)
    resources_by_name = {
        str(item["name"]): item
        for item in manifest.get("resources", [])
        if str(item.get("name", "")).lower().startswith("tbi_")
        and str(item.get("name", "")).lower().endswith(".tbl")
    }
    if not resources_by_name:
        raise SystemExit(f"no tbi_*.tbl resources in manifest: {manifest_path}")

    occurrences: dict[str, list[dict]] = {original: [] for original in by_original}
    for name, resource in sorted(resources_by_name.items()):
        path = raw_root / name
        if not path.is_file():
            raise SystemExit(f"TBI raw resource missing: {path}")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        expected = str(resource["raw_sha256"])
        if digest != expected:
            raise SystemExit(f"TBI raw hash mismatch: {name}: {digest} != {expected}")
        text = raw.decode("cp932")
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.startswith("COM_") or "=" not in line:
                continue
            _key, value = line.split("=", 1)
            if value not in occurrences:
                continue
            occurrences[value].append(
                {
                    "file": "SCENARIO.DAT",
                    "container": "SCENARIO",
                    "block_offset": int(resource["offset"]),
                    "resource_path": str(resource.get("path") or resource.get("raw_file") or name),
                    "line": line_number,
                    "section": None,
                    "codec": str(resource["codec"]),
                    "raw_sha256": expected,
                }
            )

    candidates: list[dict] = []
    missing: list[str] = []
    for entry in entries:
        original = str(entry["original"])
        found = occurrences[original]
        if not found:
            missing.append(original)
            continue
        if not entry.get("use_translation") or not entry.get("translation"):
            continue
        candidates.append(
            {
                "id": str(entry["id"]),
                "original": original,
                "translation": str(entry["translation"]),
                "state": str(entry.get("state", "draft")),
                "use_translation": True,
                "occurrences": found,
            }
        )
    if missing:
        raise SystemExit("TBI overlay strings missing from extraction: " + ", ".join(repr(x) for x in missing))
    return {"candidates": candidates}


def scenario_runtime_copy_offsets(reference_iso: Path, source_scenario: Path) -> set[int]:
    source_hashes = {
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source_scenario.glob("SCENARIO_DAT_*.txt")
    }
    if not source_hashes:
        raise SystemExit(f"no SCENARIO_DAT_*.txt files under {source_scenario}")
    with reference_iso.open("rb") as stream, mmap.mmap(
        stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as image:
        files = builder.iso_files(image)
        item = builder.resolve_iso_file(files, "ADV")
        begin = item.extent * builder.SECTOR
        container = bytes(image[begin:begin + item.size])
    offsets: set[int] = set()
    for resource in engine.merged_resources(container, "ADV").values():
        try:
            raw = resources.decompress_resource(
                container, resource.offset, resource.raw_size, resource.compressed_size
            )
        except ValueError:
            continue
        if hashlib.sha256(raw).hexdigest() in source_hashes:
            offsets.add(resource.offset)
    return offsets


def merge_overlay(
    index_payload: dict,
    overlay_payload: dict,
    excluded_adv_offsets: set[int] | None = None,
) -> dict:
    candidates = {item["id"]: item for item in index_payload["candidates"]}
    merged: list[dict] = []
    seen: set[str] = set()
    for entry in overlay_payload.get("entries", []):
        unit_id = entry["id"]
        if unit_id in seen:
            raise SystemExit(f"duplicate overlay id: {unit_id}")
        seen.add(unit_id)
        source = candidates.get(unit_id)
        if source is None:
            raise SystemExit(f"overlay id missing from extraction: {unit_id}")
        if source["original"] != entry["original"]:
            raise SystemExit(
                f"overlay source mismatch: {unit_id}\n"
                f"  index={source['original']!r}\n  overlay={entry['original']!r}"
            )
        if not entry.get("use_translation") or not entry.get("translation"):
            continue
        occurrences = list(source.get("occurrences", []))
        if excluded_adv_offsets:
            occurrences = [
                occurrence
                for occurrence in occurrences
                if not (
                    occurrence.get("container") == "ADV"
                    and int(occurrence.get("block_offset", -1)) in excluded_adv_offsets
                )
            ]
        if not occurrences:
            continue
        merged.append(
            {
                **source,
                "occurrences": occurrences,
                "translation": entry["translation"],
                "state": entry.get("state", "draft"),
                "use_translation": True,
            }
        )
    return {"candidates": merged}


def verify_patch(
    original_iso: Path,
    patched_iso: Path,
    payload: dict,
    custom_map: dict[str, bytes],
    containers: set[str] | None,
    allow_prepatched_containers: set[str] | None = None,
) -> dict:
    targets = engine.collect_targets(payload)
    allow_prepatched_containers = allow_prepatched_containers or set()
    if containers:
        targets = {stem: blocks for stem, blocks in targets.items() if stem in containers}
    result: dict[str, dict] = {}
    with original_iso.open("rb") as original_stream, patched_iso.open("rb") as patched_stream:
        with mmap.mmap(original_stream.fileno(), 0, access=mmap.ACCESS_READ) as original_image, mmap.mmap(
            patched_stream.fileno(), 0, access=mmap.ACCESS_READ
        ) as patched_image:
            original_files = builder.iso_files(original_image)
            patched_files = builder.iso_files(patched_image)
            for stem, blocks in sorted(targets.items()):
                original_item = builder.resolve_iso_file(original_files, stem)
                patched_item = builder.resolve_iso_file(patched_files, stem)
                original_begin = original_item.extent * builder.SECTOR
                patched_begin = patched_item.extent * builder.SECTOR
                original_container = bytes(
                    original_image[original_begin:original_begin + original_item.size]
                )
                patched_container = bytes(
                    patched_image[patched_begin:patched_begin + patched_item.size]
                )
                original_resources = engine.merged_resources(original_container, stem)
                patched_resources = engine.merged_resources(patched_container, stem)
                patched_by_record = {
                    record: item
                    for item in patched_resources.values()
                    for record in item.record_positions
                }
                verified_resources = verified_strings = 0
                raw_unchanged = 0
                target_offsets = set(blocks)
                for offset, original_resource in original_resources.items():
                    if not original_resource.record_positions:
                        raise SystemExit(f"{stem}:{offset:#x} has no stable table record")
                    patched_resource = patched_by_record.get(original_resource.record_positions[0])
                    if patched_resource is None:
                        raise SystemExit(
                            f"{stem}:{offset:#x} missing after patch at record "
                            f"{original_resource.record_positions[0]:#x}"
                        )
                    original_raw = resources.decompress_resource(
                        original_container,
                        original_resource.offset,
                        original_resource.raw_size,
                        original_resource.compressed_size,
                    )
                    patched_raw = resources.decompress_resource(
                        patched_container,
                        patched_resource.offset,
                        patched_resource.raw_size,
                        patched_resource.compressed_size,
                    )
                    if offset in target_offsets:
                        expected_raw, string_count = engine.rebuild_resource(
                            original_raw, blocks[offset], custom_map
                        )
                        if patched_raw != expected_raw:
                            raise SystemExit(f"patched raw mismatch: {stem}:{offset:#x}")
                        verified_resources += 1
                        verified_strings += string_count
                    else:
                        if stem not in allow_prepatched_containers and patched_raw != original_raw:
                            raise SystemExit(f"untouched raw changed: {stem}:{offset:#x}")
                        raw_unchanged += int(patched_raw == original_raw)
                result[stem] = {
                    "verified_resources": verified_resources,
                    "verified_strings": verified_strings,
                    "untouched_raw_unchanged": raw_unchanged,
                    "original_size": original_item.size,
                    "patched_size": patched_item.size,
                    "original_extent": original_item.extent,
                    "patched_extent": patched_item.extent,
                }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument(
        "--output-iso",
        type=Path,
        help="Patch a copied build artifact instead of modifying --iso in place.",
    )
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument(
        "--tbi-overlay",
        type=Path,
        help="Optional ship-movement tbi_*.tbl COM_n translation overlay.",
    )
    parser.add_argument(
        "--tbi-manifest",
        type=Path,
        help="SCENARIO full-extraction manifest used with --tbi-overlay.",
    )
    parser.add_argument(
        "--tbi-raw-root",
        type=Path,
        help="Directory containing extracted tbi_*.tbl files used with --tbi-overlay.",
    )
    parser.add_argument("--encoding-map", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument(
        "--backing-region",
        type=Path,
        help=(
            "JSON from eternal_lovers_reserve_backing_region.py. A container "
            "that outgrows its allocation puts the overflow there."
        ),
    )
    parser.add_argument(
        "--verify-source-iso",
        type=Path,
        help=(
            "Unmodified reference ISO used for post-write raw verification when "
            "--iso is patched in place. Required for in-place non-dry-run writes."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--source-scenario",
        type=Path,
        help=(
            "When supplied, exclude ADV occurrences that are exact pristine "
            "SCENARIO runtime copies; those are synchronized by "
            "moonlit_lovers_patch_adv_scenario_copies.py."
        ),
    )
    parser.add_argument(
        "--container",
        action="append",
        dest="containers",
        help="Restrict to one or more container stems, e.g. SLG or SLGSTAGE.",
    )
    args = parser.parse_args()

    excluded_adv_offsets: set[int] = set()
    if args.source_scenario:
        reference_iso = args.verify_source_iso or args.iso
        excluded_adv_offsets = scenario_runtime_copy_offsets(
            reference_iso, args.source_scenario
        )
        print(
            f"excluding {len(excluded_adv_offsets)} ADV SCENARIO runtime-copy blocks "
            "from remaining patch targets",
            flush=True,
        )
    payload = merge_overlay(
        load_json(args.index), load_json(args.overlay), excluded_adv_offsets
    )
    tbi_payload = {"candidates": []}
    if args.tbi_overlay:
        if not args.tbi_manifest or not args.tbi_raw_root:
            raise SystemExit("--tbi-overlay requires --tbi-manifest and --tbi-raw-root")
        tbi_payload = build_tbi_com_payload(
            args.tbi_overlay, args.tbi_manifest, args.tbi_raw_root
        )
        payload["candidates"].extend(tbi_payload["candidates"])
    elif args.tbi_manifest or args.tbi_raw_root:
        raise SystemExit("--tbi-manifest/--tbi-raw-root require --tbi-overlay")
    custom_map = translation.load_custom_map(args.encoding_map)
    assert custom_map is not None
    region = eternal_lovers_backing.load(args.backing_region)
    targets = engine.collect_targets(payload)
    requested = {item.upper() for item in args.containers or []}
    if requested:
        targets = {stem: blocks for stem, blocks in targets.items() if stem in requested}

    cache_dir = args.cache_dir or args.report.parent / "remaining_compressed_cache"
    patch_iso = args.iso
    if not args.dry_run and not args.output_iso and not args.verify_source_iso:
        raise SystemExit(
            "in-place patch verification requires --verify-source-iso; alternatively "
            "use --output-iso so --iso remains the immutable verification source"
        )
    if args.output_iso:
        if args.dry_run:
            raise SystemExit("--output-iso cannot be combined with --dry-run")
        args.output_iso.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.iso, args.output_iso)
        patch_iso = args.output_iso

    report = {
        "schema": "moonlit-lovers-remaining-patch/v1",
        "source_iso": str(args.iso),
        "patched_iso": str(patch_iso),
        "index": str(args.index),
        "overlay": str(args.overlay),
        "tbi_overlay": str(args.tbi_overlay) if args.tbi_overlay else None,
        "tbi_candidates": len(tbi_payload["candidates"]),
        "encoding_map": str(args.encoding_map),
        "verify_source_iso": str(args.verify_source_iso) if args.verify_source_iso else None,
        "dry_run": args.dry_run,
        "overlay_entries": len(payload["candidates"]),
        "excluded_adv_scenario_runtime_offsets": sorted(excluded_adv_offsets),
        "containers": {},
    }
    compressed_cache: dict[tuple[str, str], bytes] = {}
    mode = "rb" if args.dry_run else "r+b"
    access = mmap.ACCESS_READ if args.dry_run else mmap.ACCESS_WRITE
    with patch_iso.open(mode) as stream, mmap.mmap(stream.fileno(), 0, access=access) as image:
        files = builder.iso_files(image)
        for stem, blocks in sorted(targets.items()):
            result = engine.patch_container(
                image,
                files,
                stem,
                blocks,
                custom_map,
                args.dry_run,
                cache_dir,
                compressed_cache,
                region,
            )
            result["target_offsets"] = sorted(blocks)
            report["containers"][stem] = result
            print(
                stem,
                {key: value for key, value in result.items() if key not in ("overflows", "target_offsets")},
                flush=True,
            )
        if not args.dry_run:
            image.flush()

    if not args.dry_run:
        verification_source = args.verify_source_iso or args.iso
        if verification_source.resolve() == patch_iso.resolve():
            raise SystemExit(
                "verification source must differ from the patched ISO; use "
                "--verify-source-iso or --output-iso"
            )
        report["verification"] = verify_patch(
            verification_source,
            patch_iso,
            payload,
            custom_map,
            requested or None,
            {"SCENARIO"} if args.tbi_overlay else set(),
        )
        for stem, verification in sorted(report["verification"].items()):
            print(f"verified {stem}: {verification}", flush=True)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"report: {args.report}")


if __name__ == "__main__":
    main()
