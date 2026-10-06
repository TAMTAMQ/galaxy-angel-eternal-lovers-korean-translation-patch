#!/usr/bin/env python3
"""Insert translated non-ISB text resources into an Eternal Lovers ISO."""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import re
import struct
from collections import defaultdict
from pathlib import Path

import eternal_lovers_backing
import galaxy_angel_build as builder
import galaxy_angel_translation as translation
import ikusa_lz
import moonlit_lovers_resources as resources


HALF_SPACE = b"\xA0"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


FULL_SPACE = "　".encode("cp932")


def encode_display_text(text: str, custom_map: dict[str, bytes]) -> bytes:
    """Encode one display line.

    Word gaps become the half-width blank 0xA0, which the original uses too, but
    a line must never *open* with one: the renderer stops part-way through a
    message whose line starts with 0xA0, which is what froze Eternal Lovers.
    The original indents with the full-width space, so keep that character
    instead of folding it into a word gap, and drop a leading gap outright.
    """
    text = translation.normalize_display_punctuation(text)
    output = bytearray()
    indent = 0
    while indent < len(text) and text[indent] == "　":
        output.extend(FULL_SPACE)
        indent += 1
    for index, part in enumerate(text[indent:].replace("　", " ").split(" ")):
        if index:
            output.extend(HALF_SPACE)
        output.extend(translation.encode_text(part, custom_map))
    while output[:1] == HALF_SPACE:
        del output[0]
    return bytes(output)


def line_ranges(raw: bytes) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for line in raw.splitlines(keepends=True):
        end = cursor + len(line.rstrip(b"\r\n"))
        ranges.append((cursor, end))
        cursor += len(line)
    if cursor < len(raw):
        ranges.append((cursor, len(raw)))
    return ranges


def collect_targets(payload: dict) -> dict[str, dict[int, list[dict]]]:
    targets: dict[str, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))
    seen: set[tuple[str, int, int, str]] = set()
    for unit in payload["candidates"]:
        if not unit.get("use_translation") or not unit.get("translation"):
            continue
        for occurrence in unit.get("occurrences", []):
            stem = occurrence["container"].upper()
            offset = int(occurrence["block_offset"])
            line = int(occurrence["line"])
            key = (stem, offset, line, unit["original"])
            if key in seen:
                continue
            seen.add(key)
            targets[stem][offset].append({**unit, "occurrence": occurrence})
    return {stem: dict(blocks) for stem, blocks in targets.items()}


BATTLE_MESSAGE_LINE_RE = re.compile(rb"^(\s*#)(\d+)(\s*=\s*)(.*)$")
# No original battle row is wider than 34 columns (17 full-width characters);
# Korean rows of 37-41 columns were reported running past the window.
BATTLE_TEXT_COLUMNS = 34
BATTLE_MAX_LINES = 3
# Korean rows often split one word the way the Japanese rows split a phrase
# ("모" + "두", "주셨나" + "요").  Rejoin those without a space before rewrapping.
_ROW_JOIN_SUFFIX_RE = re.compile(
    r"^(?:을|를|은|는|의|에|에서|에게|로|으로|와|과|도|만|까지|부터|요|죠|다|니다|습니다|네요|군요|인가|인지)"
    r"(?=$|[\s,.!?…～ー」』)])"
)
_ONE_SYLLABLE_WORDS = frozenset("내네제저그이날난널넌것수더꼭잘못안다또왜뭐좀참곧늘막한두세온새첫자아응예")


def join_battle_rows(first: str, second: str) -> str:
    if not first or not second:
        return first or second
    if re.search(r"[가-힣]$", first) and re.match(r"[가-힣]", second):
        last = first.split(" ")[-1]
        if (len(last) == 1 and last not in _ONE_SYLLABLE_WORDS) or _ROW_JOIN_SUFFIX_RE.match(second):
            return first + second
    return first + " " + second


