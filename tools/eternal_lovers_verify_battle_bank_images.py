#!/usr/bin/env python3
"""Verify Eternal Lovers FSTS battle-image copies against immutable inputs.

The normal SLG image patch report covers the PIDX primary texture, but SLGRES and
SLGSTAGE use FSTS banks and are patched by ``eternal_lovers_patch_battle_bank_images``.
This verifier independently rebuilds the expected TEX/codec result for every mirrored
bank entry from the Japanese source ISO plus the current translation inputs, then
compares the complete fixed slot in the final ISO.  This makes the final readback check
independent of the patch-stage counters and catches any later stage that overwrites a
battle-bank image or its size records.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import struct
from pathlib import Path

from PIL import Image

import eternal_lovers_patch_battle_bank_images as bank_patcher
import galaxy_angel_build as builder
import moonlit_lovers_resources as resources
from eternal_lovers_patch_remaining import merged_resources
from galaxy_angel_gadat032 import decode_tex


ORIGINAL_SHA256 = "31cb2a0b6a219323ea8fc451050a75f06fc0947fb0ff33b182835adf7b6da25d"
DEFAULT_CONTAINERS = ("SLGRES", "SLGSTAGE")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_container(image: mmap.mmap, files: dict, stem: str) -> bytes:
    item = builder.resolve_iso_file(files, stem)
    begin = item.extent * builder.SECTOR
    return bytes(image[begin : begin + item.size])


def expected_raw_and_stream(
    project: Path,
    entry: dict,
    original_raw: bytes,
    capacity: int,
    codec: str,
) -> tuple[bytes, bytes, bool]:
    images_root = project / "assets/image_extraction/japanese_images" / entry["container"]
    translated_path = images_root / "mirror_png" / entry["translated_png"]
    with Image.open(translated_path) as opened:
        replacement = opened.convert("RGBA")

    if bank_patcher.pixel_hash(decode_tex(original_raw)) == bank_patcher.pixel_hash(replacement):
        return original_raw, resources.encode_stored(original_raw) if codec == "stored_xor" else b"", False

    try:
        rebuilt, compressed, _colours = bank_patcher.compress_to_fit(
            original_raw, replacement, capacity, codec
        )
        return rebuilt, compressed, False
    except ValueError:
        # Mirrors take the same refit path as the patcher, so the two agree.
        rebuilt, compressed = bank_patcher.refit(
            project, entry, original_raw, capacity, codec
        )
        if rebuilt is None or compressed is None:
            raise SystemExit(
                f"cannot reproduce bank image within source slot: "
                f"{entry['container']}:{entry['png']} capacity={capacity}"
            )
        return rebuilt, compressed, True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument(
        "--project",
        type=Path,
        default=Path("work/galaxy_angel_eternal_lovers"),
    )
    parser.add_argument("--container", action="append", default=None)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    original_digest = sha256(args.original_iso)
    if original_digest != ORIGINAL_SHA256:
        raise SystemExit(
            f"original ISO SHA-256 mismatch: {original_digest} != {ORIGINAL_SHA256}"
        )

    containers = args.container or list(DEFAULT_CONTAINERS)
    result = {
        "schema": "eternal-lovers-battle-bank-images-verify/v1",
        "original_iso": str(args.original_iso.resolve()),
        "original_sha256": original_digest,
        "iso": str(args.iso.resolve()),
        "iso_sha256": sha256(args.iso),
        "containers": {},
    }

    with args.original_iso.open("rb") as original_stream, mmap.mmap(
        original_stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as original_image, args.iso.open("rb") as final_stream, mmap.mmap(
        final_stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as final_image:
        original_files = builder.iso_files(original_image)
        final_files = builder.iso_files(final_image)

        for stem in containers:
            images_root = args.project / "assets/image_extraction/japanese_images" / stem
            manifest_path = images_root / "mirror_manifest.json"
            if not manifest_path.is_file():
                raise SystemExit(f"missing battle-bank manifest: {manifest_path}")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            original_container = load_container(original_image, original_files, stem)
            final_container = load_container(final_image, final_files, stem)
            if len(original_container) != len(final_container):
                raise SystemExit(
                    f"container size changed: {stem} {len(original_container)} != {len(final_container)}"
                )

            original_map = merged_resources(original_container, stem)
            final_map = merged_resources(final_container, stem)
            boundaries = sorted(original_map)
            verified = 0
            refitted = 0

            for source_entry in manifest:
                entry = dict(source_entry)
                entry["container"] = stem
                offset = int(entry["resource_offset"])
                original_resource = original_map.get(offset)
                final_resource = final_map.get(offset)
                if original_resource is None or final_resource is None:
                    raise SystemExit(f"missing FSTS resource: {stem}:{offset:#x}")

                index = boundaries.index(offset)
                end = boundaries[index + 1] if index + 1 < len(boundaries) else len(original_container)
                capacity = end - offset
                original_raw = resources.decompress_resource(
                    original_container,
                    offset,
                    original_resource.raw_size,
                    original_resource.compressed_size,
                )
                expected_raw, expected_stream, was_refitted = expected_raw_and_stream(
                    args.project,
                    entry,
                    original_raw,
                    capacity,
                    original_resource.codec,
                )
                if was_refitted:
                    refitted += 1

                final_raw = resources.decompress_resource(
                    final_container,
                    offset,
                    final_resource.raw_size,
                    final_resource.compressed_size,
                )
                if final_raw != expected_raw:
                    raise SystemExit(
                        f"battle-bank raw readback mismatch: {stem}:{entry['png']} {offset:#x}"
                    )
                if final_resource.raw_size != len(expected_raw):
                    raise SystemExit(
                        f"battle-bank raw-size record mismatch: {stem}:{entry['png']}"
                    )

                # Ikusa-LZ is not a canonical byte representation: a different valid stream
                # may decode to the same expected TEX.  Therefore verify the consumer-facing
                # raw bytes and slot invariants, not source-byte reproduction of compression.
                if final_resource.codec != original_resource.codec:
                    raise SystemExit(
                        f"battle-bank codec changed: {stem}:{entry['png']} "
                        f"{original_resource.codec}->{final_resource.codec}"
                    )
                if final_resource.compressed_size > capacity:
                    raise SystemExit(
                        f"battle-bank stream exceeds source slot: {stem}:{entry['png']} "
                        f"{final_resource.compressed_size}>{capacity}"
                    )
                tail = final_container[
                    offset + final_resource.compressed_size : end
                ]
                if any(tail):
                    raise SystemExit(
                        f"battle-bank protected padding changed: {stem}:{entry['png']} {offset:#x}"
                    )

                # Every duplicate FSTS record for this physical stream must expose the
                # same final sizes. ``merged_resources`` already rejects conflicting
                # duplicates; explicitly confirm the table bytes as final-write evidence.
                for record in final_resource.record_positions:
                    raw_size, compressed_size = struct.unpack_from("<II", final_container, record + 8)
                    if (raw_size, compressed_size) != (
                        final_resource.raw_size,
                        final_resource.compressed_size,
                    ):
                        raise SystemExit(
                            f"battle-bank FSTS record mismatch: {stem}:{entry['png']} record={record:#x}"
                        )
                verified += 1

            result["containers"][stem] = {
                "targets": len(manifest),
                "verified": verified,
                "refitted": refitted,
            }

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("BATTLE BANK IMAGE VERIFY OK", flush=True)


if __name__ == "__main__":
    main()
