from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from pathlib import Path

SECTOR = 2048
META_BYTES = 8 * 1024 * 1024
EXPECTED_MOVIES = 24


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(16 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_region(path: Path, offset: int, size: int) -> str:
    h = hashlib.sha256()
    remaining = size
    with path.open("rb") as f:
        f.seek(offset)
        while remaining:
            chunk = f.read(min(16 * 1024 * 1024, remaining))
            if not chunk:
                raise ValueError("unexpected EOF while hashing ISO region")
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def both_u32(data: bytes | bytearray, off: int) -> int:
    le = int.from_bytes(data[off : off + 4], "little")
    be = int.from_bytes(data[off + 4 : off + 8], "big")
    if le != be:
        raise ValueError(f"ISO9660 both-endian mismatch at 0x{off:x}: {le} != {be}")
    return le


def both_u32_bytes(value: int) -> bytes:
    return value.to_bytes(4, "little") + value.to_bytes(4, "big")


def find_record(meta: bytes, identifier: str) -> dict[str, int | str]:
    needle = identifier.encode("ascii")
    pos = meta.find(needle)
    if pos < 0:
        raise ValueError(f"ISO9660 identifier not found: {identifier}")
    if meta.find(needle, pos + 1) >= 0:
        raise ValueError(f"ISO9660 identifier is not unique: {identifier}")
    start = pos - 33
    if start < 0:
        raise ValueError(f"invalid directory record start for {identifier}")
    record_len = meta[start]
    name_len = meta[start + 32]
    if pos + name_len > start + record_len:
        raise ValueError(f"invalid directory record bounds for {identifier}")
    name = meta[pos : pos + name_len].decode("ascii")
    if name != identifier:
        raise ValueError(f"identifier mismatch: {name!r} != {identifier!r}")
    return {
        "identifier": identifier,
        "record_offset": start,
        "record_length": record_len,
        "extent": both_u32(meta, start + 2),
        "size": both_u32(meta, start + 10),
    }


def contiguous_runs(records: list[dict[str, object]]) -> list[dict[str, object]]:
    ordered = sorted(records, key=lambda r: int(r["old_extent"]))
    runs: list[dict[str, object]] = []
    for rec in ordered:
        start = int(rec["old_extent"])
        sectors = int(rec["old_sectors"])
        end = start + sectors
        if runs and start == int(runs[-1]["end"]):
            runs[-1]["end"] = end
            runs[-1]["records"].append(str(rec["name"]))  # type: ignore[index]
        else:
            runs.append({"start": start, "end": end, "records": [str(rec["name"])]})
    for run in runs:
        run["sectors"] = int(run["end"]) - int(run["start"])
    return runs


def choose_relocations(records: list[dict[str, object]], capacity_sectors: int) -> set[str]:
    total_new = sum(int(r["new_sectors"]) for r in records)
    excess = total_new - capacity_sectors
    if excess <= 0:
        return set()
    covering = sorted(
        (r for r in records if int(r["new_sectors"]) >= excess),
        key=lambda r: int(r["new_sectors"]),
    )
    if covering:
        return {str(covering[0]["name"])}
    chosen: set[str] = set()
    freed = 0
    for rec in sorted(records, key=lambda r: int(r["new_sectors"]), reverse=True):
        chosen.add(str(rec["name"]))
        freed += int(rec["new_sectors"])
        if freed >= excess:
            break
    if freed < excess:
        raise ValueError("unable to free enough movie-pool sectors")
    return chosen


def patch_volume_space_size(handle, sectors: int) -> list[int]:
    patched: list[int] = []
    for lba in range(16, 64):
        handle.seek(lba * SECTOR)
        vd = handle.read(SECTOR)
        if len(vd) != SECTOR:
            break
        if vd[1:6] != b"CD001":
            continue
        vd_type = vd[0]
        if vd_type == 255:
            break
        if vd_type in (1, 2):
            handle.seek(lba * SECTOR + 80)
            handle.write(both_u32_bytes(sectors))
            patched.append(lba)
    return patched


def main() -> int:
    ap = argparse.ArgumentParser(description="Patch all 39 Eternal Lovers subtitle PSS files into the Korean ISO.")
    ap.add_argument("--iso", type=Path, default=Path("build/Galaxy_Angel_Eternal_Lovers_KO_v18.iso"))
    ap.add_argument("--pss-dir", type=Path, default=Path("movie/subtitled/final"))
    ap.add_argument("--subtitles-dir", type=Path, default=Path("movie/subtitles"))
    ap.add_argument("--output", type=Path, default=Path("build/Galaxy_Angel_Eternal_Lovers_KO_v0.1_SUBTITLED.iso"))
    ap.add_argument("--report", type=Path, default=Path("build/eternal_lovers_movies_iso_patch.json"))
    ap.add_argument("--names", nargs="*", help="Patch only the named GADAT stems, e.g. GADAT101")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()

    nonempty_names = {
        p.name.removesuffix(".ko.ass")
        for p in args.subtitles_dir.glob("GADAT*.ko.ass")
        if any(line.startswith("Dialogue:") for line in p.read_text(encoding="utf-8").splitlines())
    }
    selected_names = set(args.names) if args.names else set(nonempty_names)
    unknown = selected_names - nonempty_names
    if unknown:
        raise ValueError(f"requested names are not non-empty subtitle movies: {sorted(unknown)}")
    replacements = sorted(
        p for p in args.pss_dir.glob("GADAT*.PSS")
        if p.stem in selected_names
    )
    if args.names:
        if {p.stem for p in replacements} != selected_names:
            missing = sorted(selected_names - {p.stem for p in replacements})
            raise ValueError(f"requested replacement PSS files are missing: {missing}")
    elif len(replacements) != EXPECTED_MOVIES:
        raise ValueError(
            f"expected {EXPECTED_MOVIES} non-empty-subtitle PSS files, found {len(replacements)}"
        )

    with args.iso.open("rb") as f:
        meta = f.read(META_BYTES)

    records: list[dict[str, object]] = []
    for replacement in replacements:
        name = replacement.stem
        rec = find_record(meta, f"{name}.PSS;1")
        old_size = int(rec["size"])
        new_size = replacement.stat().st_size
        records.append(
            {
                "name": name,
                "identifier": rec["identifier"],
                "record_offset": rec["record_offset"],
                "old_extent": rec["extent"],
                "old_size": old_size,
                "old_sectors": math.ceil(old_size / SECTOR),
                "new_size": new_size,
                "new_sectors": math.ceil(new_size / SECTOR),
                "delta_bytes": new_size - old_size,
                "replacement": str(replacement.resolve()),
                "replacement_sha256": sha256_file(replacement),
            }
        )

    source_sectors = math.ceil(args.iso.stat().st_size / SECTOR)

    # A previous subtitle build may have reduced only the ISO9660 file-size field
    # while leaving the original physical extent allocation untouched.  In that
    # case comparing new_sectors with old_sectors needlessly relocates a movie
    # even though the gap up to the next movie still has enough room.  Measure
    # the actual current extent capacity across all 39 movie records instead.
    all_movie_records: list[dict[str, int | str]] = []
    for subtitle_path in sorted(args.subtitles_dir.glob("GADAT*.ko.ass")):
        movie_name = subtitle_path.name.removesuffix(".ko.ass")
        current = find_record(meta, f"{movie_name}.PSS;1")
        all_movie_records.append(
            {
                "name": movie_name,
                "extent": int(current["extent"]),
                "size": int(current["size"]),
            }
        )
    ordered_extents = sorted({int(r["extent"]) for r in all_movie_records})
    next_extent_by_extent: dict[int, int] = {}
    for i, extent in enumerate(ordered_extents):
        next_extent_by_extent[extent] = ordered_extents[i + 1] if i + 1 < len(ordered_extents) else source_sectors

    for rec in records:
        old_extent = int(rec["old_extent"])
        rec["inplace_capacity_sectors"] = next_extent_by_extent[old_extent] - old_extent

    relocated: set[str] = set()
    placements: dict[str, int] = {}
    run_plans: list[dict[str, object]] = []

    if all(int(rec["new_sectors"]) <= int(rec["inplace_capacity_sectors"]) for rec in records):
        for rec in records:
            placements[str(rec["name"])] = int(rec["old_extent"])
        run_plans.append(
            {
                "mode": "preserve-current-extents-with-gap-capacity",
                "records": [str(rec["name"]) for rec in records],
                "old_sectors": sum(int(rec["old_sectors"]) for rec in records),
                "new_sectors": sum(int(rec["new_sectors"]) for rec in records),
                "minimum_inplace_slack_sectors": min(
                    int(rec["inplace_capacity_sectors"]) - int(rec["new_sectors"])
                    for rec in records
                ),
                "relocated": [],
            }
        )
        final_sectors = source_sectors
    else:
        runs = contiguous_runs(records)
        for run in runs:
            start = int(run["start"])
            end = int(run["end"])
            members = [r for r in records if start <= int(r["old_extent"]) < end]
            capacity = int(run["sectors"])
            run_relocated = choose_relocations(members, capacity)
            relocated.update(run_relocated)

            cursor = start
            for rec in sorted(members, key=lambda r: int(r["old_extent"])):
                name = str(rec["name"])
                if name in run_relocated:
                    continue
                placements[name] = cursor
                cursor += int(rec["new_sectors"])
            if cursor > end:
                raise AssertionError(f"packed movie files exceed original movie pool {start}:{end}")
            run_plans.append(
                {
                    **run,
                    "new_sectors_before_relocation": sum(int(r["new_sectors"]) for r in members),
                    "relocated": sorted(run_relocated),
                    "packed_end": cursor,
                    "free_sectors_after_pack": end - cursor,
                }
            )

        append_cursor = source_sectors
        for rec in sorted(records, key=lambda r: int(r["old_extent"])):
            name = str(rec["name"])
            if name not in relocated:
                continue
            placements[name] = append_cursor
            append_cursor += int(rec["new_sectors"])
        final_sectors = max(source_sectors, append_cursor)
    plan: dict[str, object] = {
        "schema": "eternal-lovers-all-movies-iso-patch/v1",
        "source_iso": str(args.iso.resolve()),
        "source_iso_bytes": args.iso.stat().st_size,
        "source_iso_sha256": sha256_file(args.iso),
        "source_iso_sectors": source_sectors,
        "movie_pools": run_plans,
        "old_movie_sectors": sum(int(r["old_sectors"]) for r in records),
        "new_movie_sectors": sum(int(r["new_sectors"]) for r in records),
        "movie_sector_delta": sum(int(r["new_sectors"]) for r in records) - sum(int(r["old_sectors"]) for r in records),
        "relocated_to_append": sorted(relocated),
        "append_sectors": final_sectors - source_sectors,
        "final_iso_sectors": final_sectors,
        "final_iso_bytes": final_sectors * SECTOR,
        "records": [],
    }
    for rec in records:
        item = dict(rec)
        item["new_extent"] = placements[str(rec["name"])]
        item["relocated"] = str(rec["name"]) in relocated
        plan["records"].append(item)  # type: ignore[union-attr]

    if args.plan_only:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.iso, args.output)
    with args.output.open("r+b") as out:
        out.truncate(final_sectors * SECTOR)
        for rec in records:
            name = str(rec["name"])
            replacement = Path(str(rec["replacement"]))
            extent = placements[name]
            out.seek(extent * SECTOR)
            with replacement.open("rb") as src:
                shutil.copyfileobj(src, out, length=16 * 1024 * 1024)
            pad = int(rec["new_sectors"]) * SECTOR - int(rec["new_size"])
            if pad:
                out.write(b"\x00" * pad)
            record_offset = int(rec["record_offset"])
            out.seek(record_offset + 2)
            out.write(both_u32_bytes(extent))
            out.seek(record_offset + 10)
            out.write(both_u32_bytes(int(rec["new_size"])))
        volume_descriptors = patch_volume_space_size(out, final_sectors)

    with args.output.open("rb") as f:
        out_meta = f.read(META_BYTES)
    verification: list[dict[str, object]] = []
    ranges: list[tuple[int, int, str]] = []
    for rec in records:
        name = str(rec["name"])
        expected_extent = placements[name]
        expected_size = int(rec["new_size"])
        current = find_record(out_meta, str(rec["identifier"]))
        if int(current["extent"]) != expected_extent or int(current["size"]) != expected_size:
            raise ValueError(f"{name}: ISO9660 directory record verification failed: {current}")
        actual_hash = sha256_region(args.output, expected_extent * SECTOR, expected_size)
        if actual_hash != rec["replacement_sha256"]:
            raise ValueError(f"{name}: readback SHA-256 mismatch")
        ranges.append((expected_extent, expected_extent + int(rec["new_sectors"]), name))
        verification.append(
            {
                "name": name,
                "extent": expected_extent,
                "size": expected_size,
                "sha256": actual_hash,
                "readback_matches": True,
            }
        )
    ranges.sort()
    for a, b in zip(ranges, ranges[1:]):
        if a[1] > b[0]:
            raise ValueError(f"movie extents overlap: {a} vs {b}")

    all_movie_names = {
        p.name.removesuffix(".ko.ass")
        for p in args.subtitles_dir.glob("GADAT*.ko.ass")
    }
    untouched_verification: list[dict[str, object]] = []
    for name in sorted(all_movie_names - selected_names):
        identifier = f"{name}.PSS;1"
        source_rec = find_record(meta, identifier)
        output_rec = find_record(out_meta, identifier)
        if (
            int(output_rec["extent"]) != int(source_rec["extent"])
            or int(output_rec["size"]) != int(source_rec["size"])
        ):
            raise ValueError(f"{name}: untouched ISO9660 directory record changed")
        extent = int(source_rec["extent"])
        size = int(source_rec["size"])
        source_hash = sha256_region(args.iso, extent * SECTOR, size)
        output_hash = sha256_region(args.output, extent * SECTOR, size)
        if output_hash != source_hash:
            raise ValueError(f"{name}: untouched PSS region changed")
        untouched_verification.append(
            {
                "name": name,
                "extent": extent,
                "size": size,
                "sha256": output_hash,
                "matches_source_iso": True,
            }
        )

    plan["volume_descriptors_updated"] = volume_descriptors
    plan["verification"] = verification
    plan["untouched_verification"] = untouched_verification
    plan["output_iso"] = str(args.output.resolve())
    plan["output_iso_size"] = args.output.stat().st_size
    plan["output_iso_sha256"] = sha256_file(args.output)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