def battle_message_replacements(
    raw: bytes,
    ranges: list[tuple[int, int]],
    units: list[dict],
    custom_map: dict[str, bytes],
) -> tuple[list[tuple[int, int, bytes, str]], set[tuple[int, int]]]:
    """Reflow two-row SLG messages into the renderer's available three rows."""
    groups: dict[tuple[str, bytes], dict[int, dict]] = defaultdict(dict)
    for unit in units:
        occurrence = unit["occurrence"]
        section = str(occurrence.get("section") or "")
        if not section.startswith("MESSAGE"):
            continue
        line_number = int(occurrence["line"])
        if not 1 <= line_number <= len(ranges):
            continue
        line_start, line_end = ranges[line_number - 1]
        match = BATTLE_MESSAGE_LINE_RE.match(raw[line_start:line_end])
        if match is None:
            continue
        key = match.group(2)
        if key[-1:] not in (b"1", b"2"):
            continue
        original = unit["original"].rstrip("\r\n").encode("cp932")
        position = raw.find(original, line_start, line_end)
        if position < 0:
            continue
        groups[(section, key[:-1])][int(key[-1:])] = {
            "unit": unit,
            "span": (position, position + len(original)),
            "match": match,
            "line_end": line_end,
        }

    replacements: list[tuple[int, int, bytes, str]] = []
    handled: set[tuple[int, int]] = set()
    for rows in groups.values():
        if set(rows) != {1, 2}:
            continue
        source_lines = [
            " ".join(rows[row]["unit"]["translation"].rstrip("\r\n").splitlines())
            for row in (1, 2)
        ]
        if all(translation.display_columns(line) <= BATTLE_TEXT_COLUMNS for line in source_lines):
            rendered = "\n".join(source_lines)
        else:
            rendered = translation._rebalance_dialogue_lines(
                join_battle_rows(*source_lines), BATTLE_TEXT_COLUMNS, BATTLE_MAX_LINES
            )
            if rendered is None:
                rendered = "\n".join(source_lines)
        output_lines = rendered.rstrip("\n").split("\n")
        if len(output_lines) > BATTLE_MAX_LINES or any(
            translation.display_columns(line) > BATTLE_TEXT_COLUMNS
            for line in output_lines
        ):
            unit_ids = ", ".join(rows[row]["unit"]["id"] for row in (1, 2))
            raise ValueError(f"battle message does not fit three rows: {unit_ids}")
        while len(output_lines) < 2:
            output_lines.append("")

        for row, text in zip((1, 2), output_lines[:2], strict=True):
            info = rows[row]
            span = info["span"]
            replacements.append(
                (span[0], span[1], encode_display_text(text, custom_map), info["unit"]["id"])
            )
            handled.add(span)

        if len(output_lines) == 3:
            info = rows[2]
            match = info["match"]
            line_end = info["line_end"]
            newline = b"\r\n" if raw[line_end:line_end + 2] == b"\r\n" else b"\n"
            third_key = match.group(2)[:-1] + b"3"
            third_prefix = match.group(1) + third_key + match.group(3)
            third = newline + third_prefix + encode_display_text(output_lines[2], custom_map)
            replacements.append((line_end, line_end, third, info["unit"]["id"] + ":row3"))
    return replacements, handled


