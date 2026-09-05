#!/usr/bin/env python3
"""Audit every byte-identical compressed scenario-index copy in an ISO."""

from __future__ import annotations

import argparse
import mmap
import struct
from pathlib import Path

import galaxy_angel_build as builder


def index_stream(image: mmap.mmap) -> bytes:
    files = builder.iso_files(image)
    item = builder.resolve_iso_file(files, "GADAT001")
    begin = item.extent * builder.SECTOR
    header = bytearray(image[begin : begin + min(item.size, 0x160000)])
    record, _raw_size, _compressed_size = builder.records(header)[
        builder.GADAT001_INDEX_OFFSET
    ]
    offset, _raw_size, compressed_size = struct.unpack_from("<III", header, record)
    return bytes(image[begin + offset : begin + offset + compressed_size])


def all_positions(image: mmap.mmap, needle: bytes) -> list[int]:
    result: list[int] = []
    cursor = 0
    while True:
        position = image.find(needle, cursor)
        if position < 0:
            return result
        result.append(position)
        cursor = position + 1


def describe(image: mmap.mmap, positions: list[int]) -> list[tuple[str, int]]:
    files = sorted(builder.iso_files(image).values(), key=lambda item: item.extent)
    result: list[tuple[str, int]] = []
    for position in positions:
        owners = [
            item for item in files
            if item.extent * builder.SECTOR <= position
            < item.extent * builder.SECTOR + item.size
        ]
        # GADAT001 intentionally has a huge logical extent for redirected
        # blocks and can overlap ordinary ISO files.  Prefer the narrowest
        # physical owner when extents overlap.
        owner = min(owners, key=lambda item: item.size) if owners else None
        result.append(
            (owner.path, position - owner.extent * builder.SECTOR)
            if owner else ("<outside ISO9660 extent>", position)
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--patched-iso", type=Path, required=True)
    args = parser.parse_args()
    with (
        args.original_iso.open("rb") as original_stream,
        mmap.mmap(original_stream.fileno(), 0, access=mmap.ACCESS_READ) as original,
        args.patched_iso.open("rb") as patched_stream,
        mmap.mmap(patched_stream.fileno(), 0, access=mmap.ACCESS_READ) as patched,
    ):
        old_stream = index_stream(original)
        new_stream = index_stream(patched)
        original_copies = all_positions(original, old_stream)
        patched_new_copies = all_positions(patched, new_stream)
        patched_stale_copies = all_positions(patched, old_stream)
        print(f"original_index_copies={describe(original, original_copies)}")
        print(f"patched_index_copies={describe(patched, patched_new_copies)}")
        print(f"patched_stale_copies={describe(patched, patched_stale_copies)}")
        if patched_stale_copies:
            raise SystemExit("stale scenario-index copies remain")
        # The appended GADAT001 backing container contains one additional,
        # harmless rebuilt copy.  What matters is that every original copy was
        # replaced and that no stale compressed stream remains anywhere.
        if len(patched_new_copies) < len(original_copies):
            raise SystemExit(
                f"scenario-index copy count changed: "
                f"{len(original_copies)}->{len(patched_new_copies)}"
            )


if __name__ == "__main__":
    main()
