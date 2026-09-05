#!/usr/bin/env python3
"""Statically compare all rebuilt ISB resources with their bytes inside an ISO."""

from __future__ import annotations

import argparse
import json
import mmap
import re
from pathlib import Path

import galaxy_angel_build as builder
import moonlit_lovers_resources as resources
from eternal_lovers_patch_remaining import merged_resources
from eternal_lovers_verify_remaining import container


NAME_RE = re.compile(r"^SCENARIO_DAT_([0-9a-fA-F]{8})\.txt$")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--patched-iso", type=Path, required=True)
    parser.add_argument("--built-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    checked = 0
    with args.original_iso.open("rb") as old_stream, args.patched_iso.open("rb") as new_stream, mmap.mmap(
        old_stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as old_image, mmap.mmap(new_stream.fileno(), 0, access=mmap.ACCESS_READ) as new_image:
        old_files = builder.iso_files(old_image)
        new_files = builder.iso_files(new_image)
        old_data = container(old_image, old_files, "SCENARIO")
        new_data = container(new_image, new_files, "SCENARIO")
        old_map = merged_resources(old_data, "SCENARIO")
        new_map = merged_resources(new_data, "SCENARIO")
        new_by_record = {
            record: item for item in new_map.values() for record in item.record_positions
        }
        for path in sorted(args.built_dir.glob("SCENARIO_DAT_*.txt")):
            match = NAME_RE.match(path.name)
            if not match:
                continue
            offset = int(match.group(1), 16)
            old_item = old_map[offset]
            record = old_item.record_positions[0]
            new_item = new_by_record[record]
            actual = resources.decompress_resource(
                new_data, new_item.offset, new_item.raw_size, new_item.compressed_size
            )
            expected = path.read_bytes()
            if actual != expected:
                raise SystemExit(f"ISB ISO mismatch: {path.name}")
            checked += 1
        # A memoryview keeps the mmap exported; drop both before the context
        # manager tries to close them.
        old_data.release()
        new_data.release()
    report = {"schema": "eternal-lovers-iso-isb-verification/v1", "resources": checked, "matched": True}
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"verified ISB resources={checked}")


if __name__ == "__main__":
    main()
