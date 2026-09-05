#!/usr/bin/env python3
"""Render the Korean for the textures found by the full visual image audit.

``assets/translation/images/candidate_translations.json`` holds a reviewed decision for every
texture the audit flagged: the Japanese actually on it, the Korean to draw, and any hint about
parts of the texture that must not be repainted (a button icon beside the label, a romanised
name under it).  This tool turns those decisions into images.

Two texture shapes cover almost all of them:

glyph-only
    Transparent background, one or two ink colours — the game composites these over its own
    panels.  The Korean is drawn on a fresh transparent canvas, one line per ink row of the
    source, so line count and vertical rhythm match the Japanese.

panel
    An opaque button or plate.  The Japanese is masked by "whiteness" against each row's own
    background, inpainted away, and the Korean drawn in its place.
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
CONTAINERS = ("GADAT030", "GADAT031", "GADAT032", "SLG", "ADV")


def displayable(text: str) -> str:
    for source, replacement in GLYPH_FALLBACKS.items():
        text = text.replace(source, replacement)
    return text


def fit_font(text: str, width: int, height: int, stroke: int = 0) -> ImageFont.FreeTypeFont:
    probe = ImageDraw.Draw(Image.new("L", (8, 8)))
    for size in range(max(9, height + 6), 6, -1):
        font = ImageFont.truetype(str(FONT_BOLD), size=size)
        box = probe.textbbox((0, 0), text, font=font, stroke_width=stroke)
        if box[2] - box[0] <= width and box[3] - box[1] <= height:
            return font
    return ImageFont.truetype(str(FONT_BOLD), size=7)


def ink_rows(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Group the mask into horizontal text rows."""
    rows = np.where(mask.any(axis=1))[0]
    if not len(rows):
        return []
    bands = []
    start = previous = int(rows[0])
    for value in rows[1:]:
        value = int(value)
        if value > previous + 2:
            bands.append((start, previous + 1))
            start = value
        previous = value
    bands.append((start, previous + 1))
    out = []
    for top, bottom in bands:
        columns = np.where(mask[top:bottom].any(axis=0))[0]
        if not len(columns):
            continue
        out.append((int(columns.min()), top, int(columns.max()) + 1, bottom))
    return out


def glyph_mask(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    return alpha > 16


def panel_mask(rgb: np.ndarray, alpha: np.ndarray, metric: str = "whiteness",
               within: np.ndarray | None = None) -> np.ndarray:
    values = rgb.astype(np.float32)
    if metric == "luminance":
        # Some chips carry a light *blue* label on a darker blue panel.  Nothing there is
        # white, so brightness alone is what separates the glyph from its panel.
        whiteness = values.mean(axis=2)
    else:
        whiteness = values.mean(axis=2) - (values.max(axis=2) - values.min(axis=2))
    visible = alpha > 16
    if within is not None:
        # Where the entry named the rectangles its labels live in, the cut is taken from those
        # rectangles alone.  A greyed-out row is far dimmer than the lit rows around it, and a
        # threshold drawn from the whole texture leaves its text standing.
        visible = visible & within
    # The label is the white population among the visible pixels.  An Otsu cut over the whole
    # texture separates it from the panel behind it; a per-row median missed most of the strokes
    # on panels where the text fills much of its row.
    values = whiteness[visible]
    if values.size < 16:
        return np.zeros(whiteness.shape, dtype=bool)
    scaled = np.clip(values, 0, 255).astype(np.uint8)
    level, _ = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if within is None:
        # Across a whole texture the text is a small minority, and Otsu alone sometimes cuts
        # low enough to take panel with it.  Inside a rectangle drawn around one caption the
        # text is most of what is there, so that floor would cut the caption in half instead.
        level = max(float(level), float(np.median(values)) + 20)
    mask = visible & (whiteness > level)
    # Grow the mask past the glyph body: these labels are drawn with a dark outline that no
    # brightness test selects, and leaving it behind prints a ghost of the Japanese.
    mask = cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=2).astype(bool) & visible
    # A lit button's rim is white too, so the mask is kept inside the shape — but only just:
    # on a 30px-tall button an aggressive erosion eats the text rows along with the rim.
    iterations = 1 if min(rgb.shape[:2]) < 40 else 2
    inside = cv2.erode(visible.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=iterations).astype(bool)
    return mask & inside


