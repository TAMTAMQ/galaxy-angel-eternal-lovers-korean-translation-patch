"""Galaxy Angel IkusaLib XOR-0x72 LZSS codec."""

import struct
from collections import defaultdict, deque

MAGIC = 0x313B3320
KEY = 0x72
RING_SIZE = 4096
RING_START = RING_SIZE - 18
# Increment whenever the emitted byte stream can change. Persistent compression
# caches include this tag so a codec fix cannot silently reuse older streams.
COMPRESSOR_VERSION = 2


def _best_matches(raw):
    """Return the longest legal LZSS match at every output position.

    Ring contents are exactly the already-decoded prefix, so candidate positions
    can be indexed by the next three raw bytes instead of rescanning all 4096
    ring entries for every byte.  This matters for large RGBA textures, where
    the old O(n*4096) matcher took minutes per asset.
    """
    size = len(raw)
    matches = [(0, 0)] * size
    by_prefix = defaultdict(deque)

    for pos in range(size):
        limit = min(18, size - pos)
        best_length = 0
        best_position = 0
        if limit >= 3:
            key = bytes(raw[pos : pos + 3])
            candidates = by_prefix[key]
            minimum = pos - RING_SIZE
            while candidates and candidates[0] < minimum:
                candidates.popleft()

            # Newest matches tend to overlap and quickly reach the 18-byte
            # maximum, so scan backwards and stop as soon as that happens.
            for source in reversed(candidates):
                distance = pos - source
                length = 0
                while length < limit:
                    if length < distance:
                        value = raw[source + length]
                    else:
                        value = raw[pos + (length % distance)]
                    if value != raw[pos + length]:
                        break
                    length += 1
                if length > best_length:
                    best_length = length
                    best_position = (RING_START + source) % RING_SIZE
                    if length == limit:
                        break

            # The game's decoder follows the classic LZSS layout: bytes before
            # RING_START are initialized to zero, while RING_START..4095 are the
            # initial write tail and must not be read before they have actually
            # been written.  The previous encoder incorrectly pointed initial
            # zero runs at (RING_START + pos), which can be the current write
            # position itself.  Python round-trips hid that bug because our
            # decoder zero-initializes the whole bytearray, but the PS2 runtime
            # then copied stale ring-tail bytes into TEX headers.  Match the
            # original encoder and source an initial zero run from the known
            # initialized prefix immediately before RING_START instead.
            if best_length < limit and pos < RING_SIZE and raw[pos] == 0:
                zero_length = 0
                while zero_length < limit and raw[pos + zero_length] == 0:
                    zero_length += 1
                if zero_length >= 3 and zero_length > best_length:
                    best_length = zero_length
                    best_position = RING_START - zero_length

            matches[pos] = (best_length, best_position)
            candidates.append(pos)
        elif pos + 3 <= size:
            by_prefix[bytes(raw[pos : pos + 3])].append(pos)

    return matches


def validate_runtime_compatible(data, offset=0):
    """Reject streams that read the decoder's initial write tail too early.

    The PS2 decoder initializes only ring[0:RING_START]. The final 18 bytes are
    the initial write window and contain unspecified data until output has
    actually written them. A Python bytearray masks invalid references by
    making those cells zero, so validate initialization explicitly.
    """
    magic, size = struct.unpack_from("<2I", data, offset)
    if magic != MAGIC:
        raise ValueError(f"bad Ikusa LZ magic at {offset:#x}")
    src = offset + 8
    produced = 0
    write = RING_START
    initialized = [True] * RING_START + [False] * (RING_SIZE - RING_START)
    while produced < size:
        flags = data[src] ^ KEY
        src += 1
        for bit in range(8):
            if produced >= size:
                break
            if flags >> bit & 1:
                src += 1
                initialized[write] = True
                write = (write + 1) % RING_SIZE
                produced += 1
                continue
            low, packed = data[src] ^ KEY, data[src + 1] ^ KEY
            src += 2
            position = low | ((packed & 0xF0) << 4)
            length = (packed & 0x0F) + 3
            for index in range(length):
                source = (position + index) % RING_SIZE
                if not initialized[source]:
                    raise ValueError(
                        f"runtime-unsafe Ikusa LZ reference at output {produced:#x}: "
                        f"ring {source:#x} has not been initialized"
                    )
                initialized[write] = True
                write = (write + 1) % RING_SIZE
                produced += 1
    return src - offset


