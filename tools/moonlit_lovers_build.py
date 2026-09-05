#!/usr/bin/env python3
"""Build Galaxy Angel: Moonlit Lovers's ISO from (re-)compressed scenario blocks.

Generalises galaxy_angel_build.py's container-relocation and central IDX.DAT
mirroring logic to Moonlit Lovers's container names (SCENARIO.DAT / SLG.DAT /
SLGSTAGE.DAT instead of 1st game's GADAT001.DAT / GADAT002.DAT). Unlike the
1st game's build script this one does not assume a GADAT001-style fixed
IDS/label offset index exists in the target container -- that structure has
not been located in Moonlit Lovers yet (see STATUS.md "아직 확인하지 못한 것"
#3). If/when it is found, port `rebuild_scenario_index` from
galaxy_angel_build.py the same way this file ported the rest.

Right now every unit in assets/translation/index.json is untranslated, so a
"pass-through" build (--built-scenario pointing at the same directory as
--original-scenario, or omitted) still exercises the full pipeline: every
selected block is decompressed, verified against the source .txt, recompressed
with ikusa_lz, and reinserted -- which is a legitimate round-trip test even
without translated text, because Ikusa LZ compression is not guaranteed to
reproduce the original disc's compressed byte stream (a different compressor
build/heuristic could have produced the original bytes), so the compressed
size can legitimately change and force an IDX.DAT update.

An explicit `--force-recompress` flag makes this recompression happen for
every scenario block even when the decompressed bytes are byte-identical to
the source .txt (the default pass-through case). This is what
moonlit_lovers_verify_structure.py's dummy round-trip test uses to prove the
offset-preservation / IDX-mirroring logic actually runs and is exercised by a
real size change, not skipped because nothing looked different.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import struct
from pathlib import Path

import eternal_lovers_backing as backing
import galaxy_angel_build as builder
import moonlit_lovers_resources as resources

SCENARIO_NAME_RE = re.compile(r"^(?P<container>.+)_DAT_(?P<offset>[0-9a-fA-F]{8})\.txt$")


def align(value: int, alignment: int = builder.SECTOR) -> int:
    return builder.align(value, alignment)


def build_container(
    image: bytearray,
    files: dict[str, builder.IsoFile],
    stem: str,
    original_scenario: Path,
    built_scenario: Path,
    lz,
    cache_dir: Path,
    compressor_fingerprint: bytes,
    force_recompress: bool,
    region: backing.BackingRegion | None,
) -> dict[int, tuple[int, int, int]] | None:
    """Recompress & reinsert every touched (or, with force_recompress, every)
    block of one container. Returns the record->tuple map of everything that
    was actually rewritten, or None if the container was not touched at all.
    """
    iso_file = builder.resolve_iso_file(files, stem)
    begin = iso_file.extent * builder.SECTOR
    container = bytearray(image[begin : begin + iso_file.size])
    recs = resources.pidx_record_map(container, stem)
    if not recs:
        return None

    sources = sorted(original_scenario.glob(f"{stem.upper()}_DAT_*.txt"))
    if not sources:
        return None

    replacement_data: dict[int, tuple[bytes, int]] = {}
    total = len(sources)
    for number, original_path in enumerate(sources, 1):
        match = SCENARIO_NAME_RE.match(original_path.name)
        if not match:
            raise SystemExit(f"unexpected scenario filename: {original_path.name}")
        offset = int(match.group("offset"), 16)
        if offset not in recs:
            raise SystemExit(f"{original_path.name}: offset {offset:#x} not a PIDX leaf")
        original_raw = original_path.read_bytes()
        _record, old_raw_size, old_compressed_size = recs[offset]
        old_raw = resources.decompress_resource(
            container, offset, old_raw_size, old_compressed_size
        )
        built_path = built_scenario / original_path.name
        if built_path.exists():
            # A block we are about to overwrite must still hold the Japanese
            # bytes the translation was built from, or the built file belongs to
            # a different disc revision.
            if old_raw != original_raw:
                raise SystemExit(f"source/container mismatch: {original_path.name}")
            raw = built_path.read_bytes()
        else:
            # Pass-through.  Use whatever the container currently holds rather
            # than the pristine source, so an earlier pass in the same pipeline
            # (eternal_lovers_patch_remaining.py rewrites 18 SCENARIO resources)
            # is not silently reverted.
            raw = old_raw
        if raw == old_raw and not force_recompress:
            continue

        cache_key = hashlib.sha256(compressor_fingerprint + raw).hexdigest()
        cache_path = cache_dir / f"{cache_key}.bin"
        if cache_path.exists():
            compressed = cache_path.read_bytes()
        else:
            compressed = lz.compress(raw)
            cache_path.write_bytes(compressed)
        decoded, consumed = lz.decompress(compressed)
        if decoded != raw or consumed != len(compressed):
            raise SystemExit(f"compressor round-trip failed: {original_path.name}")
        replacement_data[offset] = (compressed, len(raw))
        if number % 40 == 0 or number == total:
            print(f"  {stem}: compressed {number}/{total}", flush=True)

    if not replacement_data:
        return None

    rebuilt = bytearray(container)
    offsets = sorted(recs)
    append_cursor = align(len(rebuilt))
    relocated = 0
    updated_by_record: dict[int, tuple[int, int, int]] = {}
    for old_offset, (compressed, raw_size) in sorted(replacement_data.items()):
        record, _old_raw_size, _old_compressed_size = recs[old_offset]
        next_offsets = [o for o in offsets if o > old_offset]
        slot_end = next_offsets[0] if next_offsets else len(container)
        slot_capacity = slot_end - old_offset

        if len(compressed) > slot_capacity:
            raw, consumed = lz.decompress(compressed)
            if consumed != len(compressed):
                raise SystemExit(f"cached scenario block invalid at {old_offset:#x}")
            optimal = lz.compress_optimal(raw)
            decoded, optimal_used = lz.decompress(optimal)
            if decoded != raw or optimal_used != len(optimal):
                raise SystemExit(f"optimal compressor round-trip failed at {old_offset:#x}")
            if len(optimal) < len(compressed):
                compressed = optimal

        if len(compressed) <= slot_capacity:
            rebuilt[old_offset:slot_end] = bytes(slot_capacity)
            rebuilt[old_offset : old_offset + len(compressed)] = compressed
            new_offset = old_offset
        else:
            if len(rebuilt) < append_cursor:
                rebuilt.extend(bytes(append_cursor - len(rebuilt)))
            new_offset = append_cursor
            rebuilt.extend(compressed)
            append_cursor = align(len(rebuilt))
            relocated += 1

        struct.pack_into("<III", rebuilt, record, new_offset, raw_size, len(compressed))
        updated_by_record[record] = (new_offset, raw_size, len(compressed))

    print(
        f"  {stem}: kept {len(recs) - relocated}/{len(recs)} blocks in original slot; "
        f"relocated {relocated}",
        flush=True,
    )

    required = align(len(rebuilt))
    if required > iso_file.size:
        # Never move the container itself.  The 1st game proved the script
        # engine bypasses ISO9660 for cross-group / management calls and treats
        # the container's original LBA as a fixed base: ordinary dialogue keeps
        # loading through the directory record, but choices, scene changes and
        # battle transitions read the *original* sector and hang.  See
        # work/galaxy_angel/STATUS.md "v42 GADAT001 고정 베이스 근본 수정".
        #
        # Keep the original extent and the complete local PIDX table in place.
        # Append the grown container as a backing copy at the end of the image
        # and point only the records that no longer fit the original allocation
        # at it.  ISO9660 allows overlapping extents, so extending this file's
        # logical size makes those seeks valid without moving any other file.
        rebuilt.extend(bytes(required - len(rebuilt)))
        new_begin = align(len(image))
        if len(image) < new_begin:
            image.extend(bytes(new_begin - len(image)))
        new_extent = new_begin // builder.SECTOR

        # Only the tail past the original allocation has to live elsewhere; the
        # first iso_file.size bytes stay exactly where the Japanese disc put
        # them, so every block that still fits keeps its original offset.
        tail = bytes(rebuilt[iso_file.size:])
        if region is not None:
            tail_begin = region.allocate(len(tail), begin)
            if len(image) < tail_begin + len(tail):
                image.extend(bytes(tail_begin + len(tail) - len(image)))
            image[tail_begin : tail_begin + len(tail)] = tail
        else:
            tail_begin = new_begin
            image.extend(tail)

        legacy = bytearray(rebuilt[: iso_file.size])
        redirected = 0
        logical_size = iso_file.size
        for record, (record_offset, raw_size, compressed_size) in list(
            updated_by_record.items()
        ):
            if record_offset + compressed_size <= iso_file.size:
                continue
            backing_offset = tail_begin + (record_offset - iso_file.size) - begin
            if backing_offset + compressed_size > 0xFFFFFFFF:
                raise SystemExit(
                    f"{stem}: backing offset {backing_offset:#x} does not fit a "
                    "32-bit PIDX field.  Reserve a backing region closer to the "
                    "container with eternal_lovers_reserve_backing_region.py; "
                    "moving the container is not an option because the script "
                    "engine addresses it from its original base."
                )
            struct.pack_into(
                "<III", legacy, record, backing_offset, raw_size, compressed_size
            )
            updated_by_record[record] = (backing_offset, raw_size, compressed_size)
            logical_size = max(logical_size, backing_offset + compressed_size)
            redirected += 1
        image[begin : begin + iso_file.size] = legacy
        builder.patch_iso_file_record(image, iso_file, iso_file.extent, logical_size)
        print(
            f"  {stem}: kept fixed extent {iso_file.extent}; redirected "
            f"{redirected} oversized records to backing byte {tail_begin}; "
            f"logical size {iso_file.size}->{logical_size}",
            flush=True,
        )
    else:
        rebuilt.extend(bytes(iso_file.size - len(rebuilt)))
        image[begin : begin + iso_file.size] = rebuilt

    file_id, patched = builder.patch_central_idx_records(image, files, recs, updated_by_record)
    print(f"  {stem}: patched {patched} IDX.DAT records (file id {file_id})", flush=True)
    return updated_by_record


def build(
    original_iso: Path,
    output_iso: Path,
    original_scenario: Path,
    built_scenario: Path,
    lz_script: Path,
    containers: list[str],
    force_recompress: bool,
    patch_existing: bool = False,
    region_path: Path | None = None,
) -> None:
    if not patch_existing:
        shutil.copyfile(original_iso, output_iso)
    image = bytearray(output_iso.read_bytes())
    files = builder.iso_files(image)
    lz = builder.load_lz(lz_script)
    region = backing.load(region_path)

    cache_dir = output_iso.parent / "scenario_compressed_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    compressor_fingerprint = hashlib.sha256(lz_script.read_bytes()).digest()

    touched_containers = 0
    for stem in containers:
        print(f"container {stem}:", flush=True)
        result = build_container(
            image, files, stem, original_scenario, built_scenario, lz,
            cache_dir, compressor_fingerprint, force_recompress, region,
        )
        if result:
            touched_containers += 1

    image.extend(bytes(align(len(image)) - len(image)))
    builder.patch_volume_size(image)
    output_iso.write_bytes(image)
    digest = hashlib.sha256(image).hexdigest()
    print(f"built {output_iso} sha256={digest} touched_containers={touched_containers}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--output-iso", type=Path, required=True)
    parser.add_argument("--original-scenario", type=Path, required=True)
    parser.add_argument(
        "--built-scenario", type=Path,
        help="Directory of edited *_DAT_<offset>.txt files; falls back to "
             "--original-scenario for files it does not override (pass-through).",
    )
    parser.add_argument("--lz-script", type=Path, required=True)
    parser.add_argument(
        "--container", action="append", dest="containers",
        help="ISO file stem to rebuild (repeatable). Default: SCENARIO.",
    )
    parser.add_argument(
        "--backing-region", type=Path,
        help="JSON from eternal_lovers_reserve_backing_region.py.  Oversized "
             "blocks go there so the container keeps its original extent.",
    )
    parser.add_argument(
        "--patch-existing", action="store_true",
        help="Edit --output-iso in place instead of copying --original-iso over "
             "it, so this pass can run after other patchers in one pipeline.",
    )
    parser.add_argument(
        "--force-recompress", action="store_true",
        help="Recompress every scenario block even if unchanged from source "
             "(used by the structure-verification round-trip test).",
    )
    args = parser.parse_args()
    build(
        args.original_iso, args.output_iso, args.original_scenario,
        args.built_scenario or args.original_scenario, args.lz_script,
        args.containers or ["SCENARIO"], args.force_recompress,
        args.patch_existing, args.backing_region,
    )


if __name__ == "__main__":
    main()
