#!/usr/bin/env python3
"""Verify every translated non-ISB Eternal Lovers resource and IDX mirror."""

from __future__ import annotations

import argparse
import json
import mmap
import struct
from pathlib import Path

import galaxy_angel_build as builder
import galaxy_angel_translation as translation
import moonlit_lovers_resources as resources
from eternal_lovers_patch_remaining import collect_targets, merged_resources, rebuild_resource


def container(image: mmap.mmap, files: dict, stem: str) -> memoryview:
    """Zero-copy view of one container.

    A container kept at its fixed extent whose oversized records were redirected
    to a backing copy at the end of the disc has a logical size spanning most of
    the image.  Copying that slice would allocate gigabytes, so this returns a
    memoryview; every resource helper here already accepts one.
    """
    item = builder.resolve_iso_file(files, stem)
    begin = item.extent * builder.SECTOR
    return memoryview(image)[begin:begin + item.size]


def idx_positions(image: mmap.mmap, files: dict, records: dict) -> tuple[int, dict[int, int]]:
    idx = builder.resolve_iso_file(files, "IDX")
    begin = idx.extent * builder.SECTOR
    wanted: dict[tuple[int, int, int], list[int]] = {}
    for offset, (record, raw_size, compressed_size) in records.items():
        wanted.setdefault((offset, raw_size, compressed_size), []).append(record)
    candidates: dict[int, dict[int, int]] = {}
    for relative in range(0, idx.size - 15, 4):
        file_id, offset, raw_size, compressed_size = struct.unpack_from(
            "<IIII", image, begin + relative
        )
        for record in wanted.get((offset, raw_size, compressed_size), ()):
            candidates.setdefault(file_id, {})[record] = relative
    complete = [(file_id, found) for file_id, found in candidates.items()
                if len(found) == len(records)]
    if len(complete) != 1:
        raise ValueError(f"IDX mirror selection failed: {[(k, len(v)) for k, v in candidates.items()]}")
    return complete[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--patched-iso", type=Path, required=True)
    parser.add_argument("--translations", type=Path, required=True)
    parser.add_argument("--encoding-map", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    payload = json.loads(args.translations.read_text(encoding="utf-8"))
    targets = collect_targets(payload)
    custom_map = translation.load_custom_map(args.encoding_map)
    assert custom_map is not None
    result = {"schema": "eternal-lovers-remaining-verification/v1", "containers": {}}

    with args.original_iso.open("rb") as original_stream, args.patched_iso.open("rb") as patched_stream, mmap.mmap(
        original_stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as original_image, mmap.mmap(
        patched_stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as patched_image:
        original_files = builder.iso_files(original_image)
        patched_files = builder.iso_files(patched_image)
        for stem, blocks in sorted(targets.items()):
            old_data = container(original_image, original_files, stem)
            new_data = container(patched_image, patched_files, stem)
            old_map = merged_resources(old_data, stem)
            new_map = merged_resources(new_data, stem)
            new_by_record = {
                record: item
                for item in new_map.values()
                for record in item.record_positions
            }
            strings = 0
            for old_offset, units in sorted(blocks.items()):
                old_item = old_map[old_offset]
                old_raw = resources.decompress_resource(
                    old_data, old_item.offset, old_item.raw_size, old_item.compressed_size
                )
                expected, count = rebuild_resource(old_raw, units, custom_map)
                strings += count
                record = old_item.record_positions[0]
                new_item = new_by_record[record]
                actual = resources.decompress_resource(
                    new_data, new_item.offset, new_item.raw_size, new_item.compressed_size
                )
                if actual != expected:
                    raise ValueError(f"translated resource mismatch: {stem}:{old_offset:#x}")

            idx_checked = idx_changed = 0
            old_records = resources.pidx_record_map(old_data, stem)
            if old_records:
                file_id, positions = idx_positions(original_image, original_files, old_records)
                new_idx = builder.resolve_iso_file(patched_files, "IDX")
                new_begin = new_idx.extent * builder.SECTOR
                old_by_record = {
                    record: (item.offset, item.raw_size, item.compressed_size)
                    for item in old_map.values()
                    for record in item.record_positions
                }
                for _offset, (record, old_raw_size, old_compressed_size) in old_records.items():
                    old_item_tuple = old_by_record[record]
                    new_item = new_by_record[record]
                    expected_tuple = (
                        new_item.offset, new_item.raw_size, new_item.compressed_size
                    )
                    relative = positions[record]
                    actual_tuple = struct.unpack_from("<III", patched_image, new_begin + relative + 4)
                    if actual_tuple != expected_tuple:
                        raise ValueError(f"IDX mismatch: {stem} record={record:#x}")
                    idx_checked += 1
                    idx_changed += int(old_item_tuple != expected_tuple)
                idx_file_id = file_id
            else:
                idx_file_id = None
            result["containers"][stem] = {
                "resources": len(blocks),
                "strings": strings,
                "idx_file_id": idx_file_id,
                "idx_checked": idx_checked,
                "idx_changed": idx_changed,
            }
            print(stem, result["containers"][stem], flush=True)
            # A memoryview keeps the mmap exported; release each container view
            # before the context manager closes the maps.
            old_data.release()
            new_data.release()

    args.report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"verified: {args.report}")


if __name__ == "__main__":
    main()
