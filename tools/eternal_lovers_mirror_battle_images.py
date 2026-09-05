#!/usr/bin/env python3
"""Mirror the translated battle textures into the per-stage resource banks.

``SLGRES`` and ``SLGSTAGE`` carry their own copies of the ``dat/slg/2dparts`` textures — the
same picture, stored again inside each stage's bank.  Patching only ``SLG`` therefore leaves
those copies Japanese, and the game loads them during a battle.

The image patcher can chase a copy through ``--runtime-container``, but only when the copy's
*compressed* bytes are identical to the primary's.  Around 140 of these banks re-compressed
the same picture differently, so those copies were never found.  This tool instead builds a
proper translated-image set for each bank, keyed by that bank's own resources, so the patcher
rewrites them through the container index exactly as it does for SLG.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


MIRROR_CONTAINERS = ("SLGRES", "SLGSTAGE")


def load_resources(project: Path, container: str) -> list[dict]:
    path = project / "assets/full_extraction" / container / "manifest.json"
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["resources"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path("work/galaxy_angel_eternal_lovers"))
    parser.add_argument("--source-container", default="SLG")
    args = parser.parse_args()

    project = args.project
    source_root = project / "assets/image_extraction/japanese_images" / args.source_container
    translated = json.loads((source_root / "manifest.json").read_text(encoding="utf-8"))

    by_name = {r["name"]: r for r in load_resources(project, args.source_container) if r.get("name")}
    by_raw: dict[str, dict] = {}
    for entry in translated:
        resource = by_name.get(entry["name"])
        if resource and resource.get("raw_sha256"):
            by_raw[resource["raw_sha256"]] = entry

    # Validate all inputs before refreshing any derived output. Never regenerate SLG art.
    for entry in by_raw.values():
        source = source_root / "translated_png" / entry["translated_png"]
        if not source.is_file():
            raise FileNotFoundError(f"missing canonical SLG image: {source}")

    summary = {}
    for container in MIRROR_CONTAINERS:
        images_root = project / "assets/image_extraction/japanese_images" / container
        (images_root / "mirror_png").mkdir(parents=True, exist_ok=True)
        manifest = []
        for resource in load_resources(project, container):
            entry = by_raw.get(resource.get("raw_sha256") or "")
            if entry is None or not resource.get("images"):
                continue
            png = f"block_{int(resource['offset']):08x}.png"
            shutil.copy2(source_root / "translated_png" / entry["translated_png"], images_root / "mirror_png" / png)
            manifest.append({
                "name": resource["name"],
                "png": png,
                "translated_png": png,
                "width": entry["width"],
                "height": entry["height"],
                "original": entry.get("original", ""),
                "translation": entry.get("translation", ""),
                "classification": "eternal-battle-bank-mirror",
                "mirror_of": entry["name"],
                "mirror_source_png": entry["translated_png"],
                "resource_path": resource.get("path"),
                "resource_offset": int(resource["offset"]),
                "source_png": resource["images"][0]["png"],
            })
        manifest.sort(key=lambda x: x["png"])
        (images_root / "mirror_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        summary[container] = len(manifest)

    print(json.dumps({"source": args.source_container, "mirrored": summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
