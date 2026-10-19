"""Regression checks for stable custom-glyph allocations in existing saves."""
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class FontCompatibilityTests(unittest.TestCase):
    def test_append_only_and_validation(self):
        found = 0
        for name, schema in (("galaxy_angel_font", "galaxy-angel-font-compat/v1"),
                             ("eternal_lovers_font", "eternal-lovers-font-compat/v1"),
                             ("moonlit_lovers_font", "moonlit-lovers-font-compat/v1")):
            try:
                module = importlib.import_module(name)
            except ModuleNotFoundError as exc:
                if exc.name != name:
                    raise
                continue
            if not hasattr(module, "ordered_custom_chars"):
                continue  # Older sibling helpers are not this project's font builder.
            found += 1
            with self.subTest(module=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "compat.json"
                payload = {"schema": schema, "character_count": 2, "characters": "힣나"}
                path.write_text(json.dumps(payload), encoding="utf-8")
                def ordered():
                    if name == "galaxy_angel_font":
                        actual = module.ordered_custom_chars(Path(directory), None, path)
                    else:
                        actual = module.ordered_custom_chars([], path)
                        if name == "moonlit_lovers_font":
                            actual = actual[0]
                    return actual
                with patch.object(module, "collect_custom_chars", return_value=["힣"]):
                    self.assertEqual(ordered(), ["힣", "나"])
                with patch.object(module, "collect_custom_chars", return_value=["가", "힣"]):
                    with self.assertRaises(SystemExit):
                        ordered()
                    payload.update(characters="힣나가", character_count=3)
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    self.assertEqual(ordered(), ["힣", "나", "가"])
                payload["character_count"] = 2
                for invalid in ("힣힣", "힣A"):
                    payload["characters"] = invalid
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(SystemExit):
                        module.load_compatibility_chars(path)
                payload["characters"] = "힣"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(SystemExit):
                    module.load_compatibility_chars(path)
        self.assertGreater(found, 0)


if __name__ == "__main__":
    unittest.main()
