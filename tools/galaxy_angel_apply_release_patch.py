#!/usr/bin/env python3
"""Build the release ISO by applying the release patch, the way a player would.

eternal_lovers_make_release.py already proves the patch reproduces the build, but
it deletes the ISO it decoded to check.  This produces that ISO and keeps it, so
the artifact that ships is the patch's own output rather than a build tree copy
that merely hashes the same.

The original ISO and the result are both checked against release.json, so a patch
applied to the wrong disc, or a decode that silently truncated, fails here rather
than in the emulator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pyxdelta


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--output-iso", type=Path, required=True)
    args = parser.parse_args()

    release = json.loads((args.release_dir / "release.json").read_text(encoding="utf-8"))
    patch = args.release_dir / release["patch"]["name"]
    if not patch.is_file():
        raise SystemExit(f"missing patch: {patch}")

    actual = sha256(args.original_iso)
    if actual != release["source"]["sha256"]:
        raise SystemExit(
            f"original ISO is not the one this patch was made from:\n"
            f"  have {actual}\n  want {release['source']['sha256']}"
        )

    args.output_iso.parent.mkdir(parents=True, exist_ok=True)
    if not pyxdelta.decode(str(args.original_iso), str(patch), str(args.output_iso)):
        raise SystemExit("xdelta decode failed")

    produced = sha256(args.output_iso)
    if produced != release["target"]["sha256"]:
        raise SystemExit(
            f"patched ISO does not match the release:\n"
            f"  have {produced}\n  want {release['target']['sha256']}"
        )
    size = args.output_iso.stat().st_size
    if size != release["target"]["size"]:
        raise SystemExit(f"patched ISO size {size} != {release['target']['size']}")

    print(f"{release['title']} {release['version']}")
    print(f"  patch  {patch.name}")
    print(f"  output {args.output_iso}")
    print(f"  sha256 {produced}  size {size:,}  OK")


if __name__ == "__main__":
    main()
