#!/usr/bin/env python3
"""Package a finished Galaxy Angel ISO as a distributable xdelta patch.

The patch, not the ISO, is what can be shared: it carries only the difference between the
player's own Japanese disc and the Korean build.  Both sides are hashed into the release notes
and the patch is applied back to the original here, so a release is never published without
proving it reconstructs the exact ISO that was verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pyxdelta

CHUNK = 1 << 22


def digests(path: Path) -> dict:
    md5, sha1, sha256 = hashlib.md5(), hashlib.sha1(), hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            size += len(block)
            for algorithm in (md5, sha1, sha256):
                algorithm.update(block)
    return {"size": size, "md5": md5.hexdigest(), "sha1": sha1.hexdigest(), "sha256": sha256.hexdigest()}


README = """{title} (PS2) 한국어 패치 {version}

이 패치는 일본판 {title} PS2 원본 ISO에 적용하는 비공식 한국어
패치입니다. 원본 ISO 자체는 포함하지 않습니다.

지원 원본 ISO
- 크기: {source_size:,} bytes
- MD5: {source_md5}
- SHA-1: {source_sha1}
- SHA-256: {source_sha256}

패치 파일
- {patch_name}

적용 방법
1. 위 SHA-256과 일치하는 일본판 원본 ISO를 준비합니다.
2. xdelta3 또는 XDelta 패치를 지원하는 프로그램에서 원본 ISO를 Source로 지정합니다.
3. {patch_name}를 Patch로 지정합니다.
4. 새 ISO를 Output으로 지정한 뒤 패치를 적용합니다.
5. 생성된 ISO의 SHA-256이 아래 값과 일치하는지 확인합니다.

패치 적용 결과 ISO
- 크기: {target_size:,} bytes
- MD5: {target_md5}
- SHA-1: {target_sha1}
- SHA-256: {target_sha256}

주의
- 다른 리비전의 ISO, 이미 수정된 ISO, 다른 패치가 적용된 ISO에는 적용하지 마세요.
- 세이브스테이트는 패치 이전 런타임 상태를 그대로 보존할 수 있으므로 화면 문제가 보이면
  새 ISO에서 메뉴/화면을 다시 진입하거나 일반 세이브 데이터로 확인하는 것을 권장합니다.
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--patched-iso", type=Path, required=True)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--version", default="v1.0")
    parser.add_argument("--title", default="Galaxy Angel - Eternal Lovers",
                        help="game name as it should read in the README")
    parser.add_argument("--slug", default="galaxy_angel_eternal_lovers",
                        help="file-name stem for the patch")
    parser.add_argument("--skip-apply-check", action="store_true")
    args = parser.parse_args()

    args.release_dir.mkdir(parents=True, exist_ok=True)
    patch_name = f"{args.slug}_ps2_kr_{args.version}.xdelta"
    patch_path = args.release_dir / patch_name

    source = digests(args.original_iso)
    target = digests(args.patched_iso)

    if not pyxdelta.run(str(args.original_iso), str(args.patched_iso), str(patch_path)):
        raise SystemExit("xdelta encode failed")

    check = {"performed": False}
    if not args.skip_apply_check:
        rebuilt = args.release_dir / "apply_check.iso"
        if not pyxdelta.decode(str(args.original_iso), str(patch_path), str(rebuilt)):
            raise SystemExit("xdelta decode failed")
        rebuilt_digests = digests(rebuilt)
        check = {"performed": True, "matches": rebuilt_digests["sha256"] == target["sha256"]}
        rebuilt.unlink()
        if not check["matches"]:
            raise SystemExit("patch does not reproduce the verified ISO")

    (args.release_dir / "README.txt").write_text(
        README.format(
            title=args.title,
            version=args.version,
            patch_name=patch_name,
            source_size=source["size"], source_md5=source["md5"],
            source_sha1=source["sha1"], source_sha256=source["sha256"],
            target_size=target["size"], target_md5=target["md5"],
            target_sha1=target["sha1"], target_sha256=target["sha256"],
        ),
        encoding="utf-8",
    )
    (args.release_dir / "SHA256SUMS.txt").write_text(
        f"{source['sha256']}  {args.original_iso.name}\n"
        f"{target['sha256']}  {args.patched_iso.name}\n"
        f"{digests(patch_path)['sha256']}  {patch_name}\n",
        encoding="utf-8",
    )
    report = {
        "schema": "galaxy-angel-release/v1",
        "title": args.title,
        "version": args.version,
        "source": source,
        "target": target,
        "patch": {"name": patch_name, **digests(patch_path)},
        "apply_check": check,
    }
    (args.release_dir / "release.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
