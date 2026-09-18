#!/usr/bin/env python3
"""Print compact original/translation pairs from Eternal Lovers remaining text candidates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_INPUT = Path("assets/translation/remaining/remaining_candidates.json")


def one_line(text: str) -> str:
    return text.replace("\r", "\\r").replace("\n", "\\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--start", type=int, default=1, help="1-based first candidate")
    parser.add_argument("--end", type=int, help="1-based inclusive last candidate")
    parser.add_argument("--container", help="only candidates occurring in this container")
    parser.add_argument("--state", help="only candidates with this state")
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    candidates = payload["candidates"]
    start = max(args.start, 1)
    end = args.end if args.end is not None else len(candidates)

    for index, item in enumerate(candidates, 1):
        if index < start or index > end:
            continue
        if args.state and item.get("state") != args.state:
            continue
        if args.container:
            containers = {occ.get("container") for occ in item.get("occurrences", [])}
            if args.container not in containers:
                continue
        print(
            f"{index:04d}\t{item['id']}\t{item.get('state', '')}\t"
            f"{one_line(item.get('original', ''))}\t{one_line(item.get('translation', ''))}"
        )


if __name__ == "__main__":
    main()
