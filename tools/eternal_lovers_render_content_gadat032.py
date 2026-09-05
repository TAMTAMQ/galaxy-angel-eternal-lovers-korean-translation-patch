from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
from PIL import Image

import eternal_lovers_render_chapter_titles as chapter_glow
import moonlit_lovers_render_gadat032_ui as ml

ROOT = Path(__file__).resolve().parents[1]
GAME = ROOT / "work" / "galaxy_angel_eternal_lovers"
IMAGE_ROOT = GAME / "assets" / "image_extraction"
FULL_ROOT = GAME / "assets" / "full_extraction" / "GADAT032"
FULL_PNG = FULL_ROOT / "png"
FULL_MANIFEST = FULL_ROOT / "manifest.json"
OUT_ROOT = IMAGE_ROOT / "japanese_images" / "GADAT032"
ORIGINAL_DIR = OUT_ROOT / "png"
TRANSLATED_DIR = OUT_ROOT / "translated_png"
MANIFEST_PATH = OUT_ROOT / "manifest.json"

# The gxbgm slot identities come from Eternal Lovers' own album.tbl ->
# adv_sound.tbl mapping.  Do not inherit Moonlit's gxbgm numbering.
BGM = {
    "01": ("エンジェリックシンフォニー", "엔젤릭 심포니"),
    "02": ("Eternal Love 2004", "Eternal Love 2004"),
    "03": ("銀河の希望", "은하의 희망"),
    "04": ("星空の恋人たち", "별하늘의 연인들"),
    "05": ("ままならぬ想い", "뜻대로 되지 않는 마음"),
    "06": ("ＥＤＥＮ・伝説の文明", "EDEN・전설의 문명"),
    "07": ("ルシャーティとヴァイン", "루샤티와 바인"),
    "08": ("ヴァインその正体", "바인, 그 정체"),
    "09": ("支配者ゲルン", "지배자 게른"),
    "10": ("ヴァル・ファスク大艦隊", "발・파스크 대함대"),
    "11": ("ルシャーティの悲しみ", "루샤티의 슬픔"),
    "12": ("ミルフィーユ", "밀피유"),
    "13": ("ランファ", "란파"),
    "14": ("ミント", "민트"),
    "15": ("フォルテ", "포르테"),
    "16": ("ヴァニラ", "바닐라"),
    "17": ("ちとせ", "치토세"),
    "18": ("ミルフィーユ・メロウ", "밀피유・멜로우"),
    "19": ("ランファ・メロウ", "란파・멜로우"),
    "20": ("ミント・メロウ", "민트・멜로우"),
    "21": ("フォルテ・メロウ", "포르테・멜로우"),
    "22": ("ヴァニラ・メロウ", "바닐라・멜로우"),
    "23": ("ちとせ（アレンジ）", "치토세 (어레인지)"),
}

# chapter.tbl establishes the exact title-resource IDs.  The route-specific
# titles below were independently confirmed during the full GADAT032 visual
# audit recorded in the Eternal Lovers STATUS.md.
CHAPTERS = {
    "101": ("よみがえる神話", "되살아나는 신화"),
    "102": ("破壊者来りて", "파괴자, 오다"),
    "103": ("うしなわれるもの", "잃어버리는 것"),
    "104": ("エンジェル隊、絶体絶命", "엔젤대, 절체절명"),
    "107": ("ギャラクシーエンジェル", "갤럭시 엔젤"),
    "205": ("ロストメモリーズ", "로스트 메모리즈"),
    "206": ("二度目のファーストキス", "두 번째 퍼스트 키스"),
    "305": ("タクト改造計画", "택트 개조 계획"),
    "306": ("愛のスーパーダイブ", "사랑의 슈퍼 다이브"),
    "405": ("こちらミント放送局", "여기는 민트 방송국"),
    "406": ("声が聞こえる", "목소리가 들려"),
    "505": ("存在の役割", "존재의 역할"),
    "506": ("愛する人", "사랑하는 사람"),
    "605": ("半分ずつのお願い", "반씩 나눈 소원"),
    "606": ("やきもちヒーリング", "질투 힐링"),
    "705": ("おかしな三角関係", "이상한 삼각관계"),
    "706": ("トラブル・ラブレター", "트러블 러브레터"),
}

# The mission order is corroborated by Eternal Lovers' scoreattack/result
# tables and the game's route walkthrough.  These are story-stage labels, not
# the Moonlit stage texts that happened to reuse several resource names.
STAGES = {
    "0111": ("未知の艦隊", "미지의 함대"),
    "0121": ("小型船救出", "소형선 구조"),
    "0211": ("ヴァル・ファスク侵攻艦隊", "발・파스크 침공 함대"),
    "0221": ("侵攻艦隊第２陣", "침공 함대 제2진"),
    "0311": ("ヴァル・ファスク拠点攻略", "발・파스크 거점 공략"),
    "0321": ("紋章機奪還", "문장기 탈환"),
    "0411": ("７番機との戦い", "7번기와의 전투"),
    "0421": ("ヴァインの罠", "바인의 함정"),
    "0511": ("ＥＤＥＮ本星解放作戦", "EDEN 본성 해방 작전"),
    "0611": ("スカイパレス防衛戦", "스카이팔레스 방어전"),
    "0711": ("ヴァル・ファスク本星決戦", "발・파스크 본성 결전"),
    "0721": ("ゲルン艦破壊", "게른 함선 파괴"),
}