def rebuild_resource(raw: bytes, units: list[dict],
                     custom_map: dict[str, bytes]) -> tuple[bytes, int]:
    ranges = line_ranges(raw)
    raw_digest = hashlib.sha256(raw).hexdigest()
    replacements: list[tuple[int, int, bytes, str]] = []
    seen_ranges: set[tuple[int, int]] = set()
    battle_replacements, battle_ranges = battle_message_replacements(
        raw, ranges, units, custom_map
    )
    replacements.extend(battle_replacements)
    seen_ranges.update(battle_ranges)
    for unit in units:
        occurrence = unit["occurrence"]
        expected_hash = occurrence.get("raw_sha256")
        if expected_hash and raw_digest != expected_hash:
            raise ValueError(f"source hash mismatch for {unit['id']}")
        line_number = int(occurrence["line"])
        if not 1 <= line_number <= len(ranges):
            raise ValueError(f"line outside resource for {unit['id']}: {line_number}")
        start, end = ranges[line_number - 1]
        original = unit["original"].rstrip("\r\n").encode("cp932")
        position = raw.find(original, start, end)
        if position < 0:
            raise ValueError(f"source text mismatch for {unit['id']} line {line_number}")
        span = (position, position + len(original))
        if span in seen_ranges:
            continue
        seen_ranges.add(span)
        rendered = " ".join(unit["translation"].rstrip("\r\n").splitlines())
        replacements.append(
            (span[0], span[1], encode_display_text(rendered, custom_map), unit["id"])
        )
    replacements.sort(key=lambda item: (item[0], item[1]))
    for left, right in zip(replacements, replacements[1:]):
        if left[1] > right[0]:
            raise ValueError(f"overlapping replacements: {left[3]} / {right[3]}")
    rebuilt = bytearray(raw)
    for start, end, rendered, _unit_id in reversed(replacements):
        rebuilt[start:end] = rendered
    return bytes(rebuilt), len(replacements)


def encode_resource(raw: bytes, codec: str) -> bytes:
    if codec == "ikusa_lz":
        return ikusa_lz.compress(raw)
    if codec == "stored_xor":
        return resources.encode_stored(raw)
    raise ValueError(f"unsupported codec: {codec}")


def merged_resources(container: bytes, stem: str) -> dict[int, resources.Resource]:
    merged: dict[int, resources.Resource] = {}
    for item in resources.container_resources(container, stem):
        previous = merged.get(item.offset)
        if previous is None:
            merged[item.offset] = item
            continue
        if (
            previous.raw_size,
            previous.compressed_size,
            previous.codec,
        ) != (item.raw_size, item.compressed_size, item.codec):
            raise ValueError(f"conflicting duplicate resource at {stem}:{item.offset:#x}")
        previous.record_positions.extend(item.record_positions)
        previous.resource_ids.extend(item.resource_ids)
        previous.fsts_bases.extend(item.fsts_bases)
    return merged


