#!/usr/bin/env python3
"""Carry the Korean SCENARIO and adv_string.tbl content into ADV.DAT's copies.

ADV.DAT is the ADV frame's file cache: it holds byte-identical runtime copies of
a subset of SCENARIO.DAT's script resources and of dat/gadat000/adv_string.tbl.
The scenario builder writes SCENARIO.DAT and the remaining-text pass writes
GADAT000, so without this pass the game keeps reading Japanese out of ADV for
exactly those resources - which is why the mission briefing panels and their
prompts stayed Japanese on a disc whose dialogue is Korean.

Mapping is derived from the pristine Japanese ISO by SHA-256 of the decompressed
resource, then carried into the working ISO by FSTS record position, so it does
not depend on anything the earlier passes did to offsets.

Moonlit Lovers solves the same problem with
``moonlit_lovers_patch_adv_scenario_copies.py``, but that tool requires every
translated stream to fit its existing slot.  Eternal Lovers' ADV banks are packed
tighter than that, so when a stream no longer fits, this repacks the whole bank
with the shared FSTS packer instead of giving up - the bank keeps its start,
boundary and size, and only the data inside it moves.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import struct
from pathlib import Path

import eternal_lovers_patch_remaining as engine
import galaxy_angel_build as builder
import ikusa_lz
import moonlit_lovers_resources as resources


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def container_bytes(image, files, stem: str):
    item = builder.resolve_iso_file(files, stem)
    begin = item.extent * builder.SECTOR
    return item, begin, bytes(image[begin : begin + item.size])


def replacement_sources(
    original_image, files, source_scenario: Path, built_scenario: Path,
    candidates: Path | None, encoding_map: Path | None,
) -> dict[str, bytes]:
    """Pristine raw SHA-256 -> the Korean raw that must replace it."""
    wanted: dict[str, bytes] = {}
    for path in sorted(source_scenario.glob("SCENARIO_DAT_*.txt")):
        built = built_scenario / path.name
        if not built.is_file():
            continue
        pristine = path.read_bytes()
        korean = built.read_bytes()
        if korean != pristine:
            wanted[sha256(pristine)] = korean

    if candidates is None:
        return wanted

    import galaxy_angel_translation as translation

    payload = json.loads(candidates.read_text(encoding="utf-8"))
    custom_map = translation.load_custom_map(encoding_map)
    if custom_map is None:
        raise SystemExit("custom font encoding map is required with --candidates")
    # Group the non-ISB units by the resource they belong to, keyed by the
    # pristine raw hash so the ADV copy is found by content, not by offset.
    by_hash: dict[str, list[dict]] = {}
    for candidate in payload["candidates"]:
        if not candidate.get("use_translation"):
            continue
        for occurrence in candidate["occurrences"]:
            if occurrence["container"] != "ADV":
                continue
            by_hash.setdefault(occurrence["raw_sha256"], []).append(
                {
                    "id": candidate["id"],
                    "original": candidate["original"],
                    "translation": candidate["translation"],
                    "occurrence": occurrence,
                }
            )
    item, _begin, data = container_bytes(original_image, files, "ADV")
    for resource in engine.merged_resources(data, "ADV").values():
        try:
            raw = resources.decompress_resource(
                data, resource.offset, resource.raw_size, resource.compressed_size
            )
        except Exception:
            continue
        digest = sha256(raw)
        units = by_hash.get(digest)
        if not units or digest in wanted:
            continue
        rebuilt, _count = engine.rebuild_resource(raw, units, custom_map)
        if rebuilt != raw:
            wanted[digest] = rebuilt
    return wanted


def main() -> None:
    project = Path("work/galaxy_angel_eternal_lovers")
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--source-scenario", type=Path, default=project / "source/scenario")
    parser.add_argument("--built-scenario", type=Path, default=project / "build/isb_scenario")
    parser.add_argument(
        "--candidates",
        type=Path,
        default=project / "assets/translation/remaining/remaining_candidates.json",
        help="remaining-text candidates, for the ADV copies of adv_string.tbl etc.",
    )
    parser.add_argument("--encoding-map", type=Path, default=project / "build/font_map.json")
    parser.add_argument("--report", type=Path, default=project / "build/adv_runtime_report.json")
    args = parser.parse_args()

    with args.original_iso.open("rb") as stream, mmap.mmap(
        stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as original_image:
        original_files = builder.iso_files(original_image)
        wanted = replacement_sources(
            original_image, original_files, args.source_scenario, args.built_scenario,
            args.candidates, args.encoding_map,
        )
        _item, _begin, pristine_adv = container_bytes(original_image, original_files, "ADV")

    # Which FSTS record holds which pristine resource; record positions survive
    # every earlier pass, offsets do not.
    korean_by_record: dict[int, bytes] = {}
    for resource in engine.merged_resources(pristine_adv, "ADV").values():
        try:
            raw = resources.decompress_resource(
                pristine_adv, resource.offset, resource.raw_size, resource.compressed_size
            )
        except Exception:
            continue
        korean = wanted.get(sha256(raw))
        if korean is None:
            continue
        for record in resource.record_positions:
            korean_by_record[record] = korean
    if not korean_by_record:
        raise SystemExit("no ADV runtime copies matched the translated resources")

    with args.iso.open("r+b") as stream, mmap.mmap(stream.fileno(), 0) as image:
        files = builder.iso_files(image)
        item, begin, current = container_bytes(image, files, "ADV")
        resource_map = engine.merged_resources(current, "ADV")
        replacements: dict[int, tuple[bytes, int]] = {}
        already = 0
        for resource in resource_map.values():
            korean = next(
                (korean_by_record[record] for record in resource.record_positions
                 if record in korean_by_record),
                None,
            )
            if korean is None:
                continue
            raw = resources.decompress_resource(
                current, resource.offset, resource.raw_size, resource.compressed_size
            )
            if raw == korean:
                already += 1
                continue
            encoded = ikusa_lz.compress_optimal(korean)
            if resources.decompress_resource(encoded, 0, len(korean), len(encoded)) != korean:
                raise SystemExit(f"ADV compressor round trip failed at {resource.offset:#x}")
            replacements[resource.offset] = (encoded, len(korean))

        rebuilt = bytearray(current)
        repacked = False
        fits = True
        offsets = sorted(resource_map)
        for offset, (encoded, raw_size) in replacements.items():
            resource = resource_map[offset]
            bank = resource.fsts_bases[0] if resource.fsts_bases else None
            following = [o for o in offsets if o > offset
                         and (bank is None or resource_map[o].fsts_bases[:1] == [bank])]
            end = following[0] if following else len(current)
            if len(encoded) > end - offset:
                fits = False
                break
        if fits:
            for offset, (encoded, raw_size) in replacements.items():
                resource = resource_map[offset]
                bank = resource.fsts_bases[0] if resource.fsts_bases else None
                following = [o for o in offsets if o > offset
                             and (bank is None or resource_map[o].fsts_bases[:1] == [bank])]
                end = following[0] if following else len(current)
                rebuilt[offset:end] = bytes(end - offset)
                rebuilt[offset:offset + len(encoded)] = encoded
                for record, base in zip(resource.record_positions, resource.fsts_bases):
                    struct.pack_into(
                        "<III", rebuilt, record + 4, offset - base, raw_size, len(encoded)
                    )
        elif replacements:
            rebuilt, _legacy, relocated = engine.repack_container(
                current, resource_map, replacements
            )
            repacked = True
            print(f"repacked ADV FSTS banks; relocated {relocated} resources", flush=True)
        if len(rebuilt) != item.size:
            raise SystemExit(
                f"ADV size changed: {len(rebuilt)} != {item.size}; the banks must "
                "keep the original allocation"
            )
        image[begin : begin + item.size] = bytes(rebuilt)
        image.flush()

    # Read the finished ISO back and prove every copy now decodes to the Korean
    # bytes the other passes wrote into SCENARIO/GADAT000.
    verified = 0
    with args.iso.open("rb") as stream, mmap.mmap(
        stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as image:
        files = builder.iso_files(image)
        _item, _begin, final = container_bytes(image, files, "ADV")
        for resource in engine.merged_resources(final, "ADV").values():
            korean = next(
                (korean_by_record[record] for record in resource.record_positions
                 if record in korean_by_record),
                None,
            )
            if korean is None:
                continue
            raw = resources.decompress_resource(
                final, resource.offset, resource.raw_size, resource.compressed_size
            )
            if raw != korean:
                raise SystemExit(f"ADV runtime copy mismatch at {resource.offset:#x}")
            verified += 1

    report = {
        "schema": "eternal-lovers-adv-runtime/v1",
        "iso": str(args.iso),
        "runtime_copies": verified,
        "newly_patched": len(replacements),
        "already_patched": already,
        "repacked_banks": repacked,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"ADV runtime copies: {len(replacements)} patched, {already} already, "
        f"{verified} verified; repacked={repacked}"
    )


if __name__ == "__main__":
    main()
