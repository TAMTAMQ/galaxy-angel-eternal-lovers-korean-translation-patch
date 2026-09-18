from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

import galaxy_angel_translation as translation

ROOT = Path(__file__).resolve().parents[1]
SEGMENTS = ROOT / "assets" / "translation" / "isb" / "segments"

CHECKS = {
    "fullwidth_digit": re.compile(r"[０-９]"),
    "fullwidth_ascii": re.compile(r"[Ａ-Ｚａ-ｚ]"),
    "ascii_tilde": re.compile(r"~"),
    "jp_punct": re.compile(r"[、。！？]"),
    "kana_left": re.compile(r"[ぁ-ゖァ-ヺ]"),
    "fullwidth_space": re.compile(r"　"),
}


def load_units() -> list[tuple[Path, dict]]:
    rows: list[tuple[Path, dict]] = []
    for path in sorted(SEGMENTS.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        for unit in data.get("units", []):
            if unit.get("use_translation"):
                rows.append((path, unit))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--dump-file", type=str)
    parser.add_argument("--start-unit", type=int, default=1)
    parser.add_argument("--end-unit", type=int)
    parser.add_argument("--divergent", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--original-contains", type=str)
    parser.add_argument("--translation-contains", type=str)
    parser.add_argument("--check-capacity", action="store_true")
    parser.add_argument("--compare-reference", action="store_true")
    parser.add_argument("--substantive", action="store_true")
    parser.add_argument("--state", type=str)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--encoding-map", type=Path, default=ROOT / "build" / "font_map.json")
    args = parser.parse_args()

    units = load_units()
    states = collections.Counter(unit.get("state") for _, unit in units)
    if args.state:
        filtered = [(path, unit) for path, unit in units if unit.get("state") == args.state]
        print(f"state={args.state} matched={len(filtered)} showing={args.offset}:{args.offset + args.limit}")
        for path, unit in filtered[args.offset : args.offset + args.limit]:
            print(f"{path.name}\t{unit['id']}\t{unit.get('original','')}\t{unit.get('translation','').rstrip(chr(10))}")
        return
    counts: collections.Counter[str] = collections.Counter()
    samples: dict[str, list[tuple[str, str, str, str]]] = collections.defaultdict(list)
    by_original: dict[str, list[tuple[Path, dict]]] = collections.defaultdict(list)
    for path, unit in units:
        by_original[unit.get("original", "")].append((path, unit))

    if args.compare_reference:
        reference_root = ROOT.parent / "galaxy_angel" / "assets" / "translation" / "segments"
        reference_by_original: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
        for ref_path in sorted(reference_root.glob("*.json")):
            ref_data = json.loads(ref_path.read_text(encoding="utf-8-sig"))
            for ref_unit in ref_data.get("units", []):
                if not ref_unit.get("use_translation"):
                    continue
                original = ref_unit.get("original", "").rstrip("\r\n")
                translated = ref_unit.get("translation", "").rstrip("\r\n")
                if original and translated:
                    reference_by_original[original].append((ref_unit.get("id", ""), translated))
        candidates = []
        for path, unit in units:
            original = unit.get("original", "").rstrip("\r\n")
            target = unit.get("translation", "").rstrip("\r\n")
            refs = reference_by_original.get(original)
            if not refs:
                continue
            ref_counts = collections.Counter(translated for _, translated in refs)
            reference, ref_count = ref_counts.most_common(1)[0]
            if target == reference:
                continue
            if args.substantive:
                normalize = lambda text: re.sub(r"[\s,.!?、。！？…]+", "", text)
                if normalize(target) == normalize(reference):
                    continue
            candidates.append((len(refs), ref_count, path.name, unit["id"], original, target, reference, refs[0][0]))
        candidates.sort(key=lambda item: (-item[1], -item[0], item[2], item[3]))
        print(f"reference_originals={len(reference_by_original)} divergent_matches={len(candidates)}")
        for ref_total, ref_count, path_name, unit_id, original, target, reference, ref_id in candidates[: args.limit]:
            print(f"{path_name}\t{unit_id}\tref={ref_count}/{ref_total}\t{original}\tTARGET={target}\tREF={reference}\t{ref_id}")
        return

    if args.check_capacity:
        custom_map = translation.load_custom_map(args.encoding_map)
        if custom_map is None:
            raise SystemExit("custom font encoding map is required")
        overflows = []
        missing_glyphs = []
        for path, unit in units:
            rendered = " ".join(unit.get("translation", "").rstrip("\r\n").splitlines())
            normalized = translation.normalize_display_punctuation(rendered)
            encoded_length = 0
            missing = set()
            for char in normalized:
                if char in custom_map:
                    encoded_length += len(custom_map[char])
                    continue
                try:
                    encoded_length += len(char.encode("cp932"))
                except UnicodeEncodeError:
                    missing.add(char)
                    encoded_length += 2
            capacity = sum(int(item["length"]) for item in unit.get("storage", []))
            if missing:
                missing_glyphs.append((path.name, unit["id"], "".join(sorted(missing)), unit.get("original", ""), rendered))
            if encoded_length > capacity:
                overflows.append((encoded_length - capacity, path.name, unit["id"], capacity, encoded_length, unit.get("original", ""), rendered))
        overflows.sort(reverse=True)
        print(f"capacity_overflows={len(overflows)} missing_glyph_units={len(missing_glyphs)}")
        for extra, path_name, unit_id, capacity, encoded_length, original, rendered in overflows[: args.limit]:
            print(f"+{extra}\t{path_name}\t{unit_id}\t{encoded_length}>{capacity}\t{original}\t{rendered}")
        if missing_glyphs:
            print("\n## missing glyphs")
            for path_name, unit_id, missing, original, rendered in missing_glyphs[: args.limit]:
                print(f"{path_name}\t{unit_id}\t{missing}\t{original}\t{rendered}")
        return

    if args.original_contains or args.translation_contains:
        matched = 0
        for path, unit in units:
            original = unit.get("original", "")
            translated = unit.get("translation", "").rstrip("\n")
            if args.original_contains and args.original_contains not in original:
                continue
            if args.translation_contains and args.translation_contains not in translated:
                continue
            print(f"{path.name}\t{unit['id']}\t{unit.get('state','')}\t{original}\t{translated}")
            matched += 1
        print(f"matched={matched}")
        return

    if args.divergent:
        divergent = []
        for original, rows in by_original.items():
            translations = collections.defaultdict(list)
            for path, unit in rows:
                translations[unit.get("translation", "").rstrip("\n")].append((path, unit))
            if len(translations) > 1:
                divergent.append((len(rows), original, translations))
        divergent.sort(key=lambda item: (-item[0], item[1]))
        print(f"unique_originals={len(by_original)} divergent_originals={len(divergent)}")
        for total, original, translations in divergent[: args.limit]:
            print(f"\nORIGINAL[{total}] {original}")
            for translated, rows in sorted(translations.items(), key=lambda item: (-len(item[1]), item[0])):
                ids = ", ".join(unit['id'] for _, unit in rows[:6])
                suffix = " ..." if len(rows) > 6 else ""
                print(f"  [{len(rows)}] {translated} :: {ids}{suffix}")
        return

    dump_name = args.dump_file
    for path, unit in units:
        original = unit.get("original", "")
        translated = unit.get("translation", "").rstrip("\n")
        if dump_name and path.name == dump_name:
            unit_no = int(unit["id"].rsplit(":", 1)[1])
            if unit_no >= args.start_unit and (args.end_unit is None or unit_no <= args.end_unit):
                print(f"{unit['id']}\t{unit.get('state','')}\t{original}\t{translated}")
        for name, pattern in CHECKS.items():
            if pattern.search(translated):
                counts[name] += 1
                if len(samples[name]) < args.samples:
                    samples[name].append((path.name, unit["id"], original, translated))

    if dump_name:
        return

    print(f"files={len(list(SEGMENTS.glob('*.json')))} units={len(units)} unique_originals={len(by_original)}")
    print("states=" + repr(states))
    print("checks=" + repr(counts))
    for name in CHECKS:
        if not counts[name]:
            continue
        print(f"\n## {name} ({counts[name]})")
        for row in samples[name]:
            print(" | ".join(row))


if __name__ == "__main__":
    main()
