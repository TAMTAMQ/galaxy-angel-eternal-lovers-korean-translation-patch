#!/usr/bin/env python3
"""Patch changed PNGs into a GADAT texture container and runtime copies."""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import shutil
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

import galaxy_angel_build as builder
import ikusa_lz
from galaxy_angel_gadat032 import (
    decode_tex,
    encode_tex,
    named_records,
    pixel_hash,
    tex_info,
)


@dataclass
class RuntimeRegion:
    name: str
    begin: int
    end: int


@dataclass
class PatchPlan:
    png_path: Path
    tex_name: str
    resource_path: str
    offset: int
    record: int
    old_raw_size: int
    old_compressed_size: int
    old_compressed: bytes
    rebuilt: bytes
    compressed: bytes
    primary_capacity: int
    runtime_slots: list[tuple[str, int, int]]
    quantized_colours: int | None
    expected_pixel_hash: str
    source_pixel_hash: str
    tex_kind: str


def image_hash(path: Path) -> str:
    with Image.open(path) as image:
        return pixel_hash(image)


def changed_pngs(images_dir: Path, original_dir: Path) -> list[Path]:
    changed = []
    for path in sorted(images_dir.glob("*.png"), key=lambda item: item.name.lower()):
        original = original_dir / path.name
        if not original.is_file():
            raise SystemExit(f"original PNG is missing: {original}")
        if image_hash(path) != image_hash(original):
            changed.append(path)
    return changed


def central_idx_candidates(
    image: mmap.mmap,
    files: dict[str, builder.IsoFile],
    records: dict[int, tuple[int, int, int]],
) -> dict[int, dict[int, int]]:
    idx_file = builder.resolve_iso_file(files, "IDX")
    begin = idx_file.extent * builder.SECTOR
    tuples = {
        (offset, raw_size, compressed_size): record
        for offset, (record, raw_size, compressed_size) in records.items()
    }
    candidates: dict[int, dict[int, int]] = {}
    for relative in range(0, idx_file.size - 15, 4):
        file_id, offset, raw_size, compressed_size = struct.unpack_from(
            "<IIII", image, begin + relative
        )
        record = tuples.get((offset, raw_size, compressed_size))
        if record is not None:
            candidates.setdefault(file_id, {})[record] = begin + relative
    return candidates


def central_idx_positions(
    image: mmap.mmap,
    files: dict[str, builder.IsoFile],
    records: dict[int, tuple[int, int, int]],
) -> tuple[int, dict[int, int]]:
    candidates = central_idx_candidates(image, files, records)
    if not candidates:
        raise SystemExit("GADAT032 central IDX mirror was not found")
    ranked = sorted(candidates.items(), key=lambda item: len(item[1]), reverse=True)
    file_id, positions = ranked[0]
    if len(ranked) > 1 and len(ranked[1][1]) == len(positions):
        raise SystemExit("GADAT032 central IDX file id is ambiguous")
    if len(positions) != len(records):
        raise SystemExit(
            f"GADAT032 central IDX mirror is incomplete: {len(positions)}/{len(records)}"
        )
    return file_id, positions


def region_hits(image: mmap.mmap, needle: bytes, region: RuntimeRegion) -> list[int]:
    hits = []
    cursor = region.begin
    while True:
        hit = image.find(needle, cursor, region.end)
        if hit < 0:
            return hits
        hits.append(hit)
        cursor = hit + 1


def runtime_capacity(
    image: mmap.mmap,
    hit: int,
    old_size: int,
    region: RuntimeRegion,
) -> int:
    # Runtime streams have no discoverable size tuple. Their decompressor knows
    # the consumed length, and the packer commonly leaves a short zero-alignment
    # gap before the next stream. Only claim those existing zero bytes; never
    # treat unrelated data before a later LZ magic as writable capacity.
    cursor = hit + old_size
    limit = min(region.end, cursor + 0x800)
    while cursor < limit and image[cursor] == 0:
        cursor += 1
    return cursor - hit


