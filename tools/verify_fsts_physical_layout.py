#!/usr/bin/env python3
"""Verify FSTS stage-entry streams at both indexed and physical addresses."""

from __future__ import annotations

import argparse
import json
import mmap
from pathlib import Path

import galaxy_angel_build as builder
import moonlit_lovers_resources as resources
from eternal_lovers_patch_remaining import merged_resources


STAGE_MARKER = "使用ステージ".encode("cp932")
MAX_CONTROL_RAW_SIZE = 20_000


def container_bytes(image, files, stem: str) -> bytes:
    item = builder.resolve_iso_file(files, stem)
    begin = item.extent * builder.SECTOR
    return bytes(image[begin : begin + item.size])


def stage_entry_resources(container: bytes, stem: str):
    resource_map = merged_resources(container, stem)
    entries = []
    for base in sorted({item.fsts_bases[0] for item in resource_map.values()}):
        bank = sorted(
            (item for item in resource_map.values() if item.fsts_bases[0] == base),
            key=lambda item: item.offset,
        )
        previous_was_control = False
        for item in bank:
            try:
                raw = resources.decompress_resource(
                    container, item.offset, item.raw_size, item.compressed_size
                )
            except Exception:
                raw = b""
            is_control = (
                item.raw_size <= MAX_CONTROL_RAW_SIZE
                and STAGE_MARKER in raw[:512]
            )
            if is_control and not previous_was_control:
                entries.append(item)
            previous_was_control = is_control
    return entries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--container", action="append", default=["SLGSTAGE"])
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    checked = []
    with args.original_iso.open("rb") as original_stream, mmap.mmap(
        original_stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as original_image, args.iso.open("rb") as final_stream, mmap.mmap(
        final_stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as final_image:
        original_files = builder.iso_files(original_image)
        final_files = builder.iso_files(final_image)
        for stem in dict.fromkeys(args.container):
            original = container_bytes(original_image, original_files, stem)
            final = container_bytes(final_image, final_files, stem)
            final_map = merged_resources(final, stem)
            final_by_record = {
                record: item
                for item in final_map.values()
                for record in item.record_positions
            }
            for pristine in stage_entry_resources(original, stem):
                record = pristine.record_positions[0]
                current = final_by_record.get(record)
                if current is None:
                    raise SystemExit(
                        f"{stem} stage-entry record disappeared: {record:#x}"
                    )
                if current.offset != pristine.offset:
                    raise SystemExit(
                        f"{stem} stage-entry moved: record={record:#x} "
                        f"{pristine.offset:#x}->{current.offset:#x}"
                    )
                indexed_raw = resources.decompress_resource(
                    final,
                    current.offset,
                    current.raw_size,
                    current.compressed_size,
                )
                physical_raw = resources.decompress_resource(
                    final,
                    pristine.offset,
                    current.raw_size,
                    current.compressed_size,
                )
                if physical_raw != indexed_raw:
                    raise SystemExit(
                        f"{stem} physical/indexed stage-entry mismatch: "
                        f"{pristine.offset:#x}"
                    )
                checked.append(
                    {
                        "container": stem,
                        "record": record,
                        "offset": pristine.offset,
                        "raw_size": current.raw_size,
                        "compressed_size": current.compressed_size,
                    }
                )

    report = {
        "schema": "galaxy-angel-fsts-physical-layout/v1",
        "original_iso": str(args.original_iso),
        "iso": str(args.iso),
        "stage_entries_checked": len(checked),
        "entries": checked,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(f"FSTS physical layout OK: {len(checked)} stage-entry streams")


if __name__ == "__main__":
    main()

