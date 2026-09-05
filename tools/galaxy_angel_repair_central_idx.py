#!/usr/bin/env python3
"""Repair a patched Galaxy Angel ISO's stale IDX.DAT PIDX mirror."""

from __future__ import annotations

import argparse
from pathlib import Path

import galaxy_angel_build as builder


def container(image: bytearray, stem: str) -> tuple[builder.IsoFile, bytearray]:
    files = builder.iso_files(image)
    item = builder.resolve_iso_file(files, stem)
    begin = item.extent * builder.SECTOR
    return item, bytearray(image[begin : begin + item.size])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--patched-iso", type=Path, required=True)
    parser.add_argument("--output-iso", type=Path, required=True)
    parser.add_argument("--container", default="GADAT001")
    args = parser.parse_args()

    original_image = bytearray(args.original_iso.read_bytes())
    patched_image = bytearray(args.patched_iso.read_bytes())
    _, original_container = container(original_image, args.container)
    _, patched_container = container(patched_image, args.container)
    original_records = builder.records(original_container)
    patched_records = builder.records(patched_container)
    patched_by_record = {
        record: (offset, raw_size, compressed_size)
        for offset, (record, raw_size, compressed_size) in patched_records.items()
    }
    original_record_positions = {record for record, _, _ in original_records.values()}
    if set(patched_by_record) != original_record_positions:
        raise SystemExit("original and patched PIDX leaf record sets differ")

    files = builder.iso_files(patched_image)
    file_id, count = builder.patch_central_idx_records(
        patched_image, files, original_records, patched_by_record
    )
    args.output_iso.parent.mkdir(parents=True, exist_ok=True)
    args.output_iso.write_bytes(patched_image)
    print(
        f"patched {count} IDX.DAT records for {args.container} "
        f"(file id {file_id}); wrote {args.output_iso}"
    )


if __name__ == "__main__":
    main()
