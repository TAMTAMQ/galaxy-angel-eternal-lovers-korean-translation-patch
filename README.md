# 갤럭시 엔젤 이터널 러버즈 한국어 번역 패치

> 🙏 이 패치는 **완벽한 번역을 기대하시는 분보다는, AI 번역 기반이라 발생할 수 있는 사소한 오역이나 어색한 표현이 있어도 크게 개의치 않고 플레이하실 수 있는 분들을 위한 패치**입니다.

> ⚠️ **완역 패치가 아닙니다.** 본편 대사·선택지·시스템 문구·대부분의 UI 이미지는 번역되어 있지만, 전투 화면의 일부 조작 안내 문구와 아직 찾지 못한 그래픽 문구는 원문으로 남아 있습니다. 자세한 내용은 "3. 패치 내용" 참고.

PlayStation 2용 『ギャラクシーエンジェル エターナルラヴァーズ』 일본판의 비공식 팬 한국어 번역 패치입니다.

- 본편 시나리오 스크립트(ISB 바이트코드) **36,198개 번역 단위**를 134개 리소스에 삽입합니다.
- 그 밖의 텍스트(전투 메시지, 스테이지 테이블, 화자 이름표, 장 타이틀 등) **약 59만 개 문자열 위치**를 처리합니다.
- 완성형 한글 1,251자를 게임 실행 파일에 내장된 폰트 테이블에 새로 그려 넣는 방식으로 한글을 표시합니다.

> 💬 오역, 미번역 문구, 버그를 발견하시면 [Issues 탭](../../issues)에 제보해 주세요.

### 프로젝트 담당자

| 역할 | 담당 |
|---|---|
| 번역 | gemma-4-26b-a4b-it-qat |
| 검수 | Claude Opus 5, 나 |

## 1. 원본 확인

아래 **일본판 ISO와 정확히 일치하는 원본**에만 적용됩니다. 다른 리전·리비전이나 이미 수정된 ISO에는 적용하지 마세요.

| 항목 | 값 |
| --- | --- |
| ISO 크기 | `4,674,766,848 bytes` |
| MD5 | `8f008d85e4bec02d78feed9991017a7e` |
| SHA-1 | `a050714f292bd980f1c9b6927607144a4c7cf12c` |
| SHA-256 | `31cb2a0b6a219323ea8fc451050a75f06fc0947fb0ff33b182835adf7b6da25d` |

패치 적용 결과는 아래와 같아야 합니다.

| 항목 | 값 |
| --- | --- |
| ISO 크기 | `4,674,766,848 bytes` |
| SHA-256 | `89316dff27abacbdf2d819001199d6aab2ff2ca32507300ddc354c6c2f811dce` |

## 2. 패치 적용

1. [Releases](../../releases)에서 `galaxy_angel_eternal_lovers_ps2_kr_v0.1.xdelta`를 받습니다.
2. xdelta3 또는 xdelta 패치를 지원하는 프로그램(예: Delta Patcher)에서 **원본 ISO를 Source로** 지정해 적용합니다.

   ```bash
   xdelta3 -d -s "Galaxy Angel - Eternal Lovers (Japan).iso" \
       galaxy_angel_eternal_lovers_ps2_kr_v0.1.xdelta \
       "Galaxy_Angel_Eternal_Lovers_KO_v0.1.iso"
   ```

3. 결과 ISO의 SHA-256이 위 값과 같은지 확인하세요.

패치 파일 자체의 SHA-256은 `4435549aa8e8915a374b24ed37c842bd4323275c1b4229b3807ce61b17d209bd` 입니다.

원본 게임 파일(ISO, BIOS 등)은 이 저장소에 포함되어 있지 않습니다. 정당하게 소유한 정품 이미지에만 적용하세요.

## 3. 패치 내용

| 영역 | 리소스 | 분량 |
|---|---|---|
| 본편 시나리오 (SCENARIO, ISB 바이트코드) | 134개 | 36,198 단위 / 58,215 표시 줄 |
| 전투 메시지·유닛/무기 이름 (SLG) | 424개 | 213,189 문자열 위치 |
| 스테이지 테이블 (SLGSTAGE) | 1,258개 | 376,164 문자열 위치 |
| 화자 이름표·장 타이틀·스테이지명·무비명 (GADAT000 `adv_string.tbl`) | 3개 | 191 문자열 |
| 함내 안내·시스템 문구 (SCENARIO, SLGRES, ADV) | 25개 | 675 문자열 |
| UI·함내 명판 이미지 (GADAT030 / GADAT032 / SLG) | — | 695장 |
| 전투 뱅크 이미지 사본 (SLGRES / SLGSTAGE) | — | 1,365장 |

- 시나리오 블록은 `ADV.DAT` 안에도 런타임 사본이 있어, 30개 블록과 문자열 테이블 사본까지 함께 반영합니다(안 하면 브리핑 화면만 일본어로 남습니다).
- 폰트는 실행 파일 `SLPM_658.78`의 24×24 2bpp paired 글리프 테이블에 한글 1,251자를 얹습니다.

