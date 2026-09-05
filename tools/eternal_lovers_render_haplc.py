#!/usr/bin/env python3
"""Render the outlined variant of the ship-location name plates.

``dat/gadat030/haplc_NNNi.tex`` carries the same place names as ``gaplc_NNNi.tex`` — same
numbering, same text — drawn instead as white glyphs with a heavy black outline on a
transparent background, for use over bright artwork.  Only the ``gaplc`` set had been
translated, so every one of these still showed Japanese in game.

The Korean comes from the same ``gaplc_translations.json`` the other set uses, so the two
plates can never disagree about a room's name.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_BOLD = Path("C:/Windows/Fonts/malgunbd.ttf")
# Malgun Gothic has no U+30FB glyph; substitute the identical-looking middle dot at draw time
# only, exactly as the gaplc renderer does.
GLYPH_FALLBACKS = {"・": "·", "･": "·"}
OUTLINE = (0, 0, 0, 255)


def displayable(text: str) -> str:
    for source, replacement in GLYPH_FALLBACKS.items():
        text = text.replace(source, replacement)
    return text


def ink_band(image: Image.Image) -> tuple[int, int, int, int]:
    alpha = np.array(image.getchannel("A"))
    ys, xs = np.where(alpha > 16)
    if not len(ys):
        raise ValueError("empty plate")
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def fill_colour(image: Image.Image) -> tuple[int, int, int, int]:
    arr = np.array(image.convert("RGBA"))
    visible = arr[:, :, 3] > 16
    bright = arr[:, :, :3][visible & (arr[:, :, :3].mean(axis=2) > 128)]
    if not len(bright):
        return (255, 255, 246, 255)
    return tuple(int(v) for v in np.median(bright, axis=0)) + (255,)


def fit_font(text: str, width: int, height: int, stroke: int) -> ImageFont.FreeTypeFont:
    probe = ImageDraw.Draw(Image.new("L", (8, 8)))
    for size in range(height + 4, 7, -1):
        font = ImageFont.truetype(str(FONT_BOLD), size=size)
        box = probe.textbbox((0, 0), text, font=font, stroke_width=stroke)
        if box[2] - box[0] <= width and box[3] - box[1] <= height:
            return font
    return ImageFont.truetype(str(FONT_BOLD), size=8)


def render(source: Path, korean: str, stroke: int = 2) -> Image.Image:
    original = Image.open(source).convert("RGBA")
    left, top, right, bottom = ink_band(original)
    colour = fill_colour(original)
    text = displayable(korean)

    canvas = Image.new("RGBA", original.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    font = fit_font(text, original.width - 6, bottom - top, stroke)
    box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    x = round(original.width / 2 - (box[2] - box[0]) / 2 - box[0])
    y = round((top + bottom) / 2 - (box[3] - box[1]) / 2 - box[1])
    draw.text((x, y), text, font=font, fill=colour, stroke_width=stroke, stroke_fill=OUTLINE)
    return canvas


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project",
        type=Path,
        default=Path("work/galaxy_angel_eternal_lovers"),
    )
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--layout", choices=("eternal", "moonlit"), default="eternal")
    parser.add_argument("--translations", type=Path,
                        help="wording table to draw from; defaults to the project's gaplc set")
    parser.add_argument("--never-overwrite", action="store_true",
                        help="leave a plate alone when a translated PNG for it already exists")
    args = parser.parse_args()

    project = args.project
    resources = json.loads((project / "assets/full_extraction/GADAT030/manifest.json").read_text(encoding="utf-8"))["resources"]
    by_name = {r["name"]: r for r in resources if r.get("name") and r.get("images")}
    table = args.translations or project / "assets/analysis/gaplc_translations.json"
    translations = json.loads(table.read_text(encoding="utf-8"))

    if args.layout == "moonlit":
        # Moonlit keeps its per-container folder the other way round and treats the
        # extraction manifest as read-only, so only the PNG pair is written there.
        images_root = project / "assets/image_extraction/GADAT030/japanese_images"
        manifest_path = None
        by_png = {}
    else:
        images_root = project / "assets/image_extraction/japanese_images/GADAT030"
        manifest_path = images_root / "manifest.json"
        by_png = {entry["png"]: entry for entry in json.loads(manifest_path.read_text(encoding="utf-8"))}

    full_png = project / "assets/full_extraction/GADAT030/png"
    done = 0
    missing = []
    kept = []
    for stem, pair in sorted(translations.items()):
        name = stem.replace("gaplc_", "haplc_") + ".tex"
        resource = by_name.get(name)
        if resource is None:
            missing.append(name)
            continue
        png_name = f"{name.removesuffix('.tex')}.png"
        if args.never_overwrite and (images_root / "translated_png" / png_name).is_file():
            kept.append(png_name)
            continue
        source = full_png / Path(resource["images"][0]["png"])
        rendered = render(source, pair["ko"])
        png = png_name
        target_dir = args.preview or (images_root / "translated_png")
        target_dir.mkdir(parents=True, exist_ok=True)
        rendered.save(target_dir / png)
        if args.preview is None:
            (images_root / "png").mkdir(parents=True, exist_ok=True)
            Image.open(source).convert("RGBA").save(images_root / "png" / png)
            by_png[png] = {
                "name": name,
                "png": png,
                "translated_png": png,
                "width": rendered.width,
                "height": rendered.height,
                "original": pair["ja"],
                "translation": pair["ko"],
                "classification": "eternal-outlined-place-plate",
                "resource_path": resource["path"],
                "resource_offset": int(resource["offset"]),
                "source_png": resource["images"][0]["png"],
            }
        done += 1

    if args.preview is None and manifest_path is not None:
        merged = sorted(by_png.values(), key=lambda x: str(x.get("png", "")))
        manifest_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"rendered": done, "missing": missing, "left_alone": len(kept)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