# Two system dialogs are pixel-identical to Moonlit Lovers only in their *first* line, so the
# renders imported by pixel hash carried Moonlit's second line: the overwrite prompt lost its
# "previous data is lost" warning and the delete prompt gained one the Japanese never had.
# These are re-rendered from the Japanese that is actually in Eternal Lovers' textures.
DIALOGS = {
    "gdmes03.tex": (
        "ゲームデータを上書きしてもよろしいですか？\n※以前のデータは失われます",
        ["게임 데이터를 덮어써도 괜찮습니까?", "※이전 데이터는 사라집니다"],
    ),
    "gdmes04.tex": (
        "ゲームデータを削除します。\nよろしいですか？",
        ["게임 데이터를 삭제합니다.", "삭제하시겠습니까?"],
    ),
}

# Text plates whose Moonlit-imported renders were wrong for Eternal Lovers: route prompts that
# lost their second line, dialogs whose wording belonged to a different message, buttons with
# an unrelated label, and name plates where the katakana was only half erased.  Each entry is
# the Japanese actually on the texture and the Korean lines to draw in its place.
PLATES = {
    "gdmes07.tex": ("スコアアタックを中止します。\nよろしいですか？", ["스코어 어택을 중지합니다.", "중지하시겠습니까?"]),
    "gdmes20.tex": ("ミルフィーユルートを開始します。\n準備はよろしいですか？", ["밀피유 루트를 시작합니다.", "준비되셨습니까?"]),
    "gdmes21.tex": ("ランファルートを開始します。\n準備はよろしいですか？", ["란파 루트를 시작합니다.", "준비되셨습니까?"]),
    "gdmes22.tex": ("ミントルートを開始します。\n準備はよろしいですか？", ["민트 루트를 시작합니다.", "준비되셨습니까?"]),
    "gdmes23.tex": ("フォルテルートを開始します。\n準備はよろしいですか？", ["포르테 루트를 시작합니다.", "준비되셨습니까?"]),
    "gdmes24.tex": ("ヴァニラルートを開始します。\n準備はよろしいですか？", ["바닐라 루트를 시작합니다.", "준비되셨습니까?"]),
    "gdmes25.tex": ("ちとせルートを開始します。\n準備はよろしいですか？", ["치토세 루트를 시작합니다.", "준비되셨습니까?"]),
    "gdmes47.tex": ("システムデータのアクセスに失敗しました。\nシステムデータが壊れている可能性があります。\n再試行しますか？", ["시스템 데이터 접근에 실패했습니다.", "시스템 데이터가 손상되었을 수 있습니다.", "다시 시도하시겠습니까?"]),
    "gdmes53.tex": ("ゲームデータの削除に失敗しました。\n再試行しますか？", ["게임 데이터 삭제에 실패했습니다.", "다시 시도하시겠습니까?"]),
    "goeff02.tex": ("文字送り速度\n自動改ページ速度\n画面効果\nカメラ回転方向\n命令時のビュー\n振動", ["글자 넘김 속도", "자동 페이지 속도", "화면 효과", "카메라 회전 방향", "명령 시 시점", "진동"]),
}

EXTRA = {
    # Character-selection title. The resource remains Japanese in Eternal but
    # is a different render from Moonlit's corresponding title texture.
    "gxttl91.tex": ("ちとせ", "치토세"),
}

# Battle-result route name plates: glyph-only textures with a transparent background, so they
# are drawn on a fresh canvas.  The renders imported from Moonlit had inpainted the original in
# place instead, which left half of the katakana standing beside the Korean.
RESULT_PLATES = {
    "gproute2.tex": ("ミルフィーユ", "밀피유"),
    "gproute3.tex": ("ランファ", "란파"),
    "gproute4.tex": ("ミント", "민트"),
    "gproute5.tex": ("フォルテ", "포르테"),
    "gproute6.tex": ("ヴァニラ", "바닐라"),
    "gproute7.tex": ("ちとせ", "치토세"),
}


def pixel_hash(path: Path) -> str:
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        h = hashlib.sha256()
        h.update(f"{rgba.width}x{rgba.height}:RGBA".encode("ascii"))
        h.update(rgba.tobytes())
        return h.hexdigest()


def load_existing() -> dict[str, dict]:
    if not MANIFEST_PATH.is_file():
        return {}
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {str(x.get("png", "")): x for x in data if isinstance(x, dict)}


