#!/usr/bin/env python3
"""Synchronize Eternal Lovers ADV FSTS compressed-size metadata after runtime image replacement."""
from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import struct
from pathlib import Path

import galaxy_angel_build as builder
import ikusa_lz
import moonlit_lovers_resources as resources


def load_runtime_targets(report_paths: list[Path], adv_begin: int) -> dict[str, dict]:
    """Load translated ADV image identities from image-patch reports.

    The runtime-copy offsets in those reports describe the layout at image-patch
    time.  Eternal Lovers later repacks ADV during the remaining-text pass, so
    those byte offsets are intentionally treated as diagnostics only.  The
    translated TEX raw SHA-256 is the stable identity used after that repack.
    """
    targets: dict[str, dict] = {}
    for report_path in report_paths:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        for entry in payload.get("entries", []):
            runtime_hits = [
                int(runtime["iso_offset"])
                for runtime in entry.get("runtime_copies", [])
                if str(runtime.get("container", "")).upper() == "ADV"
            ]
            if not runtime_hits:
                continue
            raw_sha256 = str(entry["raw_sha256"])
            current = {
                "name": entry["name"],
                "raw_sha256": raw_sha256,
                "expected_raw_size": int(entry["new_raw_size"]),
                "expected_compressed_size": int(entry["new_compressed_size"]),
                "reported_runtime_offsets": [offset - adv_begin for offset in runtime_hits],
                "reported_runtime_count": len(runtime_hits),
            }
            prior = targets.get(raw_sha256)
            if prior is not None:
                if (
                    prior["expected_raw_size"] != current["expected_raw_size"]
                    or prior["expected_compressed_size"] != current["expected_compressed_size"]
                ):
                    raise SystemExit(
                        f"conflicting translated image identity {raw_sha256}: "
                        f"{prior['name']} vs {current['name']}"
                    )
                prior["reported_runtime_offsets"].extend(current["reported_runtime_offsets"])
                prior["reported_runtime_count"] += current["reported_runtime_count"]
                continue
            targets[raw_sha256] = current
    return targets


def sync(iso_path: Path, report_paths: list[Path], report_path: Path | None = None) -> dict:
    with iso_path.open("r+b") as stream, mmap.mmap(stream.fileno(), 0) as image:
        files = builder.iso_files(image)
        item = builder.resolve_iso_file(files, "ADV")
        adv_begin = item.extent * builder.SECTOR
        container = bytearray(image[adv_begin : adv_begin + item.size])
        before_container = bytes(container)
        targets = load_runtime_targets(report_paths, adv_begin)
        fsts_resources = resources.fsts_resources(container, "ADV")

        matched = 0
        changed_records = 0
        already_synced_records = 0
        entries: list[dict] = []
        allowed_absolute_fields: set[int] = set()
        matched_hashes: dict[str, int] = {key: 0 for key in targets}

        # Locate translated runtime images by their decompressed TEX bytes.  This
        # survives the ADV repack, unlike the runtime offsets recorded earlier.
        for resource in fsts_resources:
            candidate_targets = [
                target for target in targets.values()
                if target["expected_raw_size"] == resource.raw_size
            ]
            if not candidate_targets or resource.codec != "ikusa_lz":
                continue
            raw, used = ikusa_lz.decompress(container, resource.offset)
            raw_sha256 = hashlib.sha256(raw).hexdigest()
            target = targets.get(raw_sha256)
            if target is None:
                continue
            expected = target["expected_compressed_size"]
            if used != expected:
                raise SystemExit(
                    f"runtime stream size disagrees with image report at {resource.offset:#x}: "
                    f"{used}!={expected} ({target['name']})"
                )
            if len(raw) != resource.raw_size:
                raise SystemExit(
                    f"runtime raw size disagrees with FSTS at {resource.offset:#x}: "
                    f"{len(raw)}!={resource.raw_size}"
                )
            matched += 1
            matched_hashes[raw_sha256] += 1
            old_size = resource.compressed_size
            for record in resource.record_positions:
                field = record + 12
                allowed_absolute_fields.update(
                    range(adv_begin + field, adv_begin + field + 4)
                )
                current = struct.unpack_from("<I", container, field)[0]
                if current == used:
                    already_synced_records += 1
                    continue
                if current != old_size:
                    raise SystemExit(
                        f"unexpected FSTS size before sync at {resource.offset:#x}: "
                        f"record={current} grouped={old_size}"
                    )
                struct.pack_into("<I", container, field, used)
                changed_records += 1
            entries.append(
                {
                    "name": target["name"],
                    "offset": resource.offset,
                    "raw_size": resource.raw_size,
                    "old_compressed_size": old_size,
                    "new_compressed_size": used,
                    "record_positions": resource.record_positions,
                    "reported_runtime_offsets": target["reported_runtime_offsets"],
                }
            )

        unmatched_targets = [
            target for raw_hash, target in targets.items()
            if matched_hashes[raw_hash] == 0
        ]
        non_fsts_runtime_copies = sum(
            target["reported_runtime_count"] for target in unmatched_targets
        )

        image[adv_begin : adv_begin + item.size] = container
        image.flush()

    changed_positions = [
        adv_begin + index
        for index, (left, right) in enumerate(zip(before_container, container))
        if left != right
    ]
    unexpected = [index for index in changed_positions if index not in allowed_absolute_fields]
    if unexpected:
        raise SystemExit(
            f"ADV FSTS sync changed bytes outside compressed-size fields: {unexpected[:16]}"
        )

    with iso_path.open("rb") as stream, mmap.mmap(
        stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as image:
        files = builder.iso_files(image)
        item = builder.resolve_iso_file(files, "ADV")
        adv_begin = item.extent * builder.SECTOR
        container = bytes(image[adv_begin : adv_begin + item.size])
        final_resources = resources.fsts_resources(container, "ADV")
        strict_verified = 0
        for resource in final_resources:
            resources.decompress_resource(
                container,
                resource.offset,
                resource.raw_size,
                resource.compressed_size,
            )
            strict_verified += 1

    report = {
        "schema": "eternal-lovers-adv-image-fsts-sync/v1",
        "iso": str(iso_path),
        "image_reports": [str(path) for path in report_paths],
        "runtime_unique_images": len(targets),
        "matched_fsts_resources": matched,
        "unmatched_translated_images": len(unmatched_targets),
        "non_fsts_runtime_copies": non_fsts_runtime_copies,
        "changed_records": changed_records,
        "already_synced_records": already_synced_records,
        "changed_bytes": len(changed_positions),
        "strict_adv_resources_verified": strict_verified,
        "entries": entries,
    }
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(
        f"ADV runtime image FSTS sync: images={len(targets)} fsts={matched} "
        f"records_changed={changed_records} strict_verified={strict_verified}"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--image-report", type=Path, action="append", required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    sync(args.iso, args.image_report, args.report)


if __name__ == "__main__":
    main()
