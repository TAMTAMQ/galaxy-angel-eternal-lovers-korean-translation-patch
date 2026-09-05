#!/usr/bin/env python3
"""Galaxy Angel Eternal Lovers ISB static-analysis helpers.

This tool never starts the game or an emulator.  It scans extracted SCENARIO
resources for the observed ISL2 string record marker and emits a reproducible
manifest for further cryptanalysis.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import struct
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path


STRING_TAG = b"\x02\x00\x08\x00"
INTEGER_TAG = b"\x01\x04\x00\x00"
VOICE_RE = re.compile(rb"[a-z]{3}[0-9]{3}_[0-9]{2}_[0-9]{2}\Z")

# Proven from the sequential gaf001_01_00 .. gaf001_01_52 constants in
# SCENARIO_DAT_00002800.  This transform is deliberately scoped to that family;
# other 12-byte strings use different parameters.
GAF_KEY = bytes.fromhex("90 aa d8 12 9a c8 69 12 60 4b a8 a8")
GAF_CIPHER_TO_PLAIN = (0, 1, 2, 3, 8, 9, 10, 11, 4, 5, 6, 7)


@dataclass(frozen=True)
class StringRecord:
    file: str
    offset: int
    length: int
    payload_sha256: str
    payload_hex: str
    before_hex: str
    after_hex: str


def ror8(value: int, count: int) -> int:
    count &= 7
    return ((value >> count) | (value << (8 - count))) & 0xFF


def rol8(value: int, count: int) -> int:
    count &= 7
    return ((value << count) | (value >> (8 - count))) & 0xFF


def decode_gaf_voice(payload: bytes) -> bytes:
    if len(payload) != 12:
        raise ValueError("gaf voice transform requires exactly 12 bytes")
    plain = bytearray(12)
    for cipher_pos, plain_pos in enumerate(GAF_CIPHER_TO_PLAIN):
        plain[plain_pos] = ror8(payload[cipher_pos] ^ GAF_KEY[cipher_pos], 3)
    return bytes(plain)


def encode_gaf_voice(plain: bytes) -> bytes:
    if len(plain) != 12:
        raise ValueError("gaf voice transform requires exactly 12 bytes")
    payload = bytearray(12)
    for cipher_pos, plain_pos in enumerate(GAF_CIPHER_TO_PLAIN):
        payload[cipher_pos] = rol8(plain[plain_pos], 3) ^ GAF_KEY[cipher_pos]
    return bytes(payload)


def ror32(value: int, count: int) -> int:
    count &= 31
    return ((value >> count) | (value << (32 - count))) & 0xFFFFFFFF


def rol32(value: int, count: int) -> int:
    count &= 31
    return ((value << count) | (value >> (32 - count))) & 0xFFFFFFFF


def decode_isb_payload(data: bytes, payload_offset: int, length: int, key: int) -> bytes:
    """Decode one 0x00080002 payload exactly as the static MIPS loop does."""
    aligned = (length + 3) & ~3
    cipher = data[payload_offset : payload_offset + aligned]
    if len(cipher) != aligned:
        raise ValueError("truncated encrypted payload")
    plain = bytearray()
    for offset in range(0, aligned, 4):
        word = struct.unpack_from("<I", cipher, offset)[0]
        plain.extend(struct.pack("<I", ror32(word, 3) ^ key))
    return bytes(plain[:length])


def encode_isb_payload(plain: bytes, key: int) -> bytes:
    """Encode a payload including its compiler-provided four-byte padding."""
    aligned = (len(plain) + 3) & ~3
    return _encode_aligned_isb(plain.ljust(aligned, b"\0"), key)


def _encode_aligned_isb(plain: bytes, key: int) -> bytes:
    if len(plain) % 4:
        raise ValueError("aligned encoder requires a multiple of four bytes")
    cipher = bytearray()
    for offset in range(0, len(plain), 4):
        word = struct.unpack_from("<I", plain, offset)[0] ^ key
        cipher.extend(struct.pack("<I", rol32(word, 3)))
    return bytes(cipher)


def build_reference_byte_model(segment_dir: Path):
    counts = [Counter() for _ in range(4)]
    for path in sorted(segment_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for unit in payload.get("units", []):
            raw = unit.get("original", "").replace("\n", "").encode("cp932", "ignore")
            for index, value in enumerate(raw):
                counts[index & 3][value] += 1
    return counts


def infer_dominant_key(data: bytes, records, reference_counts):
    transformed = [bytearray() for _ in range(4)]
    for record in records:
        if record.length < 4:
            continue
        aligned = (record.length + 3) & ~3
        cipher = data[record.offset + 8 : record.offset + 8 + aligned]
        if len(cipher) != aligned:
            continue
        for offset in range(0, aligned, 4):
            word = struct.pack("<I", ror32(struct.unpack_from("<I", cipher, offset)[0], 3))
            for byte_index, value in enumerate(word):
                transformed[byte_index].append(value)

    key = bytearray()
    alternatives = []
    for byte_index in range(4):
        counts = reference_counts[byte_index]
        total = sum(counts.values())
        scores = []
        for candidate in range(256):
            score = sum(
                math.log((counts[value ^ candidate] + 0.1) / (total + 25.6))
                for value in transformed[byte_index]
            )
            scores.append((score, candidate))
        ranked = sorted(scores, reverse=True)
        key.append(ranked[0][1])
        alternatives.append(
            [{"byte": candidate, "score": score} for score, candidate in ranked[:4]]
        )
    return int.from_bytes(key, "little"), alternatives


def text_quality(raw: bytes) -> float:
    if not raw:
        return 0.0
    try:
        text = raw.decode("cp932")
    except UnicodeDecodeError:
        return -10.0
    printable = sum(char.isprintable() or char in "\r\n\t" for char in text)
    japanese = sum(
        "\u3040" <= char <= "\u30ff" or "\u3400" <= char <= "\u9fff"
        for char in text
    )
    ascii_useful = sum(char.isalnum() or char in "_.,～！？ー・ " for char in text)
    controls = sum(ord(char) < 0x20 and char not in "\r\n\t" for char in text)
    return (printable + japanese * 1.5 + ascii_useful * 0.25) / len(text) - controls * 2


def find_key_spans(data: bytes, records: list[StringRecord]):
    """Locate embedded block keys from the repeated 0x401 block marker.

    The first block key is the first word of an ISB.  Later blocks begin with
    two consecutive serialized integer tags followed by their 32-bit key.
    False marker collisions are rejected by decoding nearby tagged strings.
    """
    candidates = [(0, int.from_bytes(data[:4], "little"), "header")]
    marker = INTEGER_TAG + INTEGER_TAG
    cursor = 0
    while True:
        position = data.find(marker, cursor)
        if position < 0:
            break
        if position + 12 <= len(data):
            candidates.append(
                (position + 8, int.from_bytes(data[position + 8 : position + 12], "little"), "block")
            )
        cursor = position + 1

    accepted = []
    for index, (start, key, source) in enumerate(candidates):
        end = candidates[index + 1][0] if index + 1 < len(candidates) else len(data)
        nearby = [record for record in records if start <= record.offset < end][:12]
        scores = []
        for record in nearby:
            try:
                decoded = decode_isb_payload(data, record.offset + 8, record.length, key)
            except ValueError:
                continue
            scores.append(text_quality(decoded))
        good = sum(score >= 0.8 for score in scores)
        if source == "header" or (len(scores) >= 2 and good >= max(2, len(scores) // 2)):
            accepted.append({"start": start, "key": key, "source": source})

    for index, span in enumerate(accepted):
        span["end"] = accepted[index + 1]["start"] if index + 1 < len(accepted) else len(data)
    return accepted


def decode_records_with_spans(data: bytes, records: list[StringRecord], spans):
    decoded = []
    span_index = 0
    for record in records:
        while span_index + 1 < len(spans) and record.offset >= spans[span_index + 1]["start"]:
            span_index += 1
        span = spans[span_index]
        if not (span["start"] <= record.offset < span["end"]):
            continue
        raw = decode_isb_payload(data, record.offset + 8, record.length, span["key"])
        try:
            text = raw.decode("cp932")
        except UnicodeDecodeError:
            text = None
        decoded.append(
            {
                "offset": record.offset,
                "length": record.length,
                "key": span["key"],
                "raw_hex": raw.hex(),
                "text": text,
                "quality": text_quality(raw),
                "before_word_hex": record.before_hex[-8:],
            }
        )
    return decoded


def _window_key_score(data: bytes, records, key: int):
    scores = []
    for record in records:
        try:
            raw = decode_isb_payload(data, record.offset + 8, record.length, key)
        except ValueError:
            continue
        scores.append(text_quality(raw))
    if not scores:
        return (-100.0, 0)
    return (sum(scores) / len(scores), sum(score >= 0.8 for score in scores))


def find_key_spans_adaptive(data: bytes, records: list[StringRecord], window: int = 12):
    """Recover per-block keys from nearby integer literals and text quality."""
    if not records:
        return []
    literal_keys = []
    cursor = 0
    while True:
        position = data.find(INTEGER_TAG, cursor)
        if position < 0:
            break
        if position + 8 <= len(data):
            literal_keys.append((position + 4, int.from_bytes(data[position + 4 : position + 8], "little")))
        cursor = position + 1

    spans = [{"start": 0, "key_offset": 0, "key": int.from_bytes(data[:4], "little"), "source": "header"}]
    current_key = spans[0]["key"]
    previous_end = 0
    for index, record in enumerate(records):
        sample = records[index : index + window]
        current_average, current_good = _window_key_score(data, sample, current_key)
        lower_bound = max(previous_end, record.offset - 0x400)
        candidates = [
            (position, key)
            for position, key in literal_keys
            if lower_bound <= position < record.offset
        ]
        if candidates:
            ranked = []
            for position, key in candidates:
                if key == current_key:
                    continue
                average, good = _window_key_score(data, sample, key)
                ranked.append((good, average, position, key))
            if ranked:
                good, average, position, key = max(ranked)
                minimum_good = max(2, len(sample) * 2 // 3)
                clearly_better = average >= current_average + 0.35
                current_is_bad = current_good < max(2, len(sample) // 2)
                if good >= minimum_good and (clearly_better or current_is_bad):
                    current_key = key
                    spans.append(
                        {
                            "start": record.offset,
                            "key_offset": position,
                            "key": key,
                            "source": "integer_literal",
                        }
                    )
        previous_end = record.offset + 8 + record.length

    # A poor record can precede the first successful switch decision.  Move a
    # recovered boundary backward over immediately preceding records whenever
    # the new key is decisively better there as well.
    for span_index in range(1, len(spans)):
        span = spans[span_index]
        record_index = next(
            i for i, record in enumerate(records) if record.offset >= span["start"]
        )
        while record_index > 0:
            prior = records[record_index - 1]
            previous_key = spans[span_index - 1]["key"]
            new_score, _ = _window_key_score(data, [prior], span["key"])
            old_score, _ = _window_key_score(data, [prior], previous_key)
            if new_score < old_score + 0.35:
                break
            span["start"] = prior.offset
            record_index -= 1

    for index, span in enumerate(spans):
        span["end"] = spans[index + 1]["start"] if index + 1 < len(spans) else len(data)
    return spans


def iter_records(path: Path, context: int = 16):
    data = path.read_bytes()
    cursor = 0
    while True:
        offset = data.find(STRING_TAG, cursor)
        if offset < 0:
            return
        if offset + 8 <= len(data):
            length = int.from_bytes(data[offset + 4 : offset + 8], "little")
            payload_start = offset + 8
            payload_end = payload_start + length
            if 0 <= length <= 0x10000 and payload_end <= len(data):
                payload = data[payload_start:payload_end]
                yield StringRecord(
                    file=path.name,
                    offset=offset,
                    length=length,
                    payload_sha256=hashlib.sha256(payload).hexdigest(),
                    payload_hex=payload.hex(),
                    before_hex=data[max(0, offset - context) : offset].hex(),
                    after_hex=data[payload_end : min(len(data), payload_end + context)].hex(),
                )
        cursor = offset + 1


def build_manifest(source_dir: Path, context: int):
    records: list[StringRecord] = []
    by_file: dict[str, int] = {}
    lengths: Counter[int] = Counter()
    preceding_words: Counter[str] = Counter()
    for path in sorted(source_dir.glob("SCENARIO_DAT_*.txt")):
        found = list(iter_records(path, context=context))
        if not found:
            continue
        by_file[path.name] = len(found)
        records.extend(found)
        for record in found:
            lengths[record.length] += 1
            preceding_words[record.before_hex[-8:]] += 1

    return {
        "format": "eternal-lovers-isb-static-v1",
        "source_dir": str(source_dir.resolve()),
        "tag_hex": STRING_TAG.hex(),
        "file_count": len(by_file),
        "record_count": len(records),
        "files": by_file,
        "length_histogram": dict(sorted(lengths.items())),
        "preceding_word_histogram": dict(preceding_words.most_common()),
        "records": [asdict(record) for record in records],
    }


def validate_known_family(path: Path):
    matches = []
    for record in iter_records(path):
        if record.length != 12:
            continue
        payload = bytes.fromhex(record.payload_hex)
        plain = decode_gaf_voice(payload)
        if VOICE_RE.fullmatch(plain) and plain.startswith(b"gaf001_01_"):
            if encode_gaf_voice(plain) != payload:
                raise AssertionError(f"round trip failed at 0x{record.offset:X}")
            matches.append({"offset": record.offset, "text": plain.decode("ascii")})

    expected = [f"gaf001_01_{i:02d}" for i in range(53)]
    actual = [item["text"] for item in matches]
    if actual != expected:
        raise AssertionError(
            f"known family mismatch: expected {len(expected)} sequential values, "
            f"found {len(actual)}"
        )
    return matches


def grouped_contexts(manifest: dict, minimum: int):
    groups = defaultdict(list)
    for record in manifest["records"]:
        key = (record["length"], record["before_hex"][-8:])
        groups[key].append(record)
    return [
        {
            "length": key[0],
            "preceding_word_hex": key[1],
            "count": len(items),
            "files": sorted({item["file"] for item in items}),
        }
        for key, items in sorted(groups.items(), key=lambda item: -len(item[1]))
        if len(items) >= minimum
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("work/galaxy_angel_eternal_lovers/source/scenario"),
    )
    parser.add_argument(
        "--known-file",
        type=Path,
        default=Path(
            "work/galaxy_angel_eternal_lovers/source/scenario/"
            "SCENARIO_DAT_00002800.txt"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "work/galaxy_angel_eternal_lovers/analysis/isb_static_manifest.json"
        ),
    )
    parser.add_argument("--context", type=int, default=16)
    parser.add_argument("--minimum-group", type=int, default=8)
    parser.add_argument(
        "--reference-segments",
        type=Path,
        default=Path(
            "work/galaxy_angel_moonlit_lovers/assets/translation/segments"
        ),
    )
    parser.add_argument(
        "--infer-keys",
        action="store_true",
        help="infer one statistically dominant 32-bit key per resource",
    )
    args = parser.parse_args()

    known = validate_known_family(args.known_file)
    manifest = build_manifest(args.source_dir, context=args.context)
    manifest["known_gaf_family"] = known
    manifest["context_groups"] = grouped_contexts(
        manifest, minimum=args.minimum_group
    )
    if args.infer_keys:
        reference_counts = build_reference_byte_model(args.reference_segments)
        inferred = {}
        for path in sorted(args.source_dir.glob("SCENARIO_DAT_*.txt")):
            records = list(iter_records(path, context=0))
            if not records:
                continue
            data = path.read_bytes()
            key, alternatives = infer_dominant_key(data, records, reference_counts)
            raw_key = key.to_bytes(4, "little")
            positions = []
            cursor = 0
            while True:
                position = data.find(raw_key, cursor)
                if position < 0:
                    break
                positions.append(position)
                cursor = position + 1
            inferred[path.name] = {
                "key": key,
                "key_hex": f"{key:08x}",
                "raw_positions": positions,
                "header_key": int.from_bytes(data[:4], "little"),
                "alternatives": alternatives,
            }
        manifest["inferred_dominant_keys"] = inferred

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"validated known gaf family: {len(known)} records")
    print(
        f"scanned {manifest['file_count']} files, "
        f"found {manifest['record_count']} tagged string records"
    )
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
