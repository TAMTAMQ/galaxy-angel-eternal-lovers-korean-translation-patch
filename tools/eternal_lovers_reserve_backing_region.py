#!/usr/bin/env python3
"""Free a stretch of disc that the script containers can still address.

Galaxy Angel 1 keeps GADAT001 at its original LBA and pushes only the blocks
that outgrew their slot into a backing copy appended at the end of the image
(see work/galaxy_angel/STATUS.md "v42 GADAT001 고정 베이스 근본 수정").  That
works there because a PIDX offset is a 32-bit *container-relative* byte offset
and the whole 4.16 GB disc is within reach of GADAT001's base.

Eternal Lovers' disc is 4.72 GB and SCENARIO sits at byte 261,992,448, so the
end of the image is 4.40 GB past its base - beyond what a 32-bit offset can
express.  Appending is therefore not an option, and the original disc has zero
free sectors, so there is nowhere in range to put the overflow.

This makes room: one MOVIE2 stream - data the script engine never addresses by
container offset - is moved to the end of the image, and the sectors it used are
handed to the builders as a backing region.  Every script container then keeps
its original extent, exactly as in the 1st game.

The donor must start inside every consumer's 32-bit reach, so the region is
chosen against the containers named by --for.
"""

from __future__ import annotations

import argparse
import json
import mmap
import shutil
from pathlib import Path

import galaxy_angel_build as builder

U32_MAX = 0xFFFFFFFF


def choose_donor(files: dict[str, builder.IsoFile], consumers: list[str],
                 needed: int, prefer: str) -> builder.IsoFile:
    limit = min(
        builder.resolve_iso_file(files, stem).extent * builder.SECTOR + U32_MAX
        for stem in consumers
    )
    candidates = []
    for path, item in files.items():
        begin = item.extent * builder.SECTOR
        size = builder.align(item.size, builder.SECTOR)
        if begin + size > limit or size < needed:
            continue
        candidates.append((prefer not in path, size, path, item))
    if not candidates:
        raise SystemExit(
            f"no file of at least {needed} bytes starts within 32-bit reach "
            f"(byte {limit}) of {consumers}"
        )
    # Smallest sufficient file wins, preferring the streamed-media pool.
    candidates.sort(key=lambda row: (row[0], row[1]))
    return candidates[0][3], candidates[0][2]


def main() -> None:
    project = Path("work/galaxy_angel_eternal_lovers")
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument(
        "--for", dest="consumers", action="append", default=None,
        help="Container stem that will address this region (repeatable).",
    )
    parser.add_argument(
        "--needed", type=int, default=8 * 1024 * 1024,
        help="Minimum bytes the region must provide.",
    )
    parser.add_argument("--prefer", default="MOVIE",
                        help="Substring marking files that are safe to move.")
    parser.add_argument(
        "--region", type=Path, default=project / "build/backing_region.json"
    )
    args = parser.parse_args()
    consumers = args.consumers or ["SCENARIO", "SLG"]

    with args.iso.open("r+b") as stream, mmap.mmap(stream.fileno(), 0) as image:
        files = builder.iso_files(image)
        donor, donor_path = choose_donor(files, consumers, args.needed, args.prefer)
        begin = donor.extent * builder.SECTOR
        span = builder.align(donor.size, builder.SECTOR)
        payload = bytes(image[begin : begin + donor.size])

        new_begin = builder.align(len(image), builder.SECTOR)
        image.resize(new_begin + span)
        image[new_begin : new_begin + donor.size] = payload
        builder.patch_iso_file_record(
            image, donor, new_begin // builder.SECTOR, donor.size
        )
        # Leave the vacated sectors zeroed so a stale read is obvious rather
        # than silently returning the old movie's bytes.
        image[begin : begin + span] = bytes(span)
        builder.patch_volume_size(image)
        image.flush()

    region = {
        "schema": "eternal-lovers-backing-region/v1",
        "iso": str(args.iso),
        "donor": donor_path,
        "donor_moved_to_extent": new_begin // builder.SECTOR,
        "offset": begin,
        "size": span,
        "cursor": begin,
        "consumers": consumers,
    }
    args.region.parent.mkdir(parents=True, exist_ok=True)
    args.region.write_text(
        json.dumps(region, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"moved {donor_path} ({donor.size} bytes) to extent "
        f"{new_begin // builder.SECTOR}; backing region {begin}..{begin + span} "
        f"({span} bytes) -> {args.region}"
    )


if __name__ == "__main__":
    main()
