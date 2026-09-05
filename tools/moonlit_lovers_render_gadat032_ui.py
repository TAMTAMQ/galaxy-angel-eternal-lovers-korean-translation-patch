#!/usr/bin/env python3
"""Deterministically localize Japanese GADAT032 UI textures for Moonlit Lovers.

No OCR, VLM, LLM, or other AI is used.  The mappings in this file come from
manual contact-sheet review plus the game's own CP932 tables/RAU references.
Original extracted PNG files are never modified.  Localized images are written
as legacy block_XXXXXXXX.png names under GADAT032/japanese_images/translated_png
so they line up with assets/image_extraction/GADAT032/manifest.json.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

try:
    import cv2
except ModuleNotFoundError:  # Basic text/chapter rendering does not need OpenCV.
    cv2 = None
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
GAME = ROOT / "work" / "galaxy_angel_moonlit_lovers"
IMAGE_ROOT = GAME / "assets" / "image_extraction" / "GADAT032"
FULL_ROOT = GAME / "assets" / "full_extraction" / "GADAT032"
FULL_PNG = FULL_ROOT / "png"
OUT_ROOT = IMAGE_ROOT / "japanese_images"
ORIGINAL_DIR = OUT_ROOT / "png"
OUT_DIR = OUT_ROOT / "translated_png"
DRAFT_PATH = OUT_ROOT / "translation_draft.json"
REPORT_PATH = OUT_ROOT / "render_report.json"
FULL_MANIFEST = FULL_ROOT / "manifest.json"

FONT_REGULAR = Path("C:/Windows/Fonts/malgun.ttf")
FONT_BOLD = Path("C:/Windows/Fonts/malgunbd.ttf")

# Malgun Gothic has no U+30FB glyph, so a Japanese middle dot kept in a Korean name renders
# as tofu.  Substitute the visually identical U+00B7 at draw time only; the translation data
# keeps the original character.
GLYPH_FALLBACKS = {"・": "·", "･": "·"}


def displayable(text: str) -> str:
    for source, replacement in GLYPH_FALLBACKS.items():
        text = text.replace(source, replacement)
    return text



@dataclass
class Spec:
    resource_name: str
    jp: str
    ko: str
    mode: str
    path_contains: str | None = None
    align: str = "center"
    max_size: int = 18
    min_size: int = 8
    box: tuple[int, int, int, int] | None = None
    note: str = ""


TITLE_BUTTONS = {
    "01": ("はじめから", "처음부터"),
    "11": ("はじめから", "처음부터"),
    "02": ("つづきから", "이어하기"),
    "12": ("つづきから", "이어하기"),
    "03": ("設定", "설정"),
    "13": ("設定", "설정"),
    "04": ("おまけ", "보너스"),
    "05": ("終了", "종료"),
    "15": ("終了", "종료"),
}

SYSTEM_MENU = {
    "00": ("ロード", "불러오기"),
    "01": ("セーブ", "저장"),
    "02": ("設定", "설정"),
    "03": ("タイトルへ戻る", "타이틀로 돌아가기"),
    "04": ("閉じる", "닫기"),
}

SETTINGS = {
    "01": ("遅い", "느림"), "04": ("遅い", "느림"),
    "02": ("普通", "보통"), "05": ("普通", "보통"),
    "03": ("速い", "빠름"), "06": ("速い", "빠름"),
    "09": ("ノーマル", "노멀"), "10": ("リバース", "리버스"),
    "11": ("カメラ", "카메라"), "12": ("マップ", "맵"),
    "18": ("消音", "음소거"), "24": ("消音", "음소거"),
    "30": ("消音", "음소거"), "36": ("消音", "음소거"),
    "42": ("決定", "결정"), "43": ("キャンセル", "취소"),
}

SAVELOAD = {
    "gfbtn01": ("ロード", "불러오기"),
    "gfbtn11": ("セーブ", "저장"),
    "gfbtn02": ("閉じる", "닫기"),
    "gfbtn03": ("削除", "삭제"),
    "gfwin01": ("ロード", "불러오기"),
    "gfwin11": ("セーブ", "저장"),
}

DIALOG_BUTTONS = {
    "00": ("はい", "예"), "02": ("はい", "예"), "04": ("はい", "예"),
    "01": ("いいえ", "아니요"), "03": ("いいえ", "아니요"), "05": ("いいえ", "아니요"),
}

DIALOG_MESSAGES = {
    "00": ("ゲームを終了します。\nよろしいですか？", "게임을 종료합니다.\n종료하시겠습니까?"),
    "01": ("ゲームデータをロードします。\nよろしいですか？", "게임 데이터를 불러옵니다.\n불러오시겠습니까?"),
    "02": ("ゲームデータをセーブします。\nよろしいですか？", "게임 데이터를 저장합니다.\n저장하시겠습니까?"),
    "03": ("ゲームデータを上書きします。\nよろしいですか？", "게임 데이터를 덮어씁니다.\n덮어쓰시겠습니까?"),
    "04": ("ゲームデータを削除します。\n※以前のデータは失われます", "게임 데이터를 삭제합니다.\n※이전 데이터는 사라집니다."),
    "05": ("タイトル画面に戻ります。\nよろしいですか？", "타이틀 화면으로 돌아갑니다.\n돌아가시겠습니까?"),
    "06": ("次へ進みます。\nよろしいですか？", "다음으로 진행합니다.\n진행하시겠습니까?"),
    "07": ("ギブアップします。\nよろしいですか？", "기브업합니다.\n포기하시겠습니까?"),
    "20": ("ミルフィーユのミッションに挑戦します。", "밀피유의 미션에 도전합니다."),
    "21": ("ランファのミッションに挑戦します。", "란파의 미션에 도전합니다."),
    "22": ("ミントのミッションに挑戦します。", "민트의 미션에 도전합니다."),
    "23": ("フォルテのミッションに挑戦します。", "포르테의 미션에 도전합니다."),
    "24": ("ヴァニラのミッションに挑戦します。", "바닐라의 미션에 도전합니다."),
    "25": ("ちとせのミッションに挑戦します。", "치토세의 미션에 도전합니다."),
    # Moonlit Lovers' memory-card series.  GA1 line-mask comparison establishes
    # the message families, while the Moonlit executable's save/load state
    # machine establishes its 80/144/208 KiB free-space tiers.  Keep every
    # source line represented instead of translating only the headline.
    "30": (
        "メモリーカード（ＰＳ２）をチェックしています。\nメモリーカード（ＰＳ２）を\n抜き差ししないでください。",
        "메모리 카드(PS2)를 확인 중입니다.\n메모리 카드(PS2)를\n빼거나 꽂지 마십시오.",
    ),
    "31": (
        "メモリーカード（ＰＳ２）の空き容量が不足しています。\nゲームデータをセーブするには\n208KB以上の空き容量が必要です。再試行しますか？",
        "메모리 카드(PS2)의 여유 공간이 부족합니다.\n게임 데이터를 저장하려면\n208KB 이상의 여유 공간이 필요합니다. 다시 시도하시겠습니까?",
    ),
    "32": (
        "メモリーカード（ＰＳ２）にゲームデータがありません。\n再試行しますか？",
        "메모리 카드(PS2)에\n게임 데이터가 없습니다.\n다시 시도하시겠습니까?",
    ),
    "33": ("ゲームデータが壊れています。\n再試行しますか？", "게임 데이터가 손상되었습니다.\n다시 시도하시겠습니까?"),
    "34": (
        "ゲームデータをロードしています。\nメモリーカード（ＰＳ２）を\n抜き差ししないでください。",
        "게임 데이터를 불러오는 중입니다.\n메모리 카드(PS2)를\n빼거나 꽂지 마십시오.",
    ),
    "35": ("ロードに失敗しました。\n再試行しますか？", "불러오기에 실패했습니다.\n다시 시도하시겠습니까?"),
    "36": ("メモリーカード（ＰＳ２）がありません。\n再試行しますか？", "메모리 카드(PS2)가 없습니다.\n다시 시도하시겠습니까?"),
    "37": (
        "ゲームデータをセーブしています。\nメモリーカード（ＰＳ２）を\n抜き差ししないでください。",
        "게임 데이터를 저장하는 중입니다.\n메모리 카드(PS2)를\n빼거나 꽂지 마십시오.",
    ),
    "38": ("セーブに失敗しました。\n再試行しますか？", "저장에 실패했습니다.\n다시 시도하시겠습니까?"),
    "39": (
        "メモリーカード（ＰＳ２）にこのゲームのデータがありません。\nゲームデータを新しく作成しますか？",
        "메모리 카드(PS2)에\n이 게임의 데이터가 없습니다.\n게임 데이터를 새로 만드시겠습니까?",
    ),
    "40": (
        "壊れたゲームデータがあります。\nゲームデータを新しく作成しますか？",
        "손상된 게임 데이터가 있습니다.\n게임 데이터를 새로\n만드시겠습니까?",
    ),
    "41": (
        "ゲームデータを作成しています。\nメモリーカード（ＰＳ２）を\n抜き差ししないでください。",
        "게임 데이터를 생성하는 중입니다.\n메모리 카드(PS2)를\n빼거나 꽂지 마십시오.",
    ),
    "42": (
        "メモリーカード（ＰＳ２）がフォーマットされていません。\nフォーマットしますか？",
        "메모리 카드(PS2)가\n포맷되지 않았습니다.\n포맷하시겠습니까?",
    ),
    "43": (
        "メモリーカード（ＰＳ２）をフォーマットしています。\nメモリーカード（ＰＳ２）を抜き差ししないでください。",
        "메모리 카드(PS2)를\n포맷하는 중입니다.\n빼거나 꽂지 마십시오.",
    ),
    "44": ("フォーマットに失敗しました。\n再試行しますか？", "포맷에 실패했습니다.\n다시 시도하시겠습니까?"),
    "45": (
        "システムデータにアクセスしています。\nメモリーカード（ＰＳ２）を抜き差ししないでください。",
        "시스템 데이터에 접근 중입니다.\n메모리 카드(PS2)를\n빼거나 꽂지 마십시오.",
    ),
    "46": (
        "システムデータをセーブできません。\nこのままタイトル画面に戻りますか？",
        "시스템 데이터를 저장할 수 없습니다.\n이대로 타이틀 화면으로\n돌아가시겠습니까?",
    ),
    "47": (
        "システムデータへのアクセスに失敗しました。\nシステムデータを初期化して再試行しますか？",
        "시스템 데이터 접근에 실패했습니다.\n시스템 데이터를 초기화한 뒤\n다시 시도하시겠습니까?",
    ),
    "50": (
        "メモリーカード（ＰＳ２）にこのゲームのデータがありません。\nシステムデータをロードせずにゲームを始めますか？\n（ゲーム中はシステムデータをロードできません。）",
        "메모리 카드(PS2)에\n이 게임의 데이터가 없습니다.\n시스템 데이터를 불러오지 않고\n게임을 시작하시겠습니까?\n(게임 중에는 시스템 데이터를 불러올 수 없습니다.)",
    ),
    "51": (
        "メモリーカード（ＰＳ２）の空き容量が不足しています。\nゲームデータをセーブするには\n144KB以上の空き容量が必要です。再試行しますか？",
        "메모리 카드(PS2)의 여유 공간이 부족합니다.\n게임 데이터를 저장하려면\n144KB 이상의 여유 공간이 필요합니다. 다시 시도하시겠습니까?",
    ),
    "52": (
        "メモリーカード（ＰＳ２）の空き容量が不足しています。\nゲームデータをセーブするには\n80KB以上の空き容量が必要です。再試行しますか？",
        "메모리 카드(PS2)의 여유 공간이 부족합니다.\n게임 데이터를 저장하려면\n80KB 이상의 여유 공간이 필요합니다. 다시 시도하시겠습니까?",
    ),
    "53": (
        "システムデータのロードに失敗しました。\n再試行しますか？",
        "시스템 데이터 불러오기에 실패했습니다.\n다시 시도하시겠습니까?",
    ),
}

# album.tbl + adv_sound.tbl give exact gxbgmNN -> WAV identities.  The Japanese
# labels below match the manual audit and the game's sound-test naming.
BGM = {
    "01": ("Eternal Love 2003", "Eternal Love 2003"),
    "02": ("天使たちの休息", "천사들의 휴식"),
    "03": ("Eternal Love エンジェル隊 ver.", "Eternal Love 엔젤대 ver."),
    "04": ("ミルフィーユのテーマ", "밀피유의 테마"),
    "05": ("ランファのテーマ", "란파의 테마"),
    "06": ("ミントのテーマ", "민트의 테마"),
    "07": ("フォルテのテーマ", "포르테의 테마"),
    "08": ("ヴァニラのテーマ", "바닐라의 테마"),
    "09": ("ちとせのテーマ", "치토세의 테마"),
    "10": ("ミルフィーユのテーマ (アレンジ)", "밀피유의 테마 (어레인지)"),
    "11": ("ランファのテーマ (アレンジ)", "란파의 테마 (어레인지)"),
    "12": ("ミントのテーマ (アレンジ)", "민트의 테마 (어레인지)"),
    "13": ("フォルテのテーマ (アレンジ)", "포르테의 테마 (어레인지)"),
    "14": ("ヴァニラのテーマ (アレンジ)", "바닐라의 테마 (어레인지)"),
    "15": ("ちとせのテーマ (アレンジ)", "치토세의 테마 (어레인지)"),
    "16": ("白き月", "백색의 달"),
    "17": ("コミュニケーション", "커뮤니케이션"),
    "18": ("シヴァ皇子", "시바 황자"),
    "19": ("回想", "회상"),
    "20": ("オルゴール", "오르골"),
    "21": ("ヴァル・ファスク", "발 파스크"),
    "22": ("巨大戦艦", "거대 전함"),
    "23": ("ノア", "노아"),
    "24": ("レゾム", "레좀"),
    "25": ("エンジェル隊登場", "엔젤대 등장"),
    "26": ("翼の奇跡", "날개의 기적"),
    "27": ("紋章機発進", "문장기 발진"),
    "28": ("ブリーフィング", "브리핑"),
    "29": ("銀河駆ける紋章機", "은하를 누비는 문장기"),
    "30": ("天使の翼、発動", "천사의 날개, 발동"),
    "31": ("勝利デモ", "승리 데모"),
    "32": ("エンジェルたちの戦い", "엔젤들의 전투"),
    "33": ("ヴァル・ファスクの脅威", "발 파스크의 위협"),
}

CHAPTERS = {
    "01": ("司令官はタクト", "사령관은 택트"),
    "02": ("ミルフィーの想い", "밀피유의 마음"),
    "03": ("ハニーとダーリン", "허니와 달링"),
    "04": ("愛のビッグバン", "사랑의 빅뱅"),
    "05": ("おかしなミント", "이상한 민트"),
    "06": ("フローラルピンク", "플로럴 핑크"),
    "07": ("育てるもの", "키우는 것"),
    "08": ("ふたりの花", "두 사람의 꽃"),
    "09": ("兄と妹？", "오빠와 여동생?"),
    "10": ("ラブラブ大作戦", "러브러브 대작전"),
    "11": ("エンジェル隊集合！", "엔젤대 집합!"),
    "12": ("大いなる災い", "거대한 재앙"),
    "13": ("白と黒の真実", "백과 흑의 진실"),
    "14": ("エンジェル・スラップ", "엔젤 슬랩"),
    "15": ("黒髪の少女", "검은 머리 소녀"),
    "16": ("悩める新入隊員", "고민하는 신입대원"),
}

STAGES = {
    "gpstg0311": ("エンジェル隊集結", "엔젤대 집결"),
    "gpstg0411": ("黒き月決戦", "검은 달 결전"),
    "gpstg0511": ("ヴァル・ファスク前衛艦隊", "발 파스크 전위함대"),
    "gpstg0611": ("トランスバール本星決戦", "트랜스발 본성 결전"),
    "gpstg1111": ("軌道ステーション防衛", "궤도 스테이션 방어"),
    "gpstg1121": ("レゾム艦隊", "레좀 함대"),
    "gpstg1211": ("テオ宙域突破", "테오 주역 돌파"),
    "gpstg1621": ("トランスバール本星最終戦", "트랜스발 본성 최종전"),
    "gpstg2111": ("強奪船団遭遇", "강탈 선단 조우"),
    "gpstg4121": ("商船救出", "상선 구출"),
}
STAGE_ALIASES = {
    "gpstg1121": ["gpstg2121", "gpstg3121", "gpstg4211", "gpstg5121"],
    "gpstg1211": ["gpstg2211", "gpstg3211", "gpstg5211"],
    "gpstg1621": ["gpstg2621", "gpstg3621", "gpstg4621", "gpstg5621"],
    "gpstg2111": ["gpstg3111", "gpstg4111", "gpstg5111"],
}

MAP_LABELS = {
    "0_004": ("司令官室", "사령관실"),
    "0_005": ("ブリッジ", "함교"),
    "0_006": ("銀河展望公園", "은하 전망공원"),
    "1_008": ("ティーラウンジ", "티 라운지"),
    "1_009": ("食堂", "식당"),
    "1_010": ("宇宙コンビニ", "우주 편의점"),
    "1_012": ("ホール", "홀"),
    "2_014": ("謁見の間", "알현실"),
    "2_015": ("ミルフィーユの部屋", "밀피유의 방"),
    "2_016": ("ランファの部屋", "란파의 방"),
    "2_017": ("ミントの部屋", "민트의 방"),
    "2_018": ("フォルテの部屋", "포르테의 방"),
    "2_019": ("ヴァニラの部屋", "바닐라의 방"),
    "2_301": ("ちとせの部屋", "치토세의 방"),
    "2_304": ("ちとせの部屋", "치토세의 방"),
    "2_302": ("オリジナル・ノアの部屋", "오리지널 노아의 방"),
    "3_021": ("ロッカールーム", "로커룸"),
    "3_022": ("医務室", "의무실"),
    "3_023": ("クジラルーム", "고래방"),
    "3_025": ("格納庫", "격납고"),
    "3_026": ("機関室", "기관실"),
    "3_027": ("射撃訓練場", "사격훈련장"),
    "3_029": ("倉庫", "창고"),
    "3_030": ("トレーニングルーム", "트레이닝룸"),
    "3_303": ("シミュレーター室", "시뮬레이터실"),
}

ROUTE_NAMES = {
    "2": ("ミルフィーユ", "밀피유"),
    "3": ("ランファ", "란파"),
    "4": ("ミント", "민트"),
    "5": ("フォルテ", "포르테"),
    "6": ("ヴァニラ", "바닐라"),
    "7": ("ちとせ", "치토세"),
}

SETTINGS_LABELS = {
    "goeff02": (
        ["文字送り速度", "自動ページ速度", "画面効果", "カメラ回転方向", "カメラスムーズ", "振動"],
        ["글자 넘김 속도", "자동 페이지 속도", "화면 효과", "카메라 회전 방향", "카메라 스무딩", "진동"],
        "The first three rows match GA1 pixel widths; the last three are corroborated by Moonlit's camera debug/config strings.",
    ),
    "goeff03": (
        ["テクスチャの色数", "ポリゴンモデルの精細度"],
        ["텍스처 색상 수", "폴리곤 모델 상세도"],
        "Pixel layout closely matches GA1 poeff05.",
    ),
    "goeff04": (
        ["BGM", "ムービー", "音声", "SE"],
        ["BGM", "무비", "음성", "SE"],
        "Each row was identified by deterministic glyph-shape/width comparison.",
    ),
    "goeff05": (["小", "小", "小", "小"], ["소", "소", "소", "소"], "Same four-row small label."),
    "goeff06": (["大", "大", "大", "大"], ["대", "대", "대", "대"], "Same four-row large label."),
}

ALBUM_TITLES = {
    "gxttl21": ("キャラクター選択", "캐릭터 선택"),
    "gxttl31": ("その他", "기타"),
    "gxttl41": ("ミルフィーユ", "밀피유"),
    "gxttl51": ("ランファ", "란파"),
    "gxttl61": ("ミント", "민트"),
    "gxttl71": ("フォルテ", "포르테"),
    "gxttl81": ("ヴァニラ", "바닐라"),
    "gxttl91": ("ちとせ", "치토세"),
}

GENERIC_BUTTONS = {
    # System menu used only by Score Attack.
    ("gqsca_btn00", "システムメニュー"): ("ギブアップ", "기브업"),
    ("gqsca_btn01", "システムメニュー"): ("閉じる", "닫기"),
    # Battle-result flow. gpbtn0 is used by Score Attack result screens;
    # gpbtn2 by the normal result screen. Both are the same forward action.
    ("gpbtn0", "戦闘結果"): ("次へ", "다음"),
    ("gpbtn1", "戦闘結果"): ("ランキング", "랭킹"),
    ("gpbtn2", "戦闘結果"): ("次へ", "다음"),
    # Game-over screen; text-mask correlation with GA1's title-return button
    # is >0.89 and the screen context confirms the action.
    ("gnbtn00", "ゲームオーバー"): ("タイトルへ戻る", "타이틀로 돌아가기"),
    # KUJIRA room's sole text button is the close action.
    ("gsbtn0", "クジラルーム"): ("閉じる", "닫기"),
    # Album's primary exit button; deterministic mask comparison matches the
    # close-button family from GA1.
    ("gxbtn0", "アルバム"): ("閉じる", "닫기"),
}


def load_resources() -> tuple[dict[str, list[dict]], dict[str, dict]]:
    manifest = json.loads(FULL_MANIFEST.read_text(encoding="utf-8"))
    by_name: dict[str, list[dict]] = {}
    by_png: dict[str, dict] = {}
    for resource in manifest["resources"]:
        if not resource.get("images"):
            continue
        by_name.setdefault(resource["name"], []).append(resource)
        for image in resource["images"]:
            by_png[image["png"]] = resource
    return by_name, by_png


BY_NAME, BY_PNG = load_resources()


def resolve_resource(spec: Spec) -> tuple[dict, Path]:
    matches = BY_NAME.get(spec.resource_name, [])
    if spec.path_contains:
        matches = [r for r in matches if spec.path_contains.lower() in (r.get("path") or "").lower()]
    if len(matches) != 1:
        raise KeyError(f"resource resolution failed for {spec.resource_name!r} / {spec.path_contains!r}: {len(matches)}")
    resource = matches[0]
    images = resource.get("images", [])
    if len(images) != 1:
        raise ValueError(f"expected one image for {resource['path']}: {len(images)}")
    source = FULL_PNG / Path(images[0]["png"])
    return resource, source


def legacy_name(resource: dict) -> str:
    return f"block_{int(resource['offset']):08x}.png"


def font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    if not path.is_file():
        raise FileNotFoundError(path)
    return ImageFont.truetype(str(path), size=size)


def text_bbox(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, spacing: int = 0) -> tuple[int, int, int, int]:
    if "\n" in text:
        return draw.multiline_textbbox((0, 0), text, font=fnt, spacing=spacing, align="center")
    return draw.textbbox((0, 0), text, font=fnt)


def fit_font(text: str, max_size: int, min_size: int, width: int, height: int, bold: bool = True, spacing: int = 0) -> ImageFont.FreeTypeFont:
    probe = Image.new("RGBA", (max(8, width * 2), max(8, height * 2)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    path = FONT_BOLD if bold else FONT_REGULAR
    for size in range(max_size, min_size - 1, -1):
        fnt = font(path, size)
        box = text_bbox(draw, text, fnt, spacing)
        if box[2] - box[0] <= width and box[3] - box[1] <= height:
            return fnt
    return font(path, min_size)


def dominant_foreground(original: Image.Image) -> tuple[int, int, int, int]:
    arr = np.array(original.convert("RGBA"))
    alpha = arr[:, :, 3]
    visible = arr[alpha > 80]
    if visible.size == 0:
        return (235, 235, 235, 255)
    brightness = visible[:, :3].mean(axis=1)
    bright = visible[brightness >= np.percentile(brightness, 65)]
    if bright.size == 0:
        bright = visible
    rgb = np.median(bright[:, :3], axis=0).astype(int)
    return (int(rgb[0]), int(rgb[1]), int(rgb[2]), 255)


def source_text_bands(original: Image.Image) -> list[tuple[int, int, int, int]]:
    arr = np.array(original.convert("RGBA"))
    rgb = arr[:, :, :3].astype(np.int16)
    alpha = arr[:, :, 3]
    mx = rgb.max(axis=2)
    mn = rgb.min(axis=2)
    mask = (alpha > 70) & (mx > 100) & ((mx - mn) < 140)
    rows = np.where(mask.sum(axis=1) > 0)[0]
    if len(rows) == 0:
        return []
    bands: list[tuple[int, int, int, int]] = []
    start = previous = int(rows[0])
    for value in rows[1:]:
        value = int(value)
        if value > previous + 1:
            region = mask[start:previous + 1]
            ys, xs = np.where(region)
            bands.append((int(xs.min()), start, int(xs.max()) + 1, previous + 1))
            start = value
        previous = value
    region = mask[start:previous + 1]
    ys, xs = np.where(region)
    bands.append((int(xs.min()), start, int(xs.max()) + 1, previous + 1))
    return bands


def render_banded_text(source: Path, lines: list[str], max_size: int, min_size: int) -> Image.Image:
    lines = [displayable(line) for line in lines]
    original = Image.open(source).convert("RGBA")
    bands = source_text_bands(original)
    if len(bands) != len(lines):
        raise ValueError(f"band count mismatch {source}: {len(bands)} != {len(lines)}")

    # Do not throw away the source canvas.  Some of these apparently
    # text-only textures contain faint alpha/padding data that is invisible on
    # a normal preview but is still part of the original design.  Remove only
    # the detected Japanese face and keep every other source pixel.
    text_mask = neutral_text_mask(original, None)
    canvas = inpaint_mask(original, text_mask)
    draw = ImageDraw.Draw(canvas)
    fill = dominant_foreground(original)
    for text, (left, top, right, bottom) in zip(lines, bands):
        source_height = max(10, bottom - top)
        # Match the source row height and weight rather than using the old thin
        # 15-17px system face.  Korean may be wider, so width may use the full
        # canvas while y/height remain tied to the Japanese row.
        adaptive_max = max(max_size, source_height + 5)
        fnt = fit_font(
            text,
            adaptive_max,
            min_size,
            original.width - 4,
            source_height + 1,
            bold=True,
        )
        box = draw.textbbox((0, 0), text, font=fnt)
        tw, th = box[2] - box[0], box[3] - box[1]
        source_center_x = (left + right) / 2
        # Keep left-oriented label columns left-oriented; narrow 小/大 columns
        # remain naturally centered because their source band is centered.
        if left <= 12 and original.width > 50:
            x = max(1, left - box[0])
        else:
            x = round(source_center_x - tw / 2 - box[0])
        x = max(-box[0], min(x, original.width - box[2]))
        y = round((top + bottom) / 2 - th / 2 - box[1])
        draw.text((x, y), text, font=fnt, fill=fill)
    return canvas


def balanced_lines(text: str, count: int) -> list[str]:
    """Split Korean to the source texture's visual line count without AI."""
    explicit = text.split("\n")
    if len(explicit) == count:
        return explicit
    if count <= 1:
        return [" ".join(part.strip() for part in explicit if part.strip())]

    words = " ".join(part.strip() for part in explicit if part.strip()).split()
    if len(words) >= count:
        lines: list[str] = []
        start = 0
        remaining_chars = sum(len(word) for word in words) + max(0, len(words) - 1)
        for line_index in range(count - 1):
            remaining_lines = count - line_index
            target = remaining_chars / remaining_lines
            current: list[str] = []
            current_len = 0
            while start < len(words) - (remaining_lines - 1):
                word = words[start]
                candidate = current_len + (1 if current else 0) + len(word)
                if current and candidate > target:
                    break
                current.append(word)
                current_len = candidate
                start += 1
            if not current:
                current.append(words[start])
                current_len = len(words[start])
                start += 1
            lines.append(" ".join(current))
            remaining_chars -= current_len + 1
        lines.append(" ".join(words[start:]))
        return lines

    # No useful spaces (or fewer words than source rows): balance by Hangul/
    # punctuation character count.  This is deterministic layout only; the
    # translation itself remains the manually supplied Korean text.
    compact = "".join(words) if words else text.replace("\n", "")
    base, extra = divmod(len(compact), count)
    lines = []
    pos = 0
    for index in range(count):
        size = base + (1 if index < extra else 0)
        lines.append(compact[pos:pos + size])
        pos += size
    return lines


