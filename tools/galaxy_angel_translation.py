#!/usr/bin/env python3
"""Export, validate, and apply Galaxy Angel scenario translation segments."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path


DIALOGUE_RE = re.compile(r"(?m)^@(\d+)\(\r?\n(.*?)^@@\)\s*$", re.DOTALL)
CONTEXT_RE = re.compile(r"(?m)^\\(Speaker|Face|Voice)\(([^\r\n]*)\)\s*$")
VALID_STATES = {"untranslated", "draft", "review", "needs_human", "complete"}
RAW_TOKEN_RE = re.compile(r"⟦RAW:([0-9A-F]{2})⟧")
IDS_ANCHOR_BYTES_RE = re.compile(rb"(?m)^<\d+->IDS\d+\r?\n")
JUMP_ANCHOR_BYTES_RE = re.compile(
    rb"(?m)^(?::L\d+|<\d+->IDS\d+)\r?\n"
)
SELECTION_BLOCK_RE = re.compile(r"(?ms)^@\(ss\r?\n.*?^@\)ss(?=\r?$)")
MAX_FULLWIDTH_COLUMNS = 44
FORBIDDEN_LINE_START = frozenset(
    ",)]}\u3001\uff0c\u3009\u300b\u300d\u300f\u3011\u3015\u3017\u3019\u301b\u2019\u201d"
)


def display_columns(line: str) -> int:
    return sum(1 if ord(char) < 0x80 else 2 for char in line)


def reflow_translation_layout(text: str) -> str:
    """Rewrap only over-wide dialogue lines without changing wording.

    Existing line breaks are preserved unless a line exceeds the game's
    44-column text box. Overflow is moved forward at the last whitespace that
    still fits, cascading into the following line as needed. This keeps the
    translation text intact while preventing layout-only validation failures.
    """
    terminal = "\n" if text.endswith("\n") else ""
    body = text.rstrip("\n")
    if not body:
        return terminal
    lines = body.split("\n")
    if all(display_columns(line) <= MAX_FULLWIDTH_COLUMNS for line in lines):
        return text

    index = 0
    while index < len(lines):
        while display_columns(lines[index]) > MAX_FULLWIDTH_COLUMNS:
            line = lines[index]
            split_at: int | None = None
            for pos, char in enumerate(line):
                if display_columns(line[: pos + 1]) > MAX_FULLWIDTH_COLUMNS:
                    break
                if char == " ":
                    split_at = pos

            if split_at is None:
                split_at = 0
                while (
                    split_at < len(line)
                    and display_columns(line[: split_at + 1]) <= MAX_FULLWIDTH_COLUMNS
                ):
                    split_at += 1
                left = line[:split_at]
                overflow = line[split_at:]
            else:
                left = line[:split_at].rstrip()
                overflow = line[split_at + 1 :].lstrip()

            lines[index] = left
            if index + 1 < len(lines):
                following = lines[index + 1].lstrip()
                if overflow and following:
                    lines[index + 1] = overflow + " " + following
                else:
                    lines[index + 1] = overflow or following
            else:
                lines.append(overflow)
        index += 1

    return "\n".join(lines) + terminal


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_scenario(path: Path) -> tuple[bytes, str]:
    raw = path.read_bytes()
    decoded = raw.decode("cp932", errors="surrogateescape")
    visible = "".join(
        f"⟦RAW:{ord(char) - 0xDC00:02X}⟧" if 0xDC80 <= ord(char) <= 0xDCFF else char
        for char in decoded
    )
    return raw, visible


def normalize_display_punctuation(text: str) -> str:
    """Normalize visible punctuation to glyphs used by the Japanese renderer."""
    return text.replace("~", "～")


def encode_scenario(text: str, custom_map: dict[str, bytes] | None = None) -> bytes:
    chunks = []
    cursor = 0
    for match in RAW_TOKEN_RE.finditer(text):
        chunks.append(encode_text(text[cursor : match.start()], custom_map))
        chunks.append(bytes([int(match.group(1), 16)]))
        cursor = match.end()
    chunks.append(encode_text(text[cursor:], custom_map))
    return b"".join(chunks)


def encode_text(text: str, custom_map: dict[str, bytes] | None) -> bytes:
    output = bytearray()
    for char in text:
        if custom_map and char in custom_map:
            output.extend(custom_map[char])
        else:
            output.extend(char.encode("cp932"))
    return bytes(output)


def load_custom_map(path: Path | None) -> dict[str, bytes] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {char: bytes.fromhex(item["hex"]) for char, item in payload["characters"].items()}


def replace_selection_rows(
    text: str,
    translations: dict[str, str],
    custom_map: dict[str, bytes] | None,
) -> str:
    """Translate only CRLF-delimited selection rows."""
    if not translations:
        return text

    def replace_block(match: re.Match[str]) -> str:
        rows = match.group(0).splitlines(keepends=True)
        output: list[str] = []
        for index, row in enumerate(rows):
            ending = ""
            content = row
            if content.endswith("\r\n"):
                content, ending = content[:-2], "\r\n"
            elif content.endswith("\n"):
                content, ending = content[:-1], "\n"
            # The first/last records are structural markers, never text.
            if index == 0 or content == "@)ss":
                output.append(row)
                continue
            translated = translations.get(content)
            if translated is None:
                output.append(row)
                continue
            original_size = len(content.encode("cp932"))
            translated_size = len(encode_text(translated, custom_map))
            if translated_size > original_size:
                raise SystemExit(
                    "translated selection exceeds original byte slot: "
                    f"{content!r}: {translated_size}>{original_size}"
                )
            # Keep the selection record's original byte size. The handler
            # copies at most 34 bytes from this CRLF-delimited row; trailing
            # blanks stay within that option and do not alter marker syntax.
            output.append(translated + (" " * (original_size - translated_size)) + ending)
        return "".join(output)

    return SELECTION_BLOCK_RE.sub(replace_block, text)


def preserve_ids_anchor_offsets(original: bytes, rebuilt: bytes, unit_name: str) -> tuple[bytes, int]:
    """Keep selection/branch IDS anchors at their original byte offsets."""
    original_anchors = [(match.group(0), match.start()) for match in JUMP_ANCHOR_BYTES_RE.finditer(original)]
    if not original_anchors:
        return rebuilt, 0
    inserted = 0
    search_from = 0
    previous_anchor_end = 0
    output = bytearray(rebuilt)
    for marker, wanted in original_anchors:
        found = output.find(marker, search_from)
        if found < 0:
            raise SystemExit(f"IDS anchor missing after translation: {unit_name}: {marker!r}")
        if found > wanted:
            excess = found - wanted
            region = bytes(output[previous_anchor_end:found])
            visible_pattern = re.compile(
                rb"(?ms)^@\d+\(\r\n(.*?)^@@\)|^@\(ss\r\n(.*?)^@\)ss"
            )
            narrow = {
                "！".encode("cp932"): b"!",
                "？".encode("cp932"): b"?",
                "，".encode("cp932"): b",",
                "．".encode("cp932"): b".",
                "　".encode("cp932"): b" ",
                "…".encode("cp932"): b".",
                "、".encode("cp932"): b",",
                "。".encode("cp932"): b".",
            }
            candidates: list[tuple[int, int, bytes]] = []
            for visible in visible_pattern.finditer(region):
                for group_number in (1, 2):
                    content = visible.group(group_number)
                    if content is None:
                        continue
                    content_start = previous_anchor_end + visible.start(group_number)
                    for old, new in narrow.items():
                        cursor = 0
                        while True:
                            position = content.find(old, cursor)
                            if position < 0:
                                break
                            candidates.append((content_start + position, len(old), new))
                            cursor = position + len(old)
                    for space in re.finditer(rb" ", content):
                        candidates.append((content_start + space.start(), 1, b""))
                    for punctuation in re.finditer(rb"[,!?.]", content):
                        candidates.append((content_start + punctuation.start(), 1, b""))
            if len(candidates) < excess:
                raise SystemExit(
                    f"cannot safely shorten anchor interval: {unit_name}: "
                    f"{marker!r} need {excess}, have {len(candidates)}"
                )
            for position, old_size, replacement in sorted(candidates, reverse=True)[:excess]:
                output[position:position + old_size] = replacement
            found = wanted
        if found < wanted:
            padding = wanted - found
            # Preserve jump-target byte offsets without creating extra script
            # records. Padding must not alter any command record because some
            # handlers depend on raw argument text as well as its numeric value.
            region = bytes(output[previous_anchor_end:found])
            allocations: list[tuple[int, int, bytes]] = []
            remaining = padding
            # Structural records (`@@)`, selection markers, and commands) must
            # remain exact lines. Spread the offset compensation over ordinary
            # dialogue rows, with a modest cap per physical line.
            row_positions: list[tuple[int, int]] = []
            for dialogue in re.finditer(rb"(?ms)^@\d+\(\r?\n(.*?)^@@\)(?=\r?$)", region):
                body = dialogue.group(1)
                body_start = previous_anchor_end + dialogue.start(1)
                for row in re.finditer(rb"(?m)^([^\r\n]*)(?=\r?$)", body):
                    line = row.group(1)
                    if not line:
                        continue
                    capacity = max(0, 120 - len(line))
                    if capacity:
                        row_positions.append((body_start + row.end(1), capacity))

            assigned = [0] * len(row_positions)
            while remaining and row_positions:
                progressed = False
                for index, (_, capacity) in enumerate(row_positions):
                    if assigned[index] >= capacity:
                        continue
                    assigned[index] += 1
                    remaining -= 1
                    progressed = True
                    if not remaining:
                        break
                if not progressed:
                    break
            for (position, _), amount in zip(row_positions, assigned, strict=True):
                if amount:
                    allocations.append((position, amount, b" "))
            if remaining:
                raise SystemExit(
                    f"cannot invisibly pad anchor interval: {unit_name}: "
                    f"{marker!r} wanted={wanted} found={found} "
                    f"previous_end={previous_anchor_end} need {padding}, "
                    f"remaining {remaining}"
                )
            for position, amount, fill in sorted(allocations, reverse=True):
                output[position:position] = fill * amount
            inserted += padding
            found = wanted
        search_from = found + len(marker)
        previous_anchor_end = search_from
    return bytes(output), inserted


def nearby_context(text: str, start: int) -> dict[str, str]:
    window = text[max(0, start - 1024) : start]
    found: dict[str, str] = {}
    for match in CONTEXT_RE.finditer(window):
        found[match.group(1).lower()] = match.group(2)
    return found


def export(source_dir: Path, output_dir: Path) -> None:
    segment_dir = output_dir / "segments"
    segment_dir.mkdir(parents=True, exist_ok=True)
    index_segments = []
    population = []
    total = 0

    for path in sorted(source_dir.glob("*.txt")):
        raw, text = read_scenario(path)
        units = []
        for ordinal, match in enumerate(DIALOGUE_RE.finditer(text), 1):
            original = match.group(2).replace("\r\n", "\n")
            unit_id = f"{path.stem}:dialogue:{ordinal:04d}"
            unit = {
                "id": unit_id,
                "channel": int(match.group(1)),
                "context": nearby_context(text, match.start()),
                "original": original,
                "translation": "",
                "state": "untranslated",
                "use_translation": False,
            }
            units.append(unit)
            population.append(unit_id)
        if not units:
            continue
        relative = f"segments/{path.stem}.json"
        payload = {
            "schema": "galaxy-angel-translation-segment/v1",
            "source": {
                "path": path.name,
                "encoding": "cp932",
                "sha256": sha256(raw),
                "dialogue_count": len(units),
            },
            "units": units,
        }
        (output_dir / relative).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        index_segments.append({"path": relative, "source": path.name, "units": len(units)})
        total += len(units)

    population_hash = sha256(("\n".join(population) + "\n").encode())
    index = {
        "schema": "galaxy-angel-translation-index/v1",
        "game": "Galaxy Angel (Japan) [PS2]",
        "source_encoding": "cp932",
        "target_language": "ko",
        "scope": "all @N(...@@) scenario dialogue blocks in the supplied 271-file corpus",
        "population": {"dialogue_units": total, "id_sha256": population_hash},
        "release_approval": None,
        "segments": index_segments,
    }
    (output_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"exported {total} dialogue units in {len(index_segments)} segments")


def load_index(asset_dir: Path) -> dict:
    return json.loads((asset_dir / "index.json").read_text(encoding="utf-8"))


def validate(source_dir: Path, asset_dir: Path) -> list[tuple[dict, dict]]:
    index = load_index(asset_dir)
    loaded = []
    ids = []
    for item in index["segments"]:
        segment = json.loads((asset_dir / item["path"]).read_text(encoding="utf-8"))
        source_path = source_dir / segment["source"]["path"]
        raw, text = read_scenario(source_path)
        if sha256(raw) != segment["source"]["sha256"]:
            raise SystemExit(f"source hash mismatch: {source_path}")
        matches = list(DIALOGUE_RE.finditer(text))
        units = segment["units"]
        if len(matches) != len(units) or len(units) != item["units"]:
            raise SystemExit(f"dialogue count mismatch: {source_path}")
        for unit, match in zip(units, matches, strict=True):
            ids.append(unit["id"])
            original = match.group(2).replace("\r\n", "\n")
            if unit["original"] != original:
                raise SystemExit(f"protected original mismatch: {unit['id']}")
            if unit["state"] not in VALID_STATES:
                raise SystemExit(f"invalid state: {unit['id']}: {unit['state']}")
            if unit["use_translation"] and not unit["translation"]:
                raise SystemExit(f"enabled translation is empty: {unit['id']}")
            if "\r" in unit["translation"]:
                raise SystemExit(f"translation must use LF inside JSON: {unit['id']}")
            if unit["translation"] and not unit["translation"].endswith("\n"):
                raise SystemExit(f"translation must end with LF: {unit['id']}")
            if unit["use_translation"] and re.search(r"[가-힣]～+っ", unit["translation"]):
                raise SystemExit(
                    f"small-tsu after wave dash must be rendered as a Korean "
                    f"final consonant: {unit['id']}"
                )
            if unit["use_translation"]:
                unit["translation"] = normalize_display_punctuation(unit["translation"])
                if int(unit.get("channel", 0)) != 0:
                    unit["translation"] = reflow_translation_layout(unit["translation"])
                visible_lines = unit["translation"].rstrip("\n").split("\n")
                if int(unit.get("channel", 0)) != 0 and len(visible_lines) > 3:
                    raise SystemExit(
                        f"dialogue has too many lines: {unit['id']}: "
                        f"{len(visible_lines)} > 3"
                    )
                for line_number, line in enumerate(visible_lines, 1):
                    if not line:
                        # The Japanese never leaves a display line empty: where a line is
                        # "blank" it still holds a full-width space, and the renderer needs
                        # that character.  A line with nothing in it stops the text advancing.
                        raise SystemExit(
                            f"translation has an empty display line: {unit['id']} "
                            f"line {line_number}"
                        )
                    if line_number > 1 and line and line[0] in FORBIDDEN_LINE_START:
                        raise SystemExit(
                            f"translation line starts with punctuation: {unit['id']} "
                            f"line {line_number}: {line[0]}"
                        )
                    columns = display_columns(line)
                    if columns > MAX_FULLWIDTH_COLUMNS:
                        raise SystemExit(
                            f"translation line too wide: {unit['id']} line {line_number}: "
                            f"{columns} > {MAX_FULLWIDTH_COLUMNS} columns"
                        )
            original_tokens = RAW_TOKEN_RE.findall(unit["original"])
            translated_tokens = RAW_TOKEN_RE.findall(unit["translation"])
            if unit["use_translation"] and original_tokens != translated_tokens:
                raise SystemExit(f"protected RAW tokens changed: {unit['id']}")
        loaded.append((segment, {"path": source_path, "text": text, "matches": matches}))
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate stable IDs")
    expected = index["population"]
    if len(ids) != expected["dialogue_units"]:
        raise SystemExit("population count mismatch")
    if sha256(("\n".join(ids) + "\n").encode()) != expected["id_sha256"]:
        raise SystemExit("population ID hash mismatch")
    print(f"validated {len(ids)} dialogue units")
    return loaded


def apply(source_dir: Path, asset_dir: Path, output_dir: Path, map_path: Path | None = None) -> None:
    loaded = validate(source_dir, asset_dir)
    custom_map = load_custom_map(map_path)
    selection_path = asset_dir / "selection_translations.json"
    selection_units_path = asset_dir / "selection_units.json"
    selection_translations = {}
    if selection_path.exists():
        selection_translations = {
            key: normalize_display_punctuation(value)
            for key, value in json.loads(
                selection_path.read_text(encoding="utf-8")
            )["translations"].items()
        }
    elif selection_units_path.exists():
        selection_payload = json.loads(selection_units_path.read_text(encoding="utf-8"))
        selection_translations = {
            unit["original"]: normalize_display_punctuation(unit["translation"])
            for unit in selection_payload["units"]
            if unit.get("use_translation") and unit.get("translation")
        }
    if os.environ.get("GA_KEEP_SELECTIONS_ORIGINAL") == "1":
        selection_translations = {}
    output_dir.mkdir(parents=True, exist_ok=True)
    applied = 0
    padded = 0
    selection_only_files = 0
    written_names: set[str] = set()
    for segment, source in loaded:
        text = source["text"]
        units = segment["units"]
        cursor = 0
        pieces = []
        for unit, match in zip(units, source["matches"], strict=True):
            pieces.append(text[cursor : match.start(2)])
            selected = unit["translation"] if unit["use_translation"] else unit["original"]
            pieces.append(selected.replace("\n", "\r\n"))
            cursor = match.end(2)
            applied += int(unit["use_translation"])
        pieces.append(text[cursor:])
        rebuilt_text = "".join(pieces)
        rebuilt_text = replace_selection_rows(
            rebuilt_text, selection_translations, custom_map
        )
        rebuilt = encode_scenario(rebuilt_text, custom_map)
        # Do not pad dialogue or alter commands to retain Japanese byte
        # positions. galaxy_angel_build.py regenerates the separate IDS/:L
        # target index from these translated bytes instead.
        source_size = source["path"].stat().st_size
        if any(unit["use_translation"] for unit in units) and len(rebuilt) > source_size:
            delta = len(rebuilt) - source_size
            raise SystemExit(
                f"translated scenario size changed: {segment['source']['path']}: "
                f"{len(rebuilt)} != {source_size} (delta {delta:+d}); "
                "this engine requires the original decompressed block size"
            )
        if len(rebuilt) < source_size:
            # Scenario files terminate with the `}/` marker.  Bytes after that
            # marker are outside the parsed script, so use lexer-safe ASCII
            # whitespace as fixed-size allocation padding.
            padding = source_size - len(rebuilt)
            rebuilt += b" " * padding
            padded += padding
        output_name = segment["source"]["path"]
        (output_dir / output_name).write_bytes(rebuilt)
        written_names.add(output_name)

    # Some Moonlit Lovers scenario leaves contain only @(ss ... @)ss choices
    # and no @N(...@@) dialogue at all.  Such files do not have translation
    # segment JSONs, so the loop above never sees them.  Still apply selection
    # translations to every remaining source file; unchanged files need no
    # build override, while changed selection-only files are emitted here.
    if selection_translations:
        for path in sorted(source_dir.glob("*.txt")):
            if path.name in written_names:
                continue
            _raw, source_text = read_scenario(path)
            rebuilt_text = replace_selection_rows(
                source_text, selection_translations, custom_map
            )
            if rebuilt_text == source_text:
                continue
            rebuilt = encode_scenario(rebuilt_text, custom_map)
            source_size = path.stat().st_size
            if len(rebuilt) != source_size:
                raise SystemExit(
                    f"selection-only scenario size changed: {path.name}: "
                    f"{len(rebuilt)} != {source_size}"
                )
            (output_dir / path.name).write_bytes(rebuilt)
            selection_only_files += 1

    print(
        f"rebuilt {len(loaded)} files; applied {applied} translated units; "
        f"selection-only files {selection_only_files}; padded {padded} bytes; "
        "scenario target offsets handled by ISO builder"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("export", "validate", "apply"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--source", type=Path, required=True)
        cmd.add_argument("--assets", type=Path, required=True)
        if name == "apply":
            cmd.add_argument("--output", type=Path, required=True)
            cmd.add_argument("--encoding-map", type=Path)
    args = parser.parse_args()
    if args.command == "export":
        export(args.source, args.assets)
    elif args.command == "validate":
        validate(args.source, args.assets)
    else:
        apply(args.source, args.assets, args.output, args.encoding_map)


if __name__ == "__main__":
    main()
