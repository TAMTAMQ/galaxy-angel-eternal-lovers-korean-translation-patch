from __future__ import annotations

import argparse
import hashlib
import json
import math
import wave
from fractions import Fraction
from pathlib import Path

from galaxy_angel_build_subtitled_pss import encode_ts, parse_pictures
from galaxy_angel_pss_analyze import parse_program_stream

PACK_SIZE = 16384
MUX_RATE_BPS = 7_536_000
BASE_PTS = 12355
FRAME_RATE_BY_CODE = {
    1: Fraction(24000, 1001),
    2: Fraction(24, 1),
    3: Fraction(25, 1),
    4: Fraction(30000, 1001),
    5: Fraction(30, 1),
    6: Fraction(50, 1),
    7: Fraction(60000, 1001),
    8: Fraction(60, 1),
}
SYSTEM_HEADER = bytes.fromhex("000001bb00099cbf6100217fe0e706")
PACK_RATE_TAIL = bytes.fromhex("397ec3f8")
FIRST_VIDEO_STD = bytes.fromhex("1e6706")


def mux_rate_units(mux_rate_bps: int) -> int:
    if mux_rate_bps <= 0 or mux_rate_bps % 400 != 0:
        raise ValueError(f"mux rate must be a positive multiple of 400 bps: {mux_rate_bps}")
    units = mux_rate_bps // 400
    if units >= (1 << 22):
        raise ValueError(f"mux rate exceeds MPEG-2 22-bit field: {mux_rate_bps}")
    return units


def pack_rate_tail_for_bps(mux_rate_bps: int) -> bytes:
    units = mux_rate_units(mux_rate_bps)
    return bytes(
        (
            (units >> 14) & 0xFF,
            (units >> 6) & 0xFF,
            ((units & 0x3F) << 2) | 0x03,
            0xF8,
        )
    )