def align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def repack_container(
    original: bytes,
    resource_map: dict[int, resources.Resource],
    replacements: dict[int, tuple[bytes, int]],
    fixed_offsets: dict[int, int] | None = None,
) -> tuple[bytearray, dict[int, tuple[int, int, int]], int]:
    """Compact a PIDX data area or each FSTS bank without growing the file."""
    fixed_offsets = fixed_offsets or {}
    rebuilt = bytearray(original)
    items = [resource_map[offset] for offset in sorted(resource_map)]
    updated_legacy: dict[int, tuple[int, int, int]] = {}
    relocated = 0

    def payload(item: resources.Resource) -> tuple[bytes, int]:
        replacement = replacements.get(item.offset)
        if replacement is not None:
            return replacement
        return (
            original[item.offset:item.offset + item.compressed_size],
            item.raw_size,
        )

    if items[0].source_kind == "pidx":
        cursor = min(item.offset for item in items)
        rebuilt[cursor:] = bytes(len(rebuilt) - cursor)
        for item in items:
            cursor = align_up(cursor, builder.SECTOR)
            encoded, raw_size = payload(item)
            end = cursor + len(encoded)
            if end > len(rebuilt):
                rebuilt.extend(bytes(end - len(rebuilt)))
            rebuilt[cursor:end] = encoded
            relocated += int(cursor != item.offset)
            for record in item.record_positions:
                struct.pack_into("<III", rebuilt, record, cursor, raw_size, len(encoded))
                updated_legacy[record] = (cursor, raw_size, len(encoded))
            cursor = end
        return rebuilt, updated_legacy, relocated

    if any(item.source_kind != "fsts" or len(set(item.fsts_bases)) != 1 for item in items):
        raise ValueError("unsupported mixed or multiply referenced FSTS resources")
    bases = sorted({item.fsts_bases[0] for item in items})
    optimal_cache: dict[str, bytes] = {}
    for index, base in enumerate(bases):
        bank = [item for item in items if item.fsts_bases[0] == base]
        boundary = bases[index + 1] if index + 1 < len(bases) else len(original)
        data_start = min(item.offset for item in bank)
        bank_payloads = [
            [item, *payload(item)]
            for item in bank
        ]

        # A few runtime paths bypass FSTS and enter a translated cache block at
        # its original container-relative address.  ADV replacements are known
        # runtime copies.  In SLGSTAGE, the first small control script in each
        # consecutive group is the stage-entry block; the larger scripts before
        # it and the following route variants are reached through FSTS.
        stage_control = []
        previous_was_control = False
        for item, _encoded, _raw_size in bank_payloads:
            is_control = False
            if item.container == "SLGSTAGE" and item.raw_size <= 20_000:
                try:
                    raw = resources.decompress_resource(
                        original, item.offset, item.raw_size, item.compressed_size
                    )
                except Exception:
                    raw = b""
                is_control = "使用ステージ".encode("cp932") in raw[:512]
            stage_control.append(is_control and not previous_was_control)
            previous_was_control = is_control

        hard_fixed = {
            item.offset
            for (item, _encoded, _raw_size), is_stage_entry in zip(
                bank_payloads, stage_control
            )
            if (item.container == "ADV" and item.offset in replacements)
            or item.offset in fixed_offsets
            or is_stage_entry
        }

        def layout() -> tuple[list[tuple[resources.Resource, bytes, int, int]], int]:
            placed_by_offset: dict[int, tuple[resources.Resource, bytes, int, int]] = {}
            occupied: list[tuple[int, int]] = []

            def overlaps(begin: int, end: int) -> bool:
                return any(begin < right and end > left for left, right in occupied)

            # Hard-fixed streams win first.  Other streams are compacted around
            # those reserved ranges so fragmented original gaps cannot make an
            # otherwise fitting bank overflow.
            ordered = sorted(
                bank_payloads,
                key=lambda entry: (entry[0].offset not in hard_fixed, entry[0].offset),
            )
            movable = []
            for item, encoded, raw_size in ordered:
                if item.offset not in hard_fixed:
                    movable.append((item, encoded, raw_size))
                    continue
                begin = fixed_offsets.get(item.offset, item.offset)
                end = begin + len(encoded)
                if begin < data_start or end > boundary:
                    if item.offset in hard_fixed:
                        raise ValueError(
                            f"fixed FSTS stream exceeds bank: {item.container}:"
                            f"{item.offset:#x}"
                        )
                    movable.append((item, encoded, raw_size))
                    continue
                if overlaps(begin, end):
                    if item.offset in hard_fixed:
                        raise ValueError(
                            f"fixed FSTS streams overlap: {item.container}:"
                            f"{item.offset:#x}"
                        )
                    movable.append((item, encoded, raw_size))
                    continue
                placed_by_offset[item.offset] = (item, encoded, raw_size, begin)
                occupied.append((begin, end))

            # The ADV runtime image pass patches named copies independently.
            # Do not merge byte-identical slots while restoring explicit
            # physical addresses, or two differently translated images can
            # become owners of the same slot.
            allow_shared_slots = not fixed_offsets and not any(
                item.container == "ADV" for item, *_rest in bank_payloads
            )
            digest_positions: dict[bytes, int] = {}
            if allow_shared_slots:
                for item, encoded, _raw_size, offset in placed_by_offset.values():
                    digest_positions.setdefault(hashlib.sha256(encoded).digest(), offset)

            for item, encoded, raw_size in movable:
                digest = hashlib.sha256(encoded).digest()
                shared = digest_positions.get(digest)
                if shared is not None:
                    placed_by_offset[item.offset] = (item, encoded, raw_size, shared)
                    continue
                cursor = data_start
                while True:
                    cursor = align_up(cursor, 16)
                    conflict = next(
                        (
                            (left, right)
                            for left, right in sorted(occupied)
                            if cursor < right and cursor + len(encoded) > left
                        ),
                        None,
                    )
                    if conflict is None:
                        break
                    cursor = conflict[1]
                placed_by_offset[item.offset] = (item, encoded, raw_size, cursor)
                occupied.append((cursor, cursor + len(encoded)))
                if allow_shared_slots:
                    digest_positions[digest] = cursor

            placed = [placed_by_offset[item.offset] for item, *_ in bank_payloads]
            end = max((right for _left, right in occupied), default=data_start)
            return placed, end

        placed, cursor = layout()
        if cursor > boundary:
            # A compact FSTS bank can still overflow by a few bytes because
            # translated streams grew and every resource must remain 16-byte
            # aligned.  The first Galaxy Angel runtime patcher already solves
            # this by optimally recompressing only as many Ikusa-LZ streams as
            # necessary.  Prefer translated streams so untouched resources are
            # left byte-identical whenever possible.
            candidates = sorted(
                range(len(bank_payloads)),
                key=lambda i: (
                    bank_payloads[i][0].offset not in replacements,
                    abs(len(bank_payloads[i][1]) - 6000),
                ),
            )
            for payload_index in candidates:
                item, encoded, raw_size = bank_payloads[payload_index]
                if item.codec != "ikusa_lz":
                    continue
                raw = resources.decompress_resource(encoded, 0, raw_size, len(encoded))
                digest = hashlib.sha256(raw).hexdigest()
                optimal = optimal_cache.get(digest)
                if optimal is None:
                    optimal = ikusa_lz.compress_optimal(raw)
                    optimal_cache[digest] = optimal
                if len(optimal) >= len(encoded):
                    continue
                bank_payloads[payload_index][1] = optimal
                placed, cursor = layout()
                print(
                    f"optimized FSTS stream {item.offset:#x} "
                    f"{len(encoded)}->{len(optimal)}; "
                    f"bank overflow now {max(0, cursor - boundary)}",
                    flush=True,
                )
                if cursor <= boundary:
                    break
        if cursor > boundary:
            raise ValueError(
                f"FSTS bank repack overflow: {cursor}>{boundary} at {base:#x}"
            )

        rebuilt[data_start:boundary] = bytes(boundary - data_start)
        for item, encoded, raw_size, offset in placed:
            end = offset + len(encoded)
            rebuilt[offset:end] = encoded
            relocated += int(offset != item.offset)
            for record, record_base in zip(item.record_positions, item.fsts_bases):
                struct.pack_into(
                    "<III", rebuilt, record + 4,
                    offset - record_base, raw_size, len(encoded),
                )
    return rebuilt, updated_legacy, relocated


