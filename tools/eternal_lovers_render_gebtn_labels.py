#!/usr/bin/env python3
"""Redraw the eyecatch-menu button labels (gebtn03/04 and their f copies).

The generic candidate renderer drew these with a thin, shrunk Malgun Gothic and
left a smeared plate where the Japanese was removed.  The original labels are
heavy white gothic about 16 px tall with ~3 px strokes, centred on the plate.

This keeps every plate pixel the label never touched, refills only the label
pixels from the nearest clean pixels of the same row (the plate gradient is
vertical, so a row-wise fill keeps it), and draws the Korean at the original
height and colour.  Text that would be wider than the original label box is
condensed horizontally instead of shrunk, so the glyph height stays the same.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

PROJECT = Path(__file__).resolve().parents[1]
IMAGES = PROJECT / "assets/image_extraction/japanese_images/GADAT032"
FONT_DIR = PROJECT.parents[1] / "reference_pretendard/packages/pretendard/dist/public/static"

LABELS = {
    "block_00cd3800": "타이틀로 돌아가기",
    "block_00cd7800": "타이틀로 돌아가기",
    "block_00cdb800": "다음으로 진행",
    "block_00cdf800": "다음으로 진행",
}
# Area that holds the label on every plate (inside the rim highlights).
BOX_X0, BOX_X1 = 24, 131
BOX_Y0, BOX_Y1 = 9, 29
# Measured from the Japanese labels.
GLYPH_HEIGHT = 16
TEXT_LEFT, TEXT_RIGHT = 26, 127
TEXT_TOP = 11
TEXT_RGB = (230, 230, 230)
# The original labels are solid pixels with a thin anti-aliased fringe; a plain
# downscale leaves too many half-covered pixels and reads grey and soft.
ALPHA_LOW, ALPHA_HIGH = 0.18, 0.72
SCALE = 4


def label_mask(rgb: np.ndarray) -> np.ndarray:
    lum = rgb.mean(-1)
    sat = rgb.max(-1) - rgb.min(-1)
    mask = np.zeros(lum.shape, bool)
    box = (slice(BOX_Y0, BOX_Y1), slice(BOX_X0, BOX_X1))
    # Label pixels and their anti-aliased fringe are brighter and greyer than the plate.
    plate = np.median(rgb[box][:, :3].reshape(-1, 3), axis=0)
    dist = np.abs(rgb[box] - plate).sum(-1)
    mask[box] = (lum[box] > 110) & (sat[box] < 130) | (dist > 160)
    grown = Image.fromarray((mask * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(3))
    mask = np.array(grown) > 0
    outside = np.ones(mask.shape, bool)
    outside[box] = False
    mask[outside] = False
    return mask


def refill_rows(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    out = rgb.copy()
    for y in range(BOX_Y0, BOX_Y1):
        row = mask[y]
        if not row.any():
            continue
        clean = np.nonzero(~row)[0]
        for c in range(3):
            out[y, row, c] = np.interp(np.nonzero(row)[0], clean, rgb[y, clean, c])
    return out


def render_text(text: str, font_path: Path, stroke: int = 0) -> Image.Image:
    """Return an L mask the size of the plate with the label drawn in."""
    size = GLYPH_HEIGHT * SCALE
    font = ImageFont.truetype(str(font_path), size)
    # Fit the Hangul body (not the font's full ascent) to the measured height.
    for _ in range(40):
        box = font.getbbox("한글", anchor="ls")
        height = box[3] - box[1]
        if abs(height - GLYPH_HEIGHT * SCALE) <= 1:
            break
        size = round(size * GLYPH_HEIGHT * SCALE / height)
        font = ImageFont.truetype(str(font_path), size)
    left, top, right, bottom = font.getbbox(text, anchor="ls")
    canvas = Image.new("L", (right - left + 8 + 2 * stroke, bottom - top + 8 + 2 * stroke), 0)
    ImageDraw.Draw(canvas).text(
        (4 - left, 4 - top), text, font=font, fill=255, anchor="ls",
        stroke_width=stroke, stroke_fill=255,
    )
    canvas = canvas.crop(canvas.getbbox())
    width_px = canvas.width / SCALE
    max_width = TEXT_RIGHT - TEXT_LEFT
    target_w = min(width_px, max_width)
    small = canvas.resize((max(1, round(target_w * SCALE)), canvas.height), Image.LANCZOS)
    small = small.resize((max(1, round(target_w)), round(canvas.height / SCALE)), Image.LANCZOS)
    plate = Image.new("L", (154, 37), 0)
    x = round((TEXT_LEFT + TEXT_RIGHT - small.width) / 2)
    plate.paste(small, (x, TEXT_TOP))
    return plate


def build(name: str, text: str, font_path: Path, stroke: int = 0) -> Image.Image:
    source = Image.open(IMAGES / "png" / f"{name}.png").convert("RGBA")
    arr = np.array(source).astype(float)
    rgb = arr[..., :3]
    filled = refill_rows(rgb, label_mask(rgb))
    alpha = np.array(render_text(text, font_path, stroke)).astype(float)[..., None] / 255.0
    alpha = np.clip((alpha - ALPHA_LOW) / (ALPHA_HIGH - ALPHA_LOW), 0.0, 1.0)
    rgb_out = filled * (1 - alpha) + np.array(TEXT_RGB, float) * alpha
    out = np.concatenate([rgb_out, arr[..., 3:]], axis=-1)
    return Image.fromarray(np.clip(out.round(), 0, 255).astype(np.uint8), "RGBA")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--font", type=Path, default=FONT_DIR / "Pretendard-Black.otf")
    parser.add_argument("--stroke", type=int, default=0,
                        help="extra stroke in supersampled pixels (closes counters; keep 0 with Black)")
    parser.add_argument("--out-dir", type=Path, default=IMAGES / "translated_png")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in LABELS.items():
        build(name, text, args.font, args.stroke).save(args.out_dir / f"{name}.png")
        print(f"wrote {args.out_dir / (name + '.png')} ({text})")


if __name__ == "__main__":
    main()
