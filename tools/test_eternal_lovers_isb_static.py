import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import eternal_lovers_isb_static as isb


def add_block(data, key, texts, setter=False):
    data.extend(isb.KEY_SETTER_PREFIX if setter else isb.INTEGER_TAG * 2)
    key_offset = len(data)
    data.extend(struct.pack("<III", key, 1, 0x180000))
    data.extend(bytes.fromhex("084368c5") if setter else isb.TITLE_RECORD_TOKEN)
    records = []
    for text in texts:
        raw = text.encode("cp932")
        offset = len(data)
        data.extend(isb.STRING_TAG + struct.pack("<I", len(raw)))
        data.extend(isb.encode_isb_payload(raw, key))
        records.append(SimpleNamespace(offset=offset, length=len(raw), before_hex=""))
    return key_offset, records


class ShortKeyBlocks(unittest.TestCase):
    def check_block(self, texts, setter=False):
        data = bytearray(struct.pack("<I", 0x12345678))
        _key_offset, short = add_block(data, 0x90013cd6, texts, setter=setter)
        _other_key, following = add_block(data, 0xc36f7cd7, ["確認してください。"] * 14)
        records = short + following
        raw = bytes(data)
        with patch.object(isb, "recover_short_key_blocks", side_effect=lambda _d, _r, spans, _k: spans):
            baseline = isb.find_key_spans_adaptive(raw, records)
        spans = isb.find_key_spans_adaptive(raw, records)
        before = isb.decode_records_with_spans(raw, records, baseline)
        after = isb.decode_records_with_spans(raw, records, spans)
        self.assertEqual([row["text"] for row in after[:len(short)]], texts)
        self.assertEqual(after[len(short):], before[len(short):])
        self.assertEqual(len({row["offset"] for row in after}), len(records))

    def test_two_record_block_before_larger_block(self):
        self.check_block(["保存開始", "よしっ、やるぞっ！"])

    def test_single_record_block(self):
        self.check_block(["保存開始"])

    def test_three_record_block(self):
        self.check_block(["保存開始", "確認してください。", "了解しました。"])

    def test_key_setter_without_title(self):
        self.check_block(["…………………………。", "確認……。"], setter=True)


if __name__ == "__main__":
    unittest.main()
