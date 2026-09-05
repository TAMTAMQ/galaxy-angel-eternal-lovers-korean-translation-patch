#!/usr/bin/env python3
"""Build and inspect Galaxy Angel: Eternal Lovers' embedded 24x24 font."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


GLYPH_WIDTH = 24
GLYPH_HEIGHT = 24
GLYPH_BYTES = GLYPH_WIDTH * GLYPH_HEIGHT // 4
PAIR_BYTES = GLYPH_WIDTH * GLYPH_HEIGHT // 2
FONT_VADDR = 0x00320CC0
FONT_FILE_OFFSET = 0x00221CC0
ORIGINAL_GLYPHS = 7045
FIRST_REPLACEMENT_INDEX = 5000


def is_script_safe_sjis(code: bytes) -> bool:
    """Avoid trail bytes that the scenario VM may interpret as ASCII syntax."""

    return not 0x40 <= code[1] <= 0x7E


def index_to_sjis(index: int) -> bytes:
    """Invert the compact Shift-JIS-to-glyph mapping at ELF VA 0x0018C4F8."""

    if index < 690:
        raw_index = index
    elif index < 3655:
        raw_index = index + 720
    elif index < ORIGINAL_GLYPHS:
        raw_index = index + 763
    else:
        raise ValueError(f"glyph index {index} is outside the embedded table")
    row, column = divmod(raw_index, 188)
    if row < 31:
        lead = 0x81 + row
    else:
        lead = 0xE0 + row - 31
    if not 0x81 <= lead <= 0xFC:
        raise ValueError(f"glyph index {index} is outside the renderer's Shift-JIS space")
    trail = 0x40 + column if column < 63 else 0x41 + column
    return bytes((lead, trail))


def render_glyph(char: str, font_path: Path, font_size: int = 20,
                 font_index: int = 0) -> bytes:
    """Render a 24x24 glyph as four 2-bit pixels per byte, MSB first."""

    scale = 4
    box_size = min(font_size, GLYPH_WIDTH, GLYPH_HEIGHT)
    high = Image.new("L", (box_size * scale, box_size * scale), 0)
    font = ImageFont.truetype(
        str(font_path), int(box_size * scale * 1.06), index=font_index
    )
    ImageDraw.Draw(high).text(
        (box_size * scale / 2, box_size * scale / 2), char,
        font=font, fill=255, anchor="mm",
    )
    glyph = high.resize((box_size, box_size), Image.Resampling.LANCZOS)
    image = Image.new("L", (GLYPH_WIDTH, GLYPH_HEIGHT), 0)
    image.paste(
        glyph,
        ((GLYPH_WIDTH - box_size) // 2, (GLYPH_HEIGHT - box_size) // 2),
    )
    levels = [(value * 3 + 127) // 255 for value in image.get_flattened_data()]
    return bytes(
        (levels[i] << 6) | (levels[i + 1] << 4)
        | (levels[i + 2] << 2) | levels[i + 3]
        for i in range(0, len(levels), 4)
    )


def merge_paired_glyph(block: bytearray, parity: int, packed: bytes) -> None:
    """Replace one 2bpp plane inside a paired 24x24 PSMT4 cell."""

    if len(block) != PAIR_BYTES or len(packed) != GLYPH_BYTES:
        raise ValueError("invalid paired glyph buffers")
    plane_shift = parity * 2
    for pixel in range(GLYPH_WIDTH * GLYPH_HEIGHT):
        source_shift = 6 - (pixel & 3) * 2
        level = (packed[pixel // 4] >> source_shift) & 3
        nibble_shift = (pixel & 1) * 4
        shift = nibble_shift + plane_shift
        mask = 3 << shift
        block[pixel // 2] = (block[pixel // 2] & ~mask) | (level << shift)


def decode_glyph(raw: bytes) -> Image.Image:
    """Decode a standalone linear 2bpp cell (analysis helper)."""

    if len(raw) != GLYPH_BYTES:
        raise ValueError(f"expected {GLYPH_BYTES} bytes, got {len(raw)}")
    pixels: list[int] = []
    for value in raw:
        pixels.extend(((value >> 6) * 85, ((value >> 4) & 3) * 85,
                       ((value >> 2) & 3) * 85, (value & 3) * 85))
    image = Image.new("L", (GLYPH_WIDTH, GLYPH_HEIGHT))
    image.putdata(pixels)
    return image


def decode_paired_glyph(raw: bytes, parity: int,
                        reverse_planes: bool = False) -> Image.Image:
    """Decode one glyph from the engine's paired 24x24 PSMT4 cell."""

    if len(raw) != PAIR_BYTES:
        raise ValueError(f"expected {PAIR_BYTES} bytes, got {len(raw)}")
    plane = parity ^ int(reverse_planes)
    pixels: list[int] = []
    for value in raw:
        for nibble_shift in (0, 4):
            nibble = (value >> nibble_shift) & 15
            pixels.append(((nibble >> (plane * 2)) & 3) * 85)
    image = Image.new("L", (GLYPH_WIDTH, GLYPH_HEIGHT))
    image.putdata(pixels)
    return image


