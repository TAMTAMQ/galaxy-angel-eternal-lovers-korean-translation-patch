#!/usr/bin/env python3
"""Shared GADAT032 name-table and TEX codec helpers."""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass

from PIL import Image


NAME_RE = re.compile(rb"[A-Za-z0-9_-]+\.(?:tex|rau|pru|txa)")


@dataclass(frozen=True)
class TexInfo:
    kind: str
    width: int
    height: int
    storage_width: int
    storage_height: int
    header_size: int
    palette_colours: int = 0


def named_records(
    container: bytes | bytearray,
    records: dict[int, tuple[int, int, int]],
) -> dict[str, tuple[int, int, int, int]]:
    first_data = min(records)
    names = [match.group().decode("ascii") for match in NAME_RE.finditer(container[:first_data])]
    ordered = sorted(records.items())
    if len(names) != len(ordered):
        raise ValueError(f"GADAT032 name/record mismatch: {len(names)}/{len(ordered)}")
    return {
        name.lower(): (offset, record, raw_size, compressed_size)
        for name, (offset, (record, raw_size, compressed_size))
        in zip(names, ordered, strict=True)
    }


def tex_info(raw: bytes) -> TexInfo:
    if raw[:4] != b"TEX " or len(raw) < 64:
        raise ValueError("not a TEX resource")
    width, height = struct.unpack_from("<II", raw, 20)
    storage_width, storage_height = struct.unpack_from("<HH", raw, 56)
    if not width or not height:
        raise ValueError("invalid TEX dimensions")
    if len(raw) == 64 + storage_width * storage_height * 4:
        return TexInfo("rgba32", width, height, storage_width, storage_height, 64)
    if len(raw) == 64 + storage_width * storage_height * 3:
        return TexInfo("rgb24", width, height, storage_width, storage_height, 64)
    if len(raw) == 80 + width * height * 3:
        return TexInfo("rgb24_split", width, height, width, height, 80)
    pixel_format = struct.unpack_from("<H", raw, 46)[0]
    pixel_count = storage_width * storage_height
    # PSMCT16: a 64-byte header followed by RGB5A1 pixels.  Eternal Lovers uses it for the
    # outlined ship-location plates (haplc_*), which the earlier games did not have.
    if pixel_format == 0x02 and len(raw) == 64 + pixel_count * 2:
        return TexInfo("psmct16", width, height, storage_width, storage_height, 64)
    if pixel_format == 0x13 and len(raw) == 80 + pixel_count + 256 * 4:
        return TexInfo("indexed8", width, height, storage_width, storage_height, 80, 256)
    if pixel_format == 0x14 and len(raw) == 80 + (pixel_count + 1) // 2 + 16 * 4:
        return TexInfo("indexed4", width, height, storage_width, storage_height, 80, 16)
    raise ValueError(f"unsupported TEX layout: {width}x{height}, {len(raw)} bytes")


def decode_ps2_rgba(data: bytes) -> bytes:
    output = bytearray(data)
    for index in range(3, len(output), 4):
        output[index] = min(255, output[index] * 2)
    return bytes(output)