def render_text_only(source: Path, text: str, align: str, max_size: int, min_size: int) -> Image.Image:
    """Render Korean using the Japanese glyph rows as geometry/style guides."""
    text = displayable(text)
    original = Image.open(source).convert("RGBA")
    bands = source_text_bands(original)
    if not bands:
        alpha_box = original.getchannel("A").getbbox() or (0, 0, original.width, original.height)
        bands = [alpha_box]
    lines = balanced_lines(text, len(bands))
    if len(lines) != len(bands):
        raise ValueError(f"text band split mismatch {source}: {len(lines)} != {len(bands)}")

    canvas = Image.new("RGBA", original.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    fill = dominant_foreground(original)
    for line, (left, top, right, bottom) in zip(lines, bands):
        source_height = max(1, bottom - top)
        # The old renderer capped many 20-44px Japanese faces at 17/22pt,
        # which made Korean visibly tiny.  The source row height is the real
        # size reference; keep the declared max only as a lower ceiling.
        adaptive_max = max(max_size, source_height + 6)
        if align == "left":
            width = max(12, original.width - max(1, left) - 2)
        else:
            width = max(12, original.width - 4)
        multiline = len(bands) > 1
        height_limit = max(min_size, source_height if multiline else source_height + 2)
        fnt = fit_font(
            line,
            adaptive_max,
            min_size,
            width,
            height_limit,
            bold=True,
        )
        box = draw.textbbox((0, 0), line, font=fnt, stroke_width=0)
        text_w, text_h = box[2] - box[0], box[3] - box[1]
        if align == "left":
            x = max(0, left - box[0])
        else:
            source_cx = (left + right) / 2
            x = round(source_cx - text_w / 2 - box[0])
            x = max(-box[0], min(x, original.width - box[2]))
        source_cy = (top + bottom) / 2
        y = round(source_cy - text_h / 2 - box[1])
        # Larger source faces have a perceptible dark edge.  Reproduce that
        # with one sampled-color stroke rather than a plain thin system font.
        # Keep neighboring source rows visibly separate.  A one-pixel stroke
        # on every multiline row can bridge the original 3-4px interline gap.
        stroke_width = 1 if (not multiline and source_height >= 19) else 0
        stroke = tuple(max(0, channel - 55) for channel in fill[:3]) + (220,)
        draw.text(
            (x, y),
            line,
            font=fnt,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke,
        )
    return canvas


def neutral_text_mask(original: Image.Image, box: tuple[int, int, int, int] | None) -> np.ndarray:
    if cv2 is None:
        raise RuntimeError("OpenCV is required for neutral text-mask extraction")
    arr = np.array(original.convert("RGBA"))
    rgb = arr[:, :, :3].astype(np.int16)
    alpha = arr[:, :, 3]
    mx = rgb.max(axis=2)
    mn = rgb.min(axis=2)
    # Japanese UI glyphs are pale gray/blue; saturated artwork and dark panel
    # gradients are intentionally excluded.
    mask = (alpha > 70) & (mx > 155) & ((mx - mn) < 95)
    if box is not None:
        l, t, r, b = box
        region = np.zeros_like(mask)
        region[max(0, t):min(mask.shape[0], b), max(0, l):min(mask.shape[1], r)] = True
        mask &= region
    # Drop border-connected components and tiny speckles.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    kept = np.zeros_like(mask)
    h, w = mask.shape
    for label in range(1, count):
        x, y, cw, ch, area = stats[label]
        if area < 2:
            continue
        touches_edge = x <= 1 or y <= 1 or x + cw >= w - 1 or y + ch >= h - 1
        if touches_edge and area > 8:
            continue
        if cw > w * 0.8 and ch <= 3:
            continue
        kept[labels == label] = True
    if not kept.any():
        kept = mask
    return kept


def inpaint_mask(image: Image.Image, mask: np.ndarray) -> Image.Image:
    if cv2 is None:
        raise RuntimeError("OpenCV is required for text-mask inpainting")
    rgba = image.convert("RGBA")
    # Small dilation removes anti-aliased fringes.
    dilated = cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=1).astype(bool)
    arr = np.array(rgba)
    h, w = dilated.shape
    for y in range(h):
        x = 0
        while x < w:
            if not dilated[y, x]:
                x += 1
                continue
            start = x
            while x < w and dilated[y, x]:
                x += 1
            end = x
            lx = max(0, start - 1)
            rx = min(w - 1, end)
            lhs = arr[y, lx].astype(np.float32)
            rhs = arr[y, rx].astype(np.float32)
            span = max(1, end - start)
            for xx in range(start, end):
                amount = (xx - start + 1) / (span + 1)
                arr[y, xx] = np.rint(lhs + (rhs - lhs) * amount).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def render_preserve(
    source: Path,
    text: str,
    box: tuple[int, int, int, int] | None,
    max_size: int,
    min_size: int,
    mask_source: Path | None = None,
) -> Image.Image:
    text = displayable(text)
    original = Image.open(source).convert("RGBA")
    mask_image = Image.open(mask_source).convert("RGBA") if mask_source else original
    if mask_image.size != original.size:
        mask_image = original
    mask = neutral_text_mask(mask_image, box)
    ys, xs = np.where(mask)
    if len(xs) == 0:
        raise ValueError(f"no text mask found: {source}")
    text_box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    image = inpaint_mask(original, mask)
    l, t, r, b = text_box
    avail_w = max(12, r - l + 8)
    avail_h = max(12, b - t + 4)
    fnt = fit_font(text, max_size, min_size, avail_w, avail_h, bold=True)
    draw = ImageDraw.Draw(image)
    tb = draw.textbbox((0, 0), text, font=fnt, stroke_width=1)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    cx, cy = (l + r) / 2, (t + b) / 2
    x = round(cx - tw / 2 - tb[0])
    y = round(cy - th / 2 - tb[1])
    # Sample a representative source glyph color; keep the familiar pale-blue
    # UI outline instead of introducing an arbitrary new palette.
    arr = np.array(original)
    pix = arr[mask]
    rgb = np.median(pix[:, :3], axis=0).astype(int) if len(pix) else np.array([235, 235, 235])
    fill = tuple(int(v) for v in rgb) + (255,)
    stroke = tuple(max(0, int(v) - 55) for v in rgb) + (210,)
    draw.text((x, y), text, font=fnt, fill=fill, stroke_width=1, stroke_fill=stroke)
    return image


