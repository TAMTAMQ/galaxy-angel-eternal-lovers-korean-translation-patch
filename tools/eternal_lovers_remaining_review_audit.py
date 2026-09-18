#!/usr/bin/env python3
"""Audit the reviewed Eternal Lovers non-ISB remaining-text authority in memory."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
from pathlib import Path

import galaxy_angel_translation as translation
from eternal_lovers_merge_remaining_review import normalize_reviewed_translation

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = ROOT / "assets/translation/remaining/remaining_candidates.json"
DEFAULT_OVERRIDES = ROOT / "assets/translation/remaining/review_overrides.json"
DEFAULT_FONT_MAP = ROOT / "build/font_map.json"

CHECKS = {
    "fullwidth_digit": re.compile(r"[０-９]"),
    "fullwidth_ascii": re.compile(r"[Ａ-Ｚａ-ｚ]"),
    "jp_punct": re.compile(r"[、。！？]"),
    "ascii_tilde": re.compile(r"~"),
    "fullwidth_space": re.compile(r"　"),
    "kana_left": re.compile(r"[ぁ-ゖァ-ヺ]"),
}
PRINTF = re.compile(r"%(?:[-+0 #]*\d*(?:\.\d+)?)?[a-zA-Z]")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_reviewed(index_path: Path, override_path: Path) -> list[dict]:
    overlay = load_json(override_path)
    expected = overlay.get("index_sha256")
    if expected:
        actual = hashlib.sha256(index_path.read_bytes()).hexdigest()
        if actual != expected:
            raise SystemExit(f"remaining index SHA-256 mismatch: {actual} != {expected}")

    payload = load_json(index_path)
    candidates = payload.get("candidates", [])
    by_id = {item["id"]: item for item in candidates}
    if len(by_id) != len(candidates):
        raise SystemExit("duplicate candidate id in remaining index")

    for item in candidates:
        if item.get("state") == "internal_identifier":
            continue
        text = item.get("translation")
        if isinstance(text, str) and text:
            item["translation"] = normalize_reviewed_translation(text)

    seen: set[str] = set()
    for entry in overlay.get("entries", []):
        unit_id = str(entry["id"])
        if unit_id in seen:
            raise SystemExit(f"duplicate override id: {unit_id}")
        seen.add(unit_id)
        item = by_id.get(unit_id)
        if item is None:
            raise SystemExit(f"override id missing from index: {unit_id}")
        item["translation"] = str(entry["translation"])
        item["state"] = str(entry.get("state", "reviewed"))
        item["use_translation"] = bool(entry.get("use_translation", True))
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    parser.add_argument("--font-map", type=Path, default=DEFAULT_FONT_MAP)
    parser.add_argument("--samples", type=int, default=25)
    args = parser.parse_args()

    candidates = build_reviewed(args.index, args.overrides)
    custom_map = translation.load_custom_map(args.font_map)
    if custom_map is None:
        raise SystemExit(f"custom font map is required: {args.font_map}")

    counts: collections.Counter[str] = collections.Counter()
    samples: dict[str, list[tuple[str, str, str]]] = collections.defaultdict(list)
    missing_glyphs: list[tuple[str, str, str]] = []
    token_mismatches: list[tuple[str, str, str]] = []
    active = internal = 0

    for item in candidates:
        if not item.get("use_translation"):
            continue
        text = str(item.get("translation") or "")
        if not text:
            continue
        if item.get("state") == "internal_identifier":
            internal += 1
            continue
        active += 1
        original = str(item.get("original") or "")
        for name, pattern in CHECKS.items():
            if pattern.search(text):
                counts[name] += 1
                if len(samples[name]) < args.samples:
                    samples[name].append((item["id"], original, text))

        original_tokens = PRINTF.findall(original)
        translated_tokens = PRINTF.findall(text)
        if original_tokens != translated_tokens:
            token_mismatches.append((item["id"], original, text))

        missing: set[str] = set()
        for char in text.rstrip("\r\n"):
            if char in custom_map:
                continue
            try:
                char.encode("cp932")
            except UnicodeEncodeError:
                missing.add(char)
        if missing:
            missing_glyphs.append((item["id"], "".join(sorted(missing)), text))

    print(
        f"candidates={len(candidates)} active={active} internal_identifiers={internal} "
        f"overrides={len(load_json(args.overrides).get('entries', []))}"
    )
    print("checks=" + repr(counts))
    print(f"printf_token_mismatches={len(token_mismatches)} missing_glyph_units={len(missing_glyphs)}")
    for name in CHECKS:
        if not counts[name]:
            continue
        print(f"\n## {name} ({counts[name]})")
        for unit_id, original, text in samples[name]:
            print(f"{unit_id}\t{original.rstrip()}\t{text.rstrip()}")
    if token_mismatches:
        print("\n## printf token mismatches")
        for unit_id, original, text in token_mismatches[: args.samples]:
            print(f"{unit_id}\t{original.rstrip()}\t{text.rstrip()}")
    if missing_glyphs:
        print("\n## missing glyphs")
        for unit_id, missing, text in missing_glyphs[: args.samples]:
            print(f"{unit_id}\t{missing}\t{text.rstrip()}")


if __name__ == "__main__":
    main()
