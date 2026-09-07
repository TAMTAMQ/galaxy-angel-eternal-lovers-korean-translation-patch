#!/usr/bin/env python3
"""Patch GADAT032 image runtime copies cached as raw LZ streams in GAEL.DAT.

Eternal Lovers keeps a second, unindexed cache of many system UI textures in
GAEL.DAT.  The generic image patcher originally scanned ADV only, so the
GADAT032 primary became Korean while the in-game SELECT/pause menu continued to
load the Japanese GAEL copy.

This pass uses the immutable Japanese ISO to identify each original compressed
GADAT032 stream, takes the already-built Korean stream from the target ISO's
GADAT032 primary, and replaces byte-identical GAEL copies in their fixed slots.
Only the existing stream plus its following zero alignment gap may be written;
all later offsets stay unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mmap
from pathlib import Path

import galaxy_angel_build as builder
import ikusa_lz


MAX_ALIGNMENT_GAP = 0x800


def scan_lz_streams(data: bytes) -> dict[str, list[tuple[int, int, int]]]:
    """Index valid GAEL Ikusa-LZ streams by decompressed SHA-256.

    GAEL may store the same TEX with different compressed bytes than GADAT032,
    so decompressed identity is required for exhaustive runtime-copy matching.
    """
    mapping: dict[str, list[tuple[int, int, int]]] = {}
    cursor = 0
    while True:
        hit = data.find(b" 3;1", cursor)
        if hit < 0:
            return mapping
        cursor = hit + 1
        try:
            raw, used = ikusa_lz.decompress(data, hit)
        except Exception:
            continue
        digest = hashlib.sha256(raw).hexdigest()
        mapping.setdefault(digest, []).append((hit, used, len(raw)))


def slot_capacity(data: bytes | bytearray | memoryview, hit: int, old_size: int) -> int:
    cursor = hit + old_size
    limit = min(len(data), cursor + MAX_ALIGNMENT_GAP)
    while cursor < limit and data[cursor] == 0:
        cursor += 1
    return cursor - hit


def validate_stream(blob: bytes, expected_raw_size: int, expected_used: int, label: str) -> bytes:
    raw, used = ikusa_lz.decompress(blob)
    if len(raw) != expected_raw_size or used != expected_used:
        raise SystemExit(
            f"{label}: LZ mismatch raw={len(raw)}/{expected_raw_size} "
            f"used={used}/{expected_used}"
        )
    return raw


def patch(
    original_iso: Path,
    iso_path: Path,
    image_report: Path,
    report_path: Path | None = None,
) -> dict:
    payload = json.loads(image_report.read_text(encoding="utf-8"))
    if str(payload.get("primary_container", "")).upper() != "GADAT032":
        raise SystemExit("image report must be for GADAT032")

    with original_iso.open("rb") as source_stream, mmap.mmap(
        source_stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as source_image:
        source_files = builder.iso_files(source_image)
        source_gadat = builder.resolve_iso_file(source_files, "GADAT032")
        source_gael = builder.resolve_iso_file(source_files, "GAEL")
        source_gadat_begin = source_gadat.extent * builder.SECTOR
        source_gael_begin = source_gael.extent * builder.SECTOR
        source_gadat_data = bytes(
            source_image[source_gadat_begin : source_gadat_begin + source_gadat.size]
        )
        source_gael_data = bytes(
            source_image[source_gael_begin : source_gael_begin + source_gael.size]
        )
    source_gael_streams = scan_lz_streams(source_gael_data)

    entries: list[dict] = []
    patched_slots = 0
    already_patched_slots = 0
    changed_bytes = 0

    with iso_path.open("r+b") as target_stream, mmap.mmap(target_stream.fileno(), 0) as image:
        target_files = builder.iso_files(image)
        target_gadat = builder.resolve_iso_file(target_files, "GADAT032")
        target_gael = builder.resolve_iso_file(target_files, "GAEL")
        if target_gadat.size != source_gadat.size or target_gael.size != source_gael.size:
            raise SystemExit("GADAT032/GAEL logical sizes differ from the Japanese ISO")
        target_gadat_begin = target_gadat.extent * builder.SECTOR
        target_gael_begin = target_gael.extent * builder.SECTOR

        for item in payload.get("entries", []):
            name = str(item["name"])
            primary_offset = int(item["primary_offset"])
            old_size = int(item["old_compressed_size"])
            new_size = int(item["new_compressed_size"])
            old_raw_size = int(item["old_raw_size"])
            new_raw_size = int(item["new_raw_size"])

            old_blob = source_gadat_data[primary_offset : primary_offset + old_size]
            if len(old_blob) != old_size:
                raise SystemExit(f"{name}: Japanese primary stream is truncated")
            old_raw = validate_stream(old_blob, old_raw_size, old_size, f"{name} Japanese primary")
            old_raw_sha = hashlib.sha256(old_raw).hexdigest()

            new_blob = bytes(
                image[
                    target_gadat_begin + primary_offset :
                    target_gadat_begin + primary_offset + new_size
                ]
            )
            new_raw = validate_stream(new_blob, new_raw_size, new_size, f"{name} Korean primary")
            expected_raw_sha = str(item.get("raw_sha256", ""))
            if expected_raw_sha and hashlib.sha256(new_raw).hexdigest() != expected_raw_sha:
                raise SystemExit(f"{name}: Korean primary raw SHA-256 differs from image report")

            hits = source_gael_streams.get(old_raw_sha, [])
            if not hits:
                continue

            optimal_blob: bytes | None = None
            hit_reports: list[dict] = []
            for relative, source_used, source_raw_size in hits:
                if source_raw_size != old_raw_size:
                    raise SystemExit(
                        f"{name}: GAEL raw-size collision at {relative:#x}: "
                        f"{source_raw_size}!={old_raw_size}"
                    )
                capacity = slot_capacity(source_gael_data, relative, source_used)
                gael_blob = new_blob
                compression = "primary"
                if len(gael_blob) > capacity:
                    if optimal_blob is None:
                        optimal_blob = ikusa_lz.compress_optimal(new_raw)
                    if len(optimal_blob) > capacity:
                        raise SystemExit(
                            f"{name}: Korean stream does not fit GAEL slot at {relative:#x}: "
                            f"primary={len(new_blob)} optimal={len(optimal_blob)} capacity={capacity}"
                        )
                    gael_blob = optimal_blob
                    compression = "optimal"
                absolute = target_gael_begin + relative
                current_slot = bytes(image[absolute : absolute + capacity])
                try:
                    current_raw, current_used = ikusa_lz.decompress(current_slot)
                except Exception as exc:
                    raise SystemExit(
                        f"{name}: target GAEL slot is not a valid LZ stream at {relative:#x}: {exc}"
                    ) from exc
                current_sha = hashlib.sha256(current_raw).hexdigest()
                new_raw_sha = hashlib.sha256(new_raw).hexdigest()
                if current_sha == new_raw_sha:
                    already_patched_slots += 1
                    status = "already_patched"
                    stored_size = current_used
                    compression = "existing"
                else:
                    if current_sha != old_raw_sha:
                        raise SystemExit(
                            f"{name}: target GAEL raw data differs from both Japanese and Korean "
                            f"data at {relative:#x}"
                        )
                    before = current_slot
                    image[absolute : absolute + capacity] = bytes(capacity)
                    image[absolute : absolute + len(gael_blob)] = gael_blob
                    after = bytes(image[absolute : absolute + capacity])
                    changed_bytes += sum(left != right for left, right in zip(before, after))
                    patched_slots += 1
                    status = "patched"
                    stored_size = len(gael_blob)
                hit_reports.append(
                    {
                        "relative_offset": relative,
                        "iso_offset": absolute,
                        "capacity": capacity,
                        "source_compressed_size": source_used,
                        "compressed_size": stored_size,
                        "compression": compression,
                        "status": status,
                    }
                )
            entries.append(
                {
                    "name": name,
                    "old_compressed_size": old_size,
                    "new_compressed_size": new_size,
                    "gael_copies": hit_reports,
                }
            )
        image.flush()

        # Read back every located GAEL stream and prove it decodes to the same
        # translated TEX as the GADAT032 primary.
        verified_slots = 0
        for entry in entries:
            item = next(x for x in payload["entries"] if x["name"] == entry["name"])
            primary_offset = int(item["primary_offset"])
            new_size = int(item["new_compressed_size"])
            new_raw_size = int(item["new_raw_size"])
            primary_blob = bytes(
                image[
                    target_gadat_begin + primary_offset :
                    target_gadat_begin + primary_offset + new_size
                ]
            )
            primary_raw = validate_stream(
                primary_blob, new_raw_size, new_size, f"{entry['name']} primary readback"
            )
            for copy in entry["gael_copies"]:
                absolute = int(copy["iso_offset"])
                gael_size = int(copy["compressed_size"])
                gael_blob = bytes(image[absolute : absolute + gael_size])
                gael_raw = validate_stream(
                    gael_blob, new_raw_size, gael_size, f"{entry['name']} GAEL readback"
                )
                if gael_raw != primary_raw:
                    raise SystemExit(f"{entry['name']}: GAEL raw TEX differs from primary")
                verified_slots += 1

    report = {
        "schema": "eternal-lovers-gael-runtime-images/v1",
        "iso": str(iso_path),
        "original_iso": str(original_iso),
        "image_report": str(image_report),
        "translated_images_with_gael_copies": len(entries),
        "patched_slots": patched_slots,
        "already_patched_slots": already_patched_slots,
        "verified_slots": verified_slots,
        "changed_bytes": changed_bytes,
        "entries": entries,
    }
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(
        f"GAEL runtime images: images={len(entries)} patched={patched_slots} "
        f"already={already_patched_slots} verified={verified_slots}"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--image-report", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    patch(args.original_iso, args.iso, args.image_report, args.report)


if __name__ == "__main__":
    main()
