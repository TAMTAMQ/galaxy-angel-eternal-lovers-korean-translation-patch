#!/usr/bin/env python3
"""Redraw the battle result plates (gpwin0 / gpwin1) on the original artwork.

The earlier render was laid over a plate whose rows no longer matched the
original, so in game the unit names sat out of line with the score rows.  This
starts from the Japanese plate, removes only the label pixels, and draws each
Korean label in the box the Japanese label occupied: same left edge for the row
names, same cell centre for the column headers, same glyph height and the same
solid 230-grey core with a soft blue fringe.  Labels wider than their box are
condensed horizontally so their height still matches the original.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

PROJECT = Path(__file__).resolve().parents[1]
IMAGES = PROJECT / "assets/image_extraction/japanese_images/GADAT032"
FONT_DIR = PROJECT.parents[1] / "reference_pretendard/packages/pretendard/dist/public/static"
NAMES = ("block_01af4000", "block_01b06800")
SCALE = 4
CORE_RGB = np.array((230, 230, 230), float)
ALPHA_LOW, ALPHA_HIGH = 0.18, 0.72

TEXTS = [
    ("전투 결과", "center", 112),
    ("격추 수", "center", 64),
    ("전투 평가", "center", 64),
    ("보너스", "center", 64),
    ("점수", "center", 64),
    ("누적 격추 수", "center", 70),
    ("누적 점수", "center", 64),
    ("에르시올", "left", 140),
    ("럭키 스타", "left", 140),
    ("쿵푸 파이터", "left", 140),
    ("트릭 마스터", "left", 140),
    ("해피 트리거", "left", 140),
    ("하베스터", "left", 140),
    ("샤프 슈터", "left", 140),
    ("기타", "left", 140),
    ("합계", "left", 140),
]
# (x0, y0, x1, y1) of each Japanese label, measured on each plate.  gpwin1 is
# the score-attack plate: a STAGE bar pushes the table 11-16 px down.
BOXES = {
    "block_01af4000": [
        (32, 16, 125, 40), (167, 40, 226, 56), (236, 40, 297, 56), (307, 41, 365, 55),
        (393, 40, 422, 56), (449, 40, 516, 56), (531, 40, 590, 56),
        (21, 81, 110, 95), (21, 115, 126, 129), (21, 149, 156, 163), (23, 183, 141, 197),
        (21, 217, 141, 231), (21, 251, 110, 265), (21, 285, 156, 299), (21, 319, 65, 334),
        (20, 367, 35, 383),
    ],
    "block_01b06800": [
        (32, 16, 125, 40), (168, 56, 227, 72), (237, 56, 298, 72), (308, 57, 366, 71),
        (393, 56, 422, 72), (449, 56, 516, 72), (531, 56, 590, 72),
        (21, 92, 110, 106), (21, 126, 126, 140), (21, 160, 156, 174), (23, 194, 141, 208),
        (21, 228, 141, 242), (21, 262, 110, 276), (21, 296, 156, 310), (21, 330, 65, 345),
        (20, 373, 35, 389),
    ],
}


def labels_for(name: str) -> list[tuple[str, tuple[int, int, int, int], str, int]]:
    return [(text, box, align, width) for (text, align, width), box in zip(TEXTS, BOXES[name], strict=True)]


ROW_LABEL_RIGHT = 158                # first column separator is at x=160


def clear_rect(x0: int, y0: int, x1: int, y1: int, align: str) -> tuple[int, int, int, int]:
    """Rectangle covering a label with its glow, kept inside its cell."""
    if y1 - y0 > 20:                       # title plate
        return x0 - 4, y0 - 4, x1 + 5, y1 + 4
    if align == "left":                    # unit name rows
        return x0 - 3, y0 - 4, min(x1 + 6, ROW_LABEL_RIGHT + 1), y1 + 5
    # header cells: the separators below the labels end before the text rows,
    # and bright cell edges inside the rectangle are protected by bright_edge()
    return x0 - 3, y0 - 1, x1 + 4, y1 + 3


def bright_edge(rgba: np.ndarray) -> np.ndarray:
    """Cyan frame and cell-edge lines (bright and saturated)."""
    rgb = rgba[..., :3].astype(int)
    return ((rgb.max(-1) - rgb.min(-1)) > 150) & (rgb.mean(-1) > 140)


def label_mask(rgba: np.ndarray, labels) -> tuple[np.ndarray, np.ndarray]:
    """Label pixels to replace, and the label rectangles they belong to.

    The plate is ~216 opaque while a label and its glow are 226-255, so alpha
    picks out exactly the Japanese glyphs inside each rectangle.
    """
    rects = np.zeros(rgba.shape[:2], bool)
    for _text, (x0, y0, x1, y1), align, _width in labels:
        rx0, ry0, rx1, ry1 = clear_rect(x0, y0, x1, y1, align)
        rects[ry0:ry1, rx0:rx1] = True
    alpha = rgba[..., 3].astype(int)
    glyph = np.zeros_like(rects)
    for _text, (x0, y0, x1, y1), align, _width in labels:
        rx0, ry0, rx1, ry1 = clear_rect(x0, y0, x1, y1, align)
        for y in range(ry0, ry1):
            row = alpha[y, rx0:rx1]
            lum = rgba[y, rx0:rx1, :3].astype(int).mean(-1)
            # plate opacity differs per row (204-216); the glow sits a few steps
            # above it, and its colour spreads a little further than its alpha
            glyph[y, rx0:rx1] = (row >= np.percentile(row, 15) + 6) | (
                lum >= np.percentile(lum, 15) + 10
            )
    glyph = cv2.dilate(glyph.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    return glyph & rects & ~bright_edge(rgba), rects


def clean_name_column(rgba: np.ndarray, labels) -> np.ndarray:
    """Remove the unit names and their wide glow from the name column.

    The Japanese names brighten the plate for ~20 px around them, so nearby
    pixels are no reference.  The first score column (x 165-225) shows the
    same row band without any glow; any name-column pixel brighter or more
    opaque than that row's band colour is replaced by it.
    """
    out = rgba.astype(float).copy()
    edge = bright_edge(rgba)
    for _text, (x0, y0, x1, y1), align, _width in labels:
        if align != "left":
            continue
        rx0, ry0, rx1, ry1 = clear_rect(x0, y0, x1, y1, align)
        for y in range(ry0, ry1):
            # prefer clean plate in the same name cell past the glow, else the
            # first score column
            near = [x for x in range(x1 + 22, ROW_LABEL_RIGHT) if not edge[y, x]]
            ref_cols = near if len(near) >= 8 else [x for x in range(165, 226) if not edge[y, x]]
            ref = np.median(out[y, ref_cols], axis=0)
            row = out[y, rx0:rx1]
            lum = row[:, :3].mean(-1)
            hot = ((lum > ref[:3].mean() + 4) | (row[:, 3] > ref[3] + 3)) & ~edge[y, rx0:rx1]
            row[hot] = ref
    return out


def refill_rows(rgba: np.ndarray, mask: np.ndarray, rects: np.ndarray) -> np.ndarray:
    """Fill label pixels from clean plate pixels of the same row and rectangle."""
    out = rgba.astype(float).copy()
    edge = bright_edge(rgba)
    for y in np.nonzero(mask.any(axis=1))[0]:
        holes = np.nonzero(mask[y])[0]
        clean = np.nonzero(rects[y] & ~mask[y] & ~edge[y])[0]
        if len(clean) < 2:
            clean = np.nonzero(~mask[y] & ~edge[y])[0]
        for c in range(out.shape[2]):
            out[y, holes, c] = np.interp(holes, clean, out[y, clean, c])
    return out


def text_alpha(text: str, font_path: Path, height: int, max_width: int) -> Image.Image:
    size = height * SCALE
    font = ImageFont.truetype(str(font_path), size)
    for _ in range(40):
        box = font.getbbox("한글", anchor="ls")
        h = box[3] - box[1]
        if abs(h - height * SCALE) <= 1:
            break
        size = round(size * height * SCALE / h)
        font = ImageFont.truetype(str(font_path), size)
    left, top, right, bottom = font.getbbox(text, anchor="ls")
    canvas = Image.new("L", (right - left + 8, bottom - top + 8), 0)
    ImageDraw.Draw(canvas).text((4 - left, 4 - top), text, font=font, fill=255, anchor="ls")
    canvas = canvas.crop(canvas.getbbox())
    width = min(canvas.width / SCALE, max_width)
    return canvas.resize((max(1, round(width)), max(1, round(canvas.height / SCALE))), Image.LANCZOS)


def build(name: str, font_path: Path) -> Image.Image:
    src = np.array(Image.open(IMAGES / "png" / f"{name}.png").convert("RGBA"))
    # The plate's alpha carries the Japanese glyph shapes too (255 on the
    # label, ~216 on the plate), so refill all four channels.
    labels = labels_for(name)
    mask, rects = label_mask(src, labels)
    filled = refill_rows(src, mask, rects)
    filled = clean_name_column(filled.round().astype(np.uint8), labels)
    plate, plate_alpha = filled[..., :3], filled[..., 3]
    alpha_plate = np.zeros(plate.shape[:2], float)
    for text, (x0, y0, x1, y1), align, max_width in labels:
        height = y1 - y0 + 1 + (1 if align == "left" else 0)
        glyphs = text_alpha(text, font_path, height, max_width)
        g = np.array(glyphs).astype(float) / 255.0
        h, w = g.shape
        x = x0 if align == "left" else round((x0 + x1 + 1 - w) / 2)
        y = round((y0 + y1 + 1 - h) / 2)
        region = alpha_plate[y:y + h, x:x + w]
        np.maximum(region, g[: region.shape[0], : region.shape[1]], out=region)
    core = np.clip((alpha_plate - ALPHA_LOW) / (ALPHA_HIGH - ALPHA_LOW), 0, 1)
    # The Japanese labels carry a one-pixel blue fringe around the grey core.
    fringe = cv2.dilate((core > 0.5).astype(np.uint8), np.ones((3, 3), np.uint8)).astype(float)
    fringe = np.clip(fringe - core, 0, 1) * 0.55
    fringe_rgb = np.array((128, 174, 206), float)
    out = plate * (1 - fringe[..., None]) + fringe_rgb * fringe[..., None]
    out = out * (1 - core[..., None]) + CORE_RGB * core[..., None]
    alpha = np.maximum(plate_alpha, 228 * (fringe > 0))
    alpha = alpha * (1 - core) + 255 * core
    result = np.dstack([np.clip(out.round(), 0, 255), np.clip(alpha.round(), 0, 255)]).astype(np.uint8)
    return Image.fromarray(result, "RGBA")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--font", type=Path, default=FONT_DIR / "Pretendard-Black.otf")
    parser.add_argument("--out-dir", type=Path, default=IMAGES / "translated_png")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name in NAMES:
        build(name, args.font).save(args.out_dir / f"{name}.png")
        print(f"wrote {args.out_dir / (name + '.png')}")


if __name__ == "__main__":
    main()
