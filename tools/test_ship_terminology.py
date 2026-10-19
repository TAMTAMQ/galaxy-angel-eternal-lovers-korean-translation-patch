import json
import tempfile
import unittest
from pathlib import Path

from series_issue_audit import SHIP_NAME_VARIANT, ship_name_violations


class ShipTerminologyTests(unittest.TestCase):
    def test_canonical_term_and_wrapped_variant(self):
        self.assertIsNone(SHIP_NAME_VARIANT.search("쿵후 파이터"))
        self.assertIsNotNone(SHIP_NAME_VARIANT.search("쿵푸\n파이터"))

    def test_image_glossary_and_movie_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = [
                "assets/image_extraction/SLG/japanese_images/manifest.json",
                "assets/translation/images/candidate_translations.json",
                "assets/translation/glossary.tsv",
                "movie/subtitles/example.ass",
            ]
            for relative in files:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                value = json.dumps({"translation": "쿵푸\n파이터"}, ensure_ascii=False) if path.suffix == ".json" else "쿵푸 파이터"
                path.write_text(value, encoding="utf-8")
            self.assertEqual(len(ship_name_violations(root)), len(files))
            for relative in files:
                path = root / relative
                value = json.dumps({"translation": "쿵후 파이터"}, ensure_ascii=False) if path.suffix == ".json" else "쿵후 파이터"
                path.write_text(value, encoding="utf-8")
            self.assertEqual(ship_name_violations(root), [])


if __name__ == "__main__":
    unittest.main()
