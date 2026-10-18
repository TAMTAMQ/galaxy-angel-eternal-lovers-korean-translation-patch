#!/usr/bin/env python3
"""Verify current subtitled movies and original no-subtitle movies in an ISO."""
import argparse
import hashlib
import importlib.util
import json
import re
from pathlib import Path


def region_hash(stream, offset, size):
    stream.seek(offset)
    digest = hashlib.sha256()
    while size:
        block = stream.read(min(size, 1024 * 1024))
        if not block:
            raise ValueError("unexpected EOF")
        digest.update(block)
        size -= len(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--patched-iso", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    tool = next((args.project / "tools").glob("*patch_all_movies_iso.py"))
    spec = importlib.util.spec_from_file_location("movie_patch", tool)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    authority = {}
    for pss in (args.project / "movie/subtitled/final").glob("GADAT*.PSS"):
        if not re.fullmatch(r"GADAT\d{3}", pss.stem):
            continue
        ass = args.project / "movie/subtitles" / (pss.stem + ".ko.ass")
        if not ass.is_file() or not any(line.startswith("Dialogue:") for line in ass.read_text(encoding="utf-8-sig").splitlines()):
            raise ValueError(f"unnecessary subtitled PSS remains: {pss.name}")
        authority[pss.stem] = pss
    result = {"project": args.project.name, "subtitled": [], "original": []}
    with args.original_iso.open("rb") as original, args.patched_iso.open("rb") as patched:
        old_meta = original.read(module.META_BYTES)
        new_meta = patched.read(module.META_BYTES)
        names = sorted({match.decode() for match in re.findall(rb"GADAT\d{3}(?=\.PSS;1)", old_meta)})
        for name in names:
            current = module.find_record(new_meta, name + ".PSS;1")
            actual = region_hash(patched, int(current["extent"]) * module.SECTOR, int(current["size"]))
            if name in authority:
                pss = authority[name]
                with pss.open("rb") as stream:
                    expected = region_hash(stream, 0, pss.stat().st_size)
                size = pss.stat().st_size
                group = "subtitled"
            else:
                source = module.find_record(old_meta, name + ".PSS;1")
                size = int(source["size"])
                expected = region_hash(original, int(source["extent"]) * module.SECTOR, size)
                group = "original"
            if int(current["size"]) != size or actual != expected:
                raise ValueError(f"{name}: {group} movie mismatch")
            result[group].append({"name": name, "sha256": actual})
    if len(result["subtitled"]) != len(authority):
        raise ValueError("not every authoritative PSS was verified")
    args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"project": args.project.name, "subtitled_verified": len(result["subtitled"]), "original_verified": len(result["original"]), "matched": True}))


if __name__ == "__main__":
    main()
