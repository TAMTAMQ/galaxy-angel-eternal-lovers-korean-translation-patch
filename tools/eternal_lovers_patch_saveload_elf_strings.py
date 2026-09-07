#!/usr/bin/env python3
"""Patch user-visible Save/Load slot strings embedded directly in Eternal Lovers' ELF."""
from __future__ import annotations

import argparse
import hashlib
import json
import mmap
from pathlib import Path

import galaxy_angel_build as builder
import galaxy_angel_translation as translation

PATCHES = (
    ("%02d 壊れたゲームデータ", "%02d 손상된 게임 데이터"),
    (" ───　未使用　─── ", " ───　미사용　─── "),
    (" 　壊れたゲームデータ　 ", " 　손상된 게임 데이터　 "),
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def patch_bytes(data: bytes, encoding_map: Path) -> tuple[bytes, list[dict[str, object]]]:
    custom_map = translation.load_custom_map(encoding_map)
    if custom_map is None:
        raise SystemExit(f"custom encoding map is required: {encoding_map}")
    rebuilt = bytearray(data)
    rows: list[dict[str, object]] = []
    for japanese, korean in PATCHES:
        old = japanese.encode("cp932")
        new = translation.encode_text(korean, custom_map)
        if len(new) != len(old):
            raise SystemExit(
                f"fixed ELF string length changed for {japanese!r}: {len(old)} -> {len(new)}"
            )
        positions: list[int] = []
        cursor = 0
        while True:
            pos = data.find(old, cursor)
            if pos < 0:
                break
            positions.append(pos)
            cursor = pos + 1
        if len(positions) != 1:
            raise SystemExit(
                f"expected exactly one ELF string {japanese!r}, found {len(positions)} at {positions}"
            )
        pos = positions[0]
        rebuilt[pos : pos + len(old)] = new
        rows.append(
            {
                "offset": pos,
                "japanese": japanese,
                "korean": korean,
                "bytes": len(old),
                "old_hex": old.hex(),
                "new_hex": new.hex(),
            }
        )
    for row in rows:
        pos = int(row["offset"])
        expected = bytes.fromhex(str(row["new_hex"]))
        if rebuilt[pos : pos + len(expected)] != expected:
            raise SystemExit(f"ELF Save/Load string readback failed at {pos:#x}")
    return bytes(rebuilt), rows


def patch_elf(elf: Path, encoding_map: Path, report: Path | None) -> dict[str, object]:
    before = elf.read_bytes()
    after, rows = patch_bytes(before, encoding_map)
    elf.write_bytes(after)
    result = {
        "schema": "eternal-lovers-saveload-elf-strings/v1",
        "target": str(elf.resolve()),
        "mode": "elf",
        "before_sha256": sha256_bytes(before),
        "after_sha256": sha256_bytes(after),
        "patched": rows,
    }
    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def patch_iso(iso: Path, executable: str, encoding_map: Path, report: Path | None) -> dict[str, object]:
    with iso.open("r+b") as stream, mmap.mmap(stream.fileno(), 0) as image:
        files = builder.iso_files(image)
        matches = [item for key, item in files.items() if key.rsplit("/", 1)[-1] == executable]
        if len(matches) != 1:
            raise SystemExit(f"ISO executable resolution failed for {executable}: {len(matches)} matches")
        item = matches[0]
        begin = item.extent * builder.SECTOR
        before = bytes(image[begin : begin + item.size])
        after, rows = patch_bytes(before, encoding_map)
        if len(after) != item.size:
            raise SystemExit("patched executable size changed")
        image[begin : begin + item.size] = after
        image.flush()
    with iso.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as image:
        files = builder.iso_files(image)
        matches = [item for key, item in files.items() if key.rsplit("/", 1)[-1] == executable]
        if len(matches) != 1:
            raise SystemExit(f"ISO executable resolution failed for {executable}: {len(matches)} matches")
        item = matches[0]
        begin = item.extent * builder.SECTOR
        readback = bytes(image[begin : begin + item.size])
    if readback != after:
        raise SystemExit("ISO executable readback mismatch")
    result = {
        "schema": "eternal-lovers-saveload-elf-strings/v1",
        "target": str(iso.resolve()),
        "mode": "iso",
        "executable": executable,
        "before_sha256": sha256_bytes(before),
        "after_sha256": sha256_bytes(after),
        "patched": rows,
    }
    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--elf", type=Path)
    target.add_argument("--iso", type=Path)
    parser.add_argument("--executable", default="SLPM_658.78")
    parser.add_argument("--encoding-map", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = (
        patch_elf(args.elf, args.encoding_map, args.report)
        if args.elf is not None
        else patch_iso(args.iso, args.executable, args.encoding_map, args.report)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
