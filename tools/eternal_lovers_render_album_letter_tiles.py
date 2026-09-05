#!/usr/bin/env python3
"""Render the album menu titles that are drawn one character per texture.

``gxeff*`` and ``gyeff*`` spell a title across separate textures, one kana each, which the
menu animates in sequence.  Korean needs fewer syllables than the Japanese needed kana, so the
group keeps its texture count and the syllables fill the leading tiles; the tiles left over are
written out fully transparent.  That is the rule established on the first Galaxy Angel patch
for its per-character title group, and it keeps the animation timing the game expects.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_BOLD = Path("C:/Windows/Fonts/malgunbd.ttf")

# title -> (japanese, korean, texture names in reading order)
GROUPS = [
    ("シーン選択", "장면선택", ["gxeff21.tex", "gxeff22.tex", "gxeff23.tex", "gxeff24.tex", "gxeff25.tex"]),
    ("その他", "기타", ["gxeff31.tex", "gxeff32.tex", "gxeff33.tex"]),
    ("ミルフィーユ", "밀피유", ["gxeff41.tex", "gxeff42.tex", "gxeff43.tex", "gxeff44.tex", "gxeff45.tex", "gxeff46.tex"]),
    ("ランファ", "란파", ["gxeff51.tex", "gxeff52.tex", "gxeff53.tex", "gxeff54.tex"]),
    ("ミント", "민트", ["gxeff61.tex", "gxeff62.tex", "gxeff63.tex"]),
    ("フォルテ", "포르테", ["gxeff71.tex", "gxeff72.tex", "gxeff73.tex", "gxeff74.tex"]),
    ("ヴァニラ", "바닐라", ["gxeff81.tex", "gxeff82.tex", "gxeff83.tex", "gxeff84.tex"]),
    ("ちとせ", "치토세", ["gxeff91.tex", "gxeff92.tex", "gxeff93.tex"]),
    ("スコアアタック", "스코어어택",
     ["gyeff01.tex", "gyeff02.tex", "gyeff03.tex", "gyeff04.tex", "gyeff05.tex", "gyeff06.tex", "gyeff07.tex"]),
]


def ink_box(image: Image.Image) -> tuple[int, int, int, int]:
    alpha = np.array(image.getchannel("A"))
    ys, xs = np.where(alpha > 16)
    if not len(ys):
        return (0, 0, image.width, image.height)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def ink_colour(image: Image.Image) -> tuple[int, int, int, int]:
    arr = np.array(image.convert("RGBA"))
    visible = arr[:, :, 3] > 128
    if not visible.any():
        return (255, 255, 255, 255)
    return tuple(int(v) for v in np.median(arr[:, :, :3][visible], axis=0)) + (255,)


def fit_font(text: str, width: int, height: int) -> ImageFont.FreeTypeFont:
    probe = ImageDraw.Draw(Image.new("L", (8, 8)))
    for size in range(height + 6, 5, -1):
        font = ImageFont.truetype(str(FONT_BOLD), size=size)
        box = probe.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= width and box[3] - box[1] <= height:
            return font
    return ImageFont.truetype(str(FONT_BOLD), size=6)


def render_tile(source: Path, syllable: str | None, target_height: int | None = None) -> Image.Image:
    original = Image.open(source).convert("RGBA")
    canvas = Image.new("RGBA", original.size, (0, 0, 0, 0))
    if not syllable:
        return canvas
    left, top, right, bottom = ink_box(original)
    colour = ink_colour(original)
    draw = ImageDraw.Draw(canvas)
    # Size every syllable of a title the same way.  Some tiles hold a small kana or a long
    # vowel bar, and sizing to that tile alone would leave one Korean syllable much smaller
    # than its neighbours.
    height = target_height or max(6, bottom - top)
    font = fit_font(syllable, max(6, original.width - 2), max(6, min(height, original.height - 2)))
    box = draw.textbbox((0, 0), syllable, font=font)
    x = round((left + right) / 2 - (box[2] - box[0]) / 2 - box[0])
    y = round((top + bottom) / 2 - (box[3] - box[1]) / 2 - box[1])
    draw.text((x, y), syllable, font=font, fill=colour)
    return canvas


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path("work/galaxy_angel_eternal_lovers"))
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()

    project = args.project
    resources = json.loads((project / "assets/full_extraction/GADAT032/manifest.json").read_text(encoding="utf-8"))["resources"]
    by_name = {r["name"]: r for r in resources if r.get("name") and r.get("images")}

    images_root = project / "assets/image_extraction/japanese_images/GADAT032"
    manifest_path = images_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else []
    by_png = {e["png"]: e for e in manifest}

    written, blanks = 0, 0
    for japanese, korean, names in GROUPS:
        syllables = list(korean)
        if len(syllables) > len(names):
            raise SystemExit(f"{japanese}: Korean needs {len(syllables)} tiles but only {len(names)} exist")
        sizes = []
        for name in names:
            resource = by_name[name]
            with Image.open(project / "assets/full_extraction/GADAT032/png" / Path(resource["images"][0]["png"])) as tile:
                sizes.append(tile.size)
        target_height = int(np.median([h for _w, h in sizes]))

        # A long-vowel bar is a 17x5 texture: a syllable cannot be drawn there at all.  Only
        # tiles that are full-height carry a syllable; the rest of the group goes blank.
        usable = [i for i, (_w, h) in enumerate(sizes) if h >= target_height * 0.6]
        if len(usable) < len(syllables):
            usable = sorted(range(len(names)), key=lambda i: -sizes[i][1])[: len(syllables)]
            usable.sort()
        placement = {usable[i]: syllables[i] for i in range(len(syllables))}

        for index, name in enumerate(names):
            resource = by_name[name]
            source = project / "assets/full_extraction/GADAT032/png" / Path(resource["images"][0]["png"])
            syllable = placement.get(index)
            image = render_tile(source, syllable, target_height)
            png = f"block_{int(resource['offset']):08x}.png"
            target_dir = args.preview or (images_root / "translated_png")
            target_dir.mkdir(parents=True, exist_ok=True)
            image.save(target_dir / png)
            if args.preview is None:
                (images_root / "png").mkdir(parents=True, exist_ok=True)
                Image.open(source).convert("RGBA").save(images_root / "png" / png)
                by_png[png] = {
                    "name": name,
                    "png": png,
                    "translated_png": png,
                    "width": image.width,
                    "height": image.height,
                    "original": japanese,
                    "translation": korean,
                    "classification": "eternal-title-letter-tile",
                    "resource_path": resource["path"],
                    "resource_offset": int(resource["offset"]),
                    "source_png": resource["images"][0]["png"],
                }
            written += 1
            blanks += syllable is None

    if args.preview is None:
        merged = sorted(by_png.values(), key=lambda x: str(x.get("png", "")))
        manifest_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"tiles": written, "blank_tiles": blanks, "groups": len(GROUPS)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
