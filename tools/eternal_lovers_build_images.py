#!/usr/bin/env python3
"""Rebuild the reproducible Eternal Lovers image-localized WIP ISO."""

from __future__ import annotations

import argparse
import hashlib
import mmap
import shutil
import subprocess
import sys
from pathlib import Path

import galaxy_angel_build as iso_builder


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "work/galaxy_angel_eternal_lovers"
ORIGINAL_SHA256 = "31cb2a0b6a219323ea8fc451050a75f06fc0947fb0ff33b182835adf7b6da25d"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str]) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def patch_elf(iso_path: Path, elf_path: Path) -> None:
    with iso_path.open("r+b") as stream, mmap.mmap(stream.fileno(), 0) as image:
        files = iso_builder.iso_files(image)
        elf_file = next(
            item for key, item in files.items()
            if key.rsplit("/", 1)[-1] == "SLPM_658.78"
        )
        elf = elf_path.read_bytes()
        if len(elf) != elf_file.size:
            raise SystemExit(
                f"patched ELF size changed: {len(elf)} != {elf_file.size}"
            )
        begin = elf_file.extent * iso_builder.SECTOR
        image[begin:begin + elf_file.size] = elf
        if image[begin:begin + elf_file.size] != elf:
            raise SystemExit("patched ELF verification failed")
        image.flush()
        print(
            f"patched {elf_file.path} with {elf_path.name} "
            f"sha256={hashlib.sha256(elf).hexdigest()}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument(
        "--output-iso",
        type=Path,
        default=PROJECT / "build/Galaxy_Angel_Eternal_Lovers_KO_wip.iso",
    )
    parser.add_argument("--allow-unverified-original", action="store_true")
    parser.add_argument("--regenerate-images", action="store_true",
                        help="Explicitly redraw translated PNGs; default preserves approved images")
    args = parser.parse_args()

    original_digest = sha256(args.original_iso)
    if original_digest != ORIGINAL_SHA256 and not args.allow_unverified_original:
        raise SystemExit(
            f"original ISO SHA-256 mismatch: {original_digest} != {ORIGINAL_SHA256}"
        )

    output = args.output_iso.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    patcher = ROOT / "tools/galaxy_angel_patch_gadat032_images.py"
    images = PROJECT / "assets/image_extraction/japanese_images"
    full = PROJECT / "assets/full_extraction"
    build = PROJECT / "build"
    patched_elf = build / "SLPM_658.78.font_wip"

    # Approved translated PNGs are the default build inputs. Redrawing is opt-in only.
    gadat032_images = PROJECT / "assets/image_extraction/japanese_images/GADAT032"
    gadat032_resources = PROJECT / "assets/full_extraction/GADAT032/manifest.json"
    if args.regenerate_images:
        run([sys.executable, "-u", str(ROOT / "tools/eternal_lovers_render_content_gadat032.py")])
        for renderer in (
            "tools/eternal_lovers_render_flat_plates.py",
            "tools/eternal_lovers_render_room_popups.py",
        ):
            run([
                sys.executable, "-u", str(ROOT / renderer),
                "--images-root", str(gadat032_images),
                "--resource-manifest", str(gadat032_resources),
            ])
        run([
            sys.executable, "-u",
            str(PROJECT / "assets/analysis/render_gaplc.py"),
        ])
        for renderer in (
            "eternal_lovers_render_haplc.py",
            "eternal_lovers_render_album_letter_tiles.py",
            "eternal_lovers_render_candidate_images.py",
        ):
            run([sys.executable, "-u", str(ROOT / "tools" / renderer),
                 "--project", str(PROJECT)])
    # After the candidate renderer, which owns gfwin03.tex and would otherwise
    # overwrite it: the four copies of the save-card delete label are drawn once,
    # with one design, onto the box the Japanese occupied.
    run([
        sys.executable, "-u", str(ROOT / "tools/eternal_lovers_redraw_delete_labels.py"),
        "--project", str(PROJECT),
        "--report", str(build / "delete_label_report.json"),
    ])
    run([
        sys.executable, "-u", str(ROOT / "tools/eternal_lovers_mirror_battle_images.py"),
        "--project", str(PROJECT),
    ])

    run([
        sys.executable, "-u", str(ROOT / "tools/eternal_lovers_font.py"), "build",
        "--translations", str(
            PROJECT / "assets/translation/remaining/remaining_candidates.json"
        ),
        "--translations", str(PROJECT / "assets/translation/isb"),
        "--input-elf", str(PROJECT / "source/SLPM_658.78"),
        "--output-elf", str(patched_elf),
        "--map-output", str(build / "font_map.json"),
    ])

    run([
        sys.executable, "-u", str(ROOT / "tools/eternal_lovers_patch_isb.py"),
        "--source-dir", str(PROJECT / "source/scenario"),
        "--assets", str(PROJECT / "assets/translation/isb"),
        "--output-dir", str(build / "isb_scenario"),
        "--encoding-map", str(build / "font_map.json"),
        "--report", str(build / "isb_patch_report.json"),
    ])
    # Everything below edits this copy in place.
    shutil.copyfile(args.original_iso, output)

    # SCENARIO and SLG must keep the LBA the Japanese disc gave them, the way
    # galaxy_angel_build.py keeps GADAT001 fixed.  A PIDX offset is 32-bit and
    # relative to the container base, and this disc is 4.72 GB, so the overflow
    # cannot simply be appended at the end - it would be out of reach.  Free a
    # stretch that is in range first; the builders draw from it.
    run([
        sys.executable, "-u",
        str(ROOT / "tools/eternal_lovers_reserve_backing_region.py"),
        "--iso", str(output),
        "--for", "SCENARIO", "--for", "SLG",
        "--region", str(build / "backing_region.json"),
    ])

    run([
        sys.executable, "-u", str(patcher),
        "--iso", str(output),
        "--primary-container", "GADAT030",
        "--images-dir", str(images / "GADAT030/translated_png"),
        "--original-png-dir", str(images / "GADAT030/png"),
        "--runtime-container", "ADV",
        "--report", str(build / "gadat030_image_patch_report.json"),
        "--cache-dir", str(build / "image_cache_gadat030"),
    ])
    run([
        sys.executable, "-u", str(patcher),
        "--iso", str(output),
        "--primary-container", "GADAT032",
        "--images-dir", str(images / "GADAT032/translated_png"),
        "--original-png-dir", str(images / "GADAT032/png"),
        "--image-manifest", str(images / "GADAT032/manifest.json"),
        "--resource-manifest", str(full / "GADAT032/manifest.json"),
        "--runtime-container", "ADV",
        "--report", str(build / "gadat032_image_patch_report.json"),
        "--cache-dir", str(build / "image_cache_gadat032"),
    ])
    run([
        sys.executable, "-u", str(patcher),
        "--iso", str(output),
        "--primary-container", "SLG",
        "--images-dir", str(images / "SLG/translated_png"),
        "--original-png-dir", str(images / "SLG/png"),
        "--image-manifest", str(images / "SLG/manifest.json"),
        "--resource-manifest", str(full / "SLG/manifest.json"),
        "--runtime-container", "ADV",
        "--report", str(build / "slg_image_patch_report.json"),
        "--cache-dir", str(build / "image_cache_slg"),
    ])
    patch_elf(output, patched_elf)
    run([
        sys.executable, "-u",
        str(ROOT / "tools/eternal_lovers_patch_remaining.py"),
        "--iso", str(output),
        "--translations", str(
            PROJECT / "assets/translation/remaining/remaining_candidates.json"
        ),
        "--encoding-map", str(build / "font_map.json"),
        "--report", str(build / "remaining_patch_report.json"),
        "--cache-dir", str(build / "remaining_compressed_cache"),
        "--backing-region", str(build / "backing_region.json"),
    ])
    # SCENARIO runs last of the container rebuilds.  Keeping it at its fixed extent means its
    # ISO9660 logical size has to span the appended backing copy, and any later pass that reads
    # `image[extent : extent + size]` would then copy gigabytes.  Running it here means only the
    # verifiers (which take a zero-copy view) see the inflated size.
    run([
        sys.executable, "-u", str(ROOT / "tools/moonlit_lovers_build.py"),
        "--original-iso", str(args.original_iso.resolve()),
        "--output-iso", str(output),
        "--patch-existing",
        "--original-scenario", str(PROJECT / "source/scenario"),
        "--built-scenario", str(build / "isb_scenario"),
        "--lz-script", str(ROOT / "tools/ikusa_lz.py"),
        "--container", "SCENARIO",
        "--backing-region", str(build / "backing_region.json"),
    ])

    # ADV.DAT caches byte-identical runtime copies of a subset of SCENARIO's script resources
    # and of dat/gadat000/adv_string.tbl.  Without this pass the game keeps reading Japanese
    # out of ADV for exactly those resources - the mission briefing panels and the speaker
    # name plate.  It runs after both writers so it can copy the finished Korean bytes.
    run([
        sys.executable, "-u", str(ROOT / "tools/eternal_lovers_patch_adv_runtime.py"),
        "--iso", str(output),
        "--original-iso", str(args.original_iso.resolve()),
        "--source-scenario", str(PROJECT / "source/scenario"),
        "--built-scenario", str(build / "isb_scenario"),
        "--candidates", str(
            PROJECT / "assets/translation/remaining/remaining_candidates.json"
        ),
        "--encoding-map", str(build / "font_map.json"),
        "--report", str(build / "adv_runtime_report.json"),
    ])

    # SLGRES and SLGSTAGE are FSTS-indexed, which the PIDX image patcher cannot address, and
    # their copies are not always compressed identically to the primary, so the runtime-copy
    # scan misses some.  They get their own pass that writes through each bank's own record.
    #
    # It has to run *after* the remaining-text pass: that pass repacks the same FSTS banks and
    # relocates thousands of resources, which would move these images and leave data in the
    # slot padding they are required to keep zeroed.  Writing last means the layout the images
    # are fitted to is the final one.
    run([
        sys.executable, "-u", str(ROOT / "tools/eternal_lovers_patch_battle_bank_images.py"),
        "--iso", str(output),
        "--project", str(PROJECT),
        "--report", str(build / "battle_bank_images_report.json"),
    ])
    # Rebuild the expected fixed-slot bytes from the immutable Japanese ISO and the current
    # translation inputs, then verify every SLGRES/SLGSTAGE copy on the finished artifact so a
    # later stage cannot silently overwrite an image or one of its size records.
    run([
        sys.executable, "-u",
        str(ROOT / "tools/eternal_lovers_verify_battle_bank_images.py"),
        "--original-iso", str(args.original_iso.resolve()),
        "--iso", str(output),
        "--project", str(PROJECT),
        "--report", str(build / "battle_bank_images_verify_report.json"),
    ])
    print(f"built {output} sha256={sha256(output)}")


if __name__ == "__main__":
    main()