def patch_container(image: mmap.mmap, files: dict[str, builder.IsoFile], stem: str,
                    targets: dict[int, list[dict]], custom_map: dict[str, bytes],
                    dry_run: bool, cache_dir: Path,
                    compressed_cache: dict[tuple[str, str], bytes],
                    region=None) -> dict:
    item = builder.resolve_iso_file(files, stem)
    begin = item.extent * builder.SECTOR
    original = bytes(image[begin:begin + item.size])
    rebuilt_container = bytearray(original)
    resource_map = merged_resources(original, stem)
    offsets = sorted(resource_map)
    # Use the canonical PIDX parser rather than the legacy 0x10000-byte table
    # scan.  SLG has 5,296 leaf records and its latter half lies beyond that
    # scan window, including every record moved by the compacting pass.
    legacy_records = resources.pidx_record_map(original, stem)
    updated_legacy: dict[int, tuple[int, int, int]] = {}
    applied_resources = applied_strings = skipped_resources = 0
    optimized_resources = 0
    total_strings = 0
    replacements: dict[int, tuple[bytes, int]] = {}
    overflows: list[dict] = []

    for number, (offset, units) in enumerate(sorted(targets.items()), 1):
        resource = resource_map.get(offset)
        if resource is None:
            raise ValueError(f"resource missing: {stem}:{offset:#x}")
        raw = resources.decompress_resource(
            original, offset, resource.raw_size, resource.compressed_size
        )
        rebuilt_raw, string_count = rebuild_resource(raw, units, custom_map)
        total_strings += string_count
        digest = hashlib.sha256(rebuilt_raw).hexdigest()
        cache_key = (resource.codec, digest)
        compressed = compressed_cache.get(cache_key)
        cache_path = cache_dir / f"{resource.codec}_{digest}.bin"
        if compressed is None:
            if cache_path.is_file():
                compressed = cache_path.read_bytes()
            else:
                compressed = encode_resource(rebuilt_raw, resource.codec)
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(compressed)
            compressed_cache[cache_key] = compressed
        decoded = resources.decompress_resource(
            compressed, 0, len(rebuilt_raw), len(compressed)
        )
        if decoded != rebuilt_raw:
            raise ValueError(f"codec round-trip mismatch: {stem}:{offset:#x}")
        if resource.source_kind == "fsts":
            bases = set(resource.fsts_bases)
            if len(bases) != 1:
                raise ValueError(
                    f"multiply referenced FSTS target is unsupported: {stem}:{offset:#x} "
                    f"bases={sorted(bases)}"
                )
            bank_base = next(iter(bases))
            bank_bases = sorted(
                {
                    base
                    for item in resource_map.values()
                    for base in item.fsts_bases
                }
            )
            bank_boundary = min(
                (base for base in bank_bases if base > bank_base),
                default=len(original),
            )
            same_bank_next = [
                value
                for value in offsets
                if value > offset
                and set(resource_map[value].fsts_bases) == {bank_base}
            ]
            next_offset = min(same_bank_next, default=bank_boundary)
        else:
            next_offset = min(
                (value for value in offsets if value > offset),
                default=len(original),
            )
        capacity = next_offset - offset
        if len(compressed) > capacity and resource.codec == "ikusa_lz":
            optimal_key = ("ikusa_lz_optimal", digest)
            optimal = compressed_cache.get(optimal_key)
            optimal_path = cache_dir / f"ikusa_lz_optimal_{digest}.bin"
            if optimal is None:
                if optimal_path.is_file():
                    optimal = optimal_path.read_bytes()
                else:
                    optimal = ikusa_lz.compress_optimal(rebuilt_raw)
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    optimal_path.write_bytes(optimal)
                compressed_cache[optimal_key] = optimal
            if len(optimal) < len(compressed):
                compressed = optimal
                optimized_resources += 1
            decoded = resources.decompress_resource(
                compressed, 0, len(rebuilt_raw), len(compressed)
            )
            if decoded != rebuilt_raw:
                raise ValueError(f"optimal codec round-trip mismatch: {stem}:{offset:#x}")
        replacements[offset] = (compressed, len(rebuilt_raw))
        if len(compressed) > capacity:
            skipped_resources += 1
            overflows.append({
                "offset": offset,
                "path": resource.path,
                "codec": resource.codec,
                "required": len(compressed),
                "capacity": capacity,
                "strings": string_count,
            })
            continue
        applied_resources += 1
        applied_strings += string_count
        if number % 250 == 0 or number == len(targets):
            print(f"{stem}: planned {number}/{len(targets)} resources", flush=True)
        if dry_run:
            continue
        rebuilt_container[offset:next_offset] = bytes(capacity)
        rebuilt_container[offset:offset + len(compressed)] = compressed
        if resource.source_kind == "pidx":
            for record in resource.record_positions:
                struct.pack_into(
                    "<III", rebuilt_container, record,
                    offset, len(rebuilt_raw), len(compressed),
                )
                legacy = legacy_records.get(offset)
                if legacy is not None and legacy[0] == record:
                    updated_legacy[record] = (offset, len(rebuilt_raw), len(compressed))
        elif resource.source_kind == "fsts":
            for record in resource.record_positions:
                struct.pack_into("<II", rebuilt_container, record + 8,
                                 len(rebuilt_raw), len(compressed))
        else:
            raise ValueError(f"unknown resource source: {resource.source_kind}")

    relocated_resources = 0
    container_growth = 0
    if overflows:
        rebuilt_container, repacked_legacy, relocated_resources = repack_container(
            original, resource_map, replacements
        )
        # Reparse the finished table and derive the central-index update by
        # record position.  This remains correct when compaction changes many
        # offsets at once or duplicate resource records were merged above.
        reparsed = merged_resources(bytes(rebuilt_container), stem)
        repacked_by_record = {
            record: (item.offset, item.raw_size, item.compressed_size)
            for item in reparsed.values()
            for record in item.record_positions
        }
        updated_legacy = {
            legacy[0]: repacked_by_record[legacy[0]]
            for legacy in legacy_records.values()
            if legacy[0] in repacked_by_record
        }
        applied_resources = len(targets)
        applied_strings = total_strings
        skipped_resources = 0
        overflows = []
        if len(rebuilt_container) > len(original):
            required = align_up(len(rebuilt_container), builder.SECTOR)
            rebuilt_container.extend(bytes(required - len(rebuilt_container)))
            container_growth = required - len(original)

    idx_patched = 0
    if not dry_run:
        if len(rebuilt_container) > item.size:
            # Never move the container itself.  The 1st game proved the engine
            # bypasses ISO9660 for management/cross-group resource loads and
            # treats the container's original LBA as a fixed base, so a moved
            # container hangs at choices and battle transitions even when every
            # PIDX/IDX tuple is internally consistent.  See
            # work/galaxy_angel/STATUS.md "v42 GADAT001 고정 베이스 근본 수정".
            #
            # Keep the original extent, append the grown container as a backing
            # copy, and point only the records that no longer fit the original
            # allocation at it.  ISO9660 tolerates overlapping extents, so
            # extending the logical size makes those seeks valid.
            reparsed = merged_resources(bytes(rebuilt_container), stem)
            redirected_resources = [
                resource
                for resource in reparsed.values()
                if resource.offset + resource.compressed_size > item.size
            ]
            # A resource may begin inside the original allocation but end past
            # it. Copy the complete first crossing resource as well; mapping
            # only rebuilt_container[item.size:] would make its redirected
            # record point before the reserved region.
            overflow_start = min(
                resource.offset for resource in redirected_resources
            )
            tail = bytes(rebuilt_container[overflow_start:])
            if region is None:
                raise ValueError(
                    f"{stem} outgrew its allocation by "
                    f"{len(rebuilt_container) - item.size} bytes and no "
                    "backing region was supplied; the container must keep its "
                    "original extent, so reserve one with "
                    "eternal_lovers_reserve_backing_region.py"
                )
            tail_begin = region.allocate(len(tail), begin)
            if len(image) < tail_begin + len(tail):
                image.resize(tail_begin + len(tail))
            image[tail_begin:tail_begin + len(tail)] = tail

            legacy = bytearray(rebuilt_container[: item.size])
            logical_size = item.size
            redirected = 0
            for resource in reparsed.values():
                end = resource.offset + resource.compressed_size
                if end <= item.size:
                    continue
                if resource.source_kind != "pidx":
                    raise ValueError(
                        f"{stem}: {resource.source_kind} record at "
                        f"{resource.offset:#x} lies past the original "
                        "allocation; it cannot be redirected"
                    )
                backing_offset = tail_begin + (resource.offset - overflow_start) - begin
                if backing_offset + resource.compressed_size > 0xFFFFFFFF:
                    raise ValueError(
                        f"{stem}: backing offset {backing_offset:#x} does not fit "
                        "a 32-bit PIDX field"
                    )
                for record in resource.record_positions:
                    struct.pack_into(
                        "<III", legacy, record, backing_offset,
                        resource.raw_size, resource.compressed_size,
                    )
                    if record in updated_legacy:
                        updated_legacy[record] = (
                            backing_offset, resource.raw_size,
                            resource.compressed_size,
                        )
                logical_size = max(
                    logical_size, backing_offset + resource.compressed_size
                )
                redirected += 1
            image[begin:begin + item.size] = legacy
            builder.patch_iso_file_record(image, item, item.extent, logical_size)
            builder.patch_volume_size(image)
            print(
                f"{stem}: kept fixed extent {item.extent}; redirected "
                f"{redirected} oversized records to backing byte {tail_begin}; "
                f"logical size {item.size}->{logical_size}",
                flush=True,
            )
        else:
            image[begin:begin + item.size] = rebuilt_container
        if updated_legacy:
            _file_id, idx_patched = builder.patch_central_idx_records(
                image, files, legacy_records, updated_legacy
            )
    return {
        "target_resources": len(targets),
        "applied_resources": applied_resources,
        "skipped_resources": skipped_resources,
        "applied_strings": applied_strings,
        "optimized_resources": optimized_resources,
        "relocated_resources": relocated_resources,
        "container_growth": container_growth,
        "idx_records": idx_patched,
        "overflows": overflows,
    }


