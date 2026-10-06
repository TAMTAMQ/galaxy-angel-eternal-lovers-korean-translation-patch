#!/usr/bin/env python3
"""Grow the PIDX/IDX.DAT bank sizes of FSTS-backed containers to cover their data.

An FSTS-backed container (ADV.DAT, SLGRES.DAT, ...) has no named PIDX nodes.
Its PIDX header instead lists every FSTS bank as ``(offset, size, count,
first_index)``, and IDX.DAT mirrors the same triple.  The game reads exactly
``size`` bytes of a bank and then decodes the streams inside it, so a stream
that a repack let grow past that size loses its tail: the decoder runs on into
whatever follows in memory.

That is what crashed Eternal Lovers after the first battle.  The Korean
9112.isb mission-briefing copy in ADV ends at bank byte 0x8c7, the directory
still said 0x8b0, and the last 30 bytes of the script - its statement offset
table - came out as garbage.  The interpreter then jumped through it and the
console fell back to the PS2 browser.

The repack passes keep every stream inside the next bank's start, so the room
is there; only the size field is stale.  This pass finds each directory entry
by its pristine ``(offset, size, count)`` triple, then writes
``max(original size, 16-aligned data end)`` back at the same position in the
working ISO.  It never shrinks a bank and refuses to cross the next bank.
"""

from __future__ import annotations

import argparse
import json
import mmap
import struct
from collections import Counter
from pathlib import Path

import galaxy_angel_build as builder
import moonlit_lovers_resources as resources


ALIGN = 0x10


def align_up(value: int, alignment: int = ALIGN) -> int:
    return (value + alignment - 1) // alignment * alignment


def fsts_containers(image, files):
    for name, item in files.items():
        begin = item.extent * builder.SECTOR
        if bytes(image[begin : begin + 4]) != b"PIDX":
            continue
        view = memoryview(image)[begin : begin + item.size]
        try:
            if resources.pidx_header(view)["node_count"]:
                continue
            entries = resources.iter_fsts_entries(view)
        finally:
            del view
        if entries:
            yield name, item, entries


def bank_extents(entries) -> dict[int, int]:
    """Bank base -> bytes from the bank start to the end of its last stream."""
    extents: dict[int, int] = {}
    for offset, _raw, compressed, _record, _resource_id, base in entries:
        extents[base] = max(extents.get(base, 0), offset + compressed - base)
    return extents


def directory_entries(header: bytes, banks: dict[int, int]):
    """Positions of ``(base, size, count)`` triples in a PIDX header."""
    found = {}
    for position in range(0, len(header) - 12, 4):
        base, size, count = struct.unpack_from("<3I", header, position)
        if base in banks and count == banks[base] and size:
            found[base] = (position, size)
    return found


def idx_entries(idx: bytes, triples: dict[int, tuple[int, int]]):
    """IDX.DAT positions of the same triples, restricted to one file id."""
    hits = []
    wanted = {(base, size, count) for base, (size, count) in triples.items()}
    for position in range(4, len(idx) - 12, 4):
        triple = struct.unpack_from("<3I", idx, position)
        if triple in wanted:
            file_id = struct.unpack_from("<I", idx, position - 4)[0]
            hits.append((file_id, triple[0], position))
    if not hits:
        return {}
    file_id = Counter(hit[0] for hit in hits).most_common(1)[0][0]
    return {base: position for hit_id, base, position in hits if hit_id == file_id}


def pristine_layout(original_iso: Path):
    """Container name -> bank directory positions, from the Japanese ISO."""
    layout = {}
    with original_iso.open("rb") as stream, mmap.mmap(
        stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as image:
        files = builder.iso_files(image)
        idx_item = builder.resolve_iso_file(files, "IDX")
        idx_begin = idx_item.extent * builder.SECTOR
        idx = bytes(image[idx_begin : idx_begin + idx_item.size])
        for name, item, entries in fsts_containers(image, files):
            counts = Counter(entry[5] for entry in entries)
            first_bank = min(counts)
            begin = item.extent * builder.SECTOR
            header = bytes(image[begin : begin + first_bank])
            directory = directory_entries(header, counts)
            missing = set(counts) - set(directory)
            if missing:
                raise SystemExit(
                    f"{name}: no PIDX directory entry for banks "
                    + ", ".join(f"{base:#x}" for base in sorted(missing))
                )
            triples = {base: (directory[base][1], counts[base]) for base in directory}
            layout[name] = {
                "directory": directory,
                "counts": dict(counts),
                "idx": idx_entries(idx, triples),
            }
    return layout


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    layout = pristine_layout(args.original_iso)
    report = {"schema": "fsts-bank-sizes/v1", "iso": str(args.iso), "containers": {}}
    problems = []
    mode = "rb" if args.verify_only else "r+b"
    access = mmap.ACCESS_READ if args.verify_only else mmap.ACCESS_WRITE
    with args.iso.open(mode) as stream, mmap.mmap(stream.fileno(), 0, access=access) as image:
        files = builder.iso_files(image)
        idx_item = builder.resolve_iso_file(files, "IDX")
        idx_begin = idx_item.extent * builder.SECTOR
        for name, item, entries in fsts_containers(image, files):
            pristine = layout.get(name)
            if pristine is None:
                problems.append(f"{name}: not an FSTS container in the original ISO")
                continue
            begin = item.extent * builder.SECTOR
            extents = bank_extents(entries)
            if set(extents) != set(pristine["counts"]):
                problems.append(f"{name}: FSTS bank bases moved")
                continue
            bases = sorted(extents)
            grown = []
            for index, base in enumerate(bases):
                position, original_size = pristine["directory"][base]
                boundary = (bases[index + 1] if index + 1 < len(bases) else item.size) - base
                needed = max(original_size, align_up(extents[base]))
                if extents[base] > boundary:
                    problems.append(
                        f"{name}: bank {base:#x} data ends at {extents[base]:#x}, "
                        f"past the next bank at {boundary:#x}"
                    )
                    continue
                needed = min(needed, boundary)
                places = [begin + position]
                if base in pristine["idx"]:
                    places.append(idx_begin + pristine["idx"][base])
                for place in places:
                    current_base, current = struct.unpack_from("<2I", image, place)
                    if current_base != base:
                        problems.append(f"{name}: directory entry for {base:#x} moved")
                        continue
                    if args.verify_only:
                        if current < extents[base]:
                            problems.append(
                                f"{name}: bank {base:#x} size {current:#x} "
                                f"< data end {extents[base]:#x}"
                            )
                    elif current != needed:
                        struct.pack_into("<I", image, place + 4, needed)
                if needed != original_size:
                    grown.append(
                        {"bank": f"{base:#x}", "size": f"{original_size:#x}->{needed:#x}",
                         "idx_mirror": base in pristine["idx"]}
                    )
            report["containers"][name] = {"banks": len(bases), "grown": grown}
            print(f"{name}: {len(bases)} banks, {len(grown)} grown", flush=True)
        if not args.verify_only:
            image.flush()

    report["problems"] = problems
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if problems:
        raise SystemExit("\n".join(problems))


if __name__ == "__main__":
    main()
