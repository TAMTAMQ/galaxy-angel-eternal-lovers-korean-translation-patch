#!/usr/bin/env python3
"""Pack and unpack the translation inputs that are too large to publish raw.

A couple of the translation files carry one entry per *occurrence* rather than
per string - `remaining_candidates.json` alone lists about 590,000 of them - and
runs to a few hundred megabytes, past what a Git host accepts for a single file.
They compress to a few percent of that, so the repository carries the `.json.gz`
and the build expands it.

    python tools/galaxy_angel_expand_translations.py            # .gz -> .json
    python tools/galaxy_angel_expand_translations.py --pack     # .json -> .gz

Packing is byte-for-byte reversible: the JSON is stored as-is, not re-serialised.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
from pathlib import Path

# Files published gzipped.  Anything listed here is expected next to its .gz.
LARGE_INPUTS = (
    "assets/translation/remaining/remaining_candidates.json",
    "assets/translation/remaining/all_text_resources.json",
    "assets/translation/remaining/runtime_duplicate_hashes.json",
    "assets/translation/images/image_units.json",
    "assets/translation/battle/battle_units.json",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--pack", action="store_true",
                        help="compress the .json files instead of expanding them")
    parser.add_argument("--force", action="store_true",
                        help="overwrite a file that already exists")
    args = parser.parse_args()

    done = skipped = 0
    for relative in LARGE_INPUTS:
        plain = args.project / relative
        packed = plain.with_suffix(plain.suffix + ".gz")
        source, target = (plain, packed) if args.pack else (packed, plain)
        if not source.is_file():
            continue
        if target.is_file() and not args.force:
            print(f"  keep   {target.name} (already there)")
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if args.pack:
            target.write_bytes(gzip.compress(source.read_bytes(), 6))
        else:
            target.write_bytes(gzip.decompress(source.read_bytes()))
        print(f"  {'pack ' if args.pack else 'expand'} {source.name} "
              f"({source.stat().st_size / 2**20:.1f} MB) -> {target.name} "
              f"({target.stat().st_size / 2**20:.1f} MB)")
        done += 1

    if not done and not skipped:
        raise SystemExit("nothing to do: none of the large inputs were found")
    print(f"{'packed' if args.pack else 'expanded'} {done}, kept {skipped}")


if __name__ == "__main__":
    main()
