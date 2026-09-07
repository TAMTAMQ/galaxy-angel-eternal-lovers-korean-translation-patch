from __future__ import annotations

from dataclasses import dataclass

PICTURE_START = b"\x00\x00\x01\x00"


@dataclass(frozen=True)
class Picture:
    offset: int
    temporal_reference: int
    picture_type: int


def parse_pictures(es: bytes) -> list[Picture]:
    out: list[Picture] = []
    pos = 0
    while True:
        pos = es.find(PICTURE_START, pos)
        if pos < 0:
            break
        if pos + 6 > len(es):
            raise ValueError(f"truncated picture header at ES offset 0x{pos:x}")
        temporal_reference = (es[pos + 4] << 2) | (es[pos + 5] >> 6)
        picture_type = (es[pos + 5] >> 3) & 0x07
        out.append(Picture(pos, temporal_reference, picture_type))
        pos += 4
    return out


def encode_ts(value: int, prefix: int) -> bytes:
    if value < 0:
        raise ValueError(f"negative MPEG timestamp: {value}")
    value &= (1 << 33) - 1
    return bytes(
        [
            ((prefix & 0x0F) << 4) | (((value >> 30) & 0x07) << 1) | 1,
            (value >> 22) & 0xFF,
            (((value >> 15) & 0x7F) << 1) | 1,
            (value >> 7) & 0xFF,
            ((value & 0x7F) << 1) | 1,
        ]
    )