def named_fsts_runtime_slots(
    image: mmap.mmap,
    region: RuntimeRegion,
) -> dict[str, list[tuple[str, int, int]]]:
    """Map FSTS path strings to their exact runtime data slots.

    Moonlit Lovers ADV FSTS banks store one NUL-terminated CP932 resource path
    per FSTS record immediately after the record table.  The string order is the
    record order.  Using that structural mapping avoids false ownership when two
    different resources happen to have byte-identical compressed source data.
    """
    data = bytes(image[region.begin : region.end])
    mapping: dict[str, list[tuple[str, int, int]]] = {}
    cursor = 0
    while True:
        base = data.find(b"FSTS", cursor)
        if base < 0:
            break
        cursor = base + 4
        if base + 32 > len(data):
            continue
        _magic, count, header_size, table_size = struct.unpack_from("<4I", data, base)
        if header_size != 32 or table_size != 32 + count * 16:
            continue
        table_end = base + table_size
        if table_end > len(data):
            continue

        rows: list[tuple[int, int, int, int]] = []
        valid = True
        for index in range(count):
            record = base + 32 + index * 16
            _resource_id, relative_offset, raw_size, compressed_size = struct.unpack_from(
                "<4I", data, record
            )
            absolute_relative = base + relative_offset
            if absolute_relative + 8 > len(data):
                valid = False
                break
            rows.append((relative_offset, raw_size, compressed_size, record))
        if not valid or not rows:
            continue

        first_data = base + min(row[0] for row in rows)
        if first_data < table_end:
            continue
        string_cursor = table_end
        names: list[str] = []
        for _index in range(count):
            end = data.find(b"\0", string_cursor, first_data)
            if end < 0:
                names = []
                break
            raw_name = data[string_cursor:end]
            try:
                name = raw_name.decode("cp932")
            except UnicodeDecodeError:
                names = []
                break
            names.append(name.replace("\\", "/"))
            string_cursor = end + 1
        if len(names) != count:
            continue

        for name, (relative_offset, _raw_size, compressed_size, _record) in zip(
            names, rows
        ):
            if not name:
                continue
            hit = region.begin + base + relative_offset
            if not (region.begin <= hit < region.end):
                continue
            capacity = runtime_capacity(image, hit, compressed_size, region)
            mapping.setdefault(name.lower(), []).append((region.name, hit, capacity))
    return mapping


def compression_candidates(
    original: bytes,
    source: Image.Image,
    capacity: int,
    forced_colours: int | None,
    allow_quantization: bool,
) -> tuple[bytes, bytes, int | None]:
    info = tex_info(original)
    colour_attempts: list[int | None] = [forced_colours] if forced_colours else [None]
    if allow_quantization and forced_colours is None:
        # Keep the usual power-of-two palette attempts first, then step down one
        # colour at a time only when a tiny fixed runtime slot still overflows.
        # Pillow's quantizer accepts arbitrary max-colour counts, and trying 7
        # before 6/5/... preserves more source colour detail than jumping from
        # 8 straight to a much smaller palette.
        limits = [256, 128, 64, 32, 16, 8, 7, 6, 5, 4, 3, 2]
        if info.palette_colours:
            limits = [limit for limit in limits if limit <= info.palette_colours]
        colour_attempts.extend(limits)
    seen: set[int | None] = set()
    best_size = None
    for colours in colour_attempts:
        if colours in seen:
            continue
        seen.add(colours)
        rebuilt = encode_tex(original, source, colours)
        compressed = ikusa_lz.compress(rebuilt)
        if len(compressed) <= capacity:
            return rebuilt, compressed, colours
        optimal = ikusa_lz.compress_optimal(rebuilt)
        if best_size is None or len(optimal) < best_size:
            best_size = len(optimal)
        if len(optimal) <= capacity:
            return rebuilt, optimal, colours
    raise SystemExit(
        f"texture does not fit its smallest slot: best {best_size}>{capacity}"
    )