def chapter_background() -> Image.Image:
    frames = []
    for number in range(1, 17):
        res = BY_NAME[f"gktitle{number:02d}.tex"][0]
        src = FULL_PNG / Path(res["images"][0]["png"])
        frames.append(np.array(Image.open(src).convert("RGBA"), dtype=np.uint8))
    stack = np.stack(frames, axis=0)
    med = np.median(stack, axis=0).astype(np.uint8)
    return Image.fromarray(med, "RGBA")


def render_chapter(source: Path, text: str, max_size: int, min_size: int, background: Image.Image) -> Image.Image:
    text = displayable(text)
    original = Image.open(source).convert("RGBA")
    if background.size != original.size:
        raise ValueError(f"chapter background size mismatch: {source}")

    src = np.array(original, dtype=np.int16)
    bg = np.array(background.convert("RGBA"), dtype=np.int16)
    delta = np.max(np.abs(src[:, :, :3] - bg[:, :, :3]), axis=2)
    mask = (delta > 18) & (src[:, :, 3] > 50)
    ys, xs = np.where(mask)
    if len(xs) == 0:
        raise ValueError(f"chapter title mask not found: {source}")
    left = max(0, int(xs.min()) - 3)
    top = max(0, int(ys.min()) - 3)
    right = min(original.width, int(xs.max()) + 4)
    bottom = min(original.height, int(ys.max()) + 4)

    # Preserve the exact source frame everywhere except the title rectangle.
    # The median image is used only as a clean plate under the Japanese title,
    # never as a replacement for the whole 512x110 chapter texture.
    image = original.copy()
    image.paste(background.crop((left, top, right, bottom)), (left, top))

    target_height = max(12, bottom - top - 6)
    adaptive_max = max(max_size, target_height)
    fnt = fit_font(
        text,
        adaptive_max,
        min_size,
        max(20, right - left - 12),
        target_height,
        bold=True,
    )
    draw = ImageDraw.Draw(image)
    box = draw.textbbox((0, 0), text, font=fnt, stroke_width=2)
    tw, th = box[2] - box[0], box[3] - box[1]
    x = round((left + right) / 2 - tw / 2 - box[0])
    y = round((top + bottom) / 2 - th / 2 - box[1])

    source_pixels = src[mask]
    brightness = source_pixels[:, :3].mean(axis=1)
    bright_cut = np.percentile(brightness, 70)
    dark_cut = np.percentile(brightness, 30)
    bright = source_pixels[brightness >= bright_cut, :3]
    dark = source_pixels[brightness <= dark_cut, :3]
    fill_rgb = np.median(bright, axis=0).astype(int) if len(bright) else np.array([242, 246, 250])
    stroke_rgb = np.median(dark, axis=0).astype(int) if len(dark) else np.array([62, 93, 122])
    fill = tuple(int(v) for v in fill_rgb) + (255,)
    stroke = tuple(int(v) for v in stroke_rgb) + (230,)
    draw.text((x, y), text, font=fnt, fill=fill, stroke_width=2, stroke_fill=stroke)
    return image


