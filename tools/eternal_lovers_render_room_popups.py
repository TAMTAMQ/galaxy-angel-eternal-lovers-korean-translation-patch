#!/usr/bin/env python3
"""Re-render the ship-interior room popups, keeping the room artwork.

``dat/gadat032/艦内移動/gmplc_pop*.tex`` is a framed screenshot of a room with the room's name
on the bar underneath.  The renders imported from Moonlit Lovers by pixel hash destroyed the
screenshot — the whole texture had been treated as one text plate, so the artwork was smeared
away — and several of them carried Moonlit's caption instead of the one Eternal Lovers' own
texture shows (a guest room labelled as Noa's room, a ``???`` placeholder labelled as
Chitose's room).

This renderer touches only the caption bar: the Japanese glyphs there are inpainted out and
the Korean name is drawn in their place, so the room screenshot survives untouched.
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

# Room names as they appear on each texture's caption bar.  ``gmplc_pop2_304`` is a locked
# room the game itself labels "???", so it keeps that placeholder.
ROOMS = {
    "gmplc_pop0_004.tex": ("司令官室", "사령관실"),
    "gmplc_pop0_005.tex": ("ブリッジ", "함교"),
    "gmplc_pop0_006.tex": ("銀河展望公園", "은하전망공원"),
    "gmplc_pop1_008.tex": ("ティーラウンジ", "티 라운지"),
    "gmplc_pop1_009.tex": ("食堂", "식당"),
    "gmplc_pop1_010.tex": ("宇宙コンビニ", "우주 편의점"),
    "gmplc_pop1_012.tex": ("ホール", "홀"),
    "gmplc_pop2_014.tex": ("謁見の間", "알현실"),
    "gmplc_pop2_015.tex": ("ミルフィーユの部屋", "밀피유의 방"),
    "gmplc_pop2_016.tex": ("ランファの部屋", "란파의 방"),
    "gmplc_pop2_017.tex": ("ミントの部屋", "민트의 방"),
    "gmplc_pop2_018.tex": ("フォルテの部屋", "포르테의 방"),
    "gmplc_pop2_019.tex": ("ヴァニラの部屋", "바닐라의 방"),
    "gmplc_pop2_301.tex": ("ちとせの部屋", "치토세의 방"),
    "gmplc_pop2_302.tex": ("ゲストルーム", "게스트룸"),
    "gmplc_pop2_304.tex": ("？？？", "???"),
    "gmplc_pop3_021.tex": ("ロッカールーム", "로커룸"),
    "gmplc_pop3_022.tex": ("医務室", "의무실"),
    "gmplc_pop3_023.tex": ("クジラルーム", "쿠지라 룸"),
    "gmplc_pop3_025.tex": ("格納庫", "격납고"),
    "gmplc_pop3_026.tex": ("機関室", "기관실"),
    "gmplc_pop3_027.tex": ("射撃訓練場", "사격훈련장"),
    "gmplc_pop3_029.tex": ("倉庫", "창고"),
    "gmplc_pop3_030.tex": ("トレーニングルーム", "트레이닝룸"),
    "gmplc_pop3_303.tex": ("シミュレーションルーム", "시뮬레이션룸"),
}


def displayable(text: str) -> str:
    for source, replacement in GLYPH_FALLBACKS.items():
        text = text.replace(source, replacement)
    return text


def caption_mask(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """The pale caption glyphs on the bar under the room screenshot."""
    luminance = rgb.astype(np.float32).mean(axis=2)
    saturation = rgb.max(axis=2).astype(np.int16) - rgb.min(axis=2).astype(np.int16)
    mask = (luminance > 170) & (saturation < 60) & (alpha > 16)
    mask[: int(rgb.shape[0] * 0.75), :] = False
    return mask


def fit_font(text: str, width: int, height: int) -> ImageFont.FreeTypeFont:
    probe = ImageDraw.Draw(Image.new("L", (8, 8)))
    for size in range(height + 4, 6, -1):
        font = ImageFont.truetype(str(FONT_BOLD), size=size)
        box = probe.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= width and box[3] - box[1] <= height:
            return font
    return ImageFont.truetype(str(FONT_BOLD), size=7)


def render(source: Path, korean: str) -> Image.Image:
    original = Image.open(source).convert("RGBA")
    arr = np.array(original)
    rgb, alpha = arr[:, :, :3], arr[:, :, 3]

    mask = caption_mask(rgb, alpha)
    if mask.sum() < 20:
        raise ValueError(f"no caption found: {source}")
    ys, xs = np.where(mask)
    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)

    grown = cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8))
    plate = cv2.inpaint(rgb, grown, 3, cv2.INPAINT_TELEA)
    image = Image.fromarray(np.dstack([plate, alpha]), "RGBA")

    text = displayable(korean)
    draw = ImageDraw.Draw(image)
    font = fit_font(text, original.width - 12, max(9, box[3] - box[1]))
    tb = draw.textbbox((0, 0), text, font=font, stroke_width=1)
    x = round((box[0] + box[2]) / 2 - (tb[2] - tb[0]) / 2 - tb[0])
    x = max(3, min(x, original.width - (tb[2] - tb[0]) - 3))
    y = round((box[1] + box[3]) / 2 - (tb[3] - tb[1]) / 2 - tb[1])

    fill = tuple(int(v) for v in np.median(rgb[mask], axis=0)) + (255,)
    outline = tuple(max(0, int(v) - 90) for v in fill[:3]) + (235,)
    draw.text((x, y), text, font=font, fill=fill, stroke_width=1, stroke_fill=outline)
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
    for name, (japanese, korean) in ROOMS.items():
        png = f"block_{offset_of[name]:08x}.png"
        entry = by_png.get(png)
        if entry is None:
            raise SystemExit(f"{name} ({png}) is not in the translated image manifest")
        rendered = render(args.images_root / "png" / png, korean)
        target = (args.preview or (args.images_root / "translated_png")) / entry["translated_png"]
        target.parent.mkdir(parents=True, exist_ok=True)
        rendered.save(target)
        if args.preview is None:
            entry["original"] = japanese
            entry["translation"] = korean
            entry["classification"] = "eternal-room-popup"
        done += 1

    if args.preview is None:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"rendered": done}, ensure_ascii=False))


if __name__ == "__main__":
    main()
