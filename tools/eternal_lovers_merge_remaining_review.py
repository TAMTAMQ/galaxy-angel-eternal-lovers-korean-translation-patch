#!/usr/bin/env python3
"""Merge tracked remaining-text review overrides into the large extracted index.

The extracted ``remaining_candidates.json`` is intentionally ignored because it is
hundreds of megabytes.  Human/model review corrections are kept in the compact,
tracked ``review_overrides.json``.  This tool validates every override against the
current extracted Japanese source before producing a build input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


FULLWIDTH_TRANSLATION = str.maketrans(
    {
        **{chr(0xFF10 + index): chr(ord("0") + index) for index in range(10)},
        **{chr(0xFF21 + index): chr(ord("A") + index) for index in range(26)},
        **{chr(0xFF41 + index): chr(ord("a") + index) for index in range(26)},
        "！": "!",
        "？": "?",
        "、": ",",
        "。": ".",
        "　": " ",
    }
)


def normalize_reviewed_translation(text: str) -> str:
    """Apply the project's fixed width/punctuation rules without touching ー/～."""
    return text.translate(FULLWIDTH_TRANSLATION)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--overrides", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    overlay = load_json(args.overrides)
    expected_index_sha256 = overlay.get("index_sha256")
    if expected_index_sha256:
        actual_index_sha256 = hashlib.sha256(args.index.read_bytes()).hexdigest()
        if actual_index_sha256 != expected_index_sha256:
            raise SystemExit(
                f"remaining index SHA-256 mismatch: {actual_index_sha256} != "
                f"{expected_index_sha256}"
            )
    payload = load_json(args.index)
    candidates = payload.get("candidates", [])
    by_id = {item.get("id"): item for item in candidates}
    if len(by_id) != len(candidates):
        raise SystemExit("duplicate candidate id in remaining index")

    normalized = 0
    for source in candidates:
        if source.get("state") == "internal_identifier":
            continue
        translation = source.get("translation")
        if not isinstance(translation, str) or not translation:
            continue
        fixed = normalize_reviewed_translation(translation)
        if fixed != translation:
            source["translation"] = fixed
            normalized += 1

    entries = overlay.get("entries", [])
    seen: set[str] = set()
    applied = 0
    for entry in entries:
        unit_id = str(entry["id"])
        if unit_id in seen:
            raise SystemExit(f"duplicate override id: {unit_id}")
        seen.add(unit_id)
        source = by_id.get(unit_id)
        if source is None:
            raise SystemExit(f"override id missing from remaining index: {unit_id}")
        if "original" in entry:
            original = str(entry["original"])
            if source.get("original") != original:
                raise SystemExit(
                    f"override source mismatch: {unit_id}\n"
                    f"  index={source.get('original')!r}\n"
                    f"  override={original!r}"
                )
        translation = str(entry["translation"])
        if not translation:
            raise SystemExit(f"empty override translation: {unit_id}")
        source["translation"] = translation
        source["state"] = str(entry.get("state", "reviewed"))
        source["use_translation"] = bool(entry.get("use_translation", True))
        applied += 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"remaining review merge: candidates={len(candidates)} normalized={normalized} "
        f"overrides={len(entries)} applied={applied} output={args.output}"
    )


if __name__ == "__main__":
    main()
