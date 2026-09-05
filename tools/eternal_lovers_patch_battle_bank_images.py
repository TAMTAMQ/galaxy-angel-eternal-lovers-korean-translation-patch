#!/usr/bin/env python3
"""Write the translated battle textures into the FSTS-indexed per-stage banks.

``SLGRES`` and ``SLGSTAGE`` keep their own copy of each ``dat/slg/2dparts`` texture.  Two
existing mechanisms do not reach all of them:

* the image patcher's ``--runtime-container`` scan finds a copy only when its *compressed*
  bytes are byte-identical to the primary's, and about 140 banks re-compressed the same
  picture differently;
* the patcher's primary-container path only understands PIDX containers, while these banks are
  indexed by FSTS.

So this tool addresses each copy through its own FSTS record: it re-encodes the texture from
the bank's own original raw bytes, compresses it, writes it into that resource's slot and
updates the record's raw/compressed sizes.  A copy whose slot is too small is reported rather
than truncated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import struct
from pathlib import Path

from PIL import Image

import eternal_lovers_render_candidate_images as renderer
import galaxy_angel_build as builder
import ikusa_lz
import moonlit_lovers_resources as resources
from eternal_lovers_patch_remaining import encode_resource, merged_resources
from galaxy_angel_gadat032 import decode_tex, encode_tex, tex_info


def pixel_hash(image: Image.Image) -> str:
    rgba = image.convert("RGBA")
    digest = hashlib.sha256()
    digest.update(f"{rgba.width}x{rgba.height}:RGBA".encode("ascii"))
    digest.update(rgba.tobytes())
    return digest.hexdigest()


def compress_to_fit(
    original: bytes,
    replacement: Image.Image,
    capacity: int,
    codec: str,
    require_exact_pixels: bool = False,
):
    """Encode and compress, trying palette reductions only if the slot demands it.

    The bank's own codec is kept: a stored resource stays stored, an LZ one stays LZ.
    When ``require_exact_pixels`` is set, candidates whose decoded pixels differ from the
    supplied PNG are rejected instead of being accepted as a lossy palette fallback.
    """
    info = tex_info(original)
    replacement_hash = pixel_hash(replacement) if require_exact_pixels else None
    attempts: list[int | None] = [None]
    if info.palette_colours:
        attempts += [limit for limit in (256, 128, 64, 32, 16, 8, 4) if limit <= info.palette_colours]
    best = None
    exact_pixel_candidate_seen = False
    for colours in attempts:
        rebuilt = encode_tex(original, replacement, colours)
        if require_exact_pixels:
            rebuilt_hash = pixel_hash(decode_tex(rebuilt))
            if rebuilt_hash != replacement_hash:
                continue
            exact_pixel_candidate_seen = True
        compressed = encode_resource(rebuilt, codec)
        if len(compressed) <= capacity:
            return rebuilt, compressed, colours
        if codec == "ikusa_lz":
            optimal = ikusa_lz.compress_optimal(rebuilt)
            if len(optimal) <= capacity:
                return rebuilt, optimal, colours
            best = len(optimal) if best is None else min(best, len(optimal))
        else:
            best = len(compressed) if best is None else min(best, len(compressed))
    if require_exact_pixels and not exact_pixel_candidate_seen:
        raise ValueError("no TEX encoding preserves the supplied PNG pixels exactly")
    raise ValueError(f"does not fit its bank slot: best {best} > {capacity}")


# How much smaller a label may be drawn when its bank slot cannot hold the standard render.
SHRINK_STEPS = (2, 4, 6, 8, 10, 12)


def refit(project: Path, entry: dict, original: bytes, capacity: int, codec: str):
    """Re-draw one copy smaller until it fits the slot it has to live in.

    A bank slot is sized for the Japanese it replaces, and Korean of the same height sometimes
    compresses a few dozen bytes larger.  Shrinking the label everywhere to satisfy the one
    tight bank would make the primary texture and a thousand roomy stage banks worse, so only
    the copy in the tight slot is drawn smaller.
    """
    decisions = json.loads(
        (project / "assets/translation/images/candidate_translations.json").read_text(encoding="utf-8")
    )["entries"]
    source_name = entry.get("mirror_of")
    decision = decisions.get(source_name)
    if decision is None or decision.get("skip") or decision.get("defer") or not decision.get("ko"):
        return None, None
    slg = json.loads((project / "assets/full_extraction/SLG/manifest.json").read_text(encoding="utf-8"))
    by_name = {r["name"]: r for r in slg["resources"] if r.get("name")}
    resource = by_name.get(source_name)
    if resource is None or not resource.get("images"):
        return None, None
    png_path = project / "assets/full_extraction/SLG/png" / Path(resource["images"][0]["png"])
    for extra in SHRINK_STEPS:
        candidate = dict(decision)
        candidate["shrink"] = int(candidate.get("shrink", 0)) + extra
        try:
            image = renderer.render(png_path, candidate, project)
            rebuilt, compressed, _colours = compress_to_fit(original, image, capacity, codec)
        except ValueError:
            continue
        return rebuilt, compressed
    return None, None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--project", type=Path, default=Path("work/galaxy_angel_eternal_lovers"))
    parser.add_argument("--container", action="append", default=None)
    parser.add_argument("--layout", choices=("eternal", "moonlit"), default="eternal",
                        help="where the per-container image sets live")
    parser.add_argument("--manifest-name", default=None,
                        help="manifest inside the image set; defaults to the layout's own")
    parser.add_argument("--translated-dir", default=None,
                        help="folder inside the image set holding the Korean PNGs")
    parser.add_argument(
        "--preserve-pixels",
        action="store_true",
        help="reject any TEX encoding whose decoded pixels differ from the supplied PNG",
    )
    parser.add_argument(
        "--no-refit",
        action="store_true",
        help="fail rather than redrawing a translated label smaller to fit a bank slot",
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    containers = args.container or ["SLGRES", "SLGSTAGE"]
    report = {"schema": "eternal-lovers-battle-bank-images/v1", "containers": {}}

    with args.iso.open("r+b") as handle, mmap.mmap(handle.fileno(), 0) as image:
        files = builder.iso_files(image)
        for stem in containers:
            images_root = (
                args.project / "assets/image_extraction/japanese_images" / stem
                if args.layout == "eternal"
                else args.project / "assets/image_extraction" / stem / "japanese_images"
            )
            manifest_name = args.manifest_name or (
                "mirror_manifest.json"
            )
            translated_dir = args.translated_dir or (
                "mirror_png"
            )
            manifest_path = images_root / manifest_name
            if not manifest_path.is_file():
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            item = builder.resolve_iso_file(files, stem)
            begin = item.extent * builder.SECTOR
            container = bytearray(image[begin : begin + item.size])
            resource_map = merged_resources(bytes(container), stem)

            boundaries = sorted(resource_map)
            written, skipped, unchanged, refitted = 0, [], 0, 0
            for entry in manifest:
                offset = int(entry["resource_offset"])
                resource = resource_map.get(offset)
                if resource is None:
                    skipped.append({"png": entry["png"], "reason": "resource not found"})
                    continue
                index = boundaries.index(offset)
                end = boundaries[index + 1] if index + 1 < len(boundaries) else len(container)
                # The gap to the next resource this pass knows about is only free space when
                # it really is padding.  In ADV it is not: bytes past a resource carry records
                # the index of this container is built from, and clearing them to make room
                # silently loses resources.  So the slot ends where its own data ends unless
                # everything after it is zero.
                tail = container[offset + resource.compressed_size : end]
                capacity = (end - offset) if not any(tail) else resource.compressed_size

                try:
                    original_raw = resources.decompress_resource(
                        container, offset, resource.raw_size, resource.compressed_size
                    )
                except ValueError as error:
                    # A slot an earlier runtime-copy pass already rewrote decodes fine but no
                    # longer matches the size its record still declares.  Accept it when the
                    # picture in it is already the Korean one; otherwise report it.
                    try:
                        recovered, _consumed = ikusa_lz.decompress(bytes(container), offset)
                    except Exception:  # noqa: BLE001 - the original error is the useful one
                        skipped.append({"png": entry["png"], "reason": str(error)})
                        continue
                    with Image.open(images_root / translated_dir / entry["translated_png"]) as opened:
                        expected = opened.convert("RGBA")
                    if pixel_hash(decode_tex(recovered)) == pixel_hash(expected):
                        unchanged += 1
                        continue
                    skipped.append({"png": entry["png"], "reason": str(error)})
                    continue
                with Image.open(images_root / translated_dir / entry["translated_png"]) as opened:
                    replacement = opened.convert("RGBA")
                if pixel_hash(decode_tex(original_raw)) == pixel_hash(replacement):
                    unchanged += 1
                    continue
                try:
                    rebuilt, compressed, _colours = compress_to_fit(
                        original_raw,
                        replacement,
                        capacity,
                        resource.codec,
                        require_exact_pixels=args.preserve_pixels,
                    )
                except ValueError as error:
                    if args.no_refit or args.preserve_pixels:
                        raise ValueError(
                            f"{stem}/{entry['png']}: current translated image cannot be "
                            f"stored losslessly in its bank slot: {error}"
                        ) from error
                    # A mirror gets the same treatment as any other copy: refit()
                    # re-renders it from the canonical SLG source with a smaller
                    # label, so the picture stays the canonical one and only this
                    # tight bank slot carries the shrunk draw.
                    rebuilt, compressed = refit(
                        args.project, entry, original_raw, capacity, resource.codec
                    )
                    if rebuilt is None:
                        skipped.append({"png": entry["png"], "reason": str(error)})
                        continue
                    refitted += 1

                cleared = max(len(compressed), resource.compressed_size)
                container[offset : offset + cleared] = bytes(cleared)
                container[offset : offset + len(compressed)] = compressed
                for record in resource.record_positions:
                    struct.pack_into("<II", container, record + 8, len(rebuilt), len(compressed))
                written += 1

            image[begin : begin + item.size] = bytes(container)
            report["containers"][stem] = {
                "targets": len(manifest),
                "written": written,
                "already_translated": unchanged,
                "redrawn_smaller_to_fit": refitted,
                "skipped": skipped,
            }
        image.flush()

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: {x: (len(v[x]) if isinstance(v[x], list) else v[x]) for x in v}
                      for k, v in report["containers"].items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
