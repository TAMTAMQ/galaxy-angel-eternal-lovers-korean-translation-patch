#!/usr/bin/env python3
"""Re-render Eternal Lovers text plates whose background is a flat or row-wise gradient.

``moonlit_lovers_render_gadat032_ui.render_banded_text`` removes the Japanese with
``neutral_text_mask``, which is tuned for pale glyphs on a saturated panel.  On the battle
result name plates and the score buttons the glyphs are white on grey — the mask misses most
of the strokes, which is why the imported renders still showed ``ミル`` next to ``밀피유``.

These textures have a much simpler structure: every row of the background is one colour (a
flat bar, or a smooth vertical gradient), so a bright-pixel mask filled with each row's own
median colour erases the Japanese completely without touching anything else.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_BOLD = Path("C:/Windows/Fonts/malgunbd.ttf")
GLYPH_FALLBACKS = {"・": "·", "･": "·"}

# resource -> (japanese, korean, left edge of the region that may be repainted as a fraction
# of the width).  The fraction protects icons drawn beside the text, such as the circle button
# on the score-attack "next" button.
PLATES = {
    "gpbtn1.tex": ("ギブアップ", "포기", 0.0),
    "gpbtn1f.tex": ("ギブアップ", "포기", 0.0),
    "gpbtn2.tex": ("次へ", "다음", 0.42),
}


def displayable(text: str) -> str:
    for source, replacement in GLYPH_FALLBACKS.items():
        text = text.replace(source, replacement)
    return text


def text_mask(rgb: np.ndarray, alpha: np.ndarray, left_fraction: float) -> np.ndarray:
    """Glyph pixels, judged against each row's own background level.

    The glyphs are white; the plate behind them is either a darker grey or a saturated glow.
    Measuring "whiteness" (brightness minus colour) separates the text from both, where plain
    brightness would miss white text sitting on an already bright coloured button.
    """
    values = rgb.astype(np.float32)
    luminance = values.mean(axis=2)
    saturation = values.max(axis=2) - values.min(axis=2)
    whiteness = luminance - saturation
    visible = alpha > 16
    mask = np.zeros(luminance.shape, dtype=bool)
    for row in range(luminance.shape[0]):
        row_values = whiteness[row][visible[row]]
        if row_values.size < 4:
            continue
        level = float(np.median(row_values))
        mask[row] = visible[row] & (whiteness[row] > level + 38)
    # The glyphs carry a dark outline that is not white and would survive a whiteness test,
    # so the mask is grown by a pixel to take the outline with the stroke.
    mask = cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool) & visible
    # A lit button's rim is white too.  Keeping only what sits well inside the shape leaves the
    # rim intact and, just as importantly, stops it from inflating the text box.
    inside = cv2.erode(visible.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=2).astype(bool)
    mask &= inside
    if left_fraction > 0:
        mask[:, : int(rgb.shape[1] * left_fraction)] = False
    return mask


def repaint(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Fill the glyph pixels from their surroundings.

    These buttons are lit by a left-to-right glow, so painting a row's median across it would
    band the gradient; inpainting follows the gradient instead.
    """
    return cv2.inpaint(rgb, mask.astype(np.uint8), 3, cv2.INPAINT_TELEA)


def fit_font(text: str, width: int, height: int) -> ImageFont.FreeTypeFont:
    probe = ImageDraw.Draw(Image.new("L", (8, 8)))
    for size in range(height + 6, 6, -1):
        font = ImageFont.truetype(str(FONT_BOLD), size=size)
        box = probe.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= width and box[3] - box[1] <= height:
            return font
    return ImageFont.truetype(str(FONT_BOLD), size=7)


def render(source: Path, korean: str, left_fraction: float) -> Image.Image:
    original = Image.open(source).convert("RGBA")
    arr = np.array(original)
    rgb, alpha = arr[:, :, :3], arr[:, :, 3]

    mask = text_mask(rgb, alpha, left_fraction)
    if mask.sum() < 12:
        raise ValueError(f"no Japanese glyphs found: {source}")
    ys, xs = np.where(mask)
    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    # The label is white; the mask also holds its darker outline, so take the bright end.
    glyph = rgb[mask].astype(np.float32)
    bright = glyph[glyph.mean(axis=1) >= np.percentile(glyph.mean(axis=1), 75)]
    colour = tuple(int(v) for v in np.median(bright if len(bright) else glyph, axis=0))

    image = Image.fromarray(np.dstack([repaint(rgb, mask), alpha]), "RGBA")
    draw = ImageDraw.Draw(image)
    text = displayable(korean)
    available = original.width - 6 if left_fraction == 0 else original.width - box[0] - 4
    font = fit_font(text, available, max(9, min(box[3] - box[1] + 1, int(original.height * 0.6))))
    tb = draw.textbbox((0, 0), text, font=font)
    x = round((box[0] + box[2]) / 2 - (tb[2] - tb[0]) / 2 - tb[0])
    x = max(2, min(x, original.width - (tb[2] - tb[0]) - 2))
    y = round((box[1] + box[3]) / 2 - (tb[3] - tb[1]) / 2 - tb[1])
    outline = tuple(max(0, int(v) - 80) for v in colour) + (225,)
    draw.text((x, y), text, font=font, fill=colour + (255,), stroke_width=1, stroke_fill=outline)
    return image


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--images-root",
        type=Path,
        default=Path("work/galaxy_angel_eternal_lovers/assets/image_extraction/japanese_images/GADAT032"),
    )
    parser.add_argument(
        "--resource-manifest",
        type=Path,
        default=Path("work/galaxy_angel_eternal_lovers/assets/full_extraction/GADAT032/manifest.json"),
    )
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()

    resources = json.loads(args.resource_manifest.read_text(encoding="utf-8"))["resources"]
    offset_of = {r["name"]: int(r["offset"]) for r in resources if r.get("name")}
    manifest_path = args.images_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_png = {entry["png"]: entry for entry in manifest}

    done = 0
    for name, (japanese, korean, left_fraction) in PLATES.items():
        png = f"block_{offset_of[name]:08x}.png"
        entry = by_png.get(png)
        if entry is None:
            raise SystemExit(f"{name} ({png}) is not in the translated image manifest")
        rendered = render(args.images_root / "png" / png, korean, left_fraction)
        target = (args.preview or (args.images_root / "translated_png")) / entry["translated_png"]
        target.parent.mkdir(parents=True, exist_ok=True)
        rendered.save(target)
        if args.preview is None:
            entry["original"] = japanese
            entry["translation"] = korean
            entry["classification"] = "eternal-flat-plate"
        done += 1

    if args.preview is None:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"rendered": done}, ensure_ascii=False))


if __name__ == "__main__":
    main()
