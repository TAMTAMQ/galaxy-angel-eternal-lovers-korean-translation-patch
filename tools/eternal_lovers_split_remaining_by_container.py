#!/usr/bin/env python3
"""Split the reviewed non-ISB remaining-text payload by target container.

A candidate that occurs in multiple containers is copied to every relevant output,
with only the occurrences for that container retained. This lets the existing
remaining patcher be run in bounded stages without changing patch semantics.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    by_container: dict[str, list[dict]] = {}
    for candidate in payload.get("candidates", []):
        occurrences_by_container: dict[str, list[dict]] = {}
        for occurrence in candidate.get("occurrences", []):
            container = str(occurrence.get("container") or "")
            if not container:
                continue
            occurrences_by_container.setdefault(container, []).append(occurrence)
        for container, occurrences in occurrences_by_container.items():
            by_container.setdefault(container, []).append(
                {**candidate, "occurrences": occurrences}
            )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for container, candidates in sorted(by_container.items()):
        output = args.output_dir / f"{container}.json"
        output.write_text(
            json.dumps(
                {
                    "schema": payload.get("schema"),
                    "source": str(args.input),
                    "container": container,
                    "candidates": candidates,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        occurrences = sum(len(item.get("occurrences", [])) for item in candidates)
        print(
            f"{container}: candidates={len(candidates)} occurrences={occurrences} output={output}"
        )


if __name__ == "__main__":
    main()
