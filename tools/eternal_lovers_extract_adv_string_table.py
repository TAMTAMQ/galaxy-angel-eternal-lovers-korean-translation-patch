#!/usr/bin/env python3
"""Add dat/gadat000/adv_string.tbl to the remaining-text candidate set.

The original text audit walked GADAT000 but never produced a candidate for this
resource, so the speaker name plate, the chapter/stage/movie name lists stayed
Japanese in a disc where the dialogue itself is Korean.

Its lines look like ``#1<tab>=タクト`` or ``#201=ルシャーティ<tab>; comment``.
The value is the run between ``=`` and the end of the line or the first ``;``
comment, which is what the remaining-text patcher replaces in place.  Lines
that are themselves commented out (``;\t#20\t=...``) are dead entries and are
skipped: rewriting them would change nothing on screen and would waste slot
space.

The resource is stored once in GADAT000 and mirrored into three FSTS copies in
ADV, so a candidate carries an occurrence for every copy; missing one leaves the
container the game actually reads untranslated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import re
from pathlib import Path

import galaxy_angel_build as builder
import moonlit_lovers_resources as resources

RESOURCE_PATH = "dat/gadat000/adv_string.tbl"
MARKER = "[SPEAKER]".encode("cp932")
SECTION_RE = re.compile(r"^\[([A-Z_]+)\]")
ENTRY_RE = re.compile(r"^(#[0-9A-Fa-fxX]+)[\t ]*=(.*)$")
JAPANESE_RE = re.compile(r"[぀-ヿ㐀-鿿！-｠]")


def find_copies(image, files, stems=("GADAT000", "ADV")):
    """Every stored copy of the table, keyed by container."""
    copies = []
    for stem in stems:
        item = builder.resolve_iso_file(files, stem)
        begin = item.extent * builder.SECTOR
        data = bytes(image[begin : begin + item.size])
        for resource in resources.container_resources(data, stem):
            try:
                raw = resources.decompress_resource(
                    data, resource.offset, resource.raw_size, resource.compressed_size
                )
            except Exception:
                continue
            if MARKER not in raw:
                continue
            copies.append(
                {
                    "file": f"{stem}.DAT",
                    "container": stem,
                    "block_offset": resource.offset,
                    "resource_path": RESOURCE_PATH,
                    "resource_name": "adv_string.tbl",
                    "codec": resource.codec,
                    "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "_raw": raw,
                }
            )
    return copies


def parse_entries(raw: bytes):
    """(line number, section, value) for every live, translatable entry."""
    text = raw.decode("cp932")
    section = None
    entries = []
    for number, line in enumerate(text.split("\n"), 1):
        body = line.rstrip("\r")
        stripped = body.strip()
        if not stripped or stripped.startswith(";"):
            match = SECTION_RE.match(stripped)
            if match:
                section = match.group(1)
            continue
        match = SECTION_RE.match(stripped)
        if match:
            section = match.group(1)
            continue
        match = ENTRY_RE.match(stripped)
        if not match:
            continue
        value = match.group(2)
        for marker in (";", "//"):
            comment = value.find(marker)
            if comment >= 0:
                value = value[:comment]
        value = value.strip("\t ")
        if not value or not JAPANESE_RE.search(value):
            continue
        entries.append((number, section, value))
    return entries


def main() -> None:
    project = Path("work/galaxy_angel_eternal_lovers")
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument(
        "--candidates",
        type=Path,
        default=project / "assets/translation/remaining/remaining_candidates.json",
    )
    parser.add_argument(
        "--translations",
        type=Path,
        default=project / "assets/translation/adv_string_table_ko.json",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with args.original_iso.open("rb") as stream, mmap.mmap(
        stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as image:
        files = builder.iso_files(image)
        copies = find_copies(image, files)
    if not copies:
        raise SystemExit("adv_string.tbl was not found in GADAT000 or ADV")
    raws = {copy["raw_sha256"] for copy in copies}
    if len(raws) != 1:
        raise SystemExit(f"adv_string.tbl copies differ: {sorted(raws)}")
    raw = copies[0].pop("_raw")
    for copy in copies:
        copy.pop("_raw", None)

    entries = parse_entries(raw)
    korean = json.loads(args.translations.read_text(encoding="utf-8"))["translations"]

    payload = json.loads(args.candidates.read_text(encoding="utf-8"))
    existing = {
        (occurrence["resource_path"], occurrence["line"], candidate["original"])
        for candidate in payload["candidates"]
        for occurrence in candidate["occurrences"]
    }
    next_id = 1 + max(
        int(candidate["id"].split(":")[1]) for candidate in payload["candidates"]
    )

    by_text: dict[str, list] = {}
    for number, section, value in entries:
        by_text.setdefault(value, []).append((number, section))

    added = missing = 0
    for value, positions in sorted(by_text.items(), key=lambda item: item[1][0][0]):
        if (RESOURCE_PATH, positions[0][0], value) in existing:
            continue
        translation = korean.get(value)
        if translation is None:
            missing += 1
            continue
        occurrences = []
        for copy in copies:
            for number, section in positions:
                occurrence = dict(copy)
                occurrence["line"] = number
                occurrence["section"] = section
                occurrences.append(occurrence)
        payload["candidates"].append(
            {
                "id": f"remaining:{next_id:05d}",
                "original": value,
                "translation": translation,
                "state": "draft",
                "use_translation": translation != value,
                "occurrences": occurrences,
            }
        )
        next_id += 1
        added += 1

    payload["unique_strings"] = len(payload["candidates"])
    payload["total_occurrences"] = sum(
        len(candidate["occurrences"]) for candidate in payload["candidates"]
    )
    for container in {copy["container"] for copy in copies}:
        payload["by_container"][container] = sum(
            1
            for candidate in payload["candidates"]
            for occurrence in candidate["occurrences"]
            if occurrence["container"] == container
        )

    print(
        f"copies={len(copies)} entries={len(entries)} unique={len(by_text)} "
        f"added={added} missing_translation={missing}"
    )
    for value in sorted(by_text):
        if value not in korean:
            print("  MISSING", repr(value))
    if not args.dry_run and added:
        temporary = args.candidates.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(args.candidates)
        print("updated", args.candidates)


if __name__ == "__main__":
    main()
