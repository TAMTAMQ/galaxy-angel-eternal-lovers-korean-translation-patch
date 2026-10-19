import unittest
from types import SimpleNamespace
from unittest.mock import patch

import eternal_lovers_patch_battle_bank_images as bank


class NativeRendering(unittest.TestCase):
    def test_tight_slot_does_not_reduce_palette(self):
        with patch.object(bank, "tex_info", return_value=SimpleNamespace(palette_colours=16)), \
                patch.object(bank, "encode_tex", return_value=b"native-tex") as encode, \
                patch.object(bank, "encode_resource", return_value=b"12345678"), \
                patch.object(bank.ikusa_lz, "compress_optimal", return_value=b"123456"):
            with self.assertRaises(ValueError):
                bank.compress_to_fit(b"original", object(), 4, "ikusa_lz", allow_palette_reduction=False)
            self.assertEqual([call.args[2] for call in encode.call_args_list], [None])

    def test_roomy_bank_uses_native_palette(self):
        with patch.object(bank, "tex_info", return_value=SimpleNamespace(palette_colours=16)), \
                patch.object(bank, "encode_tex", return_value=b"native-tex") as encode, \
                patch.object(bank, "encode_resource", return_value=b"12345678"):
            raw, stream, colours = bank.compress_to_fit(b"original", object(), 32, "ikusa_lz", allow_palette_reduction=False)
            self.assertEqual((raw, stream, colours), (b"native-tex", b"12345678", None))
            self.assertEqual(encode.call_count, 1)


if __name__ == "__main__":
    unittest.main()