def decode_planar_glyph(raw: bytes, lsb_first: bool = False,
                        swap_planes: bool = False) -> Image.Image:
    pixels: list[int] = []
    plane_size = GLYPH_WIDTH * GLYPH_HEIGHT // 8
    planes = (raw[plane_size:], raw[:plane_size]) if swap_planes else (
        raw[:plane_size], raw[plane_size:]
    )
    for pixel in range(GLYPH_WIDTH * GLYPH_HEIGHT):
        byte_index, bit_index = divmod(pixel, 8)
        shift = bit_index if lsb_first else 7 - bit_index
        level = ((planes[0][byte_index] >> shift) & 1) | (
            ((planes[1][byte_index] >> shift) & 1) << 1
        )
        pixels.append(level * 85)
    image = Image.new("L", (GLYPH_WIDTH, GLYPH_HEIGHT))
    image.putdata(pixels)
    return image


def decode_variants(raw: bytes, parity: int) -> Image.Image:
    """Build a labeled montage used to identify the on-disc packing."""

    standalone = raw[:GLYPH_BYTES]
    variants: list[tuple[str, Image.Image]] = [
        ("paired normal", decode_paired_glyph(raw, parity)),
        ("paired reverse", decode_paired_glyph(raw, parity, True)),
        ("2bpp MSB 24x24", decode_glyph(standalone)),
    ]
    pixels_lsb: list[int] = []
    for value in standalone:
        pixels_lsb.extend(((value & 3) * 85, ((value >> 2) & 3) * 85,
                           ((value >> 4) & 3) * 85, (value >> 6) * 85))
    lsb = Image.new("L", (24, 24))
    lsb.putdata(pixels_lsb)
    variants.append(("2bpp LSB 24x24", lsb))
    for width, height in ((12, 24), (24, 12), (16, 18), (18, 16)):
        for low_first in (False, True):
            pixels: list[int] = []
            for value in standalone:
                pair = (value & 15, value >> 4) if low_first else (value >> 4, value & 15)
                pixels.extend(level * 17 for level in pair)
            image = Image.new("L", (width, height))
            image.putdata(pixels)
            order = "low" if low_first else "high"
            variants.append((f"4bpp {order} {width}x{height}", image))
    variants.extend((
        ("planar MSB", decode_planar_glyph(standalone)),
        ("planar LSB", decode_planar_glyph(standalone, lsb_first=True)),
        ("planar MSB swap", decode_planar_glyph(standalone, swap_planes=True)),
        ("planar LSB swap", decode_planar_glyph(standalone, True, True)),
    ))
    cell_width, cell_height = 220, 220
    rows = (len(variants) + 1) // 2
    montage = Image.new("RGB", (cell_width * 2, cell_height * rows), "#303030")
    draw = ImageDraw.Draw(montage)
    for index, (label, image) in enumerate(variants):
        x = (index % 2) * cell_width
        y = (index // 2) * cell_height
        scaled = image.resize((192, 192), Image.Resampling.NEAREST).convert("RGB")
        montage.paste(scaled, (x + 14, y + 24))
        draw.text((x + 8, y + 5), label, fill="white")
    return montage


def translation_entries(translations: Path):
    if translations.is_dir():
        index = json.loads((translations / "index.json").read_text(encoding="utf-8"))
        for item in index.get("segments", []):
            segment = json.loads(
                (translations / item["path"]).read_text(encoding="utf-8")
            )
            yield from segment.get("units", [])
        return
    payload = json.loads(translations.read_text(encoding="utf-8"))
    yield from payload.get("candidates", payload.get("entries", payload.get("units", [])))


def collect_custom_chars(translations: list[Path]) -> list[str]:
    chars: set[str] = set()
    for source in translations:
        for entry in translation_entries(source):
            if not entry.get("use_translation") or not entry.get("translation"):
                continue
            for char in entry["translation"]:
                try:
                    char.encode("cp932")
                except UnicodeEncodeError:
                    chars.add(char)
    return sorted(chars)


def build_font(translations: list[Path], input_elf: Path, output_elf: Path,
               map_output: Path, font_path: Path, font_size: int,
               font_index: int) -> None:
    chars = collect_custom_chars(translations)
    available_indices = [
        index
        for index in range(ORIGINAL_GLYPHS - 1, FIRST_REPLACEMENT_INDEX - 1, -1)
        if is_script_safe_sjis(index_to_sjis(index))
    ]
    if len(chars) > len(available_indices):
        raise SystemExit(
            f"custom glyph capacity exceeded: {len(chars)} > {len(available_indices)}"
        )
    data = bytearray(input_elf.read_bytes())
    table_end = FONT_FILE_OFFSET + ((ORIGINAL_GLYPHS + 1) // 2) * PAIR_BYTES
    if table_end > len(data):
        raise SystemExit(
            f"embedded font exceeds ELF: {table_end:#x} > {len(data):#x}"
        )
    mapping: dict[str, dict[str, int | str]] = {}
    for char, index in zip(chars, available_indices, strict=False):
        offset = FONT_FILE_OFFSET + (index // 2) * PAIR_BYTES
        block = bytearray(data[offset:offset + PAIR_BYTES])
        original_block = bytes(block)
        rendered = render_glyph(char, font_path, font_size, font_index)
        merge_paired_glyph(
            block, index & 1, rendered,
        )
        target_mask = 0xCC if index & 1 else 0x33
        if any((old ^ new) & ~target_mask for old, new in zip(original_block, block)):
            raise AssertionError(f"paired partner plane changed for glyph {index}")
        expected = list(decode_glyph(rendered).get_flattened_data())
        actual = list(decode_paired_glyph(block, index & 1).get_flattened_data())
        if actual != expected:
            raise AssertionError(f"paired glyph verification failed for glyph {index}")
        data[offset:offset + PAIR_BYTES] = block
        mapping[char] = {
            "hex": index_to_sjis(index).hex(),
            "glyph_index": index,
            "pair_file_offset": offset,
        }
    output_elf.parent.mkdir(parents=True, exist_ok=True)
    output_elf.write_bytes(data)
    map_output.parent.mkdir(parents=True, exist_ok=True)
    map_output.write_text(json.dumps({
        "schema": "eternal-lovers-font-map/v1",
        "font": str(font_path),
        "font_size": font_size,
        "font_index": font_index,
        "font_file_offset": FONT_FILE_OFFSET,
        "font_vaddr": FONT_VADDR,
        "glyph_format": "paired 24x24 PSMT4; two 2bpp glyph planes per 288-byte cell",
        "glyph_bytes": GLYPH_BYTES,
        "pair_bytes": PAIR_BYTES,
        "replacement_index_floor": FIRST_REPLACEMENT_INDEX,
        "characters": mapping,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"patched {len(mapping)} custom glyphs; "
        f"capacity={len(available_indices)} output={output_elf}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--elf", type=Path, required=True)
    inspect.add_argument("--index", type=int, required=True)
    inspect.add_argument("--output", type=Path, required=True)
    inspect.add_argument("--variants", action="store_true")
    build = sub.add_parser("build")
    build.add_argument("--translations", type=Path, action="append", required=True)
    build.add_argument("--input-elf", type=Path, required=True)
    build.add_argument("--output-elf", type=Path, required=True)
    build.add_argument("--map-output", type=Path, required=True)
    build.add_argument(
        "--font", type=Path,
        default=Path(
            r"vendor\pretendard\packages\pretendard\dist\public\static\alternative"
            r"\Pretendard-Bold.ttf"
        ),
    )
    build.add_argument("--font-size", type=int, default=20)
    build.add_argument("--font-index", type=int, default=0)
    args = parser.parse_args()
    if args.command == "inspect":
        data = args.elf.read_bytes()
        start = FONT_FILE_OFFSET + (args.index // 2) * PAIR_BYTES
        raw = data[start:start + PAIR_BYTES]
        image = (
            decode_variants(raw, args.index & 1)
            if args.variants else decode_paired_glyph(raw, args.index & 1)
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if not args.variants:
            image = image.resize((192, 192), Image.Resampling.NEAREST)
        image.save(args.output)
        print(
            f"index={args.index} sjis={index_to_sjis(args.index).hex()} "
            f"pair_file_offset={start:#x} "
            f"pair_vaddr={FONT_VADDR + (args.index // 2) * PAIR_BYTES:#x}"
        )
        return
    build_font(
        args.translations, args.input_elf, args.output_elf, args.map_output,
        args.font, args.font_size, args.font_index,
    )


if __name__ == "__main__":
    main()