def load_resources() -> dict[str, dict]:
    data = json.loads(FULL_MANIFEST.read_text(encoding="utf-8"))["resources"]
    return {r["name"]: r for r in data if r.get("images")}


def write_one(existing: dict[str, dict], resource: dict, jp: str, ko: str, kind: str, translated: Image.Image) -> None:
    source = FULL_PNG / Path(resource["images"][0]["png"])
    output_name = f"block_{int(resource['offset']):08x}.png"
    original_output = ORIGINAL_DIR / output_name
    translated_output = TRANSLATED_DIR / output_name
    shutil.copy2(source, original_output)
    translated.save(translated_output)
    with Image.open(source) as original:
        if translated.size != original.size:
            raise ValueError(f"size mismatch: {resource['name']} {translated.size} != {original.size}")
    existing[output_name] = {
        "name": resource["name"],
        "png": output_name,
        "translated_png": output_name,
        "width": translated.width,
        "height": translated.height,
        "original": jp,
        "translation": ko,
        "classification": kind,
        "resource_path": resource["path"],
        "resource_offset": int(resource["offset"]),
        "source_png": resource["images"][0]["png"],
        "source_pixel_sha256": pixel_hash(source),
    }


def main() -> None:
    ORIGINAL_DIR.mkdir(parents=True, exist_ok=True)
    TRANSLATED_DIR.mkdir(parents=True, exist_ok=True)
    resources = load_resources()
    existing = load_existing()
    counts = {"bgm": 0, "chapter": 0, "stage": 0, "dialog": 0, "plate": 0, "extra": 0, "result": 0}

    for number, (jp, ko) in BGM.items():
        r = resources[f"gxbgm{number}.tex"]
        source = FULL_PNG / Path(r["images"][0]["png"])
        translated = ml.render_text_only(source, ko, "left", 17, 10)
        write_one(existing, r, jp, ko, "eternal-bgm-table", translated)
        counts["bgm"] += 1

    for number, (jp, ko) in CHAPTERS.items():
        r = resources[f"gktitle{number}.tex"]
        source = FULL_PNG / Path(r["images"][0]["png"])
        # Eternal's chapter plate is plain black behind a cyan glow, so the Korean title is
        # rebuilt as glow-on-black rather than pasted over a median composite that keeps
        # turquoise residue from the other seventeen titles.
        translated = chapter_glow.render(source, ko)
        write_one(existing, r, jp, ko, "eternal-chapter-title", translated)
        counts["chapter"] += 1

    for number, (jp, ko) in STAGES.items():
        r = resources[f"gpstg{number}.tex"]
        source = FULL_PNG / Path(r["images"][0]["png"])
        translated = ml.render_text_only(source, ko, "left", 17, 9)
        write_one(existing, r, jp, ko, "eternal-story-stage", translated)
        counts["stage"] += 1

    for name, (jp, lines) in DIALOGS.items():
        r = resources[name]
        source = FULL_PNG / Path(r["images"][0]["png"])
        translated = ml.render_banded_text(source, lines, 20, 10)
        write_one(existing, r, jp, "\n".join(lines), "eternal-system-dialog", translated)
        counts["dialog"] += 1

    for name, (jp, lines) in PLATES.items():
        r = resources[name]
        source = FULL_PNG / Path(r["images"][0]["png"])
        translated = ml.render_banded_text(source, lines, 22, 9)
        write_one(existing, r, jp, "\n".join(lines), "eternal-text-plate", translated)
        counts["plate"] += 1

    for name, (jp, ko) in EXTRA.items():
        r = resources[name]
        source = FULL_PNG / Path(r["images"][0]["png"])
        translated = ml.render_text_only(source, ko, "center", 22, 11)
        write_one(existing, r, jp, ko, "eternal-album-title", translated)
        counts["extra"] += 1

    for name, (jp, ko) in RESULT_PLATES.items():
        r = resources[name]
        source = FULL_PNG / Path(r["images"][0]["png"])
        translated = ml.render_text_only(source, ko, "center", 20, 10)
        write_one(existing, r, jp, ko, "eternal-result-plate", translated)
        counts["result"] += 1

    merged = list(existing.values())
    merged.sort(key=lambda x: str(x.get("png", "")))
    MANIFEST_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    originals = {p.relative_to(ORIGINAL_DIR).as_posix() for p in ORIGINAL_DIR.rglob("*.png")}
    translations = {p.relative_to(TRANSLATED_DIR).as_posix() for p in TRANSLATED_DIR.rglob("*.png")}
    print(json.dumps(counts, ensure_ascii=False))
    print(f"review originals: {len(originals)}")
    print(f"review translations: {len(translations)}")
    print(f"same names: {originals == translations}")
    print(f"only originals: {len(originals - translations)}")
    print(f"only translations: {len(translations - originals)}")


if __name__ == "__main__":
    main()