def select_inputs(args: argparse.Namespace) -> list[Path]:
    selected: dict[str, Path] = {}
    for path in args.png or []:
        selected[path.name.lower()] = path
    if args.png_list:
        for raw_line in args.png_list.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            path = Path(line)
            if not path.is_absolute():
                path = (args.png_list.parent / path).resolve()
            if not path.is_file():
                raise SystemExit(f"PNG listed in --png-list is missing: {path}")
            selected[path.name.lower()] = path
    if args.images_dir:
        if not args.original_png_dir:
            raise SystemExit("--images-dir requires --original-png-dir")
        for path in changed_pngs(args.images_dir, args.original_png_dir):
            selected[path.name.lower()] = path
    return [selected[name] for name in sorted(selected)]


def prepare_iso(args: argparse.Namespace) -> Path:
    if args.iso:
        if args.base_iso or args.output_iso:
            raise SystemExit("use either --iso or --base-iso/--output-iso")
        return args.iso
    if not args.base_iso or not args.output_iso:
        raise SystemExit("--base-iso and --output-iso are required together")
    if args.base_iso.resolve() == args.output_iso.resolve():
        raise SystemExit("base and output ISO must be different files")
    shutil.copyfile(args.base_iso, args.output_iso)
    return args.output_iso


def load_runtime_audit(path: Path | None) -> dict[str, list[tuple[str, int, int]]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    mapping: dict[str, list[tuple[str, int, int]]] = {}
    for entry in payload.get("entries", []):
        name = str(entry.get("name", "")).lower()
        if not name:
            continue
        slots = []
        for slot in entry.get("runtime_slots", []):
            slots.append(
                (
                    str(slot["container"]),
                    int(slot["iso_offset"]),
                    int(slot["capacity"]),
                )
            )
        mapping[name] = slots
    return mapping


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", type=Path, help="patch this ISO in place")
    parser.add_argument("--base-iso", type=Path)
    parser.add_argument("--output-iso", type=Path)
    parser.add_argument("--png", type=Path, action="append")
    parser.add_argument(
        "--png-list",
        type=Path,
        help="UTF-8 text file containing one PNG path per line; avoids Windows command-line limits.",
    )
    parser.add_argument("--images-dir", type=Path)
    parser.add_argument("--original-png-dir", type=Path)
    parser.add_argument("--runtime-container", action="append")
    parser.add_argument(
        "--no-runtime-copies",
        action="store_true",
        help="Patch only the primary texture container; do not scan or patch runtime copies.",
    )
    parser.add_argument(
        "--image-manifest",
        type=Path,
        action="append",
        default=None,
        help="JSON image manifest mapping PNG basenames to resource offsets/paths. Repeatable: "
             "a container whose translations were produced by more than one renderer needs "
             "each of their manifests, and a later one wins where they name the same PNG.",
    )
    parser.add_argument(
        "--resource-manifest",
        type=Path,
        help="Full extraction manifest used to resolve offset-only block PNG names.",
    )
    parser.add_argument(
        "--primary-container",
        default="GADAT032",
        help="ISO texture-container stem (default: GADAT032).",
    )
    parser.add_argument("--strict-name-data-container", action="append")
    parser.add_argument(
        "--no-central-idx",
        action="store_true",
        help="container has no entries in the central IDX.DAT (SLGRES/SLGSTAGE); skip mirroring",
    )
    parser.add_argument("--quantize-colours", type=int)
    parser.add_argument("--no-quantization", action="store_true")
    parser.add_argument(
        "--require-exact-pixels",
        action="store_true",
        help="fail unless decoded rebuilt TEX pixels exactly match the input PNG",
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--runtime-audit",
        type=Path,
        help="reuse exact runtime-copy offsets from a prior audit of this ISO",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help="cache per-texture compression plans so long runs can resume",
    )
    args = parser.parse_args()

    png_paths = select_inputs(args)
    iso_path = prepare_iso(args)
    if args.no_runtime_copies and args.runtime_container:
        raise SystemExit("--no-runtime-copies cannot be combined with --runtime-container")
    runtime_names = [] if args.no_runtime_copies else (args.runtime_container or ["SLGRES"])
    strict_runtime_names = set(args.strict_name_data_container or runtime_names)
    report_path = args.report or iso_path.with_suffix(".image-patch-report.json")
    audited_runtime_slots = load_runtime_audit(args.runtime_audit)
    if args.cache_dir:
        args.cache_dir.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "iso": str(iso_path.resolve()),
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "primary_container": args.primary_container.upper(),
        "runtime_containers": runtime_names,
        "strict_name_data_containers": sorted(strict_runtime_names),
        "changed_count": len(png_paths),
        "entries": [],
    }
    if not png_paths:
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print("no changed image PNGs; ISO was not modified")
        return

    with iso_path.open("r+b") as stream, mmap.mmap(stream.fileno(), 0) as image:
        files = builder.iso_files(image)
        primary_name = args.primary_container.upper()
        gadat_item = builder.resolve_iso_file(files, primary_name)
        gadat_begin = gadat_item.extent * builder.SECTOR
        container = bytearray(image[gadat_begin : gadat_begin + gadat_item.size])
        records = builder.records(container)
        image_manifest: dict[str, dict] = {}
        resources_by_offset: dict[int, dict] = {}
        if args.resource_manifest:
            resource_payload = json.loads(args.resource_manifest.read_text(encoding="utf-8"))
            resources_by_offset = {
                int(entry["offset"]): entry
                for entry in resource_payload.get("resources", [])
                if entry.get("source_kind") == "pidx"
            }
        for manifest_file in args.image_manifest or []:
            manifest_payload = json.loads(manifest_file.read_text(encoding="utf-8"))
            manifest_entries = (
                manifest_payload if isinstance(manifest_payload, list)
                else manifest_payload.get(
                    "entries",
                    manifest_payload.get("resources", manifest_payload.get("images", [])),
                )
            )
            for entry in manifest_entries:
                for key in ("png", "translated_png", "output_png", "original_png"):
                    value = entry.get(key)
                    if value:
                        image_manifest[Path(value).name.lower()] = entry
        by_name = {} if image_manifest else named_records(container, records)
        if args.no_central_idx:
            # The per-stage battle banks are not listed in IDX.DAT at all, so there is no
            # mirror to keep in step.  Asserting that here means a container that *is* listed
            # can never be patched with the mirror silently skipped.
            if central_idx_candidates(image, files, records):
                raise SystemExit(
                    f"{args.primary_container} does have a central IDX mirror; "
                    "do not pass --no-central-idx for it"
                )
            file_id, idx_positions = None, {}
        else:
            file_id, idx_positions = central_idx_positions(image, files, records)
        runtime_regions = []
        for name in runtime_names:
            item = builder.resolve_iso_file(files, name)
            begin = item.extent * builder.SECTOR
            runtime_regions.append(RuntimeRegion(name, begin, begin + item.size))
        named_runtime_slots = {
            region.name: named_fsts_runtime_slots(image, region)
            for region in runtime_regions
        }

        plans: list[PatchPlan] = []
        claimed_runtime_hits: dict[int, str] = {}
        total_pngs = len(png_paths)
        print(f"planning {total_pngs} changed image patches...", flush=True)
        for plan_index, png_path in enumerate(png_paths, 1):
            manifest_entry = image_manifest.get(png_path.name.lower())
            tex_name = (
                str(manifest_entry.get("name") or manifest_entry.get("resource_name") or "").lower()
                if manifest_entry else png_path.with_suffix(".tex").name.lower()
            )
            print(
                f"[plan {plan_index}/{total_pngs} "
                f"{plan_index * 100 / total_pngs:5.1f}%] {tex_name}",
                flush=True,
            )
            if manifest_entry:
                if "resource_offset" in manifest_entry:
                    offset = int(manifest_entry["resource_offset"])
                elif png_path.stem.lower().startswith("block_"):
                    offset = int(png_path.stem[6:], 16)
                else:
                    raise SystemExit(f"manifest entry has no resource offset: {png_path.name}")
                if offset not in records:
                    raise SystemExit(f"manifest offset is not a PIDX record: {offset:#x}")
                record, old_raw_size, old_compressed_size = records[offset]
                resource_entry = resources_by_offset.get(offset, {})
                resource_path = str(
                    manifest_entry.get("resource_path") or resource_entry.get("path") or ""
                ).replace("\\", "/")
                manifest_name = manifest_entry.get("name") or manifest_entry.get("resource_name")
                tex_name = str(
                    manifest_name
                    if str(manifest_name or "").lower().endswith(".tex")
                    else resource_entry.get("name") or manifest_name
                ).lower()
                if not resource_path:
                    raise SystemExit(f"resource path is unavailable for offset {offset:#x}")
            else:
                if tex_name not in by_name:
                    raise SystemExit(f"texture not found in {primary_name}: {tex_name}")
                offset, record, old_raw_size, old_compressed_size = by_name[tex_name]
                resource_path = f"dat/{primary_name.lower()}/{tex_name}"
            original, consumed = ikusa_lz.decompress(container, offset)
            if len(original) != old_raw_size or consumed != old_compressed_size:
                raise SystemExit(f"source block mismatch: {tex_name}")
            old_compressed = bytes(container[offset : offset + old_compressed_size])
            next_offsets = [candidate for candidate in records if candidate > offset]
            primary_capacity = (min(next_offsets) if next_offsets else len(container)) - offset
            runtime_slots: list[tuple[str, int, int]] = []
            if args.runtime_audit:
                runtime_slots = list(audited_runtime_slots.get(tex_name, []))
                regions_by_name = {region.name: region for region in runtime_regions}
                for region_name, hit, slot_capacity in runtime_slots:
                    region = regions_by_name.get(region_name)
                    if region is None:
                        raise SystemExit(
                            f"runtime audit references unrequested container: "
                            f"{region_name}:{tex_name}"
                        )
                    if not (region.begin <= hit < region.end):
                        raise SystemExit(
                            f"runtime audit slot is outside {region_name}: "
                            f"{tex_name} at {hit:#x}"
                        )
                    if image[hit : hit + old_compressed_size] != old_compressed:
                        raise SystemExit(
                            f"runtime audit is stale for {region_name}:{tex_name} "
                            f"at {hit:#x}"
                        )
                    if slot_capacity < old_compressed_size:
                        raise SystemExit(
                            f"runtime audit capacity is too small for {region_name}:"
                            f"{tex_name}: {slot_capacity}<{old_compressed_size}"
                        )
                    owner = claimed_runtime_hits.get(hit)
                    if owner is None:
                        claimed_runtime_hits[hit] = tex_name
            else:
                runtime_path = resource_path.encode("cp932")
                normalized_runtime_path = resource_path.replace("\\", "/").lower()
                for region in runtime_regions:
                    exact_slots = named_runtime_slots.get(region.name, {}).get(
                        normalized_runtime_path, []
                    )
                    if exact_slots:
                        for region_name, hit, slot_capacity in exact_slots:
                            if image[hit : hit + old_compressed_size] != old_compressed:
                                raise SystemExit(
                                    f"named FSTS runtime slot data differs from primary source: "
                                    f"{region_name}:{tex_name} at {hit:#x}"
                                )
                            if slot_capacity < old_compressed_size:
                                raise SystemExit(
                                    f"named FSTS runtime capacity is too small: "
                                    f"{region_name}:{tex_name}: "
                                    f"{slot_capacity}<{old_compressed_size}"
                                )
                            owner = claimed_runtime_hits.get(hit)
                            if owner is not None and owner != tex_name:
                                raise SystemExit(
                                    f"named FSTS runtime slot has two owners: {hit:#x}: "
                                    f"{owner} and {tex_name}"
                                )
                            claimed_runtime_hits[hit] = tex_name
                            runtime_slots.append((region_name, hit, slot_capacity))
                        continue

                    runtime_name_refs = len(region_hits(image, runtime_path, region))
                    region_data_hits = region_hits(image, old_compressed, region)
                    if (
                        region.name in strict_runtime_names
                        and runtime_name_refs
                        and not region_data_hits
                    ):
                        raise SystemExit(
                            f"runtime name exists but compressed copy was not found: "
                            f"{region.name}:{tex_name}"
                        )
                    for hit in region_data_hits:
                        owner = claimed_runtime_hits.get(hit)
                        if owner is None:
                            claimed_runtime_hits[hit] = tex_name
                        runtime_slots.append(
                            (
                                region.name,
                                hit,
                                runtime_capacity(
                                    image, hit, old_compressed_size, region
                                ),
                            )
                        )
            capacity = min(
                [primary_capacity, *(slot_capacity for _name, _hit, slot_capacity in runtime_slots)]
            )
            with Image.open(png_path) as opened:
                source = opened.convert("RGBA")
            source_hash = pixel_hash(source)
            cache_key = hashlib.sha256(
                b"\0".join(
                    [
                        old_compressed,
                        f"ikusa-lz-v{ikusa_lz.COMPRESSOR_VERSION}".encode("ascii"),
                        source_hash.encode("ascii"),
                        str(capacity).encode("ascii"),
                        str(args.quantize_colours).encode("ascii"),
                        str(bool(args.no_quantization)).encode("ascii"),
                    ]
                )
            ).hexdigest()
            rebuilt = compressed = None
            quantized_colours = None
            if args.cache_dir:
                cache_meta_path = args.cache_dir / f"{tex_name}.json"
                cache_lz_path = args.cache_dir / f"{tex_name}.lz"
                if cache_meta_path.is_file() and cache_lz_path.is_file():
                    try:
                        cache_meta = json.loads(cache_meta_path.read_text(encoding="utf-8"))
                        if cache_meta.get("key") == cache_key:
                            cached = cache_lz_path.read_bytes()
                            cached_raw, cached_used = ikusa_lz.decompress(cached)
                            if (
                                cached_used == len(cached)
                                and len(cached_raw) == old_raw_size
                                and len(cached) <= capacity
                            ):
                                rebuilt = cached_raw
                                compressed = cached
                                quantized_colours = cache_meta.get("quantized_colours")
                    except (OSError, ValueError, KeyError, json.JSONDecodeError):
                        rebuilt = compressed = None
            if rebuilt is None or compressed is None:
                try:
                    rebuilt, compressed, quantized_colours = compression_candidates(
                        original,
                        source,
                        capacity,
                        args.quantize_colours,
                        not args.no_quantization,
                    )
                except SystemExit as error:
                    raise SystemExit(f"{tex_name}: {error}") from error
                if args.cache_dir:
                    cache_meta_path = args.cache_dir / f"{tex_name}.json"
                    cache_lz_path = args.cache_dir / f"{tex_name}.lz"
                    cache_lz_path.write_bytes(compressed)
                    cache_meta_path.write_text(
                        json.dumps(
                            {
                                "key": cache_key,
                                "quantized_colours": quantized_colours,
                                "compressed_size": len(compressed),
                                "raw_size": len(rebuilt),
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
            if len(rebuilt) != old_raw_size:
                raise SystemExit(
                    f"TEX raw size changed: {tex_name}: {len(rebuilt)}!={old_raw_size}"
                )
            decoded, used = ikusa_lz.decompress(compressed)
            if decoded != rebuilt or used != len(compressed):
                raise SystemExit(f"compression round-trip failed: {tex_name}")
            rebuilt_pixel_hash = pixel_hash(decode_tex(rebuilt))
            if args.require_exact_pixels and rebuilt_pixel_hash != source_hash:
                raise SystemExit(
                    f"exact-pixel preservation failed: {tex_name}: "
                    f"{rebuilt_pixel_hash}!={source_hash}"
                )
            plans.append(
                PatchPlan(
                    png_path,
                    tex_name,
                    resource_path,
                    offset,
                    record,
                    old_raw_size,
                    old_compressed_size,
                    old_compressed,
                    rebuilt,
                    compressed,
                    primary_capacity,
                    runtime_slots,
                    quantized_colours,
                    rebuilt_pixel_hash,
                    source_hash,
                    tex_info(original).kind,
                )
            )

        # A few byte-identical UI assets intentionally share the same SGLRES
        # runtime block.  Permit that only when their rebuilt TEX and compressed
        # bytes are identical; conflicting localized images remain a hard error.
        runtime_plan_owners: dict[int, PatchPlan] = {}
        shared_runtime_hits = 0
        for plan in plans:
            for _region_name, hit, _capacity in plan.runtime_slots:
                owner = runtime_plan_owners.get(hit)
                if owner is None:
                    runtime_plan_owners[hit] = plan
                    continue
                shared_runtime_hits += 1
                if owner.rebuilt != plan.rebuilt or owner.compressed != plan.compressed:
                    raise SystemExit(
                        f"runtime block {hit:#x} has conflicting localized data: "
                        f"{owner.tex_name} and {plan.tex_name}"
                    )
        report["shared_runtime_hits"] = shared_runtime_hits

        # All size and format checks passed. Only now mutate the ISO.
        print(f"writing {len(plans)} image patches to ISO...", flush=True)
        for write_index, plan in enumerate(plans, 1):
            slot_end = plan.offset + plan.primary_capacity
            container[plan.offset:slot_end] = bytes(plan.primary_capacity)
            container[plan.offset : plan.offset + len(plan.compressed)] = plan.compressed
            struct.pack_into(
                "<III",
                container,
                plan.record,
                plan.offset,
                len(plan.rebuilt),
                len(plan.compressed),
            )
            if idx_positions:
                struct.pack_into(
                    "<IIII",
                    image,
                    idx_positions[plan.record],
                    file_id,
                    plan.offset,
                    len(plan.rebuilt),
                    len(plan.compressed),
                )
            runtime_report = []
            for region_name, hit, capacity in plan.runtime_slots:
                image[hit : hit + capacity] = bytes(capacity)
                image[hit : hit + len(plan.compressed)] = plan.compressed
                runtime_report.append(
                    {"container": region_name, "iso_offset": hit, "capacity": capacity}
                )
            entry = {
                "name": plan.tex_name,
                "resource_path": plan.resource_path,
                "png": str(plan.png_path.resolve()),
                "tex_kind": plan.tex_kind,
                "source_pixel_sha256": plan.source_pixel_hash,
                "expected_pixel_sha256": plan.expected_pixel_hash,
                "raw_sha256": hashlib.sha256(plan.rebuilt).hexdigest(),
                "old_raw_size": plan.old_raw_size,
                "new_raw_size": len(plan.rebuilt),
                "old_compressed_size": plan.old_compressed_size,
                "new_compressed_size": len(plan.compressed),
                "primary_offset": plan.offset,
                "primary_capacity": plan.primary_capacity,
                "quantized_colours": plan.quantized_colours,
                "runtime_copies": runtime_report,
            }
            report["entries"].append(entry)
            quality = (
                "exact/native"
                if plan.quantized_colours is None
                else f"quantized {plan.quantized_colours} colours"
            )
            if write_index == 1 or write_index % 25 == 0 or write_index == len(plans):
                print(
                    f"[write {write_index}/{len(plans)} "
                    f"{write_index * 100 / len(plans):5.1f}%] "
                    f"{plan.tex_name}: {plan.tex_kind}, "
                    f"{plan.old_compressed_size}->{len(plan.compressed)}/{plan.primary_capacity}, "
                    f"runtime {len(plan.runtime_slots)}, {quality}",
                    flush=True,
                )

        image[gadat_begin : gadat_begin + gadat_item.size] = container
        image.flush()

        # Immediate byte-level verification before reporting success.
        print(f"verifying {len(plans)} written image patches...", flush=True)
        for verify_index, plan in enumerate(plans, 1):
            decoded, used = ikusa_lz.decompress(container, plan.offset)
            if decoded != plan.rebuilt or used != len(plan.compressed):
                raise SystemExit(f"primary post-write verification failed: {plan.tex_name}")
            for _region_name, hit, _capacity in plan.runtime_slots:
                runtime_raw, runtime_used = ikusa_lz.decompress(image, hit)
                if runtime_raw != plan.rebuilt or runtime_used != len(plan.compressed):
                    raise SystemExit(f"runtime post-write verification failed: {plan.tex_name}")
            if verify_index == 1 or verify_index % 25 == 0 or verify_index == len(plans):
                print(
                    f"[verify {verify_index}/{len(plans)} "
                    f"{verify_index * 100 / len(plans):5.1f}%] {plan.tex_name}",
                    flush=True,
                )

    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"patched {len(plans)} changed images; report: {report_path}")


if __name__ == "__main__":
    main()
