#!/usr/bin/env python3
"""Check shared issue regressions against a selected game's current inputs."""

import argparse
import importlib
import json
import mmap
import re
import sys
from pathlib import Path


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--original-iso", type=Path)
    parser.add_argument("--remaining", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    project = args.project.resolve()
    sys.path.insert(0, str(project / "tools"))
    translation = importlib.import_module("galaxy_angel_translation")
    assets = project / "assets/translation"
    segments = assets / ("isb/segments" if (assets / "isb").exists() else "segments")
    violations = []
    units_checked = 0
    pattern = re.compile(
        r"만약아픈|즉시정식|뱃도리|선문명|퇴피|쿠지라 룸|언니님|"
        r"(?:앙카|앵카)\s*클로|전혀머리에안들어와|황왕|절호조|흑색 달|백색 달|"
        r"군규|브리지|전축하|거편|기무슨|짐슨|쿵후\s*파이터"
    )
    for path in sorted(segments.glob("*.json")):
        for unit in load(path).get("units", []):
            if not unit.get("use_translation", True):
                continue
            text = unit.get("translation", "")
            units_checked += 1
            if pattern.search(text):
                violations.append({"path": str(path.relative_to(project)), "id": unit["id"], "kind": "shared_wording", "translation": text})
    result = {"project": project.name, "scenario_units": units_checked}
    ui_words = project / "assets/image_extraction/japanese_images/ui_translations.json"
    if ui_words.is_file():
        for entry in load(ui_words).get("images", []):
            if re.search(r"쿵후\s*파이터", str(entry.get("translation", ""))):
                violations.append({"path": str(ui_words.relative_to(project)), "id": entry["filename"], "kind": "image_terminology"})
    if (assets / "battle/battle_unique.json").is_file():
        battle = importlib.import_module("galaxy_angel_build_battle")
        authority = load(assets / "battle/battle_unique.json")["units"]
        for unit in authority:
            battle.validate_battle_window_text(unit, "series issue audit")
        result["battle_authority_units"] = len(authority)
    else:
        if not args.original_iso or not args.remaining:
            parser.error("expansion projects require --original-iso and --remaining")
        patcher = importlib.import_module("eternal_lovers_patch_remaining")
        builder = importlib.import_module("galaxy_angel_build")
        resources = importlib.import_module("moonlit_lovers_resources")
        custom_map = translation.load_custom_map(project / "build/font_map.json")
        # Both newline conventions and a missing row-2 translation must reflow.
        synthetic = 0
        cases = [
            ("알겠습니다, 지금부터 명령을 수행하겠습니다.", 2),
            (" ".join(["알겠습니다."] * 6), 3),
            ("알겠습니다.", 1),
        ]
        for newline in (b"\n", b"\r\n"):
            for text, expected_rows in cases:
                raw = newline.join([b"[MESSAGE]", b"#011=source", b"#012=", b"#021=other", b"#022="])
                unit = {"id": "empty-row-regression", "original": "source", "translation": text, "occurrence": {"section": "MESSAGE", "line": 2}}
                rebuilt, _ = patcher.rebuild_resource(raw, [unit], custom_map)
                rows = [line.split(b"=", 1)[1].strip() for line in rebuilt.splitlines() if re.match(rb"#01[123]=", line)]
                assert sum(bool(row) for row in rows) == expected_rows, "incorrect message row count"
                assert all(len(row) <= patcher.BATTLE_TEXT_COLUMNS for row in rows), "message row too wide"
                assert b"#021=other" in rebuilt, "next message changed"
                synthetic += 1
        targets = patcher.collect_targets(load(args.remaining))
        messages = resources_checked = third_rows = 0
        with args.original_iso.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as image:
            files = builder.iso_files(image)
            for stem, blocks in targets.items():
                selected = {offset: [u for u in units if str(u["occurrence"].get("section", "")).startswith("MESSAGE")] for offset, units in blocks.items()}
                selected = {offset: units for offset, units in selected.items() if units}
                if not selected:
                    continue
                item = builder.resolve_iso_file(files, stem)
                data = image[item.extent * builder.SECTOR:item.extent * builder.SECTOR + item.size]
                index = patcher.merged_resources(data, stem)
                for offset, units in selected.items():
                    item = index[offset]
                    raw = resources.decompress_resource(data, item.offset, item.raw_size, item.compressed_size)
                    rebuilt, _ = patcher.rebuild_resource(raw, units, custom_map)
                    resources_checked += 1
                    section = b""
                    for line in rebuilt.splitlines():
                        header = re.match(rb"\s*\[([^]]+)\]", line)
                        if header is not None:
                            section = header.group(1)
                        if not section.startswith(b"MESSAGE"):
                            continue
                        match = re.match(rb"\s*#(\d+[123])\s*=\s*(.*)", line)
                        if match is None or int(match.group(1)[:-1]) == 0:
                            continue
                        # Display characters use one ASCII/half-space byte or a
                        # two-byte CP932/custom glyph, so byte count equals columns.
                        value = match.group(2).split(b"//", 1)[0].strip()
                        messages += 1
                        third_rows += int(match.group(1).endswith(b"3"))
                        if len(value) > patcher.BATTLE_TEXT_COLUMNS:
                            violations.append({"kind": "battle_width", "container": stem, "offset": offset, "key": match.group(1).decode(), "columns": len(value)})
        result.update(battle_resources=resources_checked, battle_rows=messages, third_rows=third_rows, empty_row_regressions=synthetic)
    result["violations"] = violations
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**result, "violations": len(violations)}, ensure_ascii=False))
    if violations:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
