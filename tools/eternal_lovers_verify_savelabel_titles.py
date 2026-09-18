#!/usr/bin/env python3
"""Verify Eternal Lovers save/load title strings keep parser-safe word gaps.

Eternal Lovers' ISL title statement (token ``af0e22af``) is the SaveLabel-like
string persisted into save data and shown as the second line of the save/load
screen.  As in the first Galaxy Angel and Moonlit Lovers, ordinary ASCII space
(0x20) is unsafe in parser-facing strings because the command parser treats it
as a delimiter.  The Eternal Lovers ISB writer already encodes display gaps as
single-byte 0xA0; this verifier makes that compatibility rule explicit and
fails builds if a translated title ever regresses to ASCII 0x20.
"""
from __future__ import annotations

import argparse
import json
import mmap
import re
from pathlib import Path

import galaxy_angel_build as builder
import moonlit_lovers_resources as resources
from eternal_lovers_isb_static import decode_isb_payload
from eternal_lovers_patch_remaining import merged_resources
from eternal_lovers_regroup_isb import TITLE_TOKEN, statement_offsets
from eternal_lovers_verify_remaining import container

NAME_RE = re.compile(r"^SCENARIO_DAT_([0-9a-fA-F]{8})\.txt$")
HALF_SPACE = 0xA0
ASCII_SPACE = 0x20


def load_title_units(source_dir: Path, assets: Path) -> list[dict]:
    index = json.loads((assets / "index.json").read_text(encoding="utf-8"))
    rows: list[dict] = []
    for entry in index["segments"]:
        segment = json.loads((assets / entry["path"]).read_text(encoding="utf-8"))
        source_name = segment["source"]["path"]
        source = (source_dir / source_name).read_bytes()
        offsets = statement_offsets(source)
        if not offsets:
            continue
        for unit in segment["units"]:
            storage = unit.get("storage", [])
            if not storage:
                continue
            first_offset = int(storage[0]["offset"])
            statement = __import__("bisect").bisect_right(offsets, first_offset) - 1
            if statement < 0:
                continue
            token = source[offsets[statement] + 4 : offsets[statement] + 8].hex()
            if token != TITLE_TOKEN:
                continue
            if len(storage) != 1:
                raise SystemExit(f"title unit unexpectedly spans multiple records: {unit['id']}")
            rows.append(
                {
                    "id": unit["id"],
                    "source_name": source_name,
                    "original": unit["original"],
                    "translation": unit.get("translation", ""),
                    "use_translation": bool(unit.get("use_translation")),
                    "storage": storage[0],
                }
            )
    return rows


def check_resource(raw: bytes, unit: dict) -> dict:
    storage = unit["storage"]
    plain = decode_isb_payload(
        raw,
        int(storage["offset"]) + 8,
        int(storage["length"]),
        int(storage["key"]),
    )
    return {
        "ascii_space_bytes": plain.count(ASCII_SPACE),
        "half_space_bytes": plain.count(HALF_SPACE),
        "plain_hex": plain.hex(),
    }


def verify_built_dir(rows: list[dict], built_dir: Path) -> dict:
    checked = 0
    violations: list[dict] = []
    half_spaces = 0
    cache: dict[str, bytes] = {}
    for unit in rows:
        name = unit["source_name"]
        path = built_dir / name
        if not path.is_file():
            continue
        raw = cache.setdefault(name, path.read_bytes())
        result = check_resource(raw, unit)
        checked += 1
        half_spaces += result["half_space_bytes"]
        if result["ascii_space_bytes"]:
            violations.append(
                {
                    "id": unit["id"],
                    "source_name": name,
                    "translation": unit["translation"],
                    **result,
                }
            )
    return {
        "checked": checked,
        "half_space_bytes": half_spaces,
        "ascii_space_violations": violations,
    }


def verify_iso(rows: list[dict], original_iso: Path, patched_iso: Path) -> dict:
    checked = 0
    violations: list[dict] = []
    half_spaces = 0
    by_source: dict[str, list[dict]] = {}
    for row in rows:
        by_source.setdefault(row["source_name"], []).append(row)

    with original_iso.open("rb") as old_stream, patched_iso.open("rb") as new_stream, mmap.mmap(
        old_stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as old_image, mmap.mmap(new_stream.fileno(), 0, access=mmap.ACCESS_READ) as new_image:
        old_files = builder.iso_files(old_image)
        new_files = builder.iso_files(new_image)
        old_data = container(old_image, old_files, "SCENARIO")
        new_data = container(new_image, new_files, "SCENARIO")
        old_map = merged_resources(old_data, "SCENARIO")
        new_map = merged_resources(new_data, "SCENARIO")
        new_by_record = {
            record: item for item in new_map.values() for record in item.record_positions
        }
        for source_name, units in by_source.items():
            match = NAME_RE.match(source_name)
            if not match:
                raise SystemExit(f"unexpected scenario filename: {source_name}")
            original_offset = int(match.group(1), 16)
            old_item = old_map[original_offset]
            record = old_item.record_positions[0]
            new_item = new_by_record[record]
            raw = resources.decompress_resource(
                new_data, new_item.offset, new_item.raw_size, new_item.compressed_size
            )
            for unit in units:
                result = check_resource(raw, unit)
                checked += 1
                half_spaces += result["half_space_bytes"]
                if result["ascii_space_bytes"]:
                    violations.append(
                        {
                            "id": unit["id"],
                            "source_name": source_name,
                            "translation": unit["translation"],
                            **result,
                        }
                    )
        old_data.release()
        new_data.release()
    return {
        "checked": checked,
        "half_space_bytes": half_spaces,
        "ascii_space_violations": violations,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--built-dir", type=Path)
    parser.add_argument("--original-iso", type=Path)
    parser.add_argument("--iso", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    if bool(args.original_iso) != bool(args.iso):
        raise SystemExit("--original-iso and --iso must be supplied together")

    rows = load_title_units(args.source_dir, args.assets)
    untranslated = [
        {"id": row["id"], "original": row["original"]}
        for row in rows
        if not row["use_translation"] or not row["translation"]
    ]
    translations_with_spaces = sum(
        " " in row["translation"].rstrip("\r\n") for row in rows
    )
    report = {
        "schema": "eternal-lovers-savelabel-title-space-audit/v1",
        "title_token": TITLE_TOKEN,
        "title_units": len(rows),
        "translated_title_units": len(rows) - len(untranslated),
        "translations_with_ascii_space": translations_with_spaces,
        "untranslated": untranslated,
    }
    violations: list[dict] = []
    if args.built_dir:
        report["built_dir"] = verify_built_dir(rows, args.built_dir)
        violations.extend(report["built_dir"]["ascii_space_violations"])
    if args.iso:
        report["iso"] = verify_iso(rows, args.original_iso, args.iso)
        violations.extend(report["iso"]["ascii_space_violations"])

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"SaveLabel/title audit: titles={len(rows)} translated={len(rows)-len(untranslated)} "
        f"with_spaces={translations_with_spaces} violations={len(violations)}"
    )
    if untranslated or violations:
        raise SystemExit("SaveLabel/title audit failed; see report")


if __name__ == "__main__":
    main()