### 아직 번역 안 된 것 (알려진 미해결 항목)

- 그래픽으로 그려진 일부 일본어 중 아직 찾지 못한 것.
- 개발용 더미 문자열(`★ダミー★`), 리소스 경로, 데이터 파일의 `;`·`//` 주석은 화면에 나오지 않아 의도적으로 제외했습니다.

전에 이 목록에 있던 **전투 화면 조작 안내 문구는 번역했습니다.** `dat/slg/table/unit/spaparam.tbl`의
39종(카메라·패널 토글의 ON 쪽, 선회/횡전 라벨, 이탈 기동 메시지, 키 설정 화면)이 빠져 있었는데,
OFF 쪽만 등록돼 있고 짝이 되는 ON 쪽이 통째로 누락된 상태였습니다. 표기는 이미 들어가 있던
OFF 쪽에 맞췄습니다(`F2:카메라 스무스 OFF` ↔ `F2:카메라 스무스 ON`).

스테이지 목표 문구도 같은 이유로 7종 168곳이 일본어로 남아 있어 함께 번역했습니다. 두 항목 모두
완성된 ISO에서 해당 리소스를 다시 디코드해 일본어가 남지 않았음을 확인했습니다.

## 4. 직접 빌드하기

**요구 사항**: Python 3, `pip install pillow numpy opencv-python pyxdelta`

`assets/translation/`과 `tools/`만으로는 바로 빌드되지 않습니다. 원본 ISO에서 추출한 데이터가 함께 필요하며, 저작권이 있는 원본 데이터라 저장소에 포함되어 있지 않습니다.

| 항목 | 출처 |
| --- | --- |
| `tools/`, `assets/translation/` | 이 저장소 |
| `source/`, `assets/full_extraction/`, `assets/image_extraction/*/png/` | **직접 준비** — 정품 ISO에서 추출 |
| 원본 일본판 ISO | **직접 준비** |

```bash
python tools/eternal_lovers_build_images.py \
    --original-iso "Galaxy Angel - Eternal Lovers (Japan).iso" \
    --output-iso build/Galaxy_Angel_Eternal_Lovers_KO.iso
```

한 번의 실행으로 이미지 렌더 → 한글 폰트 ELF → ISB 삽입 → backing 영역 확보 → 이미지 패치 → 잔여 텍스트 → SCENARIO 재삽입 → ADV 런타임 사본 → 전투 뱅크 이미지 → 검증까지 수행합니다.

배포용 패치 생성과, **그 패치를 적용해 최종 ISO를 만드는 것**은 아래와 같이 합니다.

```bash
python tools/eternal_lovers_make_release.py \
    --original-iso "Galaxy Angel - Eternal Lovers (Japan).iso" \
    --patched-iso build/Galaxy_Angel_Eternal_Lovers_KO.iso \
    --release-dir release --version v0.1 \
    --title "Galaxy Angel - Eternal Lovers" --slug galaxy_angel_eternal_lovers

python tools/galaxy_angel_apply_release_patch.py \
    --original-iso "Galaxy Angel - Eternal Lovers (Japan).iso" \
    --release-dir release \
    --output-iso release/Galaxy_Angel_Eternal_Lovers_KO_v0.1.iso
```

`galaxy_angel_apply_release_patch.py`는 원본 ISO 해시를 `release.json`과 대조하고, 적용 결과의 해시·크기까지 다시 확인합니다.

### 검증

```bash
python tools/eternal_lovers_verify_iso_isb.py       # ISB 134개 리소스 전수 대조
python tools/eternal_lovers_verify_remaining.py     # 비-ISB 문자열 전수 대조
python tools/eternal_lovers_verify_battle_bank_images.py
```

## 5. 개발 내역

- **`.isb` 시나리오 포맷 (ISL 2.0 / STX 컴파일러)**: 실행 파일의 MIPS 코드를 정적으로 디스어셈블해 문자열 복호 루프(`0x0025C158`~`0x0025C178`)를 찾았습니다. 32비트 워드마다 `ROR32(word,3) XOR block_key`이며, 블록 키는 파일 첫 워드 또는 블록 앞의 정수 리터럴에 있습니다. 인접 문자열 군의 CP932 복호 품질을 비교해 키 경계를 자동 복구합니다(`tools/eternal_lovers_isb_static.py`).

- **문장 단위 그룹화**: 문자열 레코드는 물리적으로 붙어 있어도 별개 문자열일 수 있습니다. STX 컴파일러가 인자를 연달아 배치하기 때문입니다. 문장 앞 4바이트 함수 토큰(블록 키로 암호화되지 않은 고정 해시)으로 호출 종류를 구분합니다 — `084368c5` 메시지(1~3줄), `9f455280` 선택지(레코드 1개당 선택지 1개), `af0e22af` 타이틀, `a7074316` 음성. 이걸 무시하면 선택지 두 개가 한 문장을 반씩 나눠 갖게 됩니다(`tools/eternal_lovers_regroup_isb.py`).

