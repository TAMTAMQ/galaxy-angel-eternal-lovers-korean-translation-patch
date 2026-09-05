from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ETERNAL = ROOT / "work" / "galaxy_angel_eternal_lovers"
IMAGE_ROOT = ETERNAL / "assets" / "image_extraction"
OUT_ROOT = IMAGE_ROOT / "japanese_images"

REFERENCES = [
    (
        "galaxy_angel",
        ROOT / "work" / "galaxy_angel" / "assets" / "image_extraction" / "japanese_images",
    ),
    (
        "moonlit_lovers",
        ROOT / "work" / "galaxy_angel_moonlit_lovers" / "assets" / "image_extraction" / "GADAT032" / "japanese_images",
    ),
]


# Ship-interior block buttons carry only Latin lettering ("A BLOCK"), so they are not a
# translation target at all; the reference project reached the same conclusion and dropped
# them.  Importing a render for them stamps Korean over the original artwork.
EXCLUDED_PNG = {
    "block_0167d800.png",
    "block_0167e800.png",
    "block_0167f800.png",
    "block_01680800.png",
    "block_01681800.png",
    "block_01682800.png",
    "block_01683800.png",
    "block_01684800.png",
    "block_01685800.png",
    "block_01686800.png",
    "block_01687800.png",
    "block_01688800.png",
}


def pixel_hash(path: Path) -> str:
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        digest = hashlib.sha256()
        digest.update(f"{rgba.width}x{rgba.height}:RGBA".encode("ascii"))
        digest.update(rgba.tobytes())
        return digest.hexdigest()


def paired_reference_images(root: Path):
    original_root = root / "png"
    translated_root = root / "translated_png"
    for original in sorted(original_root.rglob("*.png")):
        relative = original.relative_to(original_root)
        translated = translated_root / relative
        if not translated.is_file():
            # Legacy GA1 review directories are mostly flat. Fall back to an
            # unambiguous basename match, but never guess when duplicates exist.
            matches = list(translated_root.rglob(original.name))
            if len(matches) != 1:
                continue
            translated = matches[0]
        yield original, translated


def build_reference_map():
    refs: dict[str, list[dict]] = {}
    for label, root in REFERENCES:
        if not root.is_dir():
            continue
        for original, translated in paired_reference_images(root):
            refs.setdefault(pixel_hash(original), []).append(
                {
                    "reference": label,
                    "original": original,
                    "translated": translated,
                }
            )
    return refs


def existing_entries(container: str) -> list[dict]:
    manifest = OUT_ROOT / container / "manifest.json"
    if not manifest.is_file():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def main() -> None:
    refs = build_reference_map()
    totals: dict[str, dict] = {}

    for container in ("GADAT030", "GADAT031", "GADAT032"):
        source_root = IMAGE_ROOT / container / "png"
        review_root = OUT_ROOT / container
        original_root = review_root / "png"
        translated_root = review_root / "translated_png"
        original_root.mkdir(parents=True, exist_ok=True)
        translated_root.mkdir(parents=True, exist_ok=True)

        existing = existing_entries(container)
        entries_by_png: dict[str, dict] = {}
        for entry in existing:
            name = entry.get("png") or entry.get("source_png")
            if isinstance(name, str):
                entries_by_png[name] = entry

        matched = 0
        added = 0
        conflicts = 0
        for source in sorted(source_root.rglob("*.png")):
            h = pixel_hash(source)
            candidates = refs.get(h, [])
            if not candidates:
                continue
            matched += 1

            # All candidates sharing a source pixel hash should also share the
            # same translated pixels. If not, keep the source untouched and
            # report the ambiguity instead of selecting arbitrarily.
            translated_hashes = {pixel_hash(c["translated"]) for c in candidates}
            if len(translated_hashes) != 1:
                conflicts += 1
                continue

            relative = source.relative_to(source_root)
            key_check = relative.as_posix()
            if key_check in EXCLUDED_PNG:
                continue
            existing_entry = entries_by_png.get(key_check)
            if existing_entry is not None and existing_entry.get("classification") not in (None, "shared-pixel-match"):
                # This texture has been re-rendered for Eternal Lovers because the imported
                # render was wrong for it (destroyed artwork, a caption from the other game, a
                # half-erased kana).  Never overwrite that with the reference render again.
                continue
            # The legacy extraction is flat today, but retain relative paths so
            # the helper remains safe if later re-extractions introduce folders.
            out_original = original_root / relative
            out_translated = translated_root / relative
            out_original.parent.mkdir(parents=True, exist_ok=True)
            out_translated.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, out_original)
            shutil.copy2(candidates[0]["translated"], out_translated)

            key = relative.as_posix()
            if key not in entries_by_png:
                ref = candidates[0]
                entries_by_png[key] = {
                    "name": source.stem,
                    "png": key,
                    "translated_png": key,
                    "width": Image.open(source).width,
                    "height": Image.open(source).height,
                    "original": "",
                    "translation": "",
                    "classification": "shared-pixel-match",
                    "reference": ref["reference"],
                    "reference_original": ref["original"].name,
                    "pixel_sha256": h,
                }
                added += 1

        # Keep the pre-existing manually reviewed GADAT030 entries and append
        # only deterministic shared-pixel matches. Sort for stable diffs.
        merged = list(entries_by_png.values())
        merged.sort(key=lambda x: str(x.get("png", "")))
        (review_root / "manifest.json").write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        totals[container] = {
            "source_png": len(list(source_root.rglob("*.png"))),
            "matched": matched,
            "added": added,
            "conflicts": conflicts,
            "review_original": len(list(original_root.rglob("*.png"))),
            "review_translated": len(list(translated_root.rglob("*.png"))),
        }

    print(json.dumps(totals, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
