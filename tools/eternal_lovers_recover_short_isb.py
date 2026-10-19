"""Check or materialize reviewed ISB records missed by text-key lookahead."""
import argparse
import json
from pathlib import Path

from eternal_lovers_isb_static import STRING_TAG, decode_isb_payload


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def recover(project, apply=False):
    assets = project / "assets/translation/isb"
    manifest = load(assets / "recovered_records.json")
    segments = {}
    added = corrected = 0
    problems = []
    for row in manifest["records"]:
        name = Path(row["file"]).stem
        segment = segments.setdefault(name, load(assets / "segments" / (name + ".json")))
        raw = (project / "source/scenario" / row["file"]).read_bytes()
        offset = row["offset"]
        if raw[offset:offset + 4] != STRING_TAG or int.from_bytes(raw[offset + 4:offset + 8], "little") != row["length"]:
            raise ValueError(f"recovery record header changed: {name}:{offset:#x}")
        actual = decode_isb_payload(raw, offset + 8, row["length"], row["key"]).decode("cp932")
        if actual != row["original"]:
            raise ValueError(f"recovery source mismatch: {name}:{offset:#x}")
        matches = [u for u in segment["units"] if any(s["offset"] == offset for s in u["storage"])]
        if len(matches) > 1:
            raise ValueError(f"duplicate recovery target: {name}:{offset:#x}")
        if matches:
            unit = matches[0]
            if len(unit["storage"]) != 1:
                raise ValueError(f"recovery target is grouped: {unit['id']}")
            storage = unit["storage"][0]
            if storage["length"] != row["length"]:
                raise ValueError(f"recovery slot changed: {unit['id']}")
            if unit.get("use_translation") and unit.get("translation") and unit["translation"] != row["translation"]:
                raise ValueError(f"reviewed translation conflicts with current authority: {unit['id']}")
            if not unit.get("use_translation") and unit.get("translation"):
                raise ValueError(f"recovery would activate an intentionally disabled translation: {unit['id']}")
            if storage["original"] != row["original"]:
                old = decode_isb_payload(raw, offset + 8, storage["length"], storage["key"]).decode("cp932")
                if old != storage["original"]:
                    raise ValueError(f"old recovery provenance mismatch: {unit['id']}")
                corrected += 1
        else:
            ordinal = max(int(u["id"].rsplit(":", 1)[1]) for u in segment["units"]) + 1
            unit = {"id": f"{name}:isb:{ordinal:04d}", "context": {"voice": None},
                    "original": row["original"], "translation": row["translation"],
                    "state": "reviewed", "use_translation": True, "storage": [{}]}
            segment["units"].append(unit)
            storage = unit["storage"][0]
            added += 1
        expected_storage = {k: row[k] for k in ("offset", "length", "key", "original")}
        if (unit.get("original") != row["original"] or storage != expected_storage
                or unit.get("translation") != row["translation"] or not unit.get("use_translation")):
            problems.append(f"{name}:{offset:#x}")
        unit.update(original=row["original"], translation=row["translation"], state="reviewed", use_translation=True)
        unit["storage"] = [expected_storage]
    if apply:
        for name, segment in segments.items():
            (assets / "segments" / (name + ".json")).write_text(json.dumps(segment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        index = load(assets / "index.json")
        total = 0
        for entry in index["segments"]:
            name = Path(entry["path"]).stem
            segment = segments[name] if name in segments else load(assets / entry["path"])
            count = len(segment["units"])
            if name in segments:
                entry["units"] = count
            total += count
        index["population"]["translation_units"] = total
        (assets / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif problems:
        raise ValueError("reviewed short-block records are missing/stale: " + ", ".join(problems))
    return {"records": len(manifest["records"]), "added": added, "corrected": corrected, "apply": apply}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(recover(args.project, args.apply)))


if __name__ == "__main__":
    main()