- **고정 슬롯과 진행 정지**: 한 줄이 고정 바이트 슬롯이고 일본어 원본은 58,215개 슬롯을 마지막 바이트까지 채웁니다. 번역이 짧아 생기는 여백을 잘못 채우면 **텍스트 표시가 끝나지 않아 A버튼 진행이 막힙니다**(스킵은 됨). 두 가지가 원인이었습니다.
  - 줄 전체가 공백이 되면 정지 → 슬롯 크기에 **비례 배분**해 빈 줄을 없앰
  - 줄이 **반각 공백(`0xA0`)으로 시작**하면 정지 → 원문의 전각 공백은 전각 그대로 인코딩하고, 줄바꿈 때문에 앞으로 밀린 공백은 버림

- **컨테이너 고정 베이스**: 1편에서 스크립트 엔진이 컨테이너의 원래 LBA를 고정 베이스로 쓰는 것이 확인되어, 이 게임도 `SCENARIO.DAT`·`SLG.DAT`를 원래 extent에 고정합니다. 다만 PIDX 오프셋이 **컨테이너 기준 32비트**라 4.7GB 디스크에서는 끝에 붙인 backing 사본에 닿지 않습니다. 그래서 스크립트 엔진이 컨테이너 오프셋으로 접근하지 않는 무비 스트림 하나를 디스크 끝으로 옮겨 사정거리 안에 backing 영역을 확보합니다(`tools/eternal_lovers_reserve_backing_region.py`). 결과적으로 원본 대비 extent가 바뀌는 파일은 그 무비 하나뿐입니다.

- **`ADV.DAT` 런타임 사본**: ADV 프레임의 파일 캐시로, 시나리오 스크립트 30블록과 `adv_string.tbl`의 바이트 동일 사본을 갖고 있습니다. 세이브스테이트의 EE 메모리를 떠서 확인한 결과, 임무 브리핑 블록은 실제로 이쪽에서 로드됩니다. FSTS 슬롯에 안 들어가면 뱅크를 재패킹합니다(`tools/eternal_lovers_patch_adv_runtime.py`).

- **화자 이름표**: `dat/gadat000/adv_string.tbl`의 `[SPEAKER]` 섹션이 텍스트 감사에서 통째로 누락돼 있었습니다. 정지 지점 세이브스테이트의 EE 메모리에서 찾아 역추적했고, 같은 파일의 장 타이틀·스테이지명·무비명까지 함께 편입했습니다(`tools/eternal_lovers_extract_adv_string_table.py`).

- **상속 이미지**: 픽셀 해시가 같다는 이유로 Moonlit Lovers의 렌더를 가져오면, 문구는 맞아도 **배치가 이 디스크의 원본과 다를 수 있습니다**. 세이브 카드의 「데이터 삭제」 라벨이 7px 어긋나고 버튼 아이콘을 침범했습니다. 해당 라벨은 원본 글자 상자에 맞춰 한 디자인으로 다시 그립니다(`tools/eternal_lovers_redraw_delete_labels.py`).

자세한 시간순 기록은 [STATUS.md](STATUS.md)에 있습니다.

## 6. 저장소 구성

- `tools/` — 추출·번역 반영·폰트 생성·검증용 파이썬 스크립트
- `assets/translation/` — 번역 텍스트 (이 폴더를 편집하는 것이 번역 작업의 전부입니다)
- `STATUS.md` — 개발 기록

저장소에 포함되지 않는 것 (`.gitignore` 참고, 직접 빌드 시 원본 디스크에서 재추출):

- `source/`, `assets/full_extraction/` — 원본 게임에서 추출한 바이너리
- `assets/image_extraction/*/png/`, `translated_png/` — 추출·번역 이미지
- `build/`, `release/` — 빌드 결과물 (패치는 Releases에 올립니다)

## 7. 라이선스 / 권리

**누구나 자유롭게 사용하셔도 됩니다.** 별도의 허락을 구할 필요 없이, 아래 조건(비영리 팬 번역 목적, 정품 게임 소유)만 지켜주시면 내려받아 적용하거나 포크해서 참고·수정하실 수 있습니다.

- **도구 소스코드** (`tools/*.py`): [MIT 라이선스](LICENSE)
- **번역 텍스트** (`assets/translation/`): 비영리 팬 번역 목적으로 자유롭게 공유·수정하실 수 있으나, 게임을 판매하거나 상업적으로 이용하는 용도로는 사용하지 말아주세요.
- **게임 원본 데이터**: 『갤럭시 엔젤』의 저작권은 BROCCOLI에 있습니다. 이 저장소는 원본 게임 파일(디스크 이미지, 실행 파일, 폰트, 대사 바이너리 등)을 포함하거나 배포하지 않으며, 패치는 반드시 정식 발매된 게임을 정당하게 소유한 상태에서 개인적으로 적용하는 용도로만 사용해주세요.