def encode_ps2_rgba(data: bytes) -> bytes:
    output = bytearray(data)
    for index in range(3, len(output), 4):
        output[index] = min(0x80, (output[index] + 1) // 2)
    return bytes(output)


def swizzle_ps2_clut(palette: list[bytes]) -> list[bytes]:
    """Swap PS2 8-bit CLUT groups; the operation is its own inverse."""
    if len(palette) != 256:
        return palette
    output = palette.copy()
    for base in range(0, 256, 32):
        output[base + 8 : base + 16] = palette[base + 16 : base + 24]
        output[base + 16 : base + 24] = palette[base + 8 : base + 16]
    return output


def decode_tex_full(raw: bytes) -> Image.Image:
    info = tex_info(raw)
    size = (info.storage_width, info.storage_height)
    if info.kind == "rgba32":
        return Image.frombytes("RGBA", size, decode_ps2_rgba(raw[64:]))
    if info.kind in ("rgb24", "rgb24_split"):
        return Image.frombytes("RGB", size, raw[info.header_size:]).convert("RGBA")
    if info.kind == "psmct16":
        pixel_count = info.storage_width * info.storage_height
        values = struct.unpack_from(f"<{pixel_count}H", raw, 64)
        pixels = bytearray()
        for value in values:
            pixels.extend((
                (value & 0x1F) * 255 // 31,
                ((value >> 5) & 0x1F) * 255 // 31,
                ((value >> 10) & 0x1F) * 255 // 31,
                255 if value & 0x8000 else 0,
            ))
        return Image.frombytes("RGBA", size, bytes(pixels))

    pixel_count = info.storage_width * info.storage_height
    index_size = pixel_count if info.kind == "indexed8" else (pixel_count + 1) // 2
    packed = raw[80 : 80 + index_size]
    palette_data = decode_ps2_rgba(raw[80 + index_size :])
    palette = [palette_data[index : index + 4] for index in range(0, len(palette_data), 4)]
    if info.kind == "indexed8":
        palette = swizzle_ps2_clut(palette)
        indices = packed
    else:
        indices = bytes(nibble for value in packed for nibble in (value & 15, value >> 4))
    pixels = bytearray()
    for index in indices[:pixel_count]:
        pixels.extend(palette[index])
    return Image.frombytes("RGBA", size, bytes(pixels))


def decode_tex(raw: bytes) -> Image.Image:
    info = tex_info(raw)
    return decode_tex_full(raw).crop((0, 0, info.width, info.height))


def quantize_rgba(image: Image.Image, colours: int) -> tuple[bytes, list[bytes]]:
    quantized = image.quantize(
        colors=colours,
        method=Image.Quantize.FASTOCTREE,
        dither=Image.Dither.NONE,
    )
    indices = quantized.tobytes()
    rendered = quantized.convert("RGBA")
    pixels = rendered.tobytes()
    palette = [bytes(4) for _ in range(colours)]
    assigned = [False] * colours
    for position, palette_index in enumerate(indices):
        if not assigned[palette_index]:
            start = position * 4
            palette[palette_index] = pixels[start : start + 4]
            assigned[palette_index] = True
    return indices, palette


def indexed_palette(raw: bytes, info: TexInfo) -> list[bytes]:
    pixel_count = info.storage_width * info.storage_height
    index_size = pixel_count if info.kind == "indexed8" else (pixel_count + 1) // 2
    palette_data = decode_ps2_rgba(raw[80 + index_size :])
    palette = [palette_data[index : index + 4] for index in range(0, len(palette_data), 4)]
    return swizzle_ps2_clut(palette) if info.kind == "indexed8" else palette


def map_to_palette(image: Image.Image, palette: list[bytes]) -> bytes:
    pixels = image.convert("RGBA").tobytes()
    exact: dict[bytes, int] = {}
    for index, colour in enumerate(palette):
        exact.setdefault(colour, index)
    cache: dict[bytes, int] = {}
    indices = bytearray()
    for position in range(0, len(pixels), 4):
        colour = pixels[position : position + 4]
        palette_index = exact.get(colour)
        if palette_index is None:
            palette_index = cache.get(colour)
        if palette_index is None:
            red, green, blue, alpha = colour
            palette_index = min(
                range(len(palette)),
                key=lambda candidate: (
                    (palette[candidate][0] - red) ** 2
                    + (palette[candidate][1] - green) ** 2
                    + (palette[candidate][2] - blue) ** 2
                    + 2 * (palette[candidate][3] - alpha) ** 2
                ),
            )
            cache[colour] = palette_index
        indices.append(palette_index)
    return bytes(indices)


def encode_tex(original: bytes, replacement: Image.Image, colours: int | None = None) -> bytes:
    info = tex_info(original)
    source = replacement.convert("RGBA")
    if source.size != (info.width, info.height):
        raise ValueError(
            f"PNG dimensions changed: {source.width}x{source.height} != "
            f"{info.width}x{info.height}"
        )
    canvas = decode_tex_full(original)
    canvas.paste(source, (0, 0))

    if info.kind == "psmct16":
        # RGB5A1: five bits per channel and a single alpha bit, so alpha is thresholded rather
        # than scaled.  Round each channel to nearest rather than truncating, otherwise every
        # repeated encode darkens the plate.
        pixels = canvas.tobytes()
        values = bytearray()
        for position in range(0, len(pixels), 4):
            red, green, blue, alpha = pixels[position : position + 4]
            packed = (
                ((red * 31 + 127) // 255)
                | (((green * 31 + 127) // 255) << 5)
                | (((blue * 31 + 127) // 255) << 10)
                | (0x8000 if alpha >= 128 else 0)
            )
            values.extend(struct.pack("<H", packed))
        return original[:64] + bytes(values)

    if info.kind in ("rgba32", "rgb24", "rgb24_split"):
        if colours:
            canvas = canvas.quantize(
                colors=colours,
                method=Image.Quantize.FASTOCTREE,
                dither=Image.Dither.NONE,
            ).convert("RGBA")
        if info.kind == "rgba32":
            payload = encode_ps2_rgba(canvas.tobytes())
        else:
            payload = canvas.convert("RGB").tobytes()
        return original[: info.header_size] + payload

    if colours is None:
        palette = indexed_palette(original, info)
        indices = map_to_palette(canvas, palette)
    else:
        palette_limit = min(info.palette_colours, colours)
        indices, palette = quantize_rgba(canvas, palette_limit)
        palette.extend(bytes(4) for _ in range(info.palette_colours - len(palette)))
    if info.kind == "indexed8":
        stored_palette = swizzle_ps2_clut(palette)
        packed = indices
    else:
        packed_data = bytearray()
        for position in range(0, len(indices), 2):
            low = indices[position]
            high = indices[position + 1] if position + 1 < len(indices) else 0
            packed_data.append(low | high << 4)
        packed = bytes(packed_data)
        stored_palette = palette
    palette_bytes = encode_ps2_rgba(b"".join(stored_palette))
    return original[:80] + packed + palette_bytes


def pixel_hash(image: Image.Image) -> str:
    rgba = image.convert("RGBA")
    digest = hashlib.sha256()
    digest.update(struct.pack("<II", rgba.width, rgba.height))
    digest.update(rgba.tobytes())
    return digest.hexdigest()
