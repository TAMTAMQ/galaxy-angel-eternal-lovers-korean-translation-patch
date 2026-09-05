#!/usr/bin/env python3
"""Encode approved Korean ISB translations into fixed-size raw resources."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import galaxy_angel_translation as translation
from eternal_lovers_isb_static import decode_isb_payload, encode_isb_payload
from eternal_lovers_patch_remaining import HALF_SPACE


def encode_tokens(text: str, custom_map: dict[str, bytes]):
    normalized = translation.normalize_display_punctuation(text)
    tokens = []
    for char in normalized:
        if char == "\u3000":
            # The original writes its own indents with the full-width space, and
            # the panel lines keep that indent.  Encoding it as the half-width
            # 0xA0 turns the first character of such a line into one the
            # original never opens a line with.
            tokens.append(FULL_SPACE)
        elif char in " \r\n\t":
            tokens.append(HALF_SPACE)
        else:
            tokens.append(translation.encode_text(char, custom_map))
    return tokens


# The Japanese original fills every fixed slot to the last byte, so trailing
# padding is something only the translation introduces.  Pad with the CP932
# full-width space the game already uses in its own text rather than the
# half-width 0xA0 we use between words: 0xA0 is a byte the original text does
# use, but never as a run at the end of a line, and the renderer stops part-way
# through some lines that end in one.
FULL_SPACE = "　".encode("cp932")


def pad_bytes(count: int) -> bytes:
    """`count` bytes of blank, using whole full-width spaces where they fit."""
    if count <= 0:
        return b""
    pairs, odd = divmod(count, len(FULL_SPACE))
    return FULL_SPACE * pairs + HALF_SPACE * odd


def fit_slots(tokens: list[bytes], lengths: list[int]):
    """Spread the encoded text over every slot the unit owns.

    Each slot is one rendered line of fixed size; the Japanese original fills
    all of them to the last byte.  Greedy packing leaves a *fully* blank
    trailing line whenever the Korean is shorter, and the engine stops
    advancing on such a line - the game freezes on the message before it.  So
    fill each slot up to its even share of what is left instead, which keeps
    every line non-empty as long as there is at least one token per remaining
    slot.
    """
    slots = []
    token_index = 0
    inserted_spaces = 0
    for index, length in enumerate(lengths):
        remaining_bytes = sum(len(token) for token in tokens[token_index:])
        remaining_room = sum(lengths[index:])
        # This slot's share of what is left, in proportion to its own size, so
        # a short trailing line never gets more than it can hold.  -(-a // b) is
        # ceil(a / b), which keeps the shares from rounding down to a shortfall.
        share = -(-remaining_bytes * length // remaining_room) if remaining_room else length
        capacity = min(length, max(share, 2)) if index + 1 < len(lengths) else length
        # No line in the original starts with a space.  One that lands on a slot
        # boundary is an artefact of where the wrap fell, so drop it instead of
        # opening the line with it.
        while token_index < len(tokens) and tokens[token_index] in (HALF_SPACE, FULL_SPACE):
            token_index += 1
        slot = bytearray()
        while token_index < len(tokens) and len(slot) + len(tokens[token_index]) <= capacity:
            slot.extend(tokens[token_index])
            token_index += 1
        if token_index < len(tokens) and len(slot) < length:
            inserted_spaces += length - len(slot)
        slot.extend(pad_bytes(length - len(slot)))
        slots.append(bytes(slot))
    overflow = b"".join(tokens[token_index:])
    return slots, overflow, inserted_spaces


def atomic_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("work/galaxy_angel_eternal_lovers/source/scenario"),
    )
    parser.add_argument(
        "--assets",
        type=Path,
        default=Path("work/galaxy_angel_eternal_lovers/assets/translation/isb"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("work/galaxy_angel_eternal_lovers/build/isb_scenario"),
    )
    parser.add_argument(
        "--encoding-map",
        type=Path,
        default=Path("work/galaxy_angel_eternal_lovers/build/font_map.json"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("work/galaxy_angel_eternal_lovers/build/isb_patch_report.json"),
    )
    parser.add_argument("--allow-overflow", action="store_true")
    args = parser.parse_args()

    custom_map = translation.load_custom_map(args.encoding_map)
    if custom_map is None:
        raise SystemExit("custom font encoding map is required")
    index = json.loads((args.assets / "index.json").read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=True)

    total_applied = 0
    total_units = 0
    overflows = []
    missing_glyphs = []
    file_reports = []
    for entry in index["segments"]:
        segment_path = args.assets / entry["path"]
        segment = json.loads(segment_path.read_text(encoding="utf-8"))
        source_path = args.source_dir / segment["source"]["path"]
        source = source_path.read_bytes()
        expected_hash = segment["source"]["sha256"]
        if hashlib.sha256(source).hexdigest() != expected_hash:
            raise SystemExit(f"source hash mismatch: {source_path}")
        rebuilt = bytearray(source)
        applied = 0
        for unit in segment["units"]:
            total_units += 1
            if not unit.get("use_translation") or not unit.get("translation"):
                continue
            rendered = " ".join(unit["translation"].rstrip("\r\n").splitlines())
            try:
                tokens = encode_tokens(rendered, custom_map)
            except UnicodeEncodeError as error:
                missing_glyphs.append({"id": unit["id"], "error": str(error)})
                continue
            lengths = [int(item["length"]) for item in unit["storage"]]
            slots, overflow, inserted = fit_slots(tokens, lengths)
            if overflow:
                overflows.append(
                    {
                        "id": unit["id"],
                        "capacity": sum(lengths),
                        "encoded_length": sum(map(len, tokens)),
                        "overflow_hex": overflow.hex(),
                    }
                )
                continue
            for storage, plain in zip(unit["storage"], slots, strict=True):
                offset = int(storage["offset"])
                key = int(storage["key"])
                encoded = encode_isb_payload(plain, key)
                payload_offset = offset + 8
                rebuilt[payload_offset : payload_offset + len(encoded)] = encoded
                check = decode_isb_payload(rebuilt, payload_offset, len(plain), key)
                if check != plain:
                    raise AssertionError(f"ISB round trip failed: {unit['id']}")
            applied += 1
            total_applied += 1

        if applied:
            target = args.output_dir / source_path.name
            target.write_bytes(rebuilt)
            file_reports.append(
                {
                    "file": source_path.name,
                    "applied_units": applied,
                    "output_sha256": hashlib.sha256(rebuilt).hexdigest(),
                }
            )
            print(f"{source_path.name}: applied={applied}", flush=True)

    report = {
        "schema": "eternal-lovers-isb-patch/v1",
        "translation_units": total_units,
        "applied_units": total_applied,
        "overflow_units": len(overflows),
        "missing_glyph_units": len(missing_glyphs),
        "files": file_reports,
        "overflows": overflows,
        "missing_glyphs": missing_glyphs,
    }
    atomic_json(args.report, report)
    print(
        f"applied={total_applied} overflows={len(overflows)} "
        f"missing_glyphs={len(missing_glyphs)} "
        f"files={len(file_reports)} report={args.report}",
        flush=True,
    )
    if (overflows or missing_glyphs) and not args.allow_overflow:
        raise SystemExit("ISB translations are not buildable; see report")


if __name__ == "__main__":
    main()
