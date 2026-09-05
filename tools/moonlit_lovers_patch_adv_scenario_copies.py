#!/usr/bin/env python3
"""Synchronize Moonlit Lovers SCENARIO translations into ADV FSTS runtime copies.

ADV.DAT contains a subset of SCENARIO.DAT resources as exact raw-byte runtime
copies.  The normal SCENARIO builder only patches SCENARIO.DAT, so these copies
must be updated separately.  Mapping is intentionally derived from the pristine
ISO by exact SHA-256 raw matches, then carried into the patched ISO by the stable
FSTS table-record position.  No local/remote AI is used and no resource is
relocated: every translated stream must fit its existing FSTS slot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import shutil
import struct
from collections import defaultdict
from pathlib import Path

import eternal_lovers_patch_remaining as remaining_engine
import galaxy_angel_build as builder
import galaxy_angel_translation as translation
import ikusa_lz
import moonlit_lovers_patch_remaining as remaining_adapter
import moonlit_lovers_resources as resources


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_container(image: bytes | bytearray | mmap.mmap, files: dict[str, builder.IsoFile], stem: str) -> tuple[builder.IsoFile, bytes]:
    item = builder.resolve_iso_file(files, stem)
    begin = item.extent * builder.SECTOR
    return item, bytes(image[begin:begin + item.size])


def source_hash_map(source_scenario: Path) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = defaultdict(list)
    for path in sorted(source_scenario.glob("SCENARIO_DAT_*.txt")):
        result[sha256(path.read_bytes())].append(path)
    if not result:
        raise SystemExit(f"no SCENARIO_DAT_*.txt files under {source_scenario}")
    return dict(result)


def choose_built(paths: list[Path], built_scenario: Path) -> tuple[str, bytes]:
    built_rows: list[tuple[str, bytes]] = []
    for source_path in paths:
        built_path = built_scenario / source_path.name
        raw = built_path.read_bytes() if built_path.is_file() else source_path.read_bytes()
        built_rows.append((source_path.name, raw))
    first_name, first_raw = built_rows[0]
    for name, raw in built_rows[1:]:
        if raw != first_raw:
            raise SystemExit(
                "duplicate pristine scenario raw maps to different translated outputs: "
                f"{first_name} / {name}"
            )
    return first_name, first_raw


def merged_by_record(container: bytes, stem: str) -> tuple[dict[int, resources.Resource], dict[int, resources.Resource]]:
    by_offset = remaining_engine.merged_resources(container, stem)
    by_record: dict[int, resources.Resource] = {}
    for item in by_offset.values():
        for record in item.record_positions:
            previous = by_record.get(record)
            if previous is not None and previous.offset != item.offset:
                raise SystemExit(f"duplicate {stem} record position: {record:#x}")
            by_record[record] = item
    return by_offset, by_record


def slot_end_for(resource: resources.Resource, resource_map: dict[int, resources.Resource], container_size: int) -> int:
    if resource.source_kind != "fsts" or len(set(resource.fsts_bases)) != 1:
        raise SystemExit(f"ADV runtime copy is not a single-bank FSTS resource: {resource.offset:#x}")
    bank_base = resource.fsts_bases[0]
    bank_bases = sorted({base for item in resource_map.values() for base in item.fsts_bases})
    bank_boundary = min((base for base in bank_bases if base > bank_base), default=container_size)
    next_same_bank = [
        offset
        for offset, item in resource_map.items()
        if offset > resource.offset and set(item.fsts_bases) == {bank_base}
    ]
    return min(next_same_bank, default=bank_boundary)


def build_plan(
    pristine_container: bytes,
    patched_container: bytes,
    source_scenario: Path,
    built_scenario: Path,
    prior_remaining_targets: dict[int, list[dict]] | None = None,
    custom_map: dict[str, bytes] | None = None,
) -> list[dict]:
    hashes = source_hash_map(source_scenario)
    pristine_resources = remaining_engine.merged_resources(pristine_container, "ADV")
    patched_resources, patched_by_record = merged_by_record(patched_container, "ADV")
    plan: list[dict] = []

    for pristine in pristine_resources.values():
        try:
            pristine_raw = resources.decompress_resource(
                pristine_container,
                pristine.offset,
                pristine.raw_size,
                pristine.compressed_size,
            )
        except ValueError:
            continue
        source_paths = hashes.get(sha256(pristine_raw))
        if not source_paths:
            continue
        if not pristine.record_positions:
            raise SystemExit(f"ADV runtime copy has no stable FSTS record: {pristine.offset:#x}")
        record = pristine.record_positions[0]
        current = patched_by_record.get(record)
        if current is None:
            raise SystemExit(f"ADV runtime copy record disappeared: {record:#x}")
        current_raw = resources.decompress_resource(
            patched_container,
            current.offset,
            current.raw_size,
            current.compressed_size,
        )
        source_name, built_raw = choose_built(source_paths, built_scenario)
        validated_prior_remaining = False
        if current_raw == built_raw:
            encoded = patched_container[current.offset:current.offset + current.compressed_size]
            already_patched = True
        else:
            if current_raw != pristine_raw:
                units = (prior_remaining_targets or {}).get(pristine.offset)
                if not units or custom_map is None:
                    raise SystemExit(
                        f"ADV runtime copy was modified by another stage: record={record:#x} "
                        f"source={source_name}"
                    )
                expected_current, _count = remaining_engine.rebuild_resource(
                    pristine_raw, units, custom_map
                )
                if current_raw != expected_current:
                    raise SystemExit(
                        f"ADV runtime copy has unrecognized prior changes: record={record:#x} "
                        f"source={source_name}"
                    )
                validated_prior_remaining = True
            encoded = remaining_engine.encode_resource(built_raw, current.codec)
            if current.codec == "ikusa_lz":
                optimal = ikusa_lz.compress_optimal(built_raw)
                if len(optimal) < len(encoded):
                    encoded = optimal
            already_patched = False
        decoded = resources.decompress_resource(encoded, 0, len(built_raw), len(encoded))
        if decoded != built_raw:
            raise SystemExit(f"ADV runtime compressor round-trip failed: {source_name}")
        slot_end = slot_end_for(current, patched_resources, len(patched_container))
        capacity = slot_end - current.offset
        if len(encoded) > capacity:
            raise SystemExit(
                f"ADV runtime copy does not fit fixed slot: {source_name} "
                f"required={len(encoded)} capacity={capacity}"
            )
        plan.append(
            {
                "source_name": source_name,
                "pristine_offset": pristine.offset,
                "current_offset": current.offset,
                "record": record,
                "record_positions": list(current.record_positions),
                "fsts_bases": list(current.fsts_bases),
                "raw_size_before": current.raw_size,
                "raw_size_after": len(built_raw),
                "compressed_size_before": current.compressed_size,
                "compressed_size_after": len(encoded),
                "capacity": capacity,
                "slot_end": slot_end,
                "codec": current.codec,
                "raw_sha256": sha256(built_raw),
                "already_patched": already_patched,
                "validated_prior_remaining": validated_prior_remaining,
                "encoded": encoded,
                "built_raw": built_raw,
            }
        )
    return sorted(plan, key=lambda row: row["record"])


def apply_plan(container: bytearray, plan: list[dict]) -> None:
    for row in plan:
        if row["already_patched"]:
            continue
        offset = row["current_offset"]
        slot_end = row["slot_end"]
        encoded = row["encoded"]
        container[offset:slot_end] = bytes(slot_end - offset)
        container[offset:offset + len(encoded)] = encoded
        for record, base in zip(row["record_positions"], row["fsts_bases"]):
            struct.pack_into(
                "<III",
                container,
                record + 4,
                offset - base,
                row["raw_size_after"],
                len(encoded),
            )


def verify_output(before_container: bytes, after_container: bytes, plan: list[dict]) -> dict:
    after_map, after_by_record = merged_by_record(after_container, "ADV")
    expected = bytearray(before_container)
    actual = bytearray(after_container)
    verified = 0
    for row in plan:
        out = after_by_record.get(row["record"])
        if out is None:
            raise SystemExit(f"ADV runtime record missing after patch: {row['record']:#x}")
        raw = resources.decompress_resource(
            after_container, out.offset, out.raw_size, out.compressed_size
        )
        if raw != row["built_raw"]:
            raise SystemExit(f"ADV runtime raw mismatch after patch: {row['source_name']}")
        verified += 1
        expected[row["current_offset"]:row["slot_end"]] = bytes(
            row["slot_end"] - row["current_offset"]
        )
        actual[row["current_offset"]:row["slot_end"]] = bytes(
            row["slot_end"] - row["current_offset"]
        )
        for record in row["record_positions"]:
            expected[record + 4:record + 16] = bytes(12)
            actual[record + 4:record + 16] = bytes(12)
    if expected != actual:
        raise SystemExit("ADV bytes changed outside translated runtime-copy slots/table fields")
    return {
        "verified_runtime_copies": verified,
        "unchanged_outside_allowed_regions": True,
        "resource_count_after": len(after_map),
    }


def report_payload(plan: list[dict], verification: dict, source_iso: Path, output_iso: Path) -> dict:
    rows = []
    for row in plan:
        rows.append({key: value for key, value in row.items() if key not in ("encoded", "built_raw")})
    return {
        "schema": "moonlit-lovers-adv-scenario-runtime/v1",
        "source_iso": str(source_iso),
        "output_iso": str(output_iso),
        "runtime_copies": len(plan),
        "already_patched": sum(1 for row in plan if row["already_patched"]),
        "newly_patched": sum(1 for row in plan if not row["already_patched"]),
        "validated_prior_remaining": sum(
            1 for row in plan if row.get("validated_prior_remaining")
        ),
        "relocated": 0,
        "verification": verification,
        "entries": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", type=Path, required=True, help="Current integrated ISO to copy/patch")
    parser.add_argument(
        "--output-iso",
        type=Path,
        help="Write a copied artifact; omit to patch --iso in place.",
    )
    parser.add_argument("--original-iso", type=Path, required=True, help="Pristine Japanese reference ISO")
    parser.add_argument("--source-scenario", type=Path, required=True)
    parser.add_argument("--built-scenario", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--remaining-index", type=Path)
    parser.add_argument("--remaining-overlay", type=Path)
    parser.add_argument("--encoding-map", type=Path)
    args = parser.parse_args()

    output_iso = args.output_iso or args.iso
    if args.output_iso:
        args.output_iso.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.iso, args.output_iso)

    remaining_args = (
        args.remaining_index,
        args.remaining_overlay,
        args.encoding_map,
    )
    if any(value is not None for value in remaining_args) and not all(
        value is not None for value in remaining_args
    ):
        raise SystemExit(
            "--remaining-index, --remaining-overlay, and --encoding-map must be supplied together"
        )
    prior_remaining_targets: dict[int, list[dict]] = {}
    custom_map: dict[str, bytes] | None = None
    if all(value is not None for value in remaining_args):
        payload = remaining_adapter.merge_overlay(
            remaining_adapter.load_json(args.remaining_index),
            remaining_adapter.load_json(args.remaining_overlay),
        )
        prior_remaining_targets = remaining_engine.collect_targets(payload).get("ADV", {})
        custom_map = translation.load_custom_map(args.encoding_map)
        assert custom_map is not None

    with args.original_iso.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as original_image:
        original_files = builder.iso_files(original_image)
        _original_item, pristine_container = load_container(original_image, original_files, "ADV")

    with output_iso.open("r+b") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_WRITE) as output_image:
        output_files = builder.iso_files(output_image)
        item, before_container = load_container(output_image, output_files, "ADV")
        plan = build_plan(
            pristine_container,
            before_container,
            args.source_scenario,
            args.built_scenario,
            prior_remaining_targets,
            custom_map,
        )
        if not plan:
            raise SystemExit("no ADV scenario runtime copies matched pristine SCENARIO resources")
        begin = item.extent * builder.SECTOR
        rebuilt = bytearray(before_container)
        apply_plan(rebuilt, plan)
        output_image[begin:begin + item.size] = rebuilt
        output_image.flush()

    with output_iso.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as output_image:
        output_files = builder.iso_files(output_image)
        _item, after_container = load_container(output_image, output_files, "ADV")
    verification = verify_output(before_container, after_container, plan)
    payload = report_payload(plan, verification, args.iso, output_iso)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"patched ADV scenario runtime copies: {payload['newly_patched']} new, "
        f"{payload['already_patched']} already; verified={verification['verified_runtime_copies']}"
    )
    print(f"report: {args.report}")


if __name__ == "__main__":
    main()
