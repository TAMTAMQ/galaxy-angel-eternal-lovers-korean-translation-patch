from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

START = b"\x00\x00\x01"


@dataclass(frozen=True)
class Packet:
    offset: int
    stream_id: int
    size: int
    payload_offset: int
    payload_size: int
    pts: int | None
    dts: int | None


def decode_ts(raw: bytes) -> int:
    if len(raw) != 5:
        raise ValueError("timestamp must be 5 bytes")
    return (
        ((raw[0] >> 1) & 0x07) << 30
        | (raw[1] << 22)
        | ((raw[2] >> 1) << 15)
        | (raw[3] << 7)
        | (raw[4] >> 1)
    )


def parse_pes_header(data: bytes, offset: int, packet_size: int) -> tuple[int, int | None, int | None]:
    stream_id = data[offset + 3]
    if stream_id in {0xBA, 0xBB, 0xBC, 0xBE, 0xBF, 0xB9}:
        return offset + 6, None, None
    if packet_size < 9 or offset + 9 > len(data):
        raise ValueError(f"short PES at 0x{offset:x}")
    if data[offset + 6] & 0xC0 != 0x80:
        return offset + 6, None, None
    flags = data[offset + 7]
    header_len = data[offset + 8]
    payload_offset = offset + 9 + header_len
    if payload_offset > offset + packet_size:
        raise ValueError(f"invalid PES header length at 0x{offset:x}")
    pts = dts = None
    ts_flags = (flags >> 6) & 0x03
    if ts_flags == 0x02 and header_len >= 5:
        pts = decode_ts(data[offset + 9 : offset + 14])
    elif ts_flags == 0x03 and header_len >= 10:
        pts = decode_ts(data[offset + 9 : offset + 14])
        dts = decode_ts(data[offset + 14 : offset + 19])
    return payload_offset, pts, dts


def parse_program_stream(path: Path) -> tuple[bytes, list[Packet]]:
    data = path.read_bytes()
    if not data.startswith(START):
        first = data.find(START)
        if first < 0:
            raise ValueError("no MPEG start code found")
        raise ValueError(f"unexpected prefix before first MPEG packet: {first} bytes")

    packets: list[Packet] = []
    pos = 0
    n = len(data)
    while pos < n:
        if data[pos : pos + 3] != START:
            raise ValueError(f"top-level packet sync lost at 0x{pos:x}: {data[pos:pos+8].hex()}")
        if pos + 4 > n:
            raise ValueError("truncated start code")
        sid = data[pos + 3]
        if sid == 0xB9:
            size = 4
            payload_offset = pos + 4
            pts = dts = None
        elif sid == 0xBA:
            if pos + 14 > n:
                raise ValueError("truncated pack header")
            stuffing = data[pos + 13] & 0x07
            size = 14 + stuffing
            payload_offset = pos + size
            pts = dts = None
        else:
            if pos + 6 > n:
                raise ValueError(f"truncated packet header at 0x{pos:x}")
            packet_len = int.from_bytes(data[pos + 4 : pos + 6], "big")
            if packet_len == 0:
                raise ValueError(f"zero-length top-level PES unsupported at 0x{pos:x}")
            size = 6 + packet_len
            if pos + size > n:
                raise ValueError(f"packet overruns file at 0x{pos:x}")
            payload_offset, pts, dts = parse_pes_header(data, pos, size)
        payload_size = max(0, pos + size - payload_offset)
        packets.append(Packet(pos, sid, size, payload_offset, payload_size, pts, dts))
        pos += size
    return data, packets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pss", type=Path)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    data, packets = parse_program_stream(args.pss)
    video = [p for p in packets if 0xE0 <= p.stream_id <= 0xEF]
    audio = [p for p in packets if p.stream_id == 0xBD]
    padding = [p for p in packets if p.stream_id == 0xBE]
    timed_video = [p for p in video if p.pts is not None]

    payload_prefixes: dict[str, int] = {}
    for p in video:
        prefix = data[p.payload_offset : p.payload_offset + min(8, p.payload_size)].hex()
        payload_prefixes[prefix] = payload_prefixes.get(prefix, 0) + 1

    audio_hash = hashlib.sha256()
    for p in audio:
        audio_hash.update(data[p.offset : p.offset + p.size])

    es = bytearray()
    video_es_ranges: list[tuple[int, int, Packet]] = []
    for p in video:
        start = len(es)
        es.extend(data[p.payload_offset : p.offset + p.size])
        video_es_ranges.append((start, len(es), p))

    picture_offsets: list[int] = []
    pos = 0
    while True:
        pos = es.find(b"\x00\x00\x01\x00", pos)
        if pos < 0:
            break
        picture_offsets.append(pos)
        pos += 4

    def picture_meta(off: int) -> dict[str, int | None]:
        if off + 6 > len(es):
            return {"temporal_reference": None, "picture_type": None}
        temporal_reference = (es[off + 4] << 2) | (es[off + 5] >> 6)
        picture_type = (es[off + 5] >> 3) & 0x07
        return {"temporal_reference": temporal_reference, "picture_type": picture_type}

    timed_picture_map: list[dict[str, int | None | bool]] = []
    picture_i = 0
    for es_start, es_end, p in video_es_ranges:
        if p.pts is None:
            continue
        while picture_i < len(picture_offsets) and picture_offsets[picture_i] < es_start:
            picture_i += 1
        pic_off = picture_offsets[picture_i] if picture_i < len(picture_offsets) else None
        in_same_pes = bool(pic_off is not None and pic_off < es_end)
        meta = picture_meta(pic_off) if pic_off is not None else {"temporal_reference": None, "picture_type": None}
        timed_picture_map.append(
            {
                "pts": p.pts,
                "dts": p.dts,
                "pes_offset": p.offset,
                "pes_es_start": es_start,
                "pes_es_end": es_end,
                "picture_es_offset": pic_off,
                "picture_offset_in_pes": (pic_off - es_start) if pic_off is not None else None,
                "picture_in_same_pes": in_same_pes,
                "temporal_reference": meta["temporal_reference"],
                "picture_type": meta["picture_type"],
            }
        )
        if pic_off is not None:
            picture_i += 1

    report = {
        "path": str(args.pss),
        "size": len(data),
        "packet_count": len(packets),
        "stream_counts": {f"0x{sid:02x}": sum(1 for p in packets if p.stream_id == sid) for sid in sorted({p.stream_id for p in packets})},
        "video_packet_count": len(video),
        "video_payload_capacity": sum(p.payload_size for p in video),
        "video_timed_packet_count": len(timed_video),
        "video_pts_first": timed_video[0].pts if timed_video else None,
        "video_pts_last": timed_video[-1].pts if timed_video else None,
        "video_dts_count": sum(p.dts is not None for p in video),
        "private_audio_packet_count": len(audio),
        "private_audio_packet_sha256": audio_hash.hexdigest(),
        "padding_packet_count": len(padding),
        "padding_bytes": sum(p.size for p in padding),
        "video_es_size": len(es),
        "video_picture_count": len(picture_offsets),
        "timed_picture_same_pes_count": sum(bool(x["picture_in_same_pes"]) for x in timed_picture_map),
        "picture_type_counts": {
            str(t): sum(1 for off in picture_offsets if picture_meta(off)["picture_type"] == t)
            for t in sorted({picture_meta(off)["picture_type"] for off in picture_offsets if picture_meta(off)["picture_type"] is not None})
        },
        "first_timed_picture_map": timed_picture_map[:30],
        "video_payload_prefixes_top20": sorted(payload_prefixes.items(), key=lambda kv: (-kv[1], kv[0]))[:20],
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