def system_header_for_bps(mux_rate_bps: int, template: bytes = SYSTEM_HEADER) -> bytes:
    if len(template) != 15 or not template.startswith(b"\x00\x00\x01\xbb\x00\x09"):
        raise ValueError("invalid MPEG-2 system header template")
    units = mux_rate_units(mux_rate_bps)
    rate_bound = bytes(
        (
            0x80 | ((units >> 15) & 0x7F),
            (units >> 7) & 0xFF,
            ((units & 0x7F) << 1) | 0x01,
        )
    )
    return template[:6] + rate_bound + template[9:]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(16 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def frame_rate_from_es(es: bytes) -> Fraction:
    seq = es.find(b"\x00\x00\x01\xb3")
    if seq < 0 or seq + 8 > len(es):
        raise ValueError("MPEG-2 sequence header not found")
    code = es[seq + 7] & 0x0F
    try:
        return FRAME_RATE_BY_CODE[code]
    except KeyError as exc:
        raise ValueError(f"unsupported MPEG-2 frame_rate_code: {code}") from exc


def gop_timecode_frame(es: bytes, offset: int, rate: Fraction) -> tuple[int, bool]:
    if es[offset : offset + 4] != b"\x00\x00\x01\xb8" or offset + 8 > len(es):
        raise ValueError(f"invalid GOP header at ES offset 0x{offset:x}")
    b4, b5, b6, b7 = es[offset + 4 : offset + 8]
    drop_frame = bool(b4 & 0x80)
    hours = (b4 >> 2) & 0x1F
    minutes = ((b4 & 0x03) << 4) | (b5 >> 4)
    seconds = ((b5 & 0x07) << 3) | (b6 >> 5)
    pictures = ((b6 & 0x1F) << 1) | (b7 >> 7)
    closed_gop = bool((b7 >> 6) & 1)

    nominal = round(float(rate))
    frame = ((hours * 60 + minutes) * 60 + seconds) * nominal + pictures
    if drop_frame:
        total_minutes = hours * 60 + minutes
        if rate == Fraction(30000, 1001):
            frame -= 2 * (total_minutes - total_minutes // 10)
        elif rate == Fraction(60000, 1001):
            frame -= 4 * (total_minutes - total_minutes // 10)
        else:
            raise ValueError(f"drop-frame GOP timecode is unsupported for frame rate {rate}")
    return frame, closed_gop


def picture_display_frames_for_rate(es: bytes, pictures: list, rate: Fraction) -> tuple[list[int], list[bool]]:
    gops: list[tuple[int, int, bool]] = []
    pos = 0
    while True:
        pos = es.find(b"\x00\x00\x01\xb8", pos)
        if pos < 0:
            break
        frame, closed = gop_timecode_frame(es, pos, rate)
        gops.append((pos, frame, closed))
        pos += 4
    if not gops:
        raise ValueError("encoded MPEG-2 stream has no GOP headers")

    display_frames: list[int] = []
    closed_flags: list[bool] = []
    gop_i = 0
    for pic in pictures:
        while gop_i + 1 < len(gops) and gops[gop_i + 1][0] < pic.offset:
            gop_i += 1
        gop_offset, base_frame, closed = gops[gop_i]
        if gop_offset > pic.offset:
            raise ValueError(f"picture at 0x{pic.offset:x} precedes first GOP header")
        display_frames.append(base_frame + pic.temporal_reference)
        closed_flags.append(closed)

    expected = list(range(len(pictures)))
    if sorted(display_frames) != expected:
        raise ValueError(
            "GOP timecode/temporal_reference does not form a complete CFR display timeline: "
            f"rate={rate} min={min(display_frames)} max={max(display_frames)} "
            f"unique={len(set(display_frames))}/{len(pictures)}"
        )
    return display_frames, closed_flags


def ticks_for_frame(frame: int, rate: Fraction) -> int:
    return int(round(Fraction(frame * 90000 * rate.denominator, rate.numerator)))


def build_pack_header(
    pack_index: int,
    *,
    mux_rate_bps: int = MUX_RATE_BPS,
    pack_rate_tail: bytes | None = None,
) -> bytes:
    if mux_rate_bps <= 0:
        raise ValueError(f"invalid mux rate: {mux_rate_bps}")
    if pack_rate_tail is None:
        pack_rate_tail = pack_rate_tail_for_bps(mux_rate_bps)
    if len(pack_rate_tail) != 4:
        raise ValueError(f"pack rate tail must be 4 bytes, got {len(pack_rate_tail)}")

    byte_offset = pack_index * PACK_SIZE
    scr_27m = (byte_offset * 8 * 27_000_000) // mux_rate_bps
    scr = scr_27m // 300
    ext = scr_27m % 300
    b0 = 0x40 | (((scr >> 30) & 0x07) << 3) | 0x04 | ((scr >> 28) & 0x03)
    b1 = (scr >> 20) & 0xFF
    b2 = (((scr >> 15) & 0x1F) << 3) | 0x04 | ((scr >> 13) & 0x03)
    b3 = (scr >> 5) & 0xFF
    b4 = ((scr & 0x1F) << 3) | 0x04 | ((ext >> 7) & 0x03)
    b5 = ((ext & 0x7F) << 1) | 0x01
    return b"\x00\x00\x01\xba" + bytes((b0, b1, b2, b3, b4, b5)) + pack_rate_tail


def build_padding_packet(total_size: int) -> bytes:
    if total_size < 6:
        raise ValueError(f"padding packet too small: {total_size}")
    payload_len = total_size - 6
    if payload_len > 0xFFFF:
        raise ValueError(f"padding packet too large: {total_size}")
    return b"\x00\x00\x01\xbe" + payload_len.to_bytes(2, "big") + (b"\xff" * payload_len)


def build_pes(stream_id: int, payload: bytes, pts: int | None = None, dts: int | None = None, *, first_video: bool = False) -> bytes:
    if first_video:
        if stream_id != 0xE0 or pts is None or dts is None:
            raise ValueError("first_video requires video PTS+DTS")
        optional = b"\x83\xc1\x0d" + encode_ts(pts, 0x3) + encode_ts(dts, 0x1) + FIRST_VIDEO_STD
    elif pts is None:
        optional = b"\x83\x00\x0a" + (b"\xff" * 10)
    elif dts is None:
        optional = b"\x83\x80\x0a" + encode_ts(pts, 0x2) + (b"\xff" * 5)
    else:
        optional = b"\x83\xc0\x0a" + encode_ts(pts, 0x3) + encode_ts(dts, 0x1)
    packet_len = len(optional) + len(payload)
    if packet_len > 0xFFFF:
        raise ValueError(f"PES too large: {packet_len}")
    return b"\x00\x00\x01" + bytes((stream_id,)) + packet_len.to_bytes(2, "big") + optional + payload


def wav_to_ss_stream(path: Path) -> tuple[bytes, bytes, dict[str, int]]:
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        rate = w.getframerate()
        frames = w.getnframes()
        if channels != 2 or sampwidth != 2 or rate != 48000:
            raise ValueError(
                f"expected 48 kHz stereo 16-bit PCM WAV, got channels={channels} sampwidth={sampwidth} rate={rate}"
            )
        raw = w.readframes(frames)

    planar = bytearray()
    block_frames = 256
    frame_bytes = 4
    block_bytes = block_frames * frame_bytes
    for base in range(0, len(raw), block_bytes):
        block = raw[base : base + block_bytes]
        left = bytearray()
        right = bytearray()
        for i in range(0, len(block), frame_bytes):
            frame = block[i : i + frame_bytes]
            if len(frame) < frame_bytes:
                raise ValueError("truncated stereo PCM frame")
            left.extend(frame[:2])
            right.extend(frame[2:4])
        planar.extend(left)
        planar.extend(right)

    header = (
        b"SShd"
        + (24).to_bytes(4, "little")
        + (1).to_bytes(4, "little")
        + rate.to_bytes(4, "little")
        + channels.to_bytes(4, "little")
        + (0x200).to_bytes(4, "little")
        + (b"\xff" * 8)
        + b"SSbd"
        + len(planar).to_bytes(4, "little")
    )
    stream = header + bytes(planar)
    return stream, raw, {"channels": channels, "sample_width": sampwidth, "sample_rate": rate, "frames": frames}


def audio_pts_for_offset(stream_offset: int, base_pts: int = BASE_PTS) -> int:
    return base_pts + int(math.floor((stream_offset * 90000 / 192000) + 0.5))


def template_timing(path: Path) -> dict[str, object]:
    data, packets = parse_program_stream(path)
    schedule: list[tuple[int, int, int]] = []
    audio_ss = bytearray()
    first_video_pts: int | None = None
    pack_offsets = [p.offset for p in packets if p.stream_id == 0xBA]
    if any(off != i * PACK_SIZE for i, off in enumerate(pack_offsets)):
        raise ValueError(f"template pack headers are not 16 KiB-aligned: {path}")

    for p in packets:
        if p.stream_id == 0xE0 and p.pts is not None and first_video_pts is None:
            first_video_pts = p.pts
        if p.stream_id != 0xBD:
            continue
        if p.pts is None:
            raise ValueError(f"template private audio packet lacks PTS at 0x{p.offset:x}")
        pack_i = p.offset // PACK_SIZE
        in_pack = p.offset % PACK_SIZE
        starts = [29, 4125, 8221, 12317] if pack_i == 0 else [14, 4110, 8206, 12302]
        try:
            slot_in_pack = starts.index(in_pack)
        except ValueError as exc:
            raise ValueError(
                f"template audio packet is not on a PSS Plex slot boundary: "
                f"offset=0x{p.offset:x} pack={pack_i} in_pack={in_pack}"
            ) from exc
        payload = data[p.payload_offset : p.offset + p.size]
        if not payload.startswith(b"\xff\xa0\x00\x00"):
            raise ValueError(f"unexpected template private audio prefix at 0x{p.offset:x}")
        body = payload[4:]
        schedule.append((pack_i * 4 + slot_in_pack, p.pts, len(body)))
        audio_ss.extend(body)

    if not schedule:
        raise ValueError(f"template contains no private audio packets: {path}")
    return {
        "schedule": schedule,
        "audio_ss": bytes(audio_ss),
        "first_video_pts": first_video_pts,
        "first_audio_pts": schedule[0][1],
        "pack_count": len(pack_offsets),
    }


def template_mux_parameters(path: Path) -> dict[str, object]:
    data, packets = parse_program_stream(path)
    pack_offsets = [p.offset for p in packets if p.stream_id == 0xBA]
    if not pack_offsets or any(off != i * PACK_SIZE for i, off in enumerate(pack_offsets)):
        raise ValueError(f"mux template pack headers are not 16 KiB-aligned: {path}")

    first_tail = data[pack_offsets[0] + 10 : pack_offsets[0] + 14]
    if len(first_tail) != 4:
        raise ValueError(f"invalid pack-rate tail in mux template: {path}")
    rate_units = (first_tail[0] << 14) | (first_tail[1] << 6) | (first_tail[2] >> 2)
    mux_rate_bps = rate_units * 50 * 8
    if mux_rate_bps <= 0:
        raise ValueError(f"invalid mux rate in template: {path}")

    original_system = data[14:29]
    if len(original_system) != 15 or not original_system.startswith(b"\x00\x00\x01\xbb\x00\x09"):
        raise ValueError(f"unexpected first system header in mux template: {path}")
    # Preserve the custom mux's video P-STD bound, but use the original movie's
    # rate_bound so the declared stream rate matches the actual interleave.
    system_header = SYSTEM_HEADER[:6] + original_system[6:9] + SYSTEM_HEADER[9:]
    return {
        "mux_rate_bps": mux_rate_bps,
        "pack_rate_tail": first_tail,
        "system_header": system_header,
        "template_pack_count": len(pack_offsets),
    }


def mux(
    m2v: Path,
    wav: Path,
    output: Path,
    *,
    mux_template_pss: Path | None = None,
    audio_prefetch_slots: int = 0,
    audio_burst_packets: int = 1,
    base_pts_override: int | None = None,
    audio_target_lead_ms: float | None = None,
) -> dict[str, object]:
    if audio_prefetch_slots < 0:
        raise ValueError(f"audio_prefetch_slots must be >= 0: {audio_prefetch_slots}")
    if audio_burst_packets < 1:
        raise ValueError(f"audio_burst_packets must be >= 1: {audio_burst_packets}")
    if audio_target_lead_ms is not None and audio_target_lead_ms < 0:
        raise ValueError(f"audio_target_lead_ms must be >= 0: {audio_target_lead_ms}")
    video = m2v.read_bytes()
    pictures = parse_pictures(video)
    if not pictures:
        raise ValueError("no MPEG-2 pictures found")
    frame_rate = frame_rate_from_es(video)
    display_frames, _closed = picture_display_frames_for_rate(video, pictures, frame_rate)
    audio_stream, wav_raw, wav_info = wav_to_ss_stream(wav)

    base_pts = BASE_PTS if base_pts_override is None else int(base_pts_override)
    if base_pts < 0:
        raise ValueError(f"base PTS must be >= 0: {base_pts}")
    mux_params = template_mux_parameters(mux_template_pss) if mux_template_pss is not None else None
    mux_rate_bps = int(mux_params["mux_rate_bps"]) if mux_params is not None else MUX_RATE_BPS
    pack_rate_tail = (
        bytes(mux_params["pack_rate_tail"])
        if mux_params is not None
        else pack_rate_tail_for_bps(mux_rate_bps)
    )
    system_header = (
        bytes(mux_params["system_header"])
        if mux_params is not None
        else system_header_for_bps(mux_rate_bps)
    )

    pic_pts = [base_pts + ticks_for_frame(f, frame_rate) for f in display_frames]
    pic_dts = [
        base_pts + ticks_for_frame(i - 1, frame_rate) if p.picture_type in (1, 2) else None
        for i, p in enumerate(pictures)
    ]

    out = bytearray()
    video_pos = 0
    audio_pos = 0
    pic_i = 0
    pack_i = 0
    global_slot = 0
    video_packets = 0
    timed_video_packets = 0
    audio_packets = 0
    padding_packets = 0
    partial_packets = 0
    first_video_done = False
    audio_slot_positions: list[int] = []
    audio_burst_remaining = 0
    slot_ticks = (4096 * 8 * 90000) / mux_rate_bps

    while True:
        pack = bytearray(
            build_pack_header(pack_i, mux_rate_bps=mux_rate_bps, pack_rate_tail=pack_rate_tail)
        )
        if pack_i == 0:
            pack.extend(system_header)

        for slot_in_pack in range(4):
            if pack_i == 0 and slot_in_pack == 3:
                slot_size = 4067
            elif slot_in_pack == 3:
                slot_size = 4082
            else:
                slot_size = 4096

            if video_pos >= len(video) and audio_pos >= len(audio_stream):
                packet = build_padding_packet(slot_size)
                padding_packets += 1
                pack.extend(packet)
                global_slot += 1
                continue

            choose_audio = False
            starting_audio_burst = False
            if audio_pos < len(audio_stream):
                if audio_burst_remaining > 0:
                    choose_audio = True
                else:
                    audio_pts = audio_pts_for_offset(audio_pos, base_pts)
                    target_origin = (
                        base_pts
                        if audio_target_lead_ms is None
                        else int(round(audio_target_lead_ms * 90.0))
                    )
                    ideal_slot = max(
                        1,
                        math.ceil(((audio_pts - target_origin) / slot_ticks) - 1e-12)
                        - audio_prefetch_slots,
                    )
                    if global_slot > 0 and global_slot >= ideal_slot:
                        choose_audio = True
                        starting_audio_burst = True
            if global_slot == 0 and video_pos < len(video):
                choose_audio = False
                starting_audio_burst = False

            if not choose_audio and video_pos >= len(video):
                packet = build_padding_packet(slot_size)
                padding_packets += 1
                pack.extend(packet)
                global_slot += 1
                continue

            if choose_audio:
                header_size = 19
                payload_capacity = slot_size - header_size
                if payload_capacity < 5:
                    raise ValueError("slot too small for private-stream audio")
                data_capacity = payload_capacity - 4
                take = min(data_capacity, len(audio_stream) - audio_pos)
                pts = audio_pts_for_offset(audio_pos, base_pts)
                payload = b"\xff\xa0\x00\x00" + audio_stream[audio_pos : audio_pos + take]
                packet = build_pes(0xBD, payload, pts=pts)
                audio_pos += take
                audio_packets += 1
                audio_slot_positions.append(global_slot)
                if starting_audio_burst:
                    audio_burst_remaining = audio_burst_packets - 1
                elif audio_burst_remaining > 0:
                    audio_burst_remaining -= 1
            else:
                first_video = not first_video_done
                header_size = 22 if first_video else 19
                payload_capacity = slot_size - header_size
                take = min(payload_capacity, len(video) - video_pos)
                end = video_pos + take

                while pic_i < len(pictures) and pictures[pic_i].offset < video_pos:
                    pic_i += 1
                timed_pic = pic_i if pic_i < len(pictures) and pictures[pic_i].offset < end else None
                if timed_pic is not None:
                    pts = pic_pts[timed_pic]
                    dts = pic_dts[timed_pic]
                else:
                    pts = dts = None

                packet = build_pes(
                    0xE0,
                    video[video_pos:end],
                    pts=pts,
                    dts=dts,
                    first_video=first_video,
                )
                video_pos = end
                video_packets += 1
                if timed_pic is not None:
                    timed_video_packets += 1
                if first_video:
                    first_video_done = True

            if len(packet) > slot_size:
                raise AssertionError(f"packet overflow: {len(packet)} > {slot_size}")
            if len(packet) < slot_size:
                remainder = slot_size - len(packet)
                if remainder < 6:
                    rollback = 6 - remainder
                    if choose_audio:
                        audio_pos -= rollback
                        payload = payload[:-rollback]
                        packet = build_pes(0xBD, payload, pts=pts)
                    else:
                        old_video_pos = video_pos
                        video_pos -= rollback
                        packet = build_pes(
                            0xE0,
                            video[old_video_pos - take : video_pos],
                            pts=pts,
                            dts=dts,
                            first_video=first_video,
                        )
                    remainder = slot_size - len(packet)
                packet += build_padding_packet(remainder)
                padding_packets += 1
                partial_packets += 1

            if len(packet) != slot_size:
                raise AssertionError(f"slot size changed: {len(packet)} != {slot_size}")
            pack.extend(packet)
            global_slot += 1

        if len(pack) != PACK_SIZE:
            raise AssertionError(f"pack size mismatch: {len(pack)}")
        out.extend(pack)
        pack_i += 1
        if video_pos >= len(video) and audio_pos >= len(audio_stream):
            break

    out.extend(b"\x00\x00\x01\xb9")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(out)

    parsed_data, parsed_packets = parse_program_stream(output)
    rebuilt_video = bytearray()
    rebuilt_audio = bytearray()
    pack_offsets: list[int] = []
    audio_payload_packets = 0
    for p in parsed_packets:
        if p.stream_id == 0xBA:
            pack_offsets.append(p.offset)
        elif p.stream_id == 0xE0:
            rebuilt_video.extend(parsed_data[p.payload_offset : p.offset + p.size])
        elif p.stream_id == 0xBD:
            payload = parsed_data[p.payload_offset : p.offset + p.size]
            if not payload.startswith(b"\xff\xa0\x00\x00"):
                raise ValueError(f"unexpected private audio prefix at 0x{p.offset:x}")
            rebuilt_audio.extend(payload[4:])
            audio_payload_packets += 1
    if bytes(rebuilt_video) != video:
        raise ValueError(f"video readback mismatch: {len(rebuilt_video)} != {len(video)}")
    if bytes(rebuilt_audio) != audio_stream:
        raise ValueError(f"audio stream readback mismatch: {len(rebuilt_audio)} != {len(audio_stream)}")
    if any(off != i * PACK_SIZE for i, off in enumerate(pack_offsets)):
        raise ValueError("pack headers are not aligned to 16 KiB boundaries")

    report: dict[str, object] = {
        "schema": "galaxy-angel-pssplex-compatible-mux/v1",
        "m2v": str(m2v.resolve()),
        "wav": str(wav.resolve()),
        "output": str(output.resolve()),
        "mux_rate_bps": mux_rate_bps,
        "base_pts": base_pts,
        "audio_prefetch_slots": audio_prefetch_slots,
        "audio_burst_packets": audio_burst_packets,
        "audio_target_lead_ms": audio_target_lead_ms,
        "mux_template_pss": str(mux_template_pss.resolve()) if mux_template_pss is not None else None,
        "video": {
            "bytes": len(video),
            "sha256": sha256_bytes(video),
            "pictures": len(pictures),
            "frame_rate": f"{frame_rate.numerator}/{frame_rate.denominator}",
            "i": sum(p.picture_type == 1 for p in pictures),
            "p": sum(p.picture_type == 2 for p in pictures),
            "b": sum(p.picture_type == 3 for p in pictures),
            "pes_packets": video_packets,
            "timed_pes_packets": timed_video_packets,
        },
        "audio": {
            **wav_info,
            "wav_pcm_bytes": len(wav_raw),
            "wav_pcm_sha256": sha256_bytes(wav_raw),
            "ss_stream_bytes": len(audio_stream),
            "ss_stream_sha256": sha256_bytes(audio_stream),
            "pes_packets": audio_packets,
            "first_audio_slots": audio_slot_positions[:40],
        },
        "pss": {
            "bytes": len(out),
            "sha256": sha256_bytes(out),
            "packs": pack_i,
            "pack_alignment": PACK_SIZE,
            "padding_packets_added": padding_packets,
            "partial_slots": partial_packets,
            "stream_end": True,
        },
        "verification": {
            "video_readback_exact": True,
            "audio_ss_stream_readback_exact": True,
            "pack_alignment_exact": True,
            "parsed_audio_packets": audio_payload_packets,
            "mux_template_used": mux_params is not None,
            "template_pack_count": int(mux_params["template_pack_count"]) if mux_params is not None else None,
        },
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Mux MPEG-2 + original PCM WAV into a PSS Plex-compatible PS2 PSS stream.")
    ap.add_argument("--m2v", type=Path, required=True)
    ap.add_argument("--wav", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--report", type=Path)
    ap.add_argument("--mux-template-pss", type=Path,
                    help="Keep the normal PSS Plex algorithm but use the original movie's mux-rate/rate-bound")
    ap.add_argument("--audio-prefetch-slots", type=int, default=0,
                    help="Schedule audio this many 4 KiB slots earlier to keep a small decoder prebuffer")
    ap.add_argument("--audio-burst-packets", type=int, default=1,
                    help="Emit this many private-audio PES packets consecutively when an audio burst becomes due")
    ap.add_argument("--base-pts", type=int,
                    help="Override the common starting video/audio PTS (90 kHz ticks)")
    ap.add_argument("--audio-target-lead-ms", type=float,
                    help="Target how far audio PTS should lead pack SCR before burst packing")
    args = ap.parse_args()

    report = mux(
        args.m2v,
        args.wav,
        args.output,
        mux_template_pss=args.mux_template_pss,
        audio_prefetch_slots=args.audio_prefetch_slots,
        audio_burst_packets=args.audio_burst_packets,
        base_pts_override=args.base_pts,
        audio_target_lead_ms=args.audio_target_lead_ms,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