def decompress(data, offset=0):
    magic, size = struct.unpack_from("<2I", data, offset)
    if magic != MAGIC:
        raise ValueError(f"bad Ikusa LZ magic at {offset:#x}")
    src = offset + 8
    out = bytearray()
    ring = bytearray(RING_SIZE)
    write = RING_START
    while len(out) < size:
        flags = data[src] ^ KEY
        src += 1
        for bit in range(8):
            if len(out) >= size:
                break
            if flags >> bit & 1:
                value = data[src] ^ KEY
                src += 1
                out.append(value)
                ring[write] = value
                write = (write + 1) % RING_SIZE
            else:
                low, packed = data[src] ^ KEY, data[src + 1] ^ KEY
                src += 2
                position = low | ((packed & 0xF0) << 4)
                length = (packed & 0x0F) + 3
                for index in range(length):
                    value = ring[(position + index) % RING_SIZE]
                    out.append(value)
                    ring[write] = value
                    write = (write + 1) % RING_SIZE
    return bytes(out), src - offset


def compress(raw):
    out = bytearray(struct.pack("<2I", MAGIC, len(raw)))
    matches = _best_matches(raw)
    pos = 0
    while pos < len(raw):
        flags = 0
        chunk = bytearray()
        for bit in range(8):
            if pos >= len(raw):
                break
            best_length, best_position = matches[pos]
            if best_length >= 3:
                packed = ((best_position >> 4) & 0xF0) | (best_length - 3)
                chunk.extend(((best_position & 0xFF) ^ KEY, packed ^ KEY))
                pos += best_length
            else:
                flags |= 1 << bit
                chunk.append(raw[pos] ^ KEY)
                pos += 1
        out.append(flags ^ KEY)
        out.extend(chunk)
    result = bytes(out)
    validate_runtime_compatible(result)
    return result


def compress_optimal(raw):
    """Compress with dynamic programming over token and flag-group costs.

    The legacy encoder greedily takes the longest match.  Runtime copies in
    SLGINIT have fixed compressed slots, where even a few bytes matter.  The
    decoded ring contents depend only on the output prefix, not on tokenization,
    so all matches can be measured first and an exact minimum-cost token path
    selected with an eight-state DP for the flag-byte position.
    """
    size = len(raw)
    matches = _best_matches(raw)

    infinity = 1 << 60
    costs = [[infinity] * 8 for _ in range(size + 1)]
    previous = [[None] * 8 for _ in range(size + 1)]
    costs[0][0] = 0
    for pos in range(size):
        best_length, best_position = matches[pos]
        for token_mod in range(8):
            current = costs[pos][token_mod]
            if current == infinity:
                continue
            flag_cost = 1 if token_mod == 0 else 0
            next_mod = (token_mod + 1) % 8
            literal_cost = current + flag_cost + 1
            if literal_cost < costs[pos + 1][next_mod]:
                costs[pos + 1][next_mod] = literal_cost
                previous[pos + 1][next_mod] = (pos, token_mod, 1, best_position)
            for length in range(3, best_length + 1):
                match_cost = current + flag_cost + 2
                if match_cost < costs[pos + length][next_mod]:
                    costs[pos + length][next_mod] = match_cost
                    previous[pos + length][next_mod] = (
                        pos, token_mod, length, best_position
                    )

    token_mod = min(range(8), key=lambda value: costs[size][value])
    tokens = []
    pos = size
    while pos:
        step = previous[pos][token_mod]
        if step is None:
            raise ValueError("optimal Ikusa LZ path reconstruction failed")
        old_pos, old_mod, length, match_position = step
        tokens.append((old_pos, length, match_position))
        pos, token_mod = old_pos, old_mod
    tokens.reverse()

    out = bytearray(struct.pack("<2I", MAGIC, size))
    for group_start in range(0, len(tokens), 8):
        group = tokens[group_start : group_start + 8]
        flags = 0
        chunk = bytearray()
        for bit, (position, length, match_position) in enumerate(group):
            if length == 1:
                flags |= 1 << bit
                chunk.append(raw[position] ^ KEY)
            else:
                packed = ((match_position >> 4) & 0xF0) | (length - 3)
                chunk.extend(((match_position & 0xFF) ^ KEY, packed ^ KEY))
        out.append(flags ^ KEY)
        out.extend(chunk)
    result = bytes(out)
    validate_runtime_compatible(result)
    return result