def verify_iso(iso: Path, report: dict) -> None:
    with iso.open("rb") as stream, mmap.mmap(
        stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as image:
        files = builder.iso_files(image)
        for stem, result in report["containers"].items():
            if not result["applied_resources"]:
                continue
            item = builder.resolve_iso_file(files, stem)
            begin = item.extent * builder.SECTOR
            container = bytes(image[begin:begin + item.size])
            parsed = merged_resources(container, stem)
            for offset in result["applied_offsets"]:
                resource = parsed[offset]
                resources.decompress_resource(
                    container, offset, resource.raw_size, resource.compressed_size
                )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--translations", type=Path, required=True)
    parser.add_argument("--encoding-map", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument(
        "--backing-region", type=Path,
        help="JSON from eternal_lovers_reserve_backing_region.py.  A container "
             "that outgrows its allocation puts the overflow there instead of "
             "moving, which the script engine does not tolerate.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    region = eternal_lovers_backing.load(args.backing_region)
    custom_map = translation.load_custom_map(args.encoding_map)
    assert custom_map is not None
    targets = collect_targets(load_json(args.translations))
    cache_dir = args.cache_dir or args.iso.parent / "remaining_compressed_cache"
    report = {
        "schema": "eternal-lovers-remaining-patch/v1",
        "iso": str(args.iso),
        "dry_run": args.dry_run,
        "containers": {},
    }
    compressed_cache: dict[tuple[str, str], bytes] = {}
    mode = "rb" if args.dry_run else "r+b"
    access = mmap.ACCESS_READ if args.dry_run else mmap.ACCESS_WRITE
    with args.iso.open(mode) as stream, mmap.mmap(stream.fileno(), 0, access=access) as image:
        files = builder.iso_files(image)
        for stem, blocks in sorted(targets.items()):
            result = patch_container(
                image, files, stem, blocks, custom_map, args.dry_run, cache_dir,
                compressed_cache, region,
            )
            result["applied_offsets"] = [
                offset for offset in sorted(blocks)
                if offset not in {item["offset"] for item in result["overflows"]}
            ]
            report["containers"][stem] = result
            print(stem, {key: value for key, value in result.items()
                         if key not in ("overflows", "applied_offsets")}, flush=True)
        if not args.dry_run:
            image.flush()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"report: {args.report}")


if __name__ == "__main__":
    main()
