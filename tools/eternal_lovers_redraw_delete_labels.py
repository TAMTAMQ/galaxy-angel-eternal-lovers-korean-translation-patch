#!/usr/bin/env python3
"""Redraw the save-card delete label so every copy sits on the Japanese pixels.

Four textures carry the same 「データ削除」 label: the save card gfwin03.tex and
three 128x36 crops of it.  The crops came in through
eternal_lovers_sync_shared_japanese_images.py, which imports a Korean render
whenever a texture's Japanese pixels match one in Moonlit Lovers - the wording
carried over, but the other game's render was fitted to its own label box, so on
this disc it sat 7px up and to the left and ran into the triangle button icon.
The card itself drifted for its own reason: its render region reached up into the
panel rule above the text, which lifted the band the label is centred in.

The candidate renderer decides font size per image from the region it is given,
so it cannot be made to draw the same label the same way in a 296x380 card and a
128x36 crop.  This draws it once instead: one font size, chosen so the label fits
the box the Japanese occupies, then placed on that box in every copy.  The
Japanese is erased with the renderer's own mask so no tail is left behind.

Each target's manifest entry gets a classification of its own, which is what
stops the sync tool importing over it again - it only overwrites entries still
marked ``shared-pixel-match``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import eternal_lovers_render_candidate_images as renderer

CLASSIFICATION = "eternal-delete-label"
JAPANESE = "データ削除"
KOREAN = "데이터 삭제"

TARGETS = {
    # png -> the rectangle to erase, generous enough to cover every glyph pixel
    # but clear of the panel rule above the label.
    "block_006af800.png": {"container": "GADAT032", "erase": (130, 336, 236, 360), "ink_from_y": 320},
    "block_00687000.png": {"container": "GADAT032", "erase": (0, 6, 96, 32), "ink_from_y": 5},
    "block_00688000.png": {"container": "GADAT032", "erase": (0, 6, 96, 32), "ink_from_y": 5},
    "block_00689000.png": {"container": "GADAT032", "erase": (0, 6, 96, 32), "ink_from_y": 5},
}


def bright_mask(image: Image.Image, from_y: int) -> np.ndarray:
    pixels = np.array(image.convert("RGBA"))
    mask = (
        (pixels[:, :, 0] > 170)
        & (pixels[:, :, 1] > 170)
        & (pixels[:, :, 2] > 170)
        & (pixels[:, :, 3] > 128)
    )
    mask[:from_y, :] = False
    return mask


def box_of(mask: np.ndarray):
    ys, xs = mask.nonzero()
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def choose_font(text: str, width: int, height: int, stroke: int):
    """Largest size whose *drawn* ink fits the Japanese label's box.

    textbbox over-reports by a few pixels once the stroke and antialiasing are
    accounted for, which would leave the Korean noticeably smaller than the
    Japanese it replaces, so measure a real draw instead.
    """
    for size in range(height + 12, 6, -1):
        font = ImageFont.truetype(str(renderer.FONT_BOLD), size=size)
        pad = size * 3
        probe = Image.new("L", (width + 2 * pad, height + 2 * pad), 0)
        ImageDraw.Draw(probe).text(
            (pad, pad), text, font=font, fill=255, stroke_width=stroke, stroke_fill=255
        )
        ink = np.array(probe) > 96
        ys, xs = ink.nonzero()
        if not len(xs):
            continue
        drawn = (int(xs.max() - xs.min()) + 1, int(ys.max() - ys.min()) + 1)
        if drawn[0] <= width and drawn[1] <= height:
            # Offset from the draw origin to the ink, so the caller can place the
            # ink itself rather than the font's bounding box.
            return font, (int(xs.min()) - pad, int(ys.min()) - pad, drawn[0], drawn[1])
    raise SystemExit(f"no font size fits {text!r} in {width}x{height}")


def main() -> None:
    project = Path("work/galaxy_angel_eternal_lovers")
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=project)
    parser.add_argument("--report", type=Path, default=project / "build/delete_label_report.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Every copy holds the same label at the same size, so one measurement drives
    # the shared design.
    first = next(iter(TARGETS))
    spec = TARGETS[first]
    root = args.project / "assets/image_extraction/japanese_images" / spec["container"]
    reference = box_of(bright_mask(Image.open(root / "png" / first), spec["ink_from_y"]))
    label_w = reference[2] - reference[0] + 1
    label_h = reference[3] - reference[1] + 1
    stroke = 1
    font, (ink_dx, ink_dy, ink_w, ink_h) = choose_font(
        renderer.displayable(KOREAN), label_w, label_h, stroke
    )
    print(f"shared design: font {font.size}px, ink {ink_w}x{ink_h} "
          f"in a {label_w}x{label_h} box")

    rows = []
    for png, spec in TARGETS.items():
        root = args.project / "assets/image_extraction/japanese_images" / spec["container"]
        source = root / "png" / png
        original = Image.open(source).convert("RGBA")
        pixels = np.array(original)
        rgb, alpha = pixels[:, :, :3], pixels[:, :, 3]
        width, height = original.size

        japanese = bright_mask(original, spec["ink_from_y"])
        target = box_of(japanese)
        if target is None:
            raise SystemExit(f"no label ink in {source}")
        if (target[2] - target[0] + 1, target[3] - target[1] + 1) != (label_w, label_h):
            raise SystemExit(f"{png}: label box {target} differs from the reference {reference}")

        left, top, right, bottom = spec["erase"]
        entry = {"metric": "luminance",
                 "region": [left / width, top / height, right / width, bottom / height]}
        mask = renderer.limit(renderer.panel_mask(rgb, alpha, "luminance"), entry)
        missed = int((japanese & ~mask).sum())
        if missed:
            raise SystemExit(f"{png}: erase would leave {missed} Japanese pixels behind")

        visible = alpha > 16
        plate = renderer.erase_label(rgb, mask, visible)
        canvas = Image.fromarray(np.dstack([plate, alpha]), "RGBA")

        ink = rgb[mask].astype(np.float32)
        brightness = ink.mean(axis=1)
        lit = ink[brightness >= np.percentile(brightness, 75)]
        colour = tuple(int(v) for v in np.median(lit if len(lit) else ink, axis=0)) + (255,)
        outline = tuple(max(0, c - 80) for c in colour[:3]) + (225,)

        # Centre the shared design on the box the Japanese occupied, placing the
        # ink itself rather than the font's origin.
        x = round(target[0] + (label_w - ink_w) / 2 - ink_dx)
        y = round(target[1] + (label_h - ink_h) / 2 - ink_dy)
        ImageDraw.Draw(canvas).text(
            (x, y), renderer.displayable(KOREAN), font=font, fill=colour,
            stroke_width=stroke, stroke_fill=outline,
        )

        drawn = box_of(bright_mask(canvas, spec["ink_from_y"]))
        rows.append({"png": png, "japanese_box": list(target), "korean_box": list(drawn)})
        print(f"{png}: Japanese {target} -> Korean {drawn}")

        if not args.dry_run:
            (root / "translated_png").mkdir(parents=True, exist_ok=True)
            canvas.save(root / "translated_png" / png)
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in manifest:
                if item.get("png") == png:
                    item["classification"] = CLASSIFICATION
                    item["original"] = JAPANESE
                    item["translation"] = KOREAN
                    item["note"] = (
                        "drawn by eternal_lovers_redraw_delete_labels.py: one shared design, "
                        "centred on the box the Japanese label occupied"
                    )
                    break
            temporary = manifest_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(manifest_path)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps({"schema": "eternal-lovers-delete-label/v1",
                    "font_px": font.size, "targets": rows},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"redrew {len(rows)} copies with one design; report {args.report}")


if __name__ == "__main__":
    main()
