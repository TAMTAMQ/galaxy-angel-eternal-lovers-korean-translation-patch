"""Run the actual MIPS wrapper in Unicorn, including branch delay slots.

Test dependency: pip install unicorn (2.1.4 used for the regression audit).
"""
import random
import struct
import unittest

from unicorn import Uc, UC_ARCH_MIPS, UC_MODE_MIPS32, UC_MODE_LITTLE_ENDIAN
from unicorn.mips_const import (
    UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_RA,
    UC_MIPS_REG_S0, UC_MIPS_REG_S1, UC_MIPS_REG_S3, UC_MIPS_REG_PC,
)

from series_save_title_boundary import GAMES, wrapper


class BoundaryTests(unittest.TestCase):
    def test_real_title_split_loop(self):
        # Actual expansion-game loop and byte predicate. Only LB becomes LBU;
        # keep the original predicate and its other callers unchanged.
        predicate = bytearray.fromhex("80ff82242000422c03004014e000842c020080142d100000010002240800e0030000000000000000")
        loop = bytearray.fromhex("0000628e2d200000f0ff438c020003122110500000004480e004040c00000000010003260b806200010010261800022ef4ff40540000628e")
        struct.pack_into("<I", loop, 20, 0x90440000)
        struct.pack_into("<I", loop, 24, (3 << 26) | (0x101380 >> 2))
        # Unicorn's MIPS32 backend lacks EE DADDU. These two zero-register
        # moves are equivalent ADDU instructions; no tested arithmetic changes.
        struct.pack_into("<I", loop, 4, 0x00002021)
        struct.pack_into("<I", predicate, 20, 0x00001021)
        cpu = Uc(UC_ARCH_MIPS, UC_MODE_MIPS32 | UC_MODE_LITTLE_ENDIAN)
        cpu.mem_map(0x100000, 0x100000)
        cpu.mem_map(0x400000, 0x3000)
        cpu.mem_write(0x101380, bytes(predicate))
        cpu.mem_write(0x110000, bytes(loop))
        rng = random.Random(20261019)
        tokens = [b"A", b" ", b"\xa0", b"\x81\x40", b"\xe5\xe5", b"\xb0", b"\xdf"]
        samples = [[b"A"] * 23 + [b"\x81\x40"] * 8,
                   [b"\xe5\xe5"] * 8 + [b"\xa0"] + [b"\xe5\xe5"] * 10]
        samples += [[rng.choice(tokens) for _ in range(40)] for _ in range(2000)]
        for sample in samples:
            raw = b"".join(sample)
            expected = 0
            for token in sample:
                expected += len(token)
                if expected >= 24:
                    break
            # The std::string length lives 16 bytes before its data pointer.
            cpu.mem_write(0x400FF0, struct.pack("<I", len(raw)))
            cpu.mem_write(0x401000, raw + b"\0")
            cpu.mem_write(0x402000, struct.pack("<I", 0x401000))
            cpu.reg_write(UC_MIPS_REG_S0, 0)
            cpu.reg_write(UC_MIPS_REG_S3, 0x402000)
            cpu.emu_start(0x110000, 0x110000 + len(loop), count=1000)
            self.assertEqual(cpu.reg_read(UC_MIPS_REG_PC), 0x110000 + len(loop))
            self.assertEqual(cpu.reg_read(UC_MIPS_REG_S0), expected)

    def test_native_code_all_games(self):
        rng = random.Random(20261019)
        alphabet = [bytes([x]) for x in range(1, 128)]
        alphabet += [bytes([x]) for x in range(0xA0, 0xE0)]
        alphabet += [bytes([lead, trail]) for lead in (*range(0x81, 0xA0), *range(0xE0, 0xFD))
                     for trail in (0x40, 0x7E, 0x80, 0xA0, 0xFC)]
        sequences = [[b"A"] * 23 + [b"\x81\x40"],
                     [b"\xe5\xe5"] * 12, [b"\xa0"] * 30, [],
                     [b"A"] * 24, [b"A"] * 22 + [b"\xe5\xe5"]]
        sequences += [[rng.choice(alphabet) for _ in range(rng.randrange(40))]
                      for _ in range(500)]
        for game, (_, original, _) in GAMES.items():
            cpu = Uc(UC_ARCH_MIPS, UC_MODE_MIPS32 | UC_MODE_LITTLE_ENDIAN)
            cpu.mem_map(0x100000, 0x200000)
            cpu.mem_map(0x400000, 0x3000)
            cpu.mem_write(0x100000, wrapper(original))
            for tokens in sequences:
                source = b"".join(tokens) + b"\0" + b"Q" * 100
                for cap in range(25):
                    expected = 0
                    for token in tokens:
                        if expected + len(token) > cap:
                            break
                        expected += len(token)
                    cpu.mem_write(0x400000, source)
                    cpu.mem_write(0x401000, b"Z" * 128)
                    for reg, value in ((UC_MIPS_REG_A0, 0x401000),
                                       (UC_MIPS_REG_A1, 0x400000),
                                       (UC_MIPS_REG_A2, cap),
                                       (UC_MIPS_REG_RA, 0x123456),
                                       (UC_MIPS_REG_S0, 0x987654),
                                       (UC_MIPS_REG_S1, 0x112233)):
                        cpu.reg_write(reg, value)
                    cpu.emu_start(0x100000, original, count=1000)
                    self.assertEqual(cpu.reg_read(UC_MIPS_REG_PC), original)
                    self.assertEqual(cpu.reg_read(UC_MIPS_REG_A2), expected, (game, cap, tokens))
                    self.assertEqual(cpu.reg_read(UC_MIPS_REG_A0), 0x401000)
                    self.assertEqual(cpu.reg_read(UC_MIPS_REG_A1), 0x400000)
                    self.assertEqual(cpu.reg_read(UC_MIPS_REG_RA), 0x123456)
                    self.assertEqual(cpu.reg_read(UC_MIPS_REG_S0), 0x987654)
                    self.assertEqual(cpu.reg_read(UC_MIPS_REG_S1), 0x112233)
                    self.assertEqual(bytes(cpu.mem_read(0x401000, 128)),
                                     b"Z" * expected + b"\0" + b"Z" * (127 - expected))
                    self.assertEqual(bytes(cpu.mem_read(0x400000, len(source))), source)


if __name__ == "__main__":
    unittest.main()