def build_specs() -> list[Spec]:
    specs: list[Spec] = []
    for num, (jp, ko) in TITLE_BUTTONS.items():
        for suffix in ("", "f"):
            specs.append(Spec(f"gtbtn{num}{suffix}.tex", jp, ko, "preserve", "system/タイトル", max_size=17, box=(8, 3, 121, 30)))
        if num in {"02", "12"}:
            name = f"gtbtn{num}b.tex"
            if name in BY_NAME:
                specs.append(Spec(name, jp, ko, "preserve", "system/タイトル", max_size=17, box=(8, 3, 121, 30)))

    for num, (jp, ko) in SYSTEM_MENU.items():
        for suffix in ("", "f", "b"):
            name = f"gqbtn{num}{suffix}.tex"
            if name in BY_NAME:
                specs.append(Spec(name, jp, ko, "preserve", "システムメニュー", max_size=17, box=(8, 2, 156, 32)))

    for num, (jp, ko) in SETTINGS.items():
        for suffix in ("", "f", "l", "b"):
            name = f"gobtn{num}{suffix}.tex"
            if name in BY_NAME:
                specs.append(Spec(name, jp, ko, "preserve", "設定画面", max_size=15, box=(3, 2, 133, 28)))

    for stem, (jp_lines, ko_lines, note) in SETTINGS_LABELS.items():
        name = f"{stem}.tex"
        if name in BY_NAME:
            specs.append(Spec(name, "\n".join(jp_lines), "\n".join(ko_lines), "banded", "設定画面", max_size=20, min_size=9, note=note))

    for stem, (jp, ko) in SAVELOAD.items():
        for suffix in ("", "f", "b") if stem.startswith("gfbtn") else ("",):
            name = f"{stem}{suffix}.tex" if suffix else f"{stem}.tex"
            if name in BY_NAME:
                specs.append(Spec(name, jp, ko, "preserve", "セーブロード", max_size=18, box=(4, 2, 204, 58)))

    for num, (jp, ko) in DIALOG_BUTTONS.items():
        for suffix in ("", "f"):
            name = f"gdbtn{num}{suffix}.tex"
            if name in BY_NAME:
                specs.append(Spec(name, jp, ko, "preserve", "汎用ダイアログ", max_size=17, box=(6, 2, 98, 26)))

    for num, (jp, ko) in DIALOG_MESSAGES.items():
        name = f"gdmes{num}.tex"
        if name in BY_NAME:
            specs.append(Spec(name, jp, ko, "text", "汎用ダイアログ", max_size=17, min_size=10))

    for num, (jp, ko) in BGM.items():
        # English-only labels are not localization targets.  Mixed labels such
        # as "Eternal Love エンジェル隊 ver." remain eligible because only the
        # Japanese portion changes in the manually supplied Korean string.
        if not re.search(r"[ぁ-ゖァ-ヺ一-鿿々〆ヵヶ]", jp):
            continue
        specs.append(Spec(f"gxbgm{num}.tex", jp, ko, "text", "アルバム", align="left", max_size=17, min_size=10))

    for stem, (jp, ko) in ALBUM_TITLES.items():
        name = f"{stem}.tex"
        if name in BY_NAME:
            specs.append(Spec(name, jp, ko, "text", "アルバム", max_size=22, min_size=11))

    for num, (jp, ko) in CHAPTERS.items():
        specs.append(Spec(f"gktitle{num}.tex", jp, ko, "chapter", "章タイトル", max_size=32, min_size=17))

    stage_values = dict(STAGES)
    for base, aliases in STAGE_ALIASES.items():
        for alias in aliases:
            stage_values[alias] = STAGES[base]
    for stem, (jp, ko) in stage_values.items():
        specs.append(Spec(f"{stem}.tex", jp, ko, "text", "戦闘結果", align="left", max_size=17, min_size=10))

    # gmblk_btn* are block-selection artwork. Do not infer Japanese text from
    # the resource name/path: these were previously false-positive translation
    # targets (for example block_00e4a800.png). Keep the original texture.

    # gmplc_btn* are map-selection polygon artwork, not label textures. The
    # actual Japanese room names live in the gmplc_pop* popup textures.
    for key, (jp, ko) in MAP_LABELS.items():
        name = f"gmplc_pop{key}.tex"
        if name in BY_NAME:
            specs.append(Spec(name, jp, ko, "preserve", "艦内移動", max_size=14, min_size=8, note="map-label"))

    # The 8-glyph title on the score-ranking screen is スコアランキング;
    # the screen path and the executable's RANKING/ランキング順位 strings
    # independently corroborate the pixel-count reading.
    if "grttl0.tex" in BY_NAME:
        specs.append(Spec("grttl0.tex", "スコアランキング", "스코어 랭킹", "text", "スコアランキング", max_size=22, min_size=11))

    for num, (jp, ko) in ROUTE_NAMES.items():
        for prefix, path_hint, box, size in (
            ("grroute", "スコアランキング", (3, 3, 157, 45), 18),
            ("gproute", "戦闘結果", (0, 0, 112, 20), 15),
        ):
            name = f"{prefix}{num}.tex"
            if name in BY_NAME:
                specs.append(Spec(name, jp, ko, "preserve", path_hint, max_size=size, min_size=9, box=box))

    for (stem, path_hint), (jp, ko) in GENERIC_BUTTONS.items():
        for suffix in ("", "f", "b", "l"):
            name = f"{stem}{suffix}.tex"
            if name in BY_NAME:
                specs.append(Spec(name, jp, ko, "preserve", path_hint, max_size=17, min_size=9))
    return specs


