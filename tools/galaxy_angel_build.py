#!/usr/bin/env python3
"""Build Galaxy Angel's fixed-layout PS2 ISO from translated scenario files."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import re
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path


SECTOR = 2048
SCENARIO_NAME_RE = re.compile(r"^(?P<container>.+)_DAT_(?P<offset>[0-9a-fA-F]{8})\.txt$")
SCRIPT_TARGET_RE = re.compile(
    rb"(?m)^(?:(ID[A-Z0-9]+):\{|:([LR][A-Z0-9]+))(?=\r?$)"
)
INDEX_VALUE_RE = re.compile(
    rb"(?m)^((?:ID|[LR])[A-Z0-9]+)=(\d+)(\r?)$"
)
GADAT001_INDEX_OFFSET = 0x14E000
GADAT000_SAVELOAD_OFFSET = 0x1B800


@dataclass(frozen=True)
class IsoFile:
    path: str
    extent: int
    size: int
    record_offset: int


def iso_files(image: bytes) -> dict[str, IsoFile]:
    pvd = image[16 * SECTOR : 17 * SECTOR]
    if pvd[1:6] != b"CD001":
        raise ValueError("not an ISO9660 image")
    root = pvd[156 : 156 + pvd[156]]
    files: dict[str, IsoFile] = {}

    def walk(record: bytes, prefix: str) -> None:
        extent = struct.unpack_from("<I", record, 2)[0]
        size = struct.unpack_from("<I", record, 10)[0]
        directory = image[extent * SECTOR : extent * SECTOR + size]
        pos = 0
        while pos < len(directory):
            length = directory[pos]
            if not length:
                pos = (pos // SECTOR + 1) * SECTOR
                continue
            record_offset = extent * SECTOR + pos
            child = directory[pos : pos + length]
            name_len = child[32]
            raw_name = child[33 : 33 + name_len]
            pos += length
            if raw_name in (b"\x00", b"\x01"):
                continue
            name = raw_name.decode("ascii").split(";", 1)[0]
            path = f"{prefix}/{name}" if prefix else name
            if child[25] & 2:
                walk(child, path)
            else:
                files[path.upper()] = IsoFile(
                    path,
                    struct.unpack_from("<I", child, 2)[0],
                    struct.unpack_from("<I", child, 10)[0],
                    record_offset,
                )

    walk(root, "")
    return files


def load_lz(path: Path):
    spec = importlib.util.spec_from_file_location("ikusa_lz", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def records(container: bytearray) -> dict[int, tuple[int, int, int]]:
    if container[:8] != b"PIDX0\0\0\0":
        return {}
    found = {}
    # PIDX0 permits nested directory records, so the first leaf record is not at a
    # fixed table-relative position.  A leaf's final three words are always the
    # aligned data offset, raw size, and compressed size.  Validate candidates by
    # checking the compressed block magic at the referenced offset.
    first_data = len(container)
    for pos in range(0x30, min(len(container) - 12, 0x10000), 4):
        data_offset, raw_size, compressed_size = struct.unpack_from("<III", container, pos)
        if data_offset < 0x800 or data_offset % 0x800 or data_offset + 8 > len(container):
            continue
        if container[data_offset : data_offset + 4] != b" 3;1":
            continue
        declared = struct.unpack_from("<I", container, data_offset + 4)[0]
        if declared != raw_size or compressed_size < 8:
            continue
        found[data_offset] = (pos, raw_size, compressed_size)
        first_data = min(first_data, data_offset)
    return found


def resolve_iso_file(files: dict[str, IsoFile], stem: str) -> IsoFile:
    wanted = f"{stem}.DAT".upper()
    matches = [item for key, item in files.items() if key.rsplit("/", 1)[-1] == wanted]
    if len(matches) != 1:
        raise SystemExit(f"ISO file resolution failed for {wanted}: {len(matches)} matches")
    return matches[0]


def align(value: int, alignment: int = SECTOR) -> int:
    return (value + alignment - 1) // alignment * alignment


def patch_iso_file_record(image: bytearray, item: IsoFile, extent: int, size: int) -> None:
    """Update both-endian ISO9660 extent and size fields for one directory entry."""
    pos = item.record_offset
    struct.pack_into("<I", image, pos + 2, extent)
    struct.pack_into(">I", image, pos + 6, extent)
    struct.pack_into("<I", image, pos + 10, size)
    struct.pack_into(">I", image, pos + 14, size)


def patch_volume_size(image: bytearray) -> None:
    sectors = len(image) // SECTOR
    sector = 16
    while sector * SECTOR + SECTOR <= len(image):
        pos = sector * SECTOR
        descriptor_type = image[pos]
        if image[pos + 1 : pos + 6] != b"CD001":
            break
        if descriptor_type in (1, 2):
            struct.pack_into("<I", image, pos + 80, sectors)
            struct.pack_into(">I", image, pos + 84, sectors)
        if descriptor_type == 255:
            break
        sector += 1


def patch_central_idx_records(
    image: bytearray,
    files: dict[str, IsoFile],
    original_records: dict[int, tuple[int, int, int]],
    updated_by_record: dict[int, tuple[int, int, int]],
) -> tuple[int, int]:
    """Mirror rebuilt PIDX leaf tuples into the flat runtime IDX.DAT table.

    GADAT containers carry a local PIDX table, but the game loads blocks through
    a second flat table in IDX.DAT.  Leaving that table unchanged makes the game
    read only the original compressed byte count; the decompressor then leaves
    the remainder of the scenario buffer filled with 0xAA.
    """
    idx_file = resolve_iso_file(files, "IDX")
    idx_begin = idx_file.extent * SECTOR
    idx_end = idx_begin + idx_file.size

    old_by_record = {
        record: (offset, raw_size, compressed_size)
        for offset, (record, raw_size, compressed_size) in original_records.items()
    }
    records_by_tuple = {}
    for record, old_tuple in old_by_record.items():
        records_by_tuple.setdefault(old_tuple, []).append(record)
    candidates: dict[int, dict[int, int]] = {}
    for relative in range(0, idx_file.size - 15, 4):
        file_id, offset, raw_size, compressed_size = struct.unpack_from(
            "<IIII", image, idx_begin + relative
        )
        if offset % SECTOR or compressed_size < 8:
            continue
        for record in records_by_tuple.get(
            (offset, raw_size, compressed_size), ()
        ):
            candidates.setdefault(file_id, {})[record] = idx_begin + relative

    if not candidates:
        raise SystemExit("IDX.DAT mirror for rebuilt PIDX container was not found")
    ranked = sorted(candidates.items(), key=lambda item: len(item[1]), reverse=True)
    file_id, positions = ranked[0]
    if len(ranked) > 1 and len(ranked[1][1]) == len(positions):
        raise SystemExit("IDX.DAT mirror file id is ambiguous")
    if len(positions) != len(old_by_record):
        raise SystemExit(
            f"IDX.DAT mirror is incomplete for file id {file_id}: "
            f"{len(positions)}/{len(old_by_record)} records"
        )

    patched = 0
    for record, old_tuple in old_by_record.items():
        new_tuple = updated_by_record.get(record, old_tuple)
        if new_tuple == old_tuple:
            continue
        position = positions[record]
        struct.pack_into("<III", image, position + 4, *new_tuple)
        patched += 1
    return file_id, patched


def patch_runtime_speaker_copies(
    image: bytearray, files: dict[str, IsoFile], original_raw: bytes,
    patched_compressed: bytes, lz,
) -> int:
    """Patch the ADV/MISC runtime copies of the speaker configuration."""
    patched = 0
    for stem in ("ADV", "MISC"):
        item = resolve_iso_file(files, stem)
        begin = item.extent * SECTOR
        end = begin + item.size
        cursor = begin
        while True:
            position = image.find(b" 3;1", cursor, end)
            if position < 0:
                break
            cursor = position + 4
            try:
                raw, consumed = lz.decompress(image, position)
            except (IndexError, struct.error, ValueError):
                continue
            if raw != original_raw:
                continue
            if len(patched_compressed) > consumed:
                raise SystemExit(
                    f"runtime speaker copy overflow in {stem}: "
                    f"{len(patched_compressed)}>{consumed}"
                )
            image[position:position + consumed] = (
                patched_compressed + bytes(consumed - len(patched_compressed))
            )
            patched += 1
    return patched


def patch_runtime_scenario_index_copies(
    image: bytearray, files: dict[str, IsoFile], original_raw: bytes,
    patched_compressed: bytes, lz,
) -> int:
    """Patch ADV/MISC copies of the scenario label/IDS byte-offset index.

    The ADV runtime reloads this copy after transitions such as battle results.
    If it retains Japanese offsets, execution resumes in the middle of a command
    whenever translated dialogue changed the preceding byte count.
    """
    patched = 0
    for stem in ("ADV", "MISC"):
        item = resolve_iso_file(files, stem)
        begin = item.extent * SECTOR
        end = begin + item.size
        cursor = begin
        while True:
            position = image.find(b" 3;1", cursor, end)
            if position < 0:
                break
            cursor = position + 4
            try:
                raw, consumed = lz.decompress(image, position)
            except (IndexError, struct.error, ValueError):
                continue
            if raw != original_raw:
                continue
            if len(patched_compressed) > consumed:
                raise SystemExit(
                    f"runtime scenario-index copy overflow in {stem}: "
                    f"{len(patched_compressed)}>{consumed}"
                )
            image[position:position + consumed] = (
                patched_compressed + bytes(consumed - len(patched_compressed))
            )
            patched += 1
    if patched == 0:
        raise SystemExit("runtime scenario-index copy was not found")
    return patched


def script_targets(directory: Path) -> dict[str, list[tuple[str, int]]]:
    """Return every indexed IDS/L/R target and its byte position."""
    found: dict[str, list[tuple[str, int]]] = {}
    for path in sorted(directory.glob("GADAT001_DAT_*.txt")):
        raw = path.read_bytes()
        for match in SCRIPT_TARGET_RE.finditer(raw):
            key = (match.group(1) or match.group(2)).decode("ascii")
            found.setdefault(key, []).append((path.name, match.start()))
    return found


def rebuild_scenario_index(
    original_raw: bytes, original_scenario: Path, built_scenario: Path
) -> tuple[bytes, int, int]:
    """Point the fixed GADAT001 IDS/L/R index at translated byte offsets.

    Dialogue length changes are valid, but the game does not discover IDS,
    :L, or :R records by scanning the script. It jumps through this separate
    table. ``\\randjump`` uses the indexed :R targets too, so leaving Japanese
    R offsets can enter the middle of a later command (for example
    ``er(005)`` from ``\\Speaker(005)``).
    """
    original_targets = script_targets(original_scenario)
    built_targets = script_targets(built_scenario)
    changed = 0
    resolved = 0

    def replace(match: re.Match[bytes]) -> bytes:
        nonlocal changed, resolved
        key = match.group(1).decode("ascii")
        old_text = match.group(2)
        old_offset = int(old_text)
        candidates = [
            item for item in original_targets.get(key, []) if item[1] == old_offset
        ]
        # Management-only IDs live in protected blocks outside the exported
        # 271 scenario files. Their index values remain unchanged.
        if not candidates:
            return match.group(0)
        if len(candidates) != 1:
            raise SystemExit(
                f"ambiguous original scenario index target: {key}={old_offset}: "
                f"{candidates}"
            )
        source_name, _ = candidates[0]
        original_in_file = [
            position
            for name, position in original_targets.get(key, [])
            if name == source_name
        ]
        occurrence_index = original_in_file.index(old_offset)
        translated = [
            position
            for name, position in built_targets.get(key, [])
            if name == source_name
        ]
        if occurrence_index >= len(translated):
            raise SystemExit(
                f"translated scenario index target occurrence missing: "
                f"{key}[{occurrence_index}] in {source_name}: {translated}"
            )
        new_offset = translated[occurrence_index]
        # Keep offsets in canonical decimal form.  The script parser does not
        # safely accept zero-padded values: for example `L0243003=09471` can be
        # interpreted as zero, which makes a local jump restart the current
        # section at offset 0.  The index is line-based, so its raw size may
        # shrink or grow as decimal digit counts change.
        new_text = str(new_offset).encode("ascii")
        resolved += 1
        changed += int(new_text != old_text)
        return match.group(1) + b"=" + new_text + match.group(3)

    rebuilt = INDEX_VALUE_RE.sub(replace, original_raw)
    return rebuilt, resolved, changed


def build(original_iso: Path, output_iso: Path, original_scenario: Path,
          built_scenario: Path, patched_elf: Path, lz_script: Path,
          patched_speakers: Path | None = None) -> None:
    shutil.copyfile(original_iso, output_iso)
    image = bytearray(output_iso.read_bytes())
    files = iso_files(image)
    elf_file = resolve_iso_file(files, "SLPM_652.54".removesuffix(".DAT")) if False else next(
        item for key, item in files.items() if key.rsplit("/", 1)[-1] == "SLPM_652.54"
    )
    elf = patched_elf.read_bytes()
    if len(elf) != elf_file.size:
        raise SystemExit("patched ELF size changed; fixed-layout ISO build refused")
    image[elf_file.extent * SECTOR : elf_file.extent * SECTOR + elf_file.size] = elf
    original_speaker_raw = patched_speaker_compressed = None
    original_save_raw = patched_save_compressed = None
    original_scenario_index_raw = patched_scenario_index_compressed = None
    original_speaker_container = None
    if patched_speakers is not None:
        speaker_file = resolve_iso_file(files, "GADAT000")
        speaker_data = patched_speakers.read_bytes()
        if len(speaker_data) != speaker_file.size:
            raise SystemExit("patched GADAT000 size changed; fixed-layout ISO build refused")
        speaker_begin = speaker_file.extent * SECTOR
        original_speaker_container = bytes(
            image[speaker_begin:speaker_begin + speaker_file.size]
        )
        original_speaker_records = records(bytearray(original_speaker_container))
        image[speaker_begin:speaker_begin + speaker_file.size] = speaker_data
        patched_speaker_records = records(bytearray(speaker_data))
        updated_speaker_records = {
            record: (offset, raw_size, compressed_size)
            for offset, (record, raw_size, compressed_size) in patched_speaker_records.items()
            if offset in original_speaker_records
            and original_speaker_records[offset]
            != (record, raw_size, compressed_size)
        }
        if updated_speaker_records:
            speaker_file_id, speaker_idx_patched = patch_central_idx_records(
                image, files, original_speaker_records, updated_speaker_records
            )
            print(
                f"patched {speaker_idx_patched} GADAT000 IDX.DAT records "
                f"for file id {speaker_file_id}",
                flush=True,
            )
    lz = load_lz(lz_script)
    if patched_speakers is not None:
        assert original_speaker_container is not None
        original_speaker_raw, _ = lz.decompress(original_speaker_container, 0x6000)
        patched_container = patched_speakers.read_bytes()
        _patched_raw, patched_used = lz.decompress(patched_container, 0x6000)
        patched_speaker_compressed = patched_container[0x6000:0x6000 + patched_used]
        original_save_raw, _ = lz.decompress(
            original_speaker_container, GADAT000_SAVELOAD_OFFSET
        )
        _patched_save_raw, patched_save_used = lz.decompress(
            patched_container, GADAT000_SAVELOAD_OFFSET
        )
        patched_save_compressed = patched_container[
            GADAT000_SAVELOAD_OFFSET:GADAT000_SAVELOAD_OFFSET + patched_save_used
        ]
    cache_dir = output_iso.parent / "scenario_compressed_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    compressor_fingerprint = hashlib.sha256(lz_script.read_bytes()).digest()
    grouped: dict[str, list[tuple[int, Path, Path]]] = {}
    for built in sorted(built_scenario.glob("*.txt")):
        match = SCENARIO_NAME_RE.match(built.name)
        if not match:
            raise SystemExit(f"unexpected scenario filename: {built.name}")
        original = original_scenario / built.name
        if built.read_bytes() == original.read_bytes():
            continue
        grouped.setdefault(match.group("container"), []).append(
            (int(match.group("offset"), 16), original, built)
        )
    for stem, replacements in grouped.items():
        iso_file = resolve_iso_file(files, stem)
        begin = iso_file.extent * SECTOR
        container = bytearray(image[begin : begin + iso_file.size])
        recs = records(container)
        replacement_data: dict[int, tuple[bytes, int]] = {}
        protected_replacement_data: dict[int, tuple[bytes, int]] = {}

        if stem.upper() == "GADAT001" and GADAT001_INDEX_OFFSET in recs:
            original_index_raw, _ = lz.decompress(container, GADAT001_INDEX_OFFSET)
            rebuilt_index_raw, resolved_targets, changed_targets = rebuild_scenario_index(
                original_index_raw, original_scenario, built_scenario
            )
            compressed_index = lz.compress(rebuilt_index_raw)
            original_scenario_index_raw = original_index_raw
            patched_scenario_index_compressed = compressed_index
            decoded_index, consumed_index = lz.decompress(compressed_index)
            if decoded_index != rebuilt_index_raw or consumed_index != len(compressed_index):
                raise SystemExit("scenario index compressor round-trip failed")
            protected_replacement_data[GADAT001_INDEX_OFFSET] = (
                compressed_index,
                len(rebuilt_index_raw),
            )
            print(
                f"rebuilt GADAT001 scenario index: resolved {resolved_targets} targets; "
                f"updated {changed_targets} offsets",
                flush=True,
            )
        total_replacements = len(replacements)
        print(
            f"compressing {stem}: 0/{total_replacements} scenario files",
            flush=True,
        )
        for replacement_number, (offset, original, built) in enumerate(replacements, 1):
            raw = built.read_bytes()
            original_raw = original.read_bytes()
            if len(raw) != len(original_raw):
                raise SystemExit(
                    f"decompressed scenario size changed: {built.name}: "
                    f"{len(raw)} != {len(original_raw)}"
                )
            cache_key = hashlib.sha256(compressor_fingerprint + raw).hexdigest()
            cache_path = cache_dir / f"{cache_key}.bin"
            if cache_path.exists():
                compressed = cache_path.read_bytes()
            else:
                compressed = lz.compress(raw)
                cache_path.write_bytes(compressed)
            decoded, consumed = lz.decompress(compressed)
            if decoded != raw or consumed != len(compressed):
                raise SystemExit(f"compressor round-trip failed: {built.name}")
            old_raw, old_used = lz.decompress(container, offset)
            if old_raw != original_raw:
                raise SystemExit(f"source/container mismatch: {built.name}")
            replacement_data[offset] = (compressed, len(raw))
            if replacement_number % 10 == 0 or replacement_number == total_replacements:
                print(
                    f"compressing {stem}: {replacement_number}/{total_replacements}",
                    flush=True,
                )

        # Preserve the Japanese disc's physical scenario layout exactly.
        # Ordinary cross-group calls such as `>0249->IDS0238` are not safe to
        # treat as normal PIDX-only references: some runtime paths resolve the
        # group resource through its original data slot. Moving/compacting a
        # scenario can therefore enter another event even when local PIDX and
        # IDX.DAT tuples are internally consistent.
        #
        # Keep every existing block at its original offset. A translated block
        # is overwritten in place when it fits its original slot. Only a block
        # that cannot fit is redirected to backing storage, leaving every other
        # Japanese offset untouched.
        rebuilt = bytearray(container)
        offsets = sorted(recs)
        append_cursor = align(len(rebuilt), SECTOR)
        relocated_blocks = 0
        updated_by_record: dict[int, tuple[int, int, int]] = {}
        for old_offset, (compressed, raw_size) in sorted(replacement_data.items()):
            record, _old_raw_size, _old_compressed_size = recs[old_offset]
            next_offsets = [offset for offset in offsets if offset > old_offset]
            slot_end = next_offsets[0] if next_offsets else len(container)
            slot_capacity = slot_end - old_offset

            # Prefer the original physical slot. If normal compression is just
            # over the slot, retry the slower optimal compressor before any
            # relocation. This keeps substantially more scenario groups at the
            # exact Japanese offset without changing translated text.
            if len(compressed) > slot_capacity:
                raw, consumed = lz.decompress(compressed)
                if consumed != len(compressed):
                    raise SystemExit(
                        f"translated scenario cache is invalid at {old_offset:#x}"
                    )
                optimal = lz.compress_optimal(raw)
                decoded, optimal_used = lz.decompress(optimal)
                if decoded != raw or optimal_used != len(optimal):
                    raise SystemExit(
                        f"optimal scenario compressor round-trip failed at {old_offset:#x}"
                    )
                if len(optimal) < len(compressed):
                    compressed = optimal

            if len(compressed) <= slot_capacity:
                rebuilt[old_offset:slot_end] = bytes(slot_capacity)
                rebuilt[old_offset:old_offset + len(compressed)] = compressed
                new_offset = old_offset
            else:
                if stem.upper() == "GADAT001":
                    raise SystemExit(
                        f"GADAT001 original-slot overflow at {old_offset:#x}: "
                        f"{len(compressed)}>{slot_capacity}; refusing to move an "
                        "event-addressable Japanese scenario block"
                    )
                if len(rebuilt) < append_cursor:
                    rebuilt.extend(bytes(append_cursor - len(rebuilt)))
                new_offset = append_cursor
                rebuilt.extend(compressed)
                append_cursor = align(len(rebuilt), SECTOR)
                relocated_blocks += 1

            struct.pack_into(
                "<III", rebuilt, record, new_offset, raw_size, len(compressed)
            )
            updated_by_record[record] = (new_offset, raw_size, len(compressed))

        # The management/table/index area is position-sensitive and therefore
        # never participates in scenario compaction. Replace its index block in
        # its original aligned slot and update both local and central PIDX data.
        for old_offset, (compressed, raw_size) in protected_replacement_data.items():
            record, _old_raw_size, _old_compressed_size = recs[old_offset]
            next_offsets = [offset for offset in offsets if offset > old_offset]
            slot_end = next_offsets[0] if next_offsets else len(container)
            if old_offset + len(compressed) > slot_end:
                raise SystemExit(
                    f"protected scenario index slot overflow: "
                    f"{len(compressed)}>{slot_end - old_offset}"
                )
            rebuilt[old_offset:slot_end] = bytes(slot_end - old_offset)
            rebuilt[old_offset:old_offset + len(compressed)] = compressed
            struct.pack_into(
                "<III", rebuilt, record, old_offset, raw_size, len(compressed)
            )
            updated_by_record[record] = (old_offset, raw_size, len(compressed))
        print(
            f"kept {len(recs) - relocated_blocks}/{len(recs)} PIDX blocks inside the original container; "
            f"relocated {relocated_blocks} oversized translated blocks",
            flush=True,
        )
        required = align(len(rebuilt), SECTOR)
        if required > iso_file.size:
            rebuilt.extend(bytes(required - len(rebuilt)))
            new_begin = align(len(image), SECTOR)
            if len(image) < new_begin:
                image.extend(bytes(new_begin - len(image)))
            new_extent = new_begin // SECTOR
            image.extend(rebuilt)
            if stem.upper() == "GADAT001":
                # The script engine bypasses ISO9660 for cross-group calls and
                # treats GADAT001's original LBA as a fixed container base.
                # Relocating the whole file therefore breaks choices and battle
                # transitions even though ordinary dialogue still loads.
                #
                # Keep that base and the complete local PIDX table in place.
                # Only records that do not fit the original allocation point
                # across the disc to the appended backing copy.  ISO9660 allows
                # overlapping extents; extending this file's logical size makes
                # those seeks valid without shifting any other resource.
                legacy = bytearray(rebuilt[: iso_file.size])
                redirected = 0
                logical_size = iso_file.size
                for record, (record_offset, raw_size, compressed_size) in list(
                    updated_by_record.items()
                ):
                    if record_offset + compressed_size <= iso_file.size:
                        continue
                    backing_offset = new_begin + record_offset - begin
                    if backing_offset + compressed_size > 0xFFFFFFFF:
                        raise SystemExit(
                            f"stable-base GADAT001 offset overflow: {backing_offset:#x}"
                        )
                    struct.pack_into(
                        "<III", legacy, record,
                        backing_offset, raw_size, compressed_size,
                    )
                    updated_by_record[record] = (
                        backing_offset, raw_size, compressed_size
                    )
                    logical_size = max(
                        logical_size, backing_offset + compressed_size
                    )
                    redirected += 1
                image[begin : begin + iso_file.size] = legacy
                patch_iso_file_record(
                    image, iso_file, iso_file.extent, logical_size
                )
                print(
                    f"kept GADAT001 at fixed extent {iso_file.extent}; "
                    f"redirected {redirected} oversized records to backing "
                    f"extent {new_extent}; logical size {logical_size}",
                    flush=True,
                )
            else:
                # Other containers have no known fixed-LBA script path.
                patch_iso_file_record(image, iso_file, new_extent, required)
                print(
                    f"relocated oversized {stem}.DAT: extent "
                    f"{iso_file.extent}->{new_extent}, size "
                    f"{iso_file.size}->{required} (+{required-iso_file.size})",
                    flush=True,
                )
        else:
            rebuilt.extend(bytes(iso_file.size - len(rebuilt)))
            image[begin:begin + iso_file.size] = rebuilt
        idx_file_id, idx_patched = patch_central_idx_records(
            image, files, recs, updated_by_record
        )
        print(
            f"patched {idx_patched} IDX.DAT records for file id {idx_file_id}",
            flush=True,
        )
    if original_speaker_raw is not None and patched_speaker_compressed is not None:
        copies = patch_runtime_speaker_copies(
            image, files, original_speaker_raw, patched_speaker_compressed, lz
        )
        print(f"patched {copies} runtime speaker-table copies", flush=True)
    if original_save_raw is not None and patched_save_compressed is not None:
        copies = patch_runtime_speaker_copies(
            image, files, original_save_raw, patched_save_compressed, lz
        )
        print(f"patched {copies} runtime save-title copies", flush=True)
    if (
        original_scenario_index_raw is not None
        and patched_scenario_index_compressed is not None
    ):
        copies = patch_runtime_scenario_index_copies(
            image, files, original_scenario_index_raw,
            patched_scenario_index_compressed, lz,
        )
        print(f"patched {copies} runtime scenario-index copies", flush=True)
    image.extend(bytes(align(len(image)) - len(image)))
    patch_volume_size(image)
    output_iso.write_bytes(image)
    print(f"built {output_iso} sha256={hashlib.sha256(image).hexdigest()} modified_containers={len(grouped)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--output-iso", type=Path, required=True)
    parser.add_argument("--original-scenario", type=Path, required=True)
    parser.add_argument("--built-scenario", type=Path, required=True)
    parser.add_argument("--patched-elf", type=Path, required=True)
    parser.add_argument("--lz-script", type=Path, required=True)
    parser.add_argument("--patched-speakers", type=Path)
    args = parser.parse_args()
    build(args.original_iso, args.output_iso, args.original_scenario, args.built_scenario,
          args.patched_elf, args.lz_script, args.patched_speakers)


if __name__ == "__main__":
    main()
