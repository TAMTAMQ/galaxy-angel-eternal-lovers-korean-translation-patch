#!/usr/bin/env python3
"""Encode approved Korean ISB translations into fixed-size raw resources."""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
from functools import lru_cache
from pathlib import Path

import galaxy_angel_translation as translation
from eternal_lovers_isb_static import STRING_TAG, decode_isb_payload, encode_isb_payload
from eternal_lovers_patch_remaining import HALF_SPACE
from eternal_lovers_regroup_isb import TITLE_TOKEN, statement_offsets


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


def _exact_fit_slots(tokens: list[bytes], lengths: list[int]):
    """Find a feasible balanced split when the proportional fast path misses one.

    Word gaps that land exactly on a slot boundary may be dropped, matching the
    normal fitter. Every slot must still contain at least one real token because
    a fully blank trailing slot can freeze the original engine.
    """
    spaces = (HALF_SPACE, FULL_SPACE)
    punctuation_breaks = (b",", b".", b"!", b"?", b";", b":")
    token_count = len(tokens)
    slot_count = len(lengths)
    suffix_bytes = [0] * (token_count + 1)
    suffix_non_space = [0] * (token_count + 1)
    for index in range(token_count - 1, -1, -1):
        suffix_bytes[index] = suffix_bytes[index + 1] + len(tokens[index])
        suffix_non_space[index] = suffix_non_space[index + 1] + (
            0 if tokens[index] in spaces else 1
        )

    @lru_cache(maxsize=None)
    def solve(slot_index: int, token_index: int):
        while token_index < token_count and tokens[token_index] in spaces:
            token_index += 1
        if slot_index == slot_count:
            return (0, ()) if token_index == token_count else None
        if token_index >= token_count:
            return None

        remaining_slots = slot_count - slot_index
        if suffix_non_space[token_index] < remaining_slots:
            return None

        remaining_room = sum(lengths[slot_index:])
        share = (
            -(-suffix_bytes[token_index] * lengths[slot_index] // remaining_room)
            if remaining_room
            else lengths[slot_index]
        )
        used = 0
        candidates = []
        for end in range(token_index + 1, token_count + 1):
            used += len(tokens[end - 1])
            if used > lengths[slot_index]:
                break
            future = solve(slot_index + 1, end)
            if future is None:
                continue
            future_cost, future_ends = future
            if slot_index + 1 >= slot_count:
                break_penalty = 0
            elif end < token_count and tokens[end] in spaces:
                # Match the first game's wrapper: prefer an eojeol boundary.
                break_penalty = 0
            elif tokens[end - 1] in punctuation_breaks:
                break_penalty = 25
            else:
                # Splitting inside a Korean eojeol is a last resort only.
                break_penalty = 1000
            cost = (used - share) ** 2 + future_cost + break_penalty
            candidates.append((cost, end, future_ends))
        if not candidates:
            return None
        cost, end, future_ends = min(
            candidates, key=lambda item: (item[0], item[1])
        )
        return cost, (end,) + future_ends

    solved = solve(0, 0)
    if solved is None:
        return None
    _cost, ends = solved
    slots = []
    token_index = 0
    inserted_spaces = 0
    for slot_index, end in enumerate(ends):
        while token_index < token_count and tokens[token_index] in spaces:
            token_index += 1
        slot = bytearray(b"".join(tokens[token_index:end]))
        length = lengths[slot_index]
        if end < token_count and len(slot) < length:
            inserted_spaces += length - len(slot)
        slot.extend(pad_bytes(length - len(slot)))
        slots.append(bytes(slot))
        token_index = end
    while token_index < token_count and tokens[token_index] in spaces:
        token_index += 1
    overflow = b"".join(tokens[token_index:])
    return slots, overflow, inserted_spaces


def fit_slots(tokens: list[bytes], lengths: list[int]):
    """Spread encoded text over every rendered slot using GA-style breaks.

    Eternal Lovers still requires every original slot to contain real text;
    leaving a trailing slot fully blank can freeze the engine.  Within that
    fixed-slot constraint, use the first game's line-break priorities: prefer
    whitespace, then punctuation, and split an eojeol only when no natural
    boundary fits.  The dynamic fitter also keeps the rows visually balanced.
    """
    balanced = _exact_fit_slots(tokens, lengths)
    if balanced is not None and not balanced[1]:
        return balanced

    # Defensive fallback for unusual records that the balanced solver cannot
    # satisfy.  This retains the older proportional packing semantics.
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
    if overflow and sum(len(token) for token in tokens) <= sum(lengths):
        exact = _exact_fit_slots(tokens, lengths)
        if exact is not None and not exact[1]:
            return exact
    return slots, overflow, inserted_spaces


# Eternal Lovers' message box is 40 columns (the Japanese is pre-wrapped at 20
# full-width characters); keep the first game's one-column margin for text.
RELAYOUT_TEXT_COLUMNS = 39
RELAYOUT_ROW_BYTES = 40


def align4(value: int) -> int:
    return (value + 3) & ~3


def break_classes(custom_map: dict[str, bytes]):
    def enc(chars: str) -> frozenset[bytes]:
        out = set()
        for char in chars:
            try:
                out.add(translation.encode_text(char, custom_map))
            except UnicodeEncodeError:
                pass
        return frozenset(out)

    punctuation = enc(",.!?;:…。！？、，．")
    forbidden_start = enc("".join(translation.FORBIDDEN_LINE_START))
    ellipsis = translation.encode_text("…", custom_map)
    return punctuation, forbidden_start, ellipsis


def count_mid_breaks(tokens: list[bytes], ends: list[int], classes) -> int:
    """Row boundaries that fall inside a word (not at a gap or after punctuation)."""
    punctuation, _forbidden, ellipsis = classes
    spaces = (HALF_SPACE, FULL_SPACE)
    mids = 0
    for end in ends[:-1]:
        if end >= len(tokens) or end == 0:
            continue
        if tokens[end] in spaces:
            continue
        if tokens[end - 1] in punctuation and not (
            tokens[end - 1] == ellipsis and tokens[end] == ellipsis
        ):
            continue
        mids += 1
    return mids


def relayout_rows(tokens: list[bytes], lengths: list[int], classes):
    """Re-split one message across its rows at word boundaries, GA-style.

    The rows of a message are consecutive string records, so their byte budget can
    move between rows as long as the aligned total stays identical: nothing after
    the message shifts.  Every row keeps real text (a blank row freezes the engine)
    and stays within the 40-column box.  Returns (row_plains, row_lengths, ends) or
    None when the text cannot be laid out that way.
    """
    punctuation, forbidden_start, ellipsis = classes
    spaces = (HALF_SPACE, FULL_SPACE)
    rows = len(lengths)
    budget = sum(align4(length) for length in lengths)
    count = len(tokens)
    width = RELAYOUT_TEXT_COLUMNS

    @lru_cache(maxsize=None)
    def solve(row: int, start: int):
        while start < count and tokens[start] in spaces:
            start += 1
        if row == rows:
            return (0, ()) if start == count else None
        if start >= count:
            return None
        best = None
        used = 0
        real = False
        for end in range(start + 1, count + 1):
            token = tokens[end - 1]
            used += len(token)
            if used > width:
                break
            real = real or token not in spaces
            if not real or token in spaces:
                continue
            next_start = end
            while next_start < count and tokens[next_start] in spaces:
                next_start += 1
            if row + 1 < rows:
                if next_start >= count:
                    continue
                if tokens[next_start] in forbidden_start:
                    continue
                if next_start == end and token == ellipsis and tokens[end] == ellipsis:
                    continue
            elif next_start < count:
                continue
            tail = solve(row + 1, next_start)
            if tail is None:
                continue
            if row + 1 >= rows or (end < count and tokens[end] in spaces):
                penalty = 0
            elif token in punctuation:
                penalty = 25
            else:
                penalty = 1000
            cost = tail[0] + (width - used) ** 2 + penalty
            if best is None or cost < best[0]:
                best = (cost, (end,) + tail[1])
        return best

    solved = solve(0, 0)
    if solved is None:
        return None
    ends = list(solved[1])

    texts = []
    start = 0
    for end in ends:
        while start < end and tokens[start] in spaces:
            start += 1
        texts.append(b"".join(tokens[start:end]))
        start = end

    # Aligned targets: each row at least its text, rows padded with blanks up to
    # the box width so that the aligned total equals the original budget exactly.
    targets = [align4(len(text)) for text in texts]
    extra = budget - sum(targets)
    if extra < 0 or extra % 4:
        return None
    while extra:
        candidates = [i for i in range(rows) if targets[i] + 4 <= RELAYOUT_ROW_BYTES]
        if not candidates:
            return None
        pick = min(candidates, key=lambda i: (targets[i], i))
        targets[pick] += 4
        extra -= 4

    plains = []
    row_lengths = []
    for text, target in zip(texts, targets):
        pad = target - len(text)
        if pad % 2:
            # An odd blank would end the row on a lone 0xA0; leave that byte to
            # the compiler's zero alignment instead.
            pad -= 1
        plain = text + pad_bytes(pad)
        plains.append(plain)
        row_lengths.append(len(plain))
    return plains, row_lengths, ends


def slot_ends(tokens: list[bytes], slots: list[bytes]) -> list[int]:
    """Token index where each fixed slot's text ends (inverse of fit_slots)."""
    spaces = (HALF_SPACE, FULL_SPACE)
    blank = set(HALF_SPACE + FULL_SPACE)
    index = 0
    ends = []
    for slot in slots:
        while index < len(tokens) and tokens[index] in spaces:
            index += 1
        used = 0
        while index < len(tokens) and slot.startswith(tokens[index], used):
            rest = slot[used + len(tokens[index]):]
            if tokens[index] in spaces and all(byte in blank for byte in rest):
                break
            used += len(tokens[index])
            index += 1
        ends.append(index)
    return ends


def rows_contiguous(storage: list[dict]) -> bool:
    keys = {int(item["key"]) for item in storage}
    if len(keys) != 1:
        return False
    for current, following in zip(storage, storage[1:]):
        if int(following["offset"]) != int(current["offset"]) + 8 + align4(int(current["length"])):
            return False
    return True


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
    title_units = 0
    title_units_with_spaces = 0
    title_half_space_bytes = 0
    title_ascii_space_violations = []
    overflows = []
    missing_glyphs = []
    file_reports = []
    classes = break_classes(custom_map)
    relayout_units = 0
    relayout_mid_before = 0
    relayout_mid_after = 0
    for entry in index["segments"]:
        segment_path = args.assets / entry["path"]
        segment = json.loads(segment_path.read_text(encoding="utf-8"))
        source_path = args.source_dir / segment["source"]["path"]
        source = source_path.read_bytes()
        expected_hash = segment["source"]["sha256"]
        if hashlib.sha256(source).hexdigest() != expected_hash:
            raise SystemExit(f"source hash mismatch: {source_path}")
        rebuilt = bytearray(source)
        statement_index = statement_offsets(source)
        applied = 0
        for unit in segment["units"]:
            total_units += 1
            if not unit.get("use_translation") or not unit.get("translation"):
                continue
            rendered = " ".join(unit["translation"].rstrip("\r\n").splitlines())
            storage_offset = int(unit["storage"][0]["offset"])
            is_title = False
            if statement_index:
                statement = bisect.bisect_right(statement_index, storage_offset) - 1
                if statement >= 0:
                    token = source[statement_index[statement] + 4 : statement_index[statement] + 8].hex()
                    is_title = token == TITLE_TOKEN
            try:
                tokens = encode_tokens(rendered, custom_map)
            except UnicodeEncodeError as error:
                missing_glyphs.append({"id": unit["id"], "error": str(error)})
                continue
            if is_title:
                title_units += 1
                title_units_with_spaces += int(" " in rendered)
                title_half_space_bytes += sum(token.count(HALF_SPACE) for token in tokens)
                encoded_title = b"".join(tokens)
                if b" " in encoded_title:
                    title_ascii_space_violations.append(
                        {"id": unit["id"], "translation": rendered}
                    )
            lengths = [int(item["length"]) for item in unit["storage"]]
            slots, overflow, inserted = fit_slots(tokens, lengths)

            relaid = None
            if len(lengths) >= 2 and not is_title and rows_contiguous(unit["storage"]):
                relaid = relayout_rows(tokens, lengths, classes)
                if relaid is not None:
                    old_mid = (
                        count_mid_breaks(tokens, slot_ends(tokens, slots), classes)
                        if not overflow else 10 ** 9
                    )
                    new_mid = count_mid_breaks(tokens, relaid[2], classes)
                    if new_mid > old_mid:
                        relaid = None
                    else:
                        relayout_mid_before += min(old_mid, len(lengths))
                        relayout_mid_after += new_mid
            if relaid is not None:
                plains, row_lengths, _ends = relaid
                key = int(unit["storage"][0]["key"])
                start = int(unit["storage"][0]["offset"])
                last = unit["storage"][-1]
                region_end = int(last["offset"]) + 8 + align4(int(last["length"]))
                cursor = start
                for plain, length in zip(plains, row_lengths, strict=True):
                    encoded = encode_isb_payload(plain, key)
                    rebuilt[cursor:cursor + 4] = STRING_TAG
                    rebuilt[cursor + 4:cursor + 8] = length.to_bytes(4, "little")
                    rebuilt[cursor + 8:cursor + 8 + len(encoded)] = encoded
                    if decode_isb_payload(rebuilt, cursor + 8, length, key) != plain:
                        raise AssertionError(f"ISB relayout round trip failed: {unit['id']}")
                    cursor += 8 + len(encoded)
                if cursor != region_end:
                    raise AssertionError(
                        f"ISB relayout changed the message size: {unit['id']} {cursor} != {region_end}"
                    )
                relayout_units += 1
                applied += 1
                total_applied += 1
                continue

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
        "title_units": title_units,
        "title_units_with_spaces": title_units_with_spaces,
        "title_half_space_bytes": title_half_space_bytes,
        "title_ascii_space_violations": title_ascii_space_violations,
        "relayout_units": relayout_units,
        "relayout_mid_word_breaks_before": relayout_mid_before,
        "relayout_mid_word_breaks_after": relayout_mid_after,
        "overflow_units": len(overflows),
        "missing_glyph_units": len(missing_glyphs),
        "files": file_reports,
        "overflows": overflows,
        "missing_glyphs": missing_glyphs,
    }
    atomic_json(args.report, report)
    print(
        f"applied={total_applied} relayout={relayout_units} "
        f"mid_word_breaks {relayout_mid_before}->{relayout_mid_after} "
        f"overflows={len(overflows)} "
        f"missing_glyphs={len(missing_glyphs)} "
        f"files={len(file_reports)} report={args.report}",
        flush=True,
    )
    if title_ascii_space_violations:
        raise SystemExit("ASCII 0x20 remains in one or more SaveLabel/title strings; see report")
    if (overflows or missing_glyphs) and not args.allow_overflow:
        raise SystemExit("ISB translations are not buildable; see report")


if __name__ == "__main__":
    main()
