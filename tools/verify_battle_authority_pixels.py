"""Check runtime pixels against native TEX encoding, without slot-fit alterations."""
import argparse
import json
import mmap
from pathlib import Path

from PIL import Image

import eternal_lovers_patch_battle_bank_images as patcher
from eternal_lovers_patch_remaining import merged_resources
from galaxy_angel_gadat032 import decode_tex, encode_tex
import galaxy_angel_build as builder
import moonlit_lovers_resources as resources


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--layout", choices=("eternal", "moonlit"), required=True)
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--container", action="append", default=None)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = {"comparison": "native TEX encoding without capacity-driven palette reduction/refit", "containers": {}, "mismatches": []}
    with args.original_iso.open("rb") as old_stream, args.iso.open("rb") as new_stream, mmap.mmap(old_stream.fileno(), 0, access=mmap.ACCESS_READ) as old, mmap.mmap(new_stream.fileno(), 0, access=mmap.ACCESS_READ) as new:
        old_files = builder.iso_files(old)
        new_files = builder.iso_files(new)
        for stem in args.container or ("SLGRES", "SLGSTAGE"):
            root = args.project / "assets/image_extraction"
            root = root / "japanese_images" / stem if args.layout == "eternal" else root / stem / "japanese_images"
            entries = json.loads((root / "mirror_manifest.json").read_text(encoding="utf-8"))
            source = builder.resolve_iso_file(old_files, stem)
            target = builder.resolve_iso_file(new_files, stem)
            original = memoryview(old)[source.extent * builder.SECTOR:source.extent * builder.SECTOR + source.size]
            patched = memoryview(new)[target.extent * builder.SECTOR:target.extent * builder.SECTOR + target.size]
            source_map = merged_resources(original, stem)
            target_map = merged_resources(patched, stem)
            by_record = {position: item for item in target_map.values() for position in item.record_positions}
            count = 0
            for entry in entries:
                source_item = source_map[int(entry["resource_offset"])]
                item = by_record[source_item.record_positions[0]]
                raw = resources.decompress_resource(patched, item.offset, item.raw_size, item.compressed_size)
                original_raw = resources.decompress_resource(original, source_item.offset, source_item.raw_size, source_item.compressed_size)
                if entry.get("mirror_source_png"):
                    primary = args.project / "assets/image_extraction"
                    primary = primary / "japanese_images/SLG" if args.layout == "eternal" else primary / "SLG/japanese_images"
                    path = primary / "translated_png" / entry["mirror_source_png"]
                else:
                    path = root / "mirror_png" / entry["translated_png"]
                with Image.open(path) as image:
                    expected = patcher.pixel_hash(decode_tex(encode_tex(original_raw, image)))
                actual = patcher.pixel_hash(decode_tex(raw))
                if actual != expected:
                    result["mismatches"].append({"container": stem, "png": entry["png"], "source": str(path), "expected": expected, "actual": actual})
                count += 1
            result["containers"][stem] = count
            original.release()
            patched.release()
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"containers": result["containers"], "pixel_mismatches": len(result["mismatches"])}))
    if result["mismatches"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
