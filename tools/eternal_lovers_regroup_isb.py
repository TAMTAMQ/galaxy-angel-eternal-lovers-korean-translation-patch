#!/usr/bin/env python3
"""Regroup ISB translation units along ISL statement boundaries.

The first extractor merged every physically contiguous Japanese string record
into one unit.  The STX compiler packs consecutive string arguments back to
back, so contiguity also merges records that are *separate strings*:

* a choice statement's options - the two answers of a yes/no prompt became one
  sentence, and the Korean was then re-split across them at an arbitrary point,
  which is what makes the second choice read as the tail of the first;
* a briefing panel's lines - the victory/defeat condition list is one statement
  holding 7-10 independent lines.

Each statement's 4-byte name token is a plain hash, identical in every block, so
the call can be identified without executing anything:

    084368c5  message   1-3 records = the wrapped lines of one dialogue box
    9f455280  choice    one record per selectable option
    af0e22af  title     single record
    a7074316  voice     the .ogg name

So records are merged only inside one message statement of at most three lines;
a choice statement, an oversized message statement (a panel, not a dialogue box)
and anything else become one unit per record.

Translations survive wherever the record set is unchanged: a rebuilt unit that
covers exactly the same offsets as an old one keeps its text and state.  Units
that were split come out untranslated, because the merged Korean was written for
a sentence that does not exist.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import hashlib
import json
import re
import struct
import sys
from pathlib import Path

import eternal_lovers_isb_static as isb

MESSAGE_TOKEN = "084368c5"
CHOICE_TOKEN = "9f455280"
MESSAGE_MAX_LINES = 3

JAPANESE_RE = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾝ]")


def atomic_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def statement_offsets(data: bytes) -> list[int] | None:
    """Recover the trailing statement index: a run of ascending u32 offsets.

    It starts with 0 and ends at the last word of the resource, which is enough
    to find it without knowing the surrounding header layout.
    """
    size = len(data)
    for start in range(0, size - 8, 4):
        first, second = struct.unpack_from("<II", data, start)
        if first != 0 or second == 0:
            continue
        entries: list[int] = []
        cursor = start
        while cursor + 4 <= size:
            value = struct.unpack_from("<I", data, cursor)[0]
            if entries and value <= entries[-1]:
                break
            if value >= start:
                break
            entries.append(value)
            cursor += 4
        if len(entries) > 4 and cursor >= size - 8:
            return entries
    return None


def japanese_records(path: Path):
    data = path.read_bytes()
    records = list(isb.iter_records(path))
    decoded = isb.decode_records_with_spans(
        data, records, isb.find_key_spans_adaptive(data, records)
    )
    usable = [r for r in decoded if r["text"] and JAPANESE_RE.search(r["text"])]
    return data, usable


def group_records(data: bytes, records: list[dict]) -> list[list[dict]]:
    """Split the file's Japanese records into units."""
    entries = statement_offsets(data)
    if entries is None:
        # Small resources whose index could not be recovered hold nothing but
        # standalone strings (chapter titles); one unit per record is correct
        # and never merges two separate strings.
        return [[record] for record in records]

    by_statement: dict[int, list[dict]] = collections.defaultdict(list)
    for record in records:
        by_statement[bisect.bisect_right(entries, record["offset"]) - 1].append(record)

    units: list[list[dict]] = []
    for index in sorted(by_statement):
        group = by_statement[index]
        token = data[entries[index] + 4 : entries[index] + 8].hex()
        if token == MESSAGE_TOKEN and len(group) <= MESSAGE_MAX_LINES:
            units.append(group)
            continue
        if token == CHOICE_TOKEN:
            units.extend([record] for record in group)
            continue
        units.extend([record] for record in group)
    units.sort(key=lambda group: group[0]["offset"])
    return units


def make_unit(stem: str, ordinal: int, records: list[dict], voice) -> dict:
    return {
        "id": f"{stem}:isb:{ordinal:04d}",
        "context": {"voice": voice},
        "original": "".join(record["text"] for record in records),
        "translation": "",
        "state": "untranslated",
        "use_translation": False,
        "storage": [
            {
                "offset": record["offset"],
                "length": record["length"],
                "key": record["key"],
                "original": record["text"],
            }
            for record in records
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    project = Path("work/galaxy_angel_eternal_lovers")
    parser.add_argument("--source-dir", type=Path, default=project / "source/scenario")
    parser.add_argument("--assets", type=Path, default=project / "assets/translation/isb")
    parser.add_argument("--report", type=Path, default=project / "build/isb_regroup_report.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    index = json.loads((args.assets / "index.json").read_text(encoding="utf-8"))
    carried = created = split_units = 0
    changed_files = []
    for entry in index["segments"]:
        segment_path = args.assets / entry["path"]
        segment = json.loads(segment_path.read_text(encoding="utf-8"))
        source_path = args.source_dir / segment["source"]["path"]
        data, records = japanese_records(source_path)
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != segment["source"]["sha256"]:
            raise SystemExit(f"source hash mismatch: {source_path}")

        previous = {
            tuple(int(item["offset"]) for item in unit["storage"]): unit
            for unit in segment["units"]
        }
        groups = group_records(data, records)
        stem = source_path.stem
        rebuilt = []
        file_split = 0
        for ordinal, group in enumerate(groups, 1):
            voice = None
            key = tuple(record["offset"] for record in group)
            old = previous.get(key)
            unit = make_unit(stem, ordinal, group, voice)
            if old is not None:
                unit["context"] = old.get("context", unit["context"])
                unit["translation"] = old.get("translation", "")
                unit["state"] = old.get("state", "untranslated")
                unit["use_translation"] = old.get("use_translation", False)
                for extra in ("max_bytes", "notes", "review"):
                    if extra in old:
                        unit[extra] = old[extra]
                carried += 1
            else:
                created += 1
                file_split += 1
            rebuilt.append(unit)
        if file_split:
            split_units += file_split
            changed_files.append({"file": source_path.name, "new_units": file_split,
                                  "units": len(rebuilt), "was": len(segment["units"])})
        if not args.dry_run:
            segment["units"] = rebuilt
            atomic_json(segment_path, segment)
            entry["units"] = len(rebuilt)

    report = {
        "schema": "eternal-lovers-isb-regroup/v1",
        "carried_units": carried,
        "new_units": created,
        "files_changed": len(changed_files),
        "changed": changed_files,
    }
    if not args.dry_run:
        index["population"]["translation_units"] = carried + created
        atomic_json(args.assets / "index.json", index)
    atomic_json(args.report, report)
    print(
        f"carried={carried} new={created} files_changed={len(changed_files)} "
        f"report={args.report}"
    )


if __name__ == "__main__":
    sys.exit(main())
