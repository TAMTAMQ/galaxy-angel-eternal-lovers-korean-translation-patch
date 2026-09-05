#!/usr/bin/env python3
"""Re-render the Eternal Lovers chapter titles as cyan glow text on black.

The generic chapter renderer inherited from Moonlit Lovers rebuilds the plate behind the
Japanese title from the median of every chapter texture.  Moonlit's chapter plates are
artwork, so that works there.  Eternal Lovers' chapter titles are pure black with a wide
cyan glow, and the median of seventeen overlapping glows is not black — it left visible
turquoise smears around the Korean text, and the capped font size made the Korean noticeably
smaller than the Japanese it replaced.

Here the plate is simply black, which is what the texture actually is outside the glyphs, and
the glow is rebuilt from the Korean text itself at the source band's height.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_BOLD = Path("C:/Windows/Fonts/malgunbd.ttf")
BAND_THRESHOLD = 25
GLYPH_FALLBACKS = {"・": "·", "･": "·"}


def displayable(text: str) -> str:
    for source, replacement in GLYPH_FALLBACKS.items():
        text = text.replace(source, replacement)
    return text


def band_of(image: Image.Image) -> tuple[int, int, int, int]:
    luminance = np.array(image.convert("RGB")).max(axis=2)
    rows = np.where(luminance.max(axis=1) > BAND_THRESHOLD)[0]
    columns = np.where(luminance.max(axis=0) > BAND_THRESHOLD)[0]
    if not len(rows) or not len(columns):
        raise ValueError("no glow band found")
    return int(columns.min()), int(rows.min()), int(columns.max()) + 1, int(rows.max()) + 1


def core_color(image: Image.Image) -> tuple[int, int, int]:
    arr = np.array(image.convert("RGB")).astype(int)
    luminance = arr.max(axis=2)
    core = arr[luminance >= max(200, int(luminance.max() * 0.9))]
    if not len(core):
        return (0, 255, 255)
    return tuple(int(v) for v in np.median(core, axis=0))


def fit_font(text: str, width: int, height: int) -> ImageFont.FreeTypeFont:
    probe = ImageDraw.Draw(Image.new("L", (8, 8)))
    for size in range(height + 8, 11, -1):
        font = ImageFont.truetype(str(FONT_BOLD), size=size)
        box = probe.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= width and box[3] - box[1] <= height:
            return font
    return ImageFont.truetype(str(FONT_BOLD), size=12)


def render(source: Path, text: str, glow_radius: float = 5.0, glow_gain: float = 1.35) -> Image.Image:
    original = Image.open(source).convert("RGBA")
    left, top, right, bottom = band_of(original)
    colour = core_color(original)
    text = displayable(text)

    # The Japanese band includes its own glow halo; the glyph bodies sit a few pixels inside
    # it, so aim the Korean at the band minus the halo and let the new glow fill the rest.
    body_height = max(12, (bottom - top) - 10)
    font = fit_font(text, original.width - 24, body_height)

    mask = Image.new("L", original.size, 0)
    draw = ImageDraw.Draw(mask)
    box = draw.textbbox((0, 0), text, font=font)
    x = round(original.width / 2 - (box[2] - box[0]) / 2 - box[0])
    y = round((top + bottom) / 2 - (box[3] - box[1]) / 2 - box[1])
    draw.text((x, y), text, font=font, fill=255)

    glow = mask.filter(ImageFilter.GaussianBlur(glow_radius))
    glow_arr = np.array(glow, dtype=np.float32) * glow_gain
    core_arr = np.array(mask, dtype=np.float32)
    intensity = np.clip(np.maximum(core_arr, glow_arr), 0, 255) / 255.0

    out = np.zeros((original.height, original.width, 4), dtype=np.float32)
    for channel, value in enumerate(colour):
        out[:, :, channel] = intensity * value
    out[:, :, 3] = 255
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGBA")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--images-root",
        type=Path,
        default=Path("work/galaxy_angel_eternal_lovers/assets/image_extraction/japanese_images/GADAT032"),
    )
    parser.add_argument("--classification", default="eternal-chapter-title")
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()

    manifest_path = args.images_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    done = 0
    for entry in manifest:
        if entry.get("classification") != args.classification:
            continue
        source = args.images_root / "png" / entry["png"]
        rendered = render(source, entry["translation"])
        target = (args.preview or (args.images_root / "translated_png")) / entry["translated_png"]
        target.parent.mkdir(parents=True, exist_ok=True)
        rendered.save(target)
        done += 1
    print(json.dumps({"classification": args.classification, "rendered": done}, ensure_ascii=False))


if __name__ == "__main__":
    main()