def validate_output(source: Path, output: Path) -> None:
    a = Image.open(source).convert("RGBA")
    b = Image.open(output).convert("RGBA")
    if a.size != b.size:
        raise ValueError(f"size mismatch {output}: {b.size} != {a.size}")


def main() -> None:
    ORIGINAL_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    specs = build_specs()
    chapter_bg = chapter_background()
    report = []
    seen_outputs: set[str] = set()

    for index, spec in enumerate(specs, 1):
        resource, source = resolve_resource(spec)
        output_name = legacy_name(resource)
        original_output = ORIGINAL_DIR / output_name
        output = OUT_DIR / output_name
        if output_name in seen_outputs:
            # Identical resource references should not happen in the manifest.
            raise ValueError(f"duplicate output target: {output_name}")
        seen_outputs.add(output_name)

        try:
            if spec.mode == "text":
                rendered = render_text_only(source, spec.ko, spec.align, spec.max_size, spec.min_size)
            elif spec.mode == "banded":
                rendered = render_banded_text(source, spec.ko.split("\n"), spec.max_size, spec.min_size)
            elif spec.mode == "preserve":
                mask_source = None
                if spec.resource_name.endswith("b.tex"):
                    normal_name = spec.resource_name[:-5] + ".tex"
                    normal_matches = BY_NAME.get(normal_name, [])
                    if spec.path_contains:
                        normal_matches = [r for r in normal_matches if spec.path_contains.lower() in (r.get("path") or "").lower()]
                    if len(normal_matches) == 1 and normal_matches[0].get("images"):
                        mask_source = FULL_PNG / Path(normal_matches[0]["images"][0]["png"])
                rendered = render_preserve(source, spec.ko, spec.box, spec.max_size, spec.min_size, mask_source)
            elif spec.mode == "chapter":
                rendered = render_chapter(source, spec.ko, spec.max_size, spec.min_size, chapter_bg)
            else:
                raise ValueError(spec.mode)
        except ValueError as exc:
            if spec.note == "map-label":
                report.append({
                    **asdict(spec),
                    "resource_path": resource.get("path"),
                    "resource_offset": resource["offset"],
                    "source_png": str(source.relative_to(FULL_PNG)).replace("\\", "/"),
                    "output_png": None,
                    "status": "pending-map-render",
                    "error": str(exc),
                })
                continue
            raise

        # Keep the untouched Japanese source next to the localized image using
        # the exact same legacy filename.  This makes manual review a simple
        # png/block_XXXXXXXX.png <-> translated_png/block_XXXXXXXX.png pair.
        shutil.copy2(source, original_output)
        rendered.save(output)
        validate_output(source, output)
        report.append({
            **asdict(spec),
            "resource_path": resource.get("path"),
            "resource_offset": resource["offset"],
            "source_png": str(source.relative_to(FULL_PNG)).replace("\\", "/"),
            "original_png": output_name,
            "output_png": output_name,
            "size": list(rendered.size),
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        })
        if index % 50 == 0:
            print(f"rendered {index}/{len(specs)}", flush=True)

    # Keep both review folders exact: remove files from earlier trial/spec
    # revisions that are no longer part of the deterministic translation set.
    current_outputs = {item["output_png"] for item in report if item.get("output_png")}
    stale = []
    for review_dir in (ORIGINAL_DIR, OUT_DIR):
        for existing in review_dir.glob("*.png"):
            if existing.name not in current_outputs:
                stale.append(str(existing.relative_to(OUT_ROOT)).replace("\\", "/"))
                existing.unlink()

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    DRAFT_PATH.write_text(json.dumps([asdict(s) for s in specs], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    REPORT_PATH.write_text(json.dumps({"schema": "moonlit-lovers-gadat032-localization/v1", "count": len(report), "stale_removed": stale, "images": report}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"synced {len(current_outputs)} Japanese originals -> {ORIGINAL_DIR}")
    print(f"rendered {len(current_outputs)} localized images -> {OUT_DIR}")
    if stale:
        print(f"removed {len(stale)} stale translated_png files")


if __name__ == "__main__":
    main()
