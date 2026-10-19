"""Keep the save-menu's 24-byte title preview on an engine SJIS boundary."""
from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import struct
from pathlib import Path

GAMES = {
    "base": ("SLPM_652.54", 0x2C0A18, (0x25FF60, 0x2602DC)),
    "moonlit": ("SLPM_654.29", 0x27B0A8, (0x20A970, 0x20AD6C)),
    "eternal": ("SLPM_658.78", 0x2A7178, (0x214328, 0x21473C)),
}
CLIP_LOADS = {"moonlit": 0x1107C8, "eternal": 0x139950}


def patch_clip_bytes(data, game):
    changed = bytearray(data)
    rows = []
    if game in CLIP_LOADS:
        address = CLIP_LOADS[game]
        loads, _ = elf_layout(data)
        offset = file_offset(loads, address, 36)
        if struct.unpack_from("<I", data, offset)[0] != 0x80440000:
            raise ValueError(f"unexpected title split byte load at {address:#x}")
        expected_loop = bytes.fromhex("010003260b806200010010261800022e")
        if data[offset+12:offset+28] != expected_loop:
            raise ValueError("unexpected title split character loop")
        struct.pack_into("<I", changed, offset, 0x90440000)
        rows.append({"address": address, "offset": offset,
                     "old": "00004480", "new": "00004490"})
    return bytes(changed), rows


def elf_layout(data):
    if data[:6] != b"\x7fELF\x01\x01" or struct.unpack_from("<H", data, 18)[0] != 8:
        raise ValueError("expected a little-endian ELF32 MIPS executable")
    phoff, shoff = struct.unpack_from("<II", data, 28)
    phsize, phcount, shsize, shcount = struct.unpack_from("<HHHH", data, 42)
    loads = [struct.unpack_from("<8I", data, phoff + i * phsize)
             for i in range(phcount)]
    loads = [p for p in loads if p[0] == 1]
    sections = [struct.unpack_from("<10I", data, shoff + i * shsize)
                for i in range(shcount)]
    return loads, sections


def file_offset(loads, address, size=4):
    matches = [p[1] + address - p[2] for p in loads
               if p[2] <= address and address + size <= p[2] + p[4]]
    if len(matches) != 1:
        raise ValueError(f"address {address:#x} is not in exactly one file-backed segment")
    return matches[0]


def padding_gaps(data):
    loads, sections = elf_layout(data)
    result = []
    for p in loads:
        used = sorted((s[4], s[4] + s[5]) for s in sections
                      if s[2] & 2 and s[1] != 8 and s[5]
                      and p[1] <= s[4] and s[4] + s[5] <= p[1] + p[4])
        for left, right in zip(used, used[1:]):
            start = (left[1] + 3) & ~3
            if right[0] - start >= 92 and not any(data[start:right[0]]):
                result.append((p[2] + start - p[1], start, right[0] - start))
    return result


def jump(target, link=False):
    return ((3 if link else 2) << 26) | (target >> 2)


def wrapper(original_copy):
    # Tail-call the original copier, after reducing the byte count to a whole
    # token boundary and writing an early terminator (the caller only writes +24).
    def r(rs, rt, rd, fn):
        return rs << 21 | rt << 16 | rd << 11 | fn
    def i(op, rs, rt, imm):
        return op << 26 | rs << 21 | rt << 16 | (imm & 0xFFFF)
    words = [
        r(5, 0, 8, 0x21), r(5, 6, 9, 0x21),
        i(4, 8, 9, 15), r(8, 0, 11, 0x21),
        i(0x24, 8, 10, 0), i(4, 10, 0, 12), i(0x0B, 10, 12, 0x80),
        i(5, 12, 0, 5), i(9, 10, 10, -0xA0), i(0x0B, 10, 12, 0x40),
        i(5, 12, 0, 2), 0, i(9, 8, 8, 1), i(9, 8, 8, 1),
        r(9, 8, 12, 0x2B), i(4, 12, 0, -14), 0,
        r(11, 0, 8, 0x21), r(8, 5, 6, 0x23), r(4, 6, 8, 0x21),
        i(0x28, 8, 0, 0), jump(original_copy), 0,
    ]
    return struct.pack("<23I", *words)