def _along(values: np.ndarray, known: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Interpolate each row across its unknown runs, and say how far the samples were."""
    height, width = known.shape
    columns = np.arange(width, dtype=np.float32)
    filled = values.astype(np.float32).copy()
    distance = np.full(known.shape, np.inf, dtype=np.float32)
    for row in range(height):
        xs = np.where(known[row])[0]
        if xs.size == 0:
            continue
        holes = ~known[row]
        if not holes.any():
            distance[row] = 0.0
            continue
        for channel in range(values.shape[2]):
            filled[row, holes, channel] = np.interp(columns[holes], xs, values[row][xs][:, channel])
        left = np.searchsorted(xs, columns[holes], side="right") - 1
        right = np.minimum(left + 1, xs.size - 1)
        left = np.maximum(left, 0)
        distance[row, holes] = np.minimum(np.abs(columns[holes] - xs[left]),
                                          np.abs(columns[holes] - xs[right]))
        distance[row, known[row]] = 0.0
    return filled, distance


def erase_label(rgb: np.ndarray, mask: np.ndarray, visible: np.ndarray) -> np.ndarray:
    """Repaint the masked glyphs with the panel that runs behind them.

    What the panel looks like under a stroke is read off the pixels around that stroke and
    interpolated across it, so both a left-to-right and a top-to-bottom gradient survive.  The
    nearer neighbours win: on a button bar the label spans the whole height and only the pixels
    beside it say anything, while inside a bordered list the pixels beside a row are the box's
    own border and the clear gap above and below the row is what the panel looks like.
    """
    known = visible & ~mask
    across, near_x = _along(rgb, known)
    down, near_y = _along(np.swapaxes(rgb, 0, 1), known.T)
    down, near_y = np.swapaxes(down, 0, 1), near_y.T
    weight_x = np.where(np.isfinite(near_x), 1.0 / np.maximum(near_x, 0.5), 0.0)
    weight_y = np.where(np.isfinite(near_y), 1.0 / np.maximum(near_y, 0.5), 0.0)
    total = weight_x + weight_y
    if not (total > 0).all():
        return cv2.inpaint(inpaint_source(rgb, visible), mask.astype(np.uint8), 3, cv2.INPAINT_TELEA)
    blended = (across * weight_x[:, :, None] + down * weight_y[:, :, None]) / total[:, :, None]
    plate = rgb.copy()
    plate[mask] = np.clip(blended[mask], 0, 255).astype(np.uint8)
    return plate


def inpaint_source(rgb: np.ndarray, visible: np.ndarray) -> np.ndarray:
    """Hide fully transparent pixels from the inpainter.

    Their RGB is usually black, and OpenCV happily propagates that into the hole it is filling,
    leaving dark ghosts in the shape of the text that was just removed.
    """
    source = rgb.copy()
    if visible.any() and not visible.all():
        source[~visible] = np.median(rgb[visible], axis=0).astype(np.uint8)
    return source


def limit(mask: np.ndarray, entry: dict) -> np.ndarray:
    height, width = mask.shape
    if entry.get("keep_left"):
        mask[:, : int(width * entry["keep_left"])] = False
    if entry.get("keep_right"):
        mask[:, int(width * (1 - entry["keep_right"])) :] = False
    boxes = entry.get("regions") or ([entry["region"]] if entry.get("region") else [])
    if boxes:
        keep = np.zeros_like(mask)
        for left, top, right, bottom in boxes:
            keep[int(height * top) : int(height * bottom), int(width * left) : int(width * right)] = True
        mask &= keep
    return mask


def draw_lines(canvas: Image.Image, bands: list, lines: list[str], fill, stroke: int, stroke_fill,
               shrink: int = 0, centre_on_band: bool = False, confine: bool = False) -> None:
    draw = ImageDraw.Draw(canvas)
    # Korean is wider than the Japanese it replaces, but it must not run into a button's rim or
    # into an icon the source keeps beside the text, so the widest row of the source plus a
    # small allowance is the budget.  Where the entry named the rectangle the label lives in,
    # that rectangle is the budget: widening to most of the canvas would run the label into
    # whatever the rectangle was drawn to avoid.
    span = max(right - left for left, _t, right, _b in bands)
    budget = span + 6 if confine else min(canvas.width - 4, max(span + 6, int(canvas.width * 0.86)))
    for text, (left, top, right, bottom) in zip(lines, bands):
        text = displayable(text)
        if not text:
            continue
        height = max(8, bottom - top - shrink)
        font = fit_font(text, max(8, budget - 2 * shrink), height, stroke)
        # A two-pixel outline is right on a large name plate and turns a small one into a
        # single smear, so the outline is trimmed to whatever the chosen size can carry.
        while stroke and font.size < 11 + 4 * stroke:
            stroke -= 1
            font = fit_font(text, budget, height, stroke)
        box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
        centre_x = (left + right) / 2 if (centre_on_band or len(bands) > 1) else canvas.width / 2
        x = round(centre_x - (box[2] - box[0]) / 2 - box[0])
        limit_left, limit_right = (left - 3, right + 3) if confine else (1, canvas.width - 1)
        x = max(limit_left, min(x, limit_right - (box[2] - box[0])))
        x = max(0, min(x, canvas.width - (box[2] - box[0])))
        y = round((top + bottom) / 2 - (box[3] - box[1]) / 2 - box[1])
        draw.text((x, y), text, font=font, fill=fill, stroke_width=stroke, stroke_fill=stroke_fill)


def fill_region_plate(rgb: np.ndarray, alpha: np.ndarray, region) -> tuple[np.ndarray, tuple]:
    """Repaint a whole rectangle from the panel around it.

    Some labels are white text on a bright coloured bar, where no threshold separates glyph
    from background reliably.  For those the rectangle holding the label is simply refilled,
    row by row, with the colour the same row has just outside the rectangle.
    """
    height, width = alpha.shape
    left, top, right, bottom = (int(width * region[0]), int(height * region[1]),
                                int(width * region[2]), int(height * region[3]))
    plate = rgb.copy()
    ink, darker = [], []
    for row in range(top, bottom):
        outside = list(range(max(0, left - 6), left)) + list(range(right, min(width, right + 6)))
        outside = [x for x in outside if alpha[row, x] > 16]
        if not outside:
            continue
        colour = np.median(rgb[row, outside], axis=0)
        inside = rgb[row, left:right].astype(np.float32)
        # The label is whatever departs from the bar it sits on — on these rows it is a dark
        # navy or crimson against a bright bar, not the white a brightness test would look for.
        deviating = inside[np.abs(inside - colour).sum(axis=1) > 120]
        ink.extend(deviating.tolist())
        # The glyph body and its halo both depart from the bar.  Keep them apart so the label
        # can be redrawn in the body's colour rather than the halo's.
        darker.extend(deviating[deviating.mean(axis=1) < colour.mean() - 30].tolist())
        plate[row, left:right] = colour.astype(np.uint8)
    if len(darker) >= max(24, len(ink) // 8):
        colour = tuple(int(v) for v in np.median(np.array(darker), axis=0))
    elif ink:
        colour = tuple(int(v) for v in np.median(np.array(ink), axis=0))
    else:
        colour = (255, 255, 255)
    return plate, (left, top, right, bottom, colour)


def borrowed_mask(entry: dict, project: Path | None, shape: tuple[int, int], metric: str):
    """Take the label mask from a brighter sibling of the same button.

    The battle menu ships each button three times — normal, dimmed and highlighted — as the
    same 16-colour picture in different tints.  On the dimmed copy the label is barely a shade
    apart from the panel and no threshold finds it, but all three are pixel-aligned, so the
    copy that does separate cleanly says exactly where the Japanese is.
    """
    name = entry.get("mask_from")
    if not name or project is None:
        return None
    for container in CONTAINERS:
        manifest_path = project / "assets/full_extraction" / container / "manifest.json"
        if not manifest_path.is_file():
            continue
        for resource in json.loads(manifest_path.read_text(encoding="utf-8"))["resources"]:
            if resource.get("name") != name or not resource.get("images"):
                continue
            path = project / "assets/full_extraction" / container / "png" / Path(resource["images"][0]["png"])
            arr = np.array(Image.open(path).convert("RGBA"))
            if arr.shape[:2] != shape:
                raise ValueError(f"{name} is {arr.shape[1]}x{arr.shape[0]}, not the size it lends a mask to")
            return panel_mask(arr[:, :, :3], arr[:, :, 3], metric)
    raise ValueError(f"mask_from texture not found: {name}")


def render(source: Path, entry: dict, project: Path | None = None) -> Image.Image:
    original = Image.open(source).convert("RGBA")
    arr = np.array(original)
    rgb, alpha = arr[:, :, :3], arr[:, :, 3]
    lines = entry["ko"]

    # A glyph-only texture is not merely mostly transparent — a button with a portrait on it is
    # that too.  What makes it glyph-only is that the visible pixels are just ink: one or two
    # colours plus their anti-aliasing.  Judging by transparency alone repaints artwork away.
    visible = alpha > 16
    ink_colours = len(np.unique(rgb[visible].reshape(-1, 3), axis=0)) if visible.any() else 0
    mode = entry.get("mode")

    if mode == "fill":
        plate, (left, top, right, bottom, colour) = fill_region_plate(rgb, alpha, entry["region"])
        canvas = Image.fromarray(np.dstack([plate, alpha]), "RGBA")
        step = (bottom - top) / len(lines)
        bands = [(left, round(top + i * step), right, round(top + (i + 1) * step)) for i in range(len(lines))]
        # Outline away from the fill so a dark label keeps a light edge and a light one a
        # dark edge, as the source art does.
        light = sum(colour) > 3 * 128
        outline = tuple(max(0, c - 90) if light else min(255, c + 110) for c in colour) + (225,)
        draw_lines(canvas, bands, lines, colour + (255,), 1, outline, confine=True)
        return canvas

    if mode == "glyph" or (mode is None and float(visible.mean()) < 0.75 and ink_colours <= 6):
        mask = limit(glyph_mask(rgb, alpha), entry)
        bands = ink_rows(mask)
        if len(bands) != len(lines):
            bands = ink_rows(glyph_mask(rgb, alpha))
        if len(bands) != len(lines):
            raise ValueError(f"{source.name}: {len(bands)} ink rows but {len(lines)} Korean lines")
        ink = rgb[mask] if mask.any() else rgb[alpha > 16]
        brightness = ink.astype(np.float32).mean(axis=1)
        bright = ink[brightness >= np.percentile(brightness, 75)]
        colour = tuple(int(v) for v in np.median(bright if len(bright) else ink, axis=0)) + (255,)
        dark = ink[brightness <= np.percentile(brightness, 20)]
        # Only treat the source as outlined when its second colour really is a dark rim, not
        # the faint anti-aliasing that a two-colour palette also produces.
        outlined = (
            len(np.unique(ink.reshape(-1, 3), axis=0)) > 1
            and len(dark)
            and float(np.median(dark.mean(axis=1))) < 110
            and float(np.median(bright.mean(axis=1))) - float(np.median(dark.mean(axis=1))) > 60
        )
        canvas = Image.new("RGBA", original.size, (0, 0, 0, 0))
        # Anything the entry protects — an icon beside the label — is not text and has to be
        # carried over rather than redrawn.
        protected = limit(np.ones(alpha.shape, dtype=bool), entry)
        keep = (alpha > 16) & ~protected
        if keep.any():
            carried = np.array(canvas)
            carried[keep] = arr[keep]
            canvas = Image.fromarray(carried, "RGBA")
        stroke_fill = tuple(int(v) for v in np.median(dark, axis=0)) + (255,) if outlined and len(dark) else None
        draw_lines(canvas, bands, lines, colour, 2 if stroke_fill else 0, stroke_fill,
                   int(entry.get("shrink", 0)),
                   centre_on_band=bool(entry.get("centre_on_band")),
                   confine=bool(entry.get("region") or entry.get("regions")))

        # These plates are stored as one or two flat colours over a 1-bit alpha, which is why
        # the originals compress into their small resource slots.  Anti-aliased output carries
        # dozens of shades, grows by a few hundred bytes and no longer fits every copy of the
        # texture in the per-stage banks, so the drawing is flattened back to the source's own
        # structure.
        if len(np.unique(np.array(original)[visible][:, 3])) <= 2:
            out = np.array(canvas)
            solid = out[:, :, 3] >= 128
            palette = np.zeros_like(out)
            palette[solid] = np.array(colour, dtype=np.uint8)
            if stroke_fill is not None:
                # Keep the outline: pixels closer to the stroke colour stay the stroke colour.
                to_fill = np.abs(out[:, :, :3].astype(int) - np.array(colour[:3])).sum(axis=2)
                to_stroke = np.abs(out[:, :, :3].astype(int) - np.array(stroke_fill[:3])).sum(axis=2)
                palette[solid & (to_stroke < to_fill)] = np.array(stroke_fill, dtype=np.uint8)
            canvas = Image.fromarray(palette, "RGBA")
        return canvas

    metric = entry.get("metric", "whiteness")
    lent = borrowed_mask(entry, project, alpha.shape, metric)
    if lent is not None:
        mask = limit(lent, entry)
    elif entry.get("regions") and entry.get("threshold_per_region"):
        # Each caption gets its own cut.  Ask for this only where the captions differ in
        # brightness — a greyed-out row among lit ones — because a cut taken from one small
        # rectangle is noisier than one taken from the whole texture.
        height, width = alpha.shape
        mask = np.zeros(alpha.shape, dtype=bool)
        for left, top, right, bottom in entry["regions"]:
            box = np.zeros(alpha.shape, dtype=bool)
            box[int(height * top) : int(height * bottom), int(width * left) : int(width * right)] = True
            mask |= panel_mask(rgb, alpha, metric, box)
        mask = limit(mask, entry)
    else:
        mask = limit(panel_mask(rgb, alpha, metric), entry)
    if mask.sum() < 12 and lent is None and not entry.get("metric"):
        mask = limit(panel_mask(rgb, alpha, "luminance"), entry)
    if mask.sum() < 12:
        raise ValueError(f"{source.name}: no Japanese glyphs found on the panel")
    # On a lit panel the mask also catches highlights, so its row grouping is not a reliable
    # line count.  The area it covers is reliable, so the Korean lines are laid out evenly
    # inside that area instead of being matched row for row.
    regions = entry.get("regions")
    if regions and len(regions) == len(lines):
        height, width = mask.shape
        bands = []
        for left, top, right, bottom in regions:
            box = (int(width * left), int(height * top), int(width * right), int(height * bottom))
            sub = np.zeros_like(mask)
            sub[box[1] : box[3], box[0] : box[2]] = mask[box[1] : box[3], box[0] : box[2]]
            rows_here = ink_rows(sub)
            bands.append(rows_here[0] if rows_here else box)
        ink = rgb[mask].astype(np.float32)
        brightness = ink.mean(axis=1)
        bright = ink[brightness >= np.percentile(brightness, 75)]
        colour = tuple(int(v) for v in np.median(bright if len(bright) else ink, axis=0)) + (255,)
        plate = erase_label(rgb, mask, visible)
        canvas = Image.fromarray(np.dstack([plate, alpha]), "RGBA")
        outline = tuple(max(0, c - 80) for c in colour[:3]) + (225,)
        for text, band in zip(lines, bands):
            # Each label keeps to its own region: an affection caption must not drift over the
            # gauge drawn beside it.
            draw_lines(canvas, [band], [text], colour, 1, outline, int(entry.get("shrink", 0)),
                       centre_on_band=True, confine=True)
        return canvas

    rows = ink_rows(mask)
    left = min(b[0] for b in rows)
    right = max(b[2] for b in rows)
    top = min(b[1] for b in rows)
    bottom = max(b[3] for b in rows)
    if len(rows) == len(lines):
        bands = rows
    else:
        step = (bottom - top) / len(lines)
        bands = [(left, round(top + i * step), right, round(top + (i + 1) * step)) for i in range(len(lines))]
    ink = rgb[mask].astype(np.float32)
    brightness = ink.mean(axis=1)
    bright = ink[brightness >= np.percentile(brightness, 75)]
    colour = tuple(int(v) for v in np.median(bright if len(bright) else ink, axis=0)) + (255,)
    plate = erase_label(rgb, mask, visible)
    # A label can sit on transparency rather than on a panel — an icon strip, for instance,
    # where only the icon and the glyphs are opaque.  Repainting the glyphs' colour there
    # leaves them standing as solid blobs, so their alpha has to go too.
    plate_alpha = alpha.copy()
    ring = cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=2).astype(bool) & ~mask
    if ring.any() and float((alpha[ring] <= 16).mean()) > 0.5:
        plate_alpha[mask] = 0
    canvas = Image.fromarray(np.dstack([plate, plate_alpha]), "RGBA")
    outline = tuple(max(0, c - 80) for c in colour[:3]) + (225,)
    draw_lines(canvas, bands, lines, colour, 1, outline, int(entry.get("shrink", 0)),
               centre_on_band=bool(entry.get("centre_on_band")),
               confine=bool(entry.get("region") or entry.get("regions")))
    return canvas


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path("work/galaxy_angel_eternal_lovers"))
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--only", action="append", help="limit to these resource names")
    parser.add_argument(
        "--layout",
        choices=("eternal", "moonlit"),
        default="eternal",
        help="where the per-container image sets live: japanese_images/<C> (Eternal) or "
             "<C>/japanese_images (Moonlit)",
    )
    parser.add_argument(
        "--never-overwrite",
        action="store_true",
        help="skip any target whose translated_png already exists, and report it. Moonlit's "
             "existing renders are frozen by an explicit instruction.",
    )
    args = parser.parse_args()

    project = args.project
    decisions = json.loads((project / "assets/translation/images/candidate_translations.json").read_text(encoding="utf-8"))["entries"]

    resources: dict[str, tuple[str, dict]] = {}
    for container in CONTAINERS:
        manifest_path = project / "assets/full_extraction" / container / "manifest.json"
        if not manifest_path.is_file():
            continue
        for resource in json.loads(manifest_path.read_text(encoding="utf-8"))["resources"]:
            if not resource.get("images"):
                continue
            name = resource.get("name")
            if name and name not in resources:
                resources[name] = (container, resource)
            # Runtime copies in ADV carry no resource name, so allow addressing them by the
            # container and PNG they were extracted to.
            key = f"{container}:{Path(resource['images'][0]['png']).name}"
            resources.setdefault(key, (container, resource))

    manifests: dict[str, dict[str, dict]] = {}
    rendered, failures = 0, []
    for name, entry in sorted(decisions.items()):
        if entry.get("skip") or entry.get("defer") or entry.get("handled_by"):
            continue
        if args.only and name not in args.only:
            continue
        container, resource = resources[name]
        source = project / "assets/full_extraction" / container / "png" / Path(resource["images"][0]["png"])
        try:
            image = render(source, entry, project)
        except Exception as error:  # noqa: BLE001 - reported per texture, nothing is written for it
            failures.append({"name": name, "container": container, "error": str(error)})
            continue

        images_root = (
            project / "assets/image_extraction/japanese_images" / container
            if args.layout == "eternal"
            else project / "assets/image_extraction" / container / "japanese_images"
        )
        png = entry.get("output_png") or f"block_{int(resource['offset']):08x}.png"
        target_dir = args.preview / container if args.preview else images_root / "translated_png"
        if args.never_overwrite and args.preview is None and (target_dir / png).exists():
            failures.append({"name": name, "container": container, "error": "translated_png already exists; left untouched"})
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        image.save(target_dir / png)
        if args.preview is None:
            (images_root / "png").mkdir(parents=True, exist_ok=True)
            Image.open(source).convert("RGBA").save(images_root / "png" / png)
            store = manifests.setdefault(container, {})
            store[png] = {
                "name": name,
                "png": png,
                "translated_png": png,
                "width": image.width,
                "height": image.height,
                "original": "\n".join(entry["jp"]),
                "translation": "\n".join(entry["ko"]),
                "classification": "eternal-audit-find",
                "resource_path": resource["path"],
                "resource_offset": int(resource["offset"]),
                "source_png": resource["images"][0]["png"],
            }
        rendered += 1

    if args.preview is None:
        for container, store in manifests.items():
            manifest_path = (
                project / "assets/image_extraction/japanese_images" / container / "manifest.json"
                if args.layout == "eternal"
                else project / "assets/image_extraction" / container / "japanese_images" / "manifest.json"
            )
            existing = {}
            if manifest_path.is_file():
                existing = {e["png"]: e for e in json.loads(manifest_path.read_text(encoding="utf-8"))}
            existing.update(store)
            merged = sorted(existing.values(), key=lambda x: str(x.get("png", "")))
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"rendered": rendered, "failures": failures}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