def patch_bytes(data, game):
    executable, original, calls = GAMES[game]
    loads, _ = elf_layout(data)
    changed = bytearray(data)
    segment_extension = None
    gaps = padding_gaps(data)
    if not gaps and game == "moonlit":
        # This executable keeps its alignment padding just outside PT_LOAD.
        # Include 92 already-zero bytes without moving either segment or section.
        first, second = loads
        end = first[1] + first[4]
        if first[4] != first[5] or second[1] - end < 92 or any(data[end:end+92]):
            raise ValueError("unexpected Moonlit segment alignment padding")
        _, sections = elf_layout(data)
        if any(s[1] != 8 and s[5] and s[4] < end + 92 and end < s[4] + s[5]
               for s in sections):
            raise ValueError("Moonlit padding overlaps a section")
        phoff = struct.unpack_from("<I", data, 28)[0]
        phsize, phcount = struct.unpack_from("<HH", data, 42)
        headers = [phoff + n * phsize for n in range(phcount)
                   if struct.unpack_from("<8I", data, phoff + n * phsize) == first]
        if len(headers) != 1:
            raise ValueError("Moonlit load segment resolution failed")
        struct.pack_into("<II", changed, headers[0] + 16, first[4] + 92, first[5] + 92)
        segment_extension = {"header_offset": headers[0], "added_bytes": 92}
        gaps = [(first[2] + first[4], end, 92)]
    # Deterministic ELF section-alignment padding, never a resource or BSS slot.
    if not gaps:
        raise ValueError("no zero-filled loaded section-alignment padding available")
    cave, cave_offset, capacity = gaps[0]
    code = wrapper(original)
    rows = []
    for call in calls:
        offset = file_offset(loads, call, 12)
        expected = (jump(original, True), 0x24060018)
        if struct.unpack_from("<II", data, offset) != expected:
            raise ValueError(f"unexpected save-title call/delay slot at {call:#x}")
        terminator = struct.unpack_from("<I", data, offset + 8)[0]
        if terminator & 0xFC1FFFFF != 0xA0000080:
            raise ValueError(f"unexpected save-title terminator at {call + 8:#x}")
        struct.pack_into("<I", changed, offset, jump(cave, True))
        rows.append({"address": call, "offset": offset, "old": data[offset:offset+4].hex(),
                     "new": bytes(changed[offset:offset+4]).hex()})
    changed[cave_offset:cave_offset + len(code)] = code
    final, clip_rows = patch_clip_bytes(bytes(changed), game)
    return final, {"executable": executable, "cave_address": cave,
                           "cave_offset": cave_offset, "capacity": capacity,
                           "code_hex": code.hex(), "calls": rows,
                           "segment_extension": segment_extension,
                           "unsigned_split_loads": clip_rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", choices=GAMES, required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--elf", type=Path)
    target.add_argument("--iso", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--clip-only", action="store_true",
                        help="Apply the split-load correction to an already boundary-patched ISO")
    args = parser.parse_args()
    path = args.elf or args.iso
    def patch_target(before):
        if args.clip_only:
            after, rows = patch_clip_bytes(before, args.game)
            return after, {"unsigned_split_loads": rows}
        return patch_bytes(before, args.game)
    if args.elf:
        before = path.read_bytes()
        after, result = patch_target(before)
        path.write_bytes(after)
        assert path.read_bytes() == after
    else:
        import galaxy_angel_build as builder
        with path.open("r+b") as stream, mmap.mmap(stream.fileno(), 0) as image:
            matches = [f for name, f in builder.iso_files(image).items()
                       if name.rsplit("/", 1)[-1] == GAMES[args.game][0]]
            if len(matches) != 1:
                raise ValueError("executable resolution failed")
            entry = matches[0]
            start = entry.extent * builder.SECTOR
            before = bytes(image[start:start+entry.size])
            after, result = patch_target(before)
            image[start:start+entry.size] = after
            image.flush()
        with path.open("rb") as stream:
            stream.seek(start)
            if stream.read(len(after)) != after:
                raise ValueError("ISO executable readback mismatch")
    result.update(schema="series-save-title-boundary/v1", game=args.game,
                  target=str(path.resolve()),
                  before_sha256=hashlib.sha256(before).hexdigest(),
                  after_sha256=hashlib.sha256(after).hexdigest())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
