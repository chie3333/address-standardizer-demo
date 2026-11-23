"""처음부터 BigQuery로 표준화하고, 결과를 워크벤치 AddressResult 형식으로 내는 워크벤치 어댑터.

LLM 파서·DuckDB 없이 입력 전체를 BigQuery SQL 몇 번으로 처리한다(bigquery/sql/04~06). 결과 행은
AddressResult JSON과 같은 모양이라 워크벤치 화면·승인·내려받기·제출 양식 컬럼(output_fields)을 그대로 쓴다.

    python -m address_workbench.server --adapter address_workbench.bigquery_engine:create_adapter

행마다 채우는 것
  - 판정: 원문과 기준자료가 정확히 맞을 때만 확정한다(SQL 05). 시군구 중 시만 맞거나, 지역 표기가 없거나,
    원문의 읍·면이 다르면 번호가 맞아도 검토로 보낸다.
  - 지역 검증 경로(region_validation)와 원문 토큰(output.tokens): 시도·시군구·읍면동·리·도로명 중 어디까지 맞고
    어디서 안 맞는지.
  - 후보별 구성요소 비교(candidate_comparisons): 시도·시군구·읍면동·리·도로명·건물번호·지번마다 일치/불일치/미확인.
  - 복수 주소: 한 칸에 주소가 여러 개면 조각으로 나눠 각각 매칭하고, 조각별 결과를 후보와 output.address_candidates로 낸다.
  - 도로명·지번 관계(output.road_lot_relation): 1:N(한 지번에 건물 여러 개), N:1(한 건물이 여러 필지), N:M.

판정 대응
    확정(코드 3개)  → resolved        확정(코드 일부 빈칸) → address_only    지번만 확정 → lot_only
    검토 필요       → held            미일치               → no_match        입력 없음   → excluded(blank_input)

준비: `pip install -r address_workbench/requirements-bigquery.txt`, `gcloud auth application-default login`,
기준표(bigquery/sql/03) 생성. 프로젝트·데이터세트는 bigquery_stage와 같은 환경 변수(ADDRESS_BQ_*)를 쓴다.
"""
from __future__ import annotations

import html
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from time import perf_counter
from typing import Iterable, Mapping

from .adapter import AdapterResult, Comparison, InputRecord, Progress
from .bigquery_stage import BigQueryStage, Settings
from .old_regions import OLD_REGIONS, drop_repeated_block, rename_old_regions  # noqa: F401 - OLD_REGIONS는 엔진 이름으로도 쓴다

REFERENCE_YEAR = 2025
ENGINE = "bigquery_sql"
ENGINE_VERSION = "2"

STATUS = {"지번만 확정": "lot_only", "검토 필요": "held", "미일치": "no_match", "입력 없음": "excluded"}
# BigQuery 사유 코드 → 파이프라인 검토 유형(review_type). 화면의 검토 유형 집계와 같은 이름을 쓴다.
REVIEW_TYPES = {
    "sub_number_omitted": "부번 생략",
    "sub_number_mismatch": "명시된 부번 불일치",
    "number_not_found": "기준연도 번호 미일치",
    "missing_address_number": "번호 부족·건물명/상세만",
    "scope_not_found": "지역명 미일치·유사 지명 후보",
    "region_partial": "지역명 미일치·유사 지명 후보",
    "multiple_addresses": "기타·복수 주소/필지",
    "multiple_road_addresses": "한 지번에 여러 도로명주소",
    "road_lot_conflict": "도로명·지번 병기",
    "multiple_standard_outputs": "기타·지역/번호 해석 부족",
}
VERIFIED = "기본 도로명주소 확인"
MULTI = "기타·복수 주소/필지"

# 시도: (정식 이름, 약칭, 다른 표기). SQL 05의 sido_map과 같다.
SIDO = (
    ("서울특별시", "서울", "서울시"), ("부산광역시", "부산", "부산시"), ("대구광역시", "대구", "대구시"),
    ("인천광역시", "인천", "인천시"), ("광주광역시", "광주", "광주광역시"), ("대전광역시", "대전", "대전시"),
    ("울산광역시", "울산", "울산시"), ("세종특별자치시", "세종", "세종시"), ("경기도", "경기", "경기도"),
    ("강원특별자치도", "강원", "강원도"), ("충청북도", "충북", "충청북도"), ("충청남도", "충남", "충청남도"),
    ("전북특별자치도", "전북", "전라북도"), ("전라남도", "전남", "전라남도"), ("경상북도", "경북", "경상북도"),
    ("경상남도", "경남", "경상남도"), ("제주특별자치도", "제주", "제주도"),
)
SIDO_FORMS = {form: full for full, *rest in SIDO for form in (full, *rest)}
LEVELS = ("시도", "시군구", "읍면동", "리", "도로명")

_UNIT = re.compile(r"(?:^|[\s,(])((?:지하\s*)?[0-9]+)\s*(동|층|호)(?![가-힣])")
_MARKER = re.compile(r"우편물(?:\s*수령지)?\s*:?|우편\s*[:(]|본점\s*:?|지점\s*:|[가-힣]{0,6}센터\s*:|\(?주소\s*:")
# 주소 모양: 이름(…로/길/리/동/가/읍/면) 뒤에 건물번호·지번이 온다. 도로명 안의 숫자(경원로11번길, 청계천로10나길)는 번호가 아니다.
_ADDRESS_LIKE = re.compile(r"[가-힣0-9](?:로|길|리|동|가|읍|면)\s*(?:[0-9]+번?[가-힣]?길\s*)?(?:산\s*)?[0-9]+(?![0-9]*\s*(?:번|[가-힣]?길))")
_SIGUNGU_WORD = re.compile(r"[가-힣]{1,5}[시군]")
_REGION_WORD = re.compile(r"[가-힣][가-힣0-9]*?(?:특별자치도|특별자치시|특별시|광역시|도|시|군|구|읍|면|동|리|가|로|길)")


# ---------------------------------------------------------------- 원문 다루기

def compact(text: str | None) -> str:
    """공백을 지운 원문. 띄어 쓴 숫자(197 8층)는 붙지 않게 '|'로 경계를 남긴다."""
    text = html.unescape(unicodedata.normalize("NFKC", text or ""))
    return re.sub(r"\s+", "", re.sub(r"([0-9])\s+(?=[0-9])", r"\1|", text))


def detail_units(text: str | None) -> list[str]:
    """원문의 동·층·호 표기(101동 203호, 지하 2층)를 하나씩만 있을 때 남긴다. 행정동(일도1동)은 앞 글자로 거른다."""
    found = defaultdict(list)
    for number, label in _UNIT.findall(text or ""):
        found[label].append(re.sub(r"\s+", " ", number) + label)
    return [found[label][0] for label in "동층호" if len(found[label]) == 1]


def region_led_building(word: str) -> bool:
    """지역 이름으로 시작하지만 뒤에 지역이 아닌 이름이 붙은 낱말인지(대전광역시청, 공주시청, 경상남도교육청).

    앞의 지역 이름을 떼고 남는 부분이 또 지역·도로 이름(경상남도사천시, 신수동신수서길)이거나 번호(둔산동352-42)면
    건물명이 아니다."""
    core = word.strip(",.()")
    pos = 0
    for name, _ in stated_words(core):
        if not core.startswith(name, pos):
            break
        pos += len(name)
    rest = core[pos:]
    return (0 < pos and bool(re.match(r"[가-힣A-Za-z]", rest))
            and not re.fullmatch(r"[가-힣0-9]*(?:로|길|동|리|가|읍|면|시|군|구)", rest))


def _region_key(word: str) -> str | None:
    word = word.strip(",.()")
    if region_led_building(word):        # 대전광역시청은 시도가 다시 나온 것이 아니다(복수 주소로 나누지 않음)
        return None
    for form, full in SIDO_FORMS.items():
        if word == form or (word.startswith(full) and len(word) > len(full)):
            return "시도:" + full
    if _SIGUNGU_WORD.fullmatch(word):
        return "시군구:" + word
    return None


_EN_SIDO = {
    "seoul": "서울특별시", "busan": "부산광역시", "daegu": "대구광역시", "incheon": "인천광역시", "gwangju": "광주광역시",
    "daejeon": "대전광역시", "ulsan": "울산광역시", "sejong": "세종특별자치시", "gyeonggi": "경기도", "gangwon": "강원특별자치도",
    "chungbuk": "충청북도", "chungcheongbuk": "충청북도", "chungnam": "충청남도", "chungcheongnam": "충청남도",
    "jeonbuk": "전북특별자치도", "jeollabuk": "전북특별자치도", "jeonnam": "전라남도", "jeollanam": "전라남도",
    "gyeongbuk": "경상북도", "gyeongsangbuk": "경상북도", "gyeongnam": "경상남도", "gyeongsangnam": "경상남도",
    "jeju": "제주특별자치도",
}
_SIDO_FULL = tuple(full for full, *_ in SIDO)


def _fix_word(word: str) -> str:
    """영문 시도(Seoul, Chungnam)는 한글 정식 이름으로, 시도 정식 이름과 한 글자만 다른 표기(경상남독)는 그 이름으로 바꾼다."""
    key = re.sub(r"[^a-z]", "", word.lower())
    if key:
        key = key[:-2] if key.endswith("do") and key[:-2] in _EN_SIDO else key
        return _EN_SIDO.get(key, word)
    if word in SIDO_FORMS or len(word) < 4:
        return word
    near = [full for full in _SIDO_FULL if len(full) == len(word) and sum(a != b for a, b in zip(full, word)) == 1]
    return near[0] if len(near) == 1 else word


def _word_class(word: str) -> int:
    """어순 정리용 단계: 0 시도, 1 시군구, 2 읍면동, 3 리, 4 도로명, 5 번호, 6 그 밖(상세·건물명)."""
    core = word.strip(",.()")
    key = _region_key(core)
    if key and key.startswith("시도:"):
        return 0
    if re.fullmatch(r"(?:산)?[0-9][0-9-]*(?:번지)?(?:[0-9]+호)?", core):
        return 5
    if re.fullmatch(r"[가-힣]+[시군구]", core):
        return 1
    if re.fullmatch(r"[가-힣]+[0-9]*[읍면동가]", core):
        return 2
    if re.fullmatch(r"[가-힣]+[0-9]*리", core):
        return 3
    if re.fullmatch(r"[가-힣0-9·]+(?:로|길|거리)", core):
        return 4
    return 6


def reorder(text: str) -> str:
    """역순(141-1 상촌길 … 경상남도)이나 뒤섞인 어순(김해시 경원로11번길 경상남도 내동 17-10)을
    시도 → 시군구 → 읍면동 → 리 → 도로명 → 번호 → 상세 순으로 다시 놓는다. 시도가 맨 앞이 아니거나
    번호로 시작할 때만 바꾸고, 정상 어순(지번·도로명 병기 포함)은 그대로 둔다."""
    words = text.split()
    classes = [_word_class(word) for word in words]
    ordered = [c for c in classes if c != 6]
    if not ordered or not (ordered[0] == 5 or (0 in ordered and ordered.index(0) > 0)):
        return text
    return " ".join(word for _, _, word in sorted(zip(classes, range(len(words)), words)))


def prepare(text: str | None) -> list[str]:
    """매칭 전에 원문을 정리하고 주소 조각으로 나눈다. 원문 자체는 바꾸지 않고 정리한 사본을 돌려준다.

    옛 행정구역 이름 바꾸기(연기군 → 세종특별자치시) → 영문·오타 시도 이름 바꾸기 → 바로 이어서 반복된 지역 이름
    하나로(부산광역시 부산광역시) → 복수 주소 나누기 → 조각마다 어순 정리.
    """
    if not text or not text.strip():
        return []
    lines = []
    for line in html.unescape(unicodedata.normalize("NFKC", rename_old_regions(text)[0])).splitlines():
        words: list[str] = []
        for word in map(_fix_word, line.split()):
            if words and words[-1] == word and _region_key(word):
                continue
            words.append(word)
        lines.append(" ".join(drop_repeated_block(words)))
    return [reorder(segment) for segment in split_segments("\n".join(lines))]


def split_segments(text: str | None) -> list[str]:
    """한 칸에 적힌 주소를 조각으로 나눈다. 주소 모양(…로/길/리/동 + 번호)이 둘 이상일 때만 여러 조각이 된다.

    나누는 곳: 줄바꿈, '우편물·본점·주소:' 같은 표시, 번호가 나온 뒤 시도가 다시 나오거나 같은 시·군 이름이 다시 나오는 곳.
    주소 모양이 없는 조각(11층, 담당자 이름 등)은 앞 조각에 붙인다.
    """
    if not text or not text.strip():
        return []
    pieces: list[str] = []
    for line in re.split(r"[\r\n]+", _MARKER.sub("\n", html.unescape(text))):
        current: list[str] = []
        seen: set[str] = set()
        for word in line.split():
            key = _region_key(word)
            has_address = bool(_ADDRESS_LIKE.search(compact(" ".join(current))))
            if key and current and has_address and (key in seen or key.startswith("시도:")):
                pieces.append(" ".join(current))
                current, seen = [], set()
            current.append(word)
            if key:
                seen.add(key)
        if current:
            pieces.append(" ".join(current))
    segments: list[str] = []
    for piece in (p.strip(" ,/-") for p in pieces):
        if not piece:
            continue
        if _ADDRESS_LIKE.search(compact(piece)) or not segments:
            segments.append(piece)
        else:
            segments[-1] = f"{segments[-1]} {piece}"
    # 첫 조각이 주소 모양이 아니면(앞쪽 머리말) 다음 조각에 붙인다.
    if len(segments) > 1 and not _ADDRESS_LIKE.search(compact(segments[0])):
        segments[1] = f"{segments[0]} {segments[1]}"
        segments.pop(0)
    return segments


# ---------------------------------------------------------------- 지역 구성요소 비교

def _stems(name: str) -> list[str]:
    """시군구·읍면동 이름의 원문 표기 후보(정식 이름, 끝 글자 뺀 이름)."""
    out = [name]
    if len(name) >= 3 and name[-1] in "시군구읍면동":
        out.append(name[:-1])
    return out


def spaced(text: str | None) -> str:
    return html.unescape(unicodedata.normalize("NFKC", text or ""))


def match_level(text: str | None, level: str, value: str | None) -> str | None:
    """원문에서 이 단계 이름의 표기를 찾는다. 정식 이름(띄어쓰기 무시)과 시도 약칭은 어디서든,
    끝 글자를 뺀 이름(정읍, 삼남)은 앞뒤가 한글이 아닌 독립된 낱말일 때만 받는다(동현새터길의 '동현' ≠ 동현동)."""
    if not value:
        return None
    flat = compact(text)
    full = [f for f in _level_forms(level, value) if level == "시도" or f in (compact(value), *value.split())]
    if found := _find(flat, full):
        return found
    for stem in sorted(set(_level_forms(level, value)) - set(full), key=len, reverse=True):
        if re.search(rf"(?<![가-힣]){re.escape(stem)}(?![가-힣])", spaced(text)):
            return stem
    return None


def _find(text: str, forms: Iterable[str]) -> str | None:
    for form in sorted({f for f in forms if f}, key=len, reverse=True):
        if form in text:
            return form
    return None


def _level_forms(level: str, value: str | None) -> list[str]:
    if not value:
        return []
    if level == "시도":
        return [form for form, full in SIDO_FORMS.items() if full == value]
    if level == "시군구":
        parts = value.split()
        return [compact(value), *(stem for part in parts for stem in _stems(part))]
    if level == "읍면동":
        return _stems(value)
    return [compact(value)]


_SIDO_TAILS = ("특별자치도", "특별자치시", "특별시", "광역시")


def _level_of(name: str) -> str | None:
    if name in _SIDO_TAILS:
        return None
    suffix = name[-1]
    if name in SIDO_FORMS or name.endswith(_SIDO_TAILS):
        return "시도"
    if suffix == "도":
        return None
    if suffix in "시군구":
        return "시군구"
    if suffix in "읍면동가":
        return "읍면동"
    return "리" if suffix == "리" else "도로명"


def _cut(piece: str) -> list[tuple[str, str]]:
    """띄어쓰기 없는 조각을 이름 단위로 자른다. 가장 짧게 자르되, 이름 끝(뒤가 한글이 아님)에서는
    끝 글자를 이어 붙인다(정읍+시, 섶자리+길)."""
    out, pos = [], 0
    while (match := _REGION_WORD.match(piece, pos)) is not None:
        end = match.end()
        while end < len(piece) and piece[end] in "시군구읍면동리가로길" and (
                end + 1 == len(piece) or not "가" <= piece[end + 1] <= "힣"):
            end += 1
        pos = end
        name = piece[match.start():end]
        if (level := _level_of(name)) is not None:
            out.append((name, level))
    return out


def stated_words(text: str | None, known: Iterable[tuple[str, str]] = ()) -> list[tuple[str, str]]:
    """원문에 적힌 지역·도로 이름 조각과 그 단계(끝 글자 기준), 원문 순서대로.

    known(기준자료에서 찾은 이름과 단계)을 먼저 떼어 내고 남은 부분을 자른다. 띄어쓰기 없는 원문
    (충청남도공주시동현새터길68)도 이름 경계가 맞게 나뉜다. 아파트 동(101동)처럼 숫자로 시작하는 조각과
    섬 이름 같은 '…도'는 뺀다.
    """
    known = sorted({(compact(name), level) for name, level in known if name}, key=lambda item: -len(item[0]))
    out = []
    for word in re.split(r"[\s,()/:]+", html.unescape(unicodedata.normalize("NFKC", text or ""))):
        found = []
        for name, level in known:
            start = 0
            while (at := word.find(name, start)) >= 0:
                if not any(a < at + len(name) and at < b for a, b, *_ in found):
                    found.append((at, at + len(name), name, level))
                start = at + 1
        found.sort()
        pos = 0
        for start, stop, name, level in found:
            out.extend(_cut(word[pos:start]))
            out.append((name, level))
            pos = stop
        out.extend(_cut(word[pos:]))
    return out


@dataclass(frozen=True)
class Place:
    """후보 하나의 지역 구성요소. 도로명 후보와 지번 후보 모두 이 모양으로 비교한다."""
    kind: str
    address: str
    sido: str | None = None
    sigungu: str | None = None
    emd: str | None = None
    ri: str | None = None
    road: str | None = None
    main: int | None = None
    sub: int | None = None
    mountain: bool = False
    legal_dong_code: str | None = None
    road_name_code: str | None = None
    admin_dongs: tuple[str, ...] = ()
    emds: tuple[str, ...] = ()   # 건물이 걸친 법정동(읍면동) 전부
    ris: tuple[str, ...] = ()    # 건물이 걸친 리 전부

    @classmethod
    def from_road(cls, item: Mapping) -> "Place":
        return cls("road", item.get("address") or "", item.get("sido"), item.get("sigungu"), item.get("emd"),
                   item.get("ri"), item.get("road_name"), _int(item.get("main_no")), _int(item.get("sub_no")),
                   False, item.get("legal_dong_code"), item.get("road_name_code"), tuple(item.get("admin_dongs") or ()),
                   tuple(item.get("emds") or ()), tuple(item.get("ris") or ()))

    @classmethod
    def from_lot(cls, item: Mapping) -> "Place":
        return cls("lot", item.get("lot") or "", item.get("sido"), item.get("sigungu"), item.get("emd"),
                   item.get("ri"), None, _int(item.get("lot_main")), _int(item.get("lot_sub")),
                   str(item.get("mountain")).lower() == "true", item.get("legal_dong_code"))

    def value(self, level: str) -> str | None:
        return {"시도": self.sido, "시군구": self.sigungu, "읍면동": self.emd, "리": self.ri, "도로명": self.road}[level]

    def number(self) -> str:
        if self.main is None:
            return ""
        return ("산 " if self.mountain else "") + (f"{self.main}-{self.sub}" if self.sub else str(self.main))

    def path(self) -> list[dict]:
        code = self.legal_dong_code or ""
        prefixes = {"시도": code[:2], "시군구": code[:5], "읍면동": code[:8], "리": code, "도로명": self.road_name_code or ""}
        return [{"label": level, "name": self.value(level), "code_prefix": prefixes[level] or None, "node_id": None}
                for level in LEVELS if self.value(level)]


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _input_number(text: str, name: str | None, lot: bool) -> str | None:
    """원문에서 이름 바로 뒤의 번호(본번-부번)를 읽는다. 지번은 '산'과 '번지 N호'도 읽는다."""
    if not name:
        return None
    pattern = re.escape(compact(name)) + (r"(산)?([0-9]+)(?:번지)?(?:-([0-9]+)|([0-9]+)호)?" if lot else r"([0-9]+)(?:-([0-9]+))?")
    match = re.search(pattern, text)
    if not match:
        return None
    if lot:
        mountain, main, sub = match.group(1), match.group(2), match.group(3) or match.group(4)
        return ("산 " if mountain else "") + (f"{int(main)}-{int(sub)}" if sub and int(sub) else str(int(main)))
    main, sub = match.group(1), match.group(2)
    return f"{int(main)}-{int(sub)}" if sub and int(sub) else str(int(main))


def compare(text: str | None, place: Place) -> list[Comparison]:
    """원문과 후보 하나를 시도·시군구·읍면동·리·도로명·번호별로 비교한다."""
    flat = compact(text)
    known = [(found, level) for level in LEVELS if (found := match_level(text, level, place.value(level)))]
    stated = defaultdict(list)
    for name, level in stated_words(text, known):
        stated[level].append(name)
    rows: list[Comparison] = []
    levels = [level for level in LEVELS if level != "도로명" or place.kind == "road"]
    for level in levels:
        value = place.value(level)
        if not value:
            continue
        found = match_level(text, level, value)
        parts = value.split()
        if found and level == "시군구" and len(parts) > 1:
            missing = [part for part in parts if match_level(text, level, part) is None]
            if missing and compact(value) not in flat:
                rows.append(Comparison(component=level, input_value=found, candidate_value=value, outcome="unknown",
                                       evidence=f"일부만 일치 · {' '.join(missing)}이(가) 원문에 없음"))
                continue
        if found:
            note = "기준자료 이름 일치" if found in (value, compact(value)) else f"'{found}' → {value}"
            rows.append(Comparison(component=level, input_value=found, candidate_value=value, outcome="match", evidence=note))
            continue
        spans = {"읍면동": place.emds, "리": place.ris}.get(level, ())
        other = next((name for name in spans if name != value and match_level(text, level, name)), None)
        if other:
            rows.append(Comparison(component=level, input_value=match_level(text, level, other), candidate_value=value,
                                   outcome="match", evidence=f"건물이 {', '.join(spans)}에 걸쳐 있음 · {other} 일치"))
            continue
        admin = next((name for name in place.admin_dongs if level == "읍면동" and match_level(text, level, name)), None)
        if admin:
            rows.append(Comparison(component=level, input_value=match_level(text, level, admin), candidate_value=value,
                                   outcome="match", evidence=f"행정동 {admin} 일치 · 법정동 {value}"))
            continue
        others = [name for name in stated[level] if match_level(name, level, value) is None
                  and not any(match_level(name, level, adm) for adm in (*place.admin_dongs, *spans))]
        if others:
            note = "원문의 이름이 기준자료와 다름"
            if level == "읍면동" and place.admin_dongs:
                note += f" (법정동 {value} · 행정동 {', '.join(place.admin_dongs)})"
            rows.append(Comparison(component=level, input_value=", ".join(others), candidate_value=value,
                                   outcome="mismatch", evidence=note))
        else:
            rows.append(Comparison(component=level, input_value=None, candidate_value=value, outcome="unknown",
                                   evidence="원문에 없음 · 기준자료 경로로 보완"))
    label = "건물번호" if place.kind == "road" else "지번"
    anchor = place.road if place.kind == "road" else (place.ri or place.emd)
    given = _input_number(flat, anchor, place.kind == "lot")
    expected = place.number()
    if expected:
        if given is None:
            rows.append(Comparison(component=label, input_value=None, candidate_value=expected, outcome="unknown",
                                   evidence="원문에서 이 이름 뒤의 번호를 찾지 못함"))
        elif given == expected:
            rows.append(Comparison(component=label, input_value=given, candidate_value=expected, outcome="match",
                                   evidence="번호 정확 일치"))
        else:
            same_main = given.split("-")[0] == expected.split("-")[0]
            rows.append(Comparison(component=label, input_value=given, candidate_value=expected, outcome="mismatch",
                                   evidence="본번은 같고 부번이 다름" if same_main else "번호가 다름"))
    return rows


def region_validation(text: str | None, places: list[Place], address_id: str, year: int) -> tuple[dict, list[dict]]:
    """지역 검증 경로와 원문 토큰. 원문에 적힌 이름 중 후보 경로에 없는 것은 미확인으로 남긴다."""
    known = [(found, level) for place in places[:1] for level in LEVELS
             if (found := match_level(text, level, place.value(level)))]
    words = stated_words(text, known)
    tokens = [{"raw": name, "labels": [level]} for name, level in words]
    paths = []
    for place in places:
        path = place.path()
        if path and path not in paths:
            paths.append(path)
    if not paths:
        return ({"address_id": address_id, "reference_year": year, "status": "no_match", "paths": [],
                 "db_labels": [[] for _ in words]}, tokens)
    first = places[0]

    def labels(name: str) -> list[str]:
        """원문 조각이 첫 후보 경로의 어느 단계 이름과 맞는지. 같은 단계 이름의 표기(약칭·끝 글자 생략)도 받는다."""
        return [level for level in LEVELS if match_level(name, level, first.value(level)) == compact(name)]

    db_labels = [labels(name) for name, _ in words]
    unmatched = [i for i, found in enumerate(db_labels) if not found]
    base = {"address_id": address_id, "reference_year": year}
    if unmatched:
        matched = [i for i, found in enumerate(db_labels) if found]
        return ({**base, "status": "no_match", "paths": [], "db_labels": [[] for _ in words],
                 "partial_match": {"matched_tokens": matched, "unmatched_tokens": unmatched, "paths": paths[:1],
                                   "db_labels": [db_labels[i] for i in matched], "alternatives": [], "suggestions": []}},
                tokens)
    return ({**base, "status": "unique_path" if len(paths) == 1 else "multiple_paths", "paths": paths,
             "db_labels": db_labels}, tokens)


def road_lot_relation(roads: dict[str, list[str]]) -> dict | None:
    """도로명주소 → 연결 지번 목록에서 1:N(한 지번에 건물 여러 개)·N:1(한 건물이 여러 필지)·N:M을 가린다."""
    road_set = {road for road in roads if road}
    lot_set = {lot for lots in roads.values() for lot in lots if lot}
    if len(road_set) > 1 and len(lot_set) == 1:
        return {"kind": "1:N", "roads": len(road_set), "lots": 1, "label": f"한 지번에 도로명주소 {len(road_set)}개"}
    if len(road_set) == 1 and len(lot_set) > 1:
        return {"kind": "N:1", "roads": 1, "lots": len(lot_set), "label": f"한 도로명주소에 지번 {len(lot_set)}개"}
    if len(road_set) > 1 and len(lot_set) > 1:
        return {"kind": "N:M", "roads": len(road_set), "lots": len(lot_set),
                "label": f"도로명주소 {len(road_set)}개 · 지번 {len(lot_set)}개"}
    return None


# ---------------------------------------------------------------- 결과 행 만들기

def _standard(item: Mapping | None) -> dict | None:
    if not item or not item.get("road_name_code"):
        return None
    return {
        "standard_road_address": item["address"],
        "road_name_code": item.get("road_name_code") or "",
        "legal_dong_code": item.get("legal_dong_code") or "",
        "administrative_area_code": item.get("administrative_area_code") or "",
    }


def _road_candidate(item: Mapping, evidence: list[str], year: int) -> dict:
    return {
        "kind": "road", "address": item["address"], "rank": 1, "evidence": [line for line in evidence if line],
        "reference_years": [year], "raw_records": [[year, int(serial)] for serial in item.get("serials") or []],
        "linked_road_addresses": [], "linked_lot_addresses": list(item.get("lot_addresses") or []),
        "admin_dongs": list(item.get("admin_dongs") or []), "standard_address": _standard(item),
        "building_name": "", "building_input": "",
    }


def _lot_candidate(item: Mapping, evidence: list[str], year: int) -> dict:
    return {
        "kind": "lot", "address": item.get("lot") or "", "rank": 1, "evidence": [line for line in evidence if line],
        "reference_years": [year], "raw_records": [], "linked_road_addresses": list(item.get("road_addresses") or []),
        "linked_lot_addresses": [], "admin_dongs": [], "standard_address": None, "building_name": "", "building_input": "",
    }


SIBLING_WARNING = "sub_number_siblings"


def sub_number_warning(main: Mapping) -> str | None:
    """부번 없는 주소로 확정했는데 같은 도로·본번에 부번이 붙은 건물이 있으면 주의 문구를 만든다(남부순환로 1756 ↔ 1756-6)."""
    subs = [int(sub) for sub in main.get("sibling_subs") or []]
    if not subs or _int(main.get("sub_no")) not in (0, None):
        return None
    road, number = main.get("road_name") or "", _int(main.get("main_no"))
    shown = ", ".join(f"{number}-{sub}" for sub in subs[:5]) + (f" 외 {len(subs) - 5}개" if len(subs) > 5 else "")
    return f"주의: 같은 본번에 부번이 붙은 건물 {len(subs)}개 있음({road} {shown}) · 원문에서 부번이 빠졌을 수 있음"


@dataclass
class Built:
    row: dict
    comparisons: dict[int, list[Comparison]]


def build_single(row: Mapping, index: int, record: InputRecord, year: int = REFERENCE_YEAR,
                 elapsed_ms: float = 0.0, text: str | None = None) -> Built:
    """BigQuery 결과 한 행(06 조회)을 AddressResult JSON 한 행과 후보별 구성요소 비교로 바꾼다."""
    text = record.raw_address if text is None else text
    sql_status = row.get("status") or "미일치"
    reason = row.get("reason") or ""
    code = row.get("reason_code")
    main = row.get("main_detail") or None
    if sql_status == "확정" and main:
        all_codes = all(main.get(key) for key in ("road_name_code", "legal_dong_code", "administrative_area_code"))
        status = "resolved" if all_codes else "address_only"
    else:
        status = STATUS.get(sql_status, "no_match")

    raw = record.raw_address
    numeric_only = bool(raw and raw.strip() and re.fullmatch(r"[0-9\s.,/-]+", raw.strip()))
    if numeric_only:
        status, code, reason = "excluded", "numeric_only", "숫자만 있는 입력"
    result = {
        "record": {"address_id": record.address_id, "raw_address": raw,
                   "normalized_address": " ".join(text.split()) if text and text.strip() else None},
        "input_index": index, "status": status,
        "output": {"engine": ENGINE, "status": status, "reason_code": code, "reason": reason,
                   "bigquery_status": sql_status, "address_candidates": [], "unresolved_segments": [], "tokens": []},
        "standardization": None, "region_validation": None, "review": None,
        "traces": [{"module_name": ENGINE, "module_version": ENGINE_VERSION, "status": "completed",
                    "reason_code": code, "elapsed_ms": elapsed_ms,
                    "details": {"bigquery_status": sql_status, "reason": reason}}],
    }
    if status == "excluded":
        result["output"] = {"status": "excluded", "reason_code": code or "blank_input",
                            "representative_address_id": None, "normalized_address": None, "review_tag": None}
        return Built(result, {})

    units = detail_units(raw)
    if status in {"resolved", "address_only"}:
        candidates = [_road_candidate(main, [reason], year)]
        places = [Place.from_road(main)]
    else:
        roads = [item for item in row.get("candidate_details") or [] if item and item.get("address")]
        lots = [item for item in row.get("lot_details") or [] if item and item.get("lot")] if status != "lot_only" else []
        candidates = [_road_candidate(item, [reason], year) for item in roads]
        candidates += [_lot_candidate(item, [reason], year) for item in lots]
        places = [Place.from_road(item) for item in roads] + [Place.from_lot(item) for item in lots]

    comparisons = {i: compare(text, place) for i, place in enumerate(places)}
    # 정확히 일치할 때만 확정: 원문에 적힌 시도·시군구·읍면동·리 중 하나라도 기준자료(법정동·행정동)와 다르면 검토로 보낸다.
    if status in {"resolved", "address_only"}:
        wrong = [c for c in comparisons[0] if c["outcome"] == "mismatch" and c["component"] in LEVELS[:4]]
        if wrong:
            status = "held"
            code = "region_partial"
            reason = ("원문의 " + ", ".join(f"{c['component']} '{c['input_value']}'" for c in wrong)
                      + f"이(가) 기준자료({', '.join(c['candidate_value'] for c in wrong)})와 달라 확정하지 않음 · " + reason)
            result.update(status=status)
            result["output"].update(status=status, reason_code=code, reason=reason)
            result["traces"][0].update(reason_code=code)
    validation, tokens = region_validation(text, places, record.address_id, year)
    result["region_validation"] = validation
    result["output"]["tokens"] = tokens
    numbers = sorted({c["input_value"] for rows in comparisons.values() for c in rows
                      if c["component"] in {"건물번호", "지번"} and c["input_value"]})
    result["output"]["unresolved_segments"] = numbers

    # 관계는 실제로 연결된 도로명주소·지번 사이에서만 본다. 부번이 다른 건물 후보끼리처럼
    # 서로 대안인 후보들은 묶지 않는다.
    road_items = [c for c in candidates if c["kind"] == "road"]
    if status in {"resolved", "address_only"} or (status != "lot_only" and len(candidates) == 1 and road_items):
        links = {c["address"]: c["linked_lot_addresses"] for c in road_items}       # 한 건물 → 필지들 (N:1)
    elif status == "lot_only":
        links = {c["address"]: c["linked_lot_addresses"] for c in road_items}       # 한 필지 → 건물들 (1:N, N:M)
    else:
        links = {}
    relation = road_lot_relation(links)
    result["output"]["road_lot_relation"] = relation
    if relation:
        for c in candidates:
            c["evidence"].append(f"도로명·지번 관계 {relation['kind']}: {relation['label']}")

    if status in {"resolved", "address_only"}:
        warning = sub_number_warning(main)
        if warning:
            # 확정은 유지하고 주의만 붙인다: 근거·판정 사유·처리 이력(사유 코드)에 모두 남겨 화면과 CSV에서 보이게 한다.
            candidates[0]["evidence"].append(warning)
            result["output"]["warnings"] = [warning]
            result["output"]["reason"] = f"{reason} · {warning}"
            result["traces"].append({"module_name": "sub_number_check", "module_version": ENGINE_VERSION,
                                     "status": "completed", "reason_code": SIBLING_WARNING, "elapsed_ms": 0.0,
                                     "details": {"sibling_subs": list(main.get("sibling_subs") or [])}})
        standard = {key: main.get(key) or "" for key in ("road_name_code", "legal_dong_code", "administrative_area_code")}
        address = {"standard_road_address": main["address"], **standard}
        result["standardization"] = {
            "status": status, "address": address,
            "reason_code": None if status == "resolved" else "codes_missing_or_conflicting",
            "raw_record_ids": [str(serial) for serial in main.get("serials") or []],
        }
        result["review"] = {
            "review_type": VERIFIED, "status": "verified", "base_status": status,
            "base_reason": result["standardization"]["reason_code"] or "",
            "confirmed_lot": row.get("standard_lot_address") or "", "candidates": candidates,
            "standard_address": address, "detail_units": units, "tier": "near_confirmed",
        }
        return Built(result, comparisons)

    approvable = [c for c in candidates if c["standard_address"]]
    result["review"] = {
        "review_type": REVIEW_TYPES.get(code or "", "기타·지역/번호 해석 부족"),
        "status": "no_candidate" if not candidates else "single_candidate" if len(candidates) == 1 else "tied_candidates",
        "base_status": status, "base_reason": code or "",
        "confirmed_lot": (row.get("standard_lot_address") or "") if status == "lot_only" else "",
        "candidates": candidates, "standard_address": None, "detail_units": units,
        # 후보가 하나뿐이고 코드까지 있으면 사람이 상세 화면에서 한 건씩 승인할 수 있는 '확인 필요'로 둔다.
        "tier": "check_needed" if status == "held" and len(candidates) == 1 and approvable else "general",
    }
    return Built(result, comparisons)


def build_multi(parts: list[tuple[str, Mapping]], index: int, record: InputRecord,
                year: int = REFERENCE_YEAR, elapsed_ms: float = 0.0) -> Built:
    """한 칸에 주소가 여러 개인 행. 조각마다 매칭한 결과를 후보로 모으고, 확정하지 않고 검토로 보낸다."""
    candidates: list[dict] = []
    comparisons: dict[int, list[Comparison]] = {}
    pieces = []
    for k, (segment, row) in enumerate(parts, start=1):
        piece = build_single(row, index, InputRecord(f"{record.address_id}#{k}", segment), year, text=segment).row
        label = f"주소 {k}/{len(parts)}: {segment}"
        found = (piece.get("review") or {}).get("candidates") or []
        pieces.append({"index": k, "segment": segment, "bigquery_status": row.get("status"),
                       "status": piece["status"], "reason": row.get("reason"),
                       "standard_road_address": ((piece.get("standardization") or {}).get("address") or {}).get(
                           "standard_road_address"),
                       "candidates": [c["address"] for c in found]})
        for c in found:
            c = {**c, "evidence": [label, *c["evidence"]]}
            place = Place.from_road({**c, **(c.get("standard_address") or {})}) if c["kind"] == "road" else None
            candidates.append(c)
            comparisons[len(candidates) - 1] = built_comparisons(segment, row, c, place)
    confirmed = {p["standard_road_address"] for p in pieces if p["status"] in {"resolved", "address_only"}}
    raw = record.raw_address
    result = {
        "record": {"address_id": record.address_id, "raw_address": raw,
                   "normalized_address": " ".join(raw.split()) if raw else None},
        "input_index": index, "status": "held",
        "output": {"engine": ENGINE, "status": "held", "reason_code": "multiple_addresses",
                   "reason": f"한 칸에 주소 {len(parts)}개 · 조각별로 따로 매칭함",
                   "review_tag": "복수의 주소 확인 필요", "address_candidates": pieces,
                   "unresolved_segments": [], "tokens": []},
        "standardization": None, "region_validation": None,
        "review": {"review_type": MULTI,
                   "status": "no_candidate" if not candidates else "single_candidate" if len(candidates) == 1
                   else "tied_candidates",
                   "base_status": "held", "base_reason": "multiple_addresses", "confirmed_lot": "",
                   "candidates": candidates, "standard_address": None, "detail_units": detail_units(raw),
                   "tier": "general"},
        "traces": [{"module_name": ENGINE, "module_version": ENGINE_VERSION, "status": "completed",
                    "reason_code": "multiple_addresses", "elapsed_ms": elapsed_ms,
                    "details": {"segments": [p["segment"] for p in pieces],
                                "confirmed_segments": sorted(a for a in confirmed if a)}}],
    }
    return Built(result, comparisons)


def built_comparisons(segment: str, row: Mapping, candidate: Mapping, place: Place | None) -> list[Comparison]:
    """복수 주소 조각의 후보 비교. 조각의 06 결과에서 후보 구성요소를 다시 찾는다."""
    details = [row.get("main_detail")] + list(row.get("candidate_details") or [])
    for item in details:
        if item and item.get("address") == candidate["address"]:
            return compare(segment, Place.from_road(item))
    for item in row.get("lot_details") or []:
        if item and item.get("lot") == candidate["address"]:
            return compare(segment, Place.from_lot(item))
    return compare(segment, place) if place else []


def build_dataset(records: tuple[InputRecord, ...], rows: Iterable[Mapping], *, run_id: str,
                  steps: list[dict], segments: Mapping[str, list[str]] | None = None,
                  year: int = REFERENCE_YEAR) -> tuple[dict, dict]:
    """06 결과 행을 입력 순서대로 AddressResult로 바꾼다. 복수 주소 행은 '<id>#<k>' 조각 행을 모아 하나로 만든다.

    segments는 prepare()가 정리한 조각이다. 조각이 하나면 그 정리한 원문으로 구성요소를 비교한다."""
    by_id = {row["address_id"]: row for row in rows}
    segments = segments or {}
    total_ms = sum(step["elapsed_ms"] for step in steps)
    each = round(total_ms / len(records), 3) if records else 0.0
    results, comparisons = [], {}
    for i, record in enumerate(records):
        parts = segments.get(record.address_id) or []
        if len(parts) > 1:
            missing = [f"{record.address_id}#{k}" for k in range(1, len(parts) + 1) if f"{record.address_id}#{k}" not in by_id]
            if missing:
                raise ValueError(f"BigQuery 결과에 없는 입력이 있습니다: {missing[:5]}")
            built = build_multi([(seg, by_id[f"{record.address_id}#{k}"]) for k, seg in enumerate(parts, start=1)],
                                i, record, year, each)
        else:
            if record.address_id not in by_id:
                raise ValueError(f"BigQuery 결과에 없는 입력이 있습니다: {record.address_id}")
            built = build_single(by_id[record.address_id], i, record, year, each, text=parts[0] if parts else None)
        results.append(built.row)
        if built.comparisons:
            comparisons[record.address_id] = built.comparisons
    summary = dict(Counter(row["status"] for row in results).most_common())
    review_summary: dict[str, Counter] = defaultdict(Counter)
    for row in results:
        if row["review"]:
            review_summary[row["review"]["review_type"]][row["review"]["status"]] += 1
    review_summary = {key: dict(value) for key, value in review_summary.items()}
    relations = Counter((row["output"].get("road_lot_relation") or {}).get("kind") for row in results)
    relations.pop(None, None)
    batch = {"run_id": f"{run_id}:bigquery", "reference_version": str(year), "results": [], "summary": summary,
             "parsed_df": [], "search_traces": steps, "review_summary": review_summary,
             "road_lot_relations": dict(relations)}
    dataset = {"run_id": run_id, "reference_version": str(year), "results": results, "summary": summary,
               "batch_results": [batch], "review_summary": review_summary}
    return dataset, comparisons


# ---------------------------------------------------------------- 건물명 후보 단계

BUILDING_REVIEW = "건물명 일치 후보"
_BUILDING_SUFFIX = re.compile(r"(?:아파트|맨션|빌라트|빌라|연립주택|연립|다세대주택|다세대|오피스텔|빌딩)$")
# 동·층·호 상세(206동, 1504호, 지하 2층). 한 자리 숫자+동(대저2동, 농소1동)은 행정동 이름이라 떼지 않는다.
_DETAIL_TAIL = re.compile(r"(?:지하)?(?:[0-9]{2,}(?:-[0-9]+)?동|[0-9]+(?:-[0-9]+)?(?:층|호))(?:[0-9]+(?:동|층|호))*$")
# 건물 고유 이름이 아닌 용도·표시 낱말(01 건물명 사전의 is_generic과 같은 취지).
_NOT_BUILDING = {
    "주택", "단독주택", "다세대주택", "다가구주택", "연립주택", "공동주택", "농막", "창고", "마을회관", "경로당", "소매점",
    "축사", "우사", "공장", "사무소", "사무실", "상가", "교회", "민박", "슈퍼", "상점", "주유소", "농협",
    "우편물", "우편", "본점", "지점", "주소", "수령지", "담당자", "번지", "필지", "외", "지하", "사서함", "센터", "근린생활시설",
}
_MATCH_KIND = {0: "건물명 정확 일치", 1: "건물명 핵심 이름 일치(아파트·맨션 등 표기 차이)", 2: "건물명 일부 포함 일치",
               3: "건물명 철자 유사(한 글자 차이)"}


def normalize_name(text: str) -> str:
    """건물명 비교용 표기: NFKC·소문자·공백 제거, APT → 아파트, 제2차 → 2차(01 건물명 사전의 name_norm과 같음)."""
    name = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "").lower())
    name = name.replace("a.p.t", "아파트").replace("apt", "아파트")
    return re.sub(r"제([0-9]+)차", r"\1차", name)


def building_core(name: str) -> str:
    return _BUILDING_SUFFIX.sub("", name)


def building_phrase(text: str | None) -> tuple[str, str, str] | None:
    """원문에서 건물명으로 보이는 가장 긴 조각을 고른다. (원문 표기, 정규화 이름, 핵심 이름) 또는 None.

    지역·도로명·번호 낱말, 동·층·호 같은 상세, 용도·표시 낱말은 빼고, 이어진 나머지 낱말을 한 이름으로 붙인다
    (종우원에이 아파트 → 종우원에이아파트)."""
    pieces, current = [], []

    def flush():
        if current:
            pieces.append(" ".join(current))
            current.clear()

    for word in re.split(r"[\s,()/:\[\]]+", spaced(text)):
        word = _DETAIL_TAIL.sub("", word.strip(".·-~"))
        # 지역·번호에 붙여 쓴 건물명(건입동661-1장원사라빌라가동)은 번호 뒤만 남기고, 끝의 '가동·A동'은 동 표기로 뗀다.
        word = re.sub(r"^.*?[0-9]+(?:-[0-9]+)?(?=[가-힣A-Za-z]{2})", "", word)
        word = re.sub(r"(?<=[가-힣]{3})(?:[가나다라마바사]|[A-Za-z])동$", "", word)
        if _BUILDING_SUFFIX.fullmatch(word) or re.fullmatch(r"(?:제)?[0-9]+차", word):
            if current:                       # 띄어 쓴 '아파트·빌라'와 'N차'는 앞 이름에 붙인다(종우원에이 아파트, 진우하이빌 3차)
                current.append(word)
            continue
        if (not word or word in _NOT_BUILDING or not re.search(r"[가-힣A-Za-z]", word) or word[0].isdigit()
                or re.search(r"(?:로|길)[0-9]+(?:번|번길|길|[가-힣]?길)?$|(?:로|길)[0-9]*번", word)   # 도로명 조각(태평로152번)
                or _word_class(word) != 6 or stated_words(word) == [(word, _level_of(word))]):
            flush()
            continue
        current.append(word)
    flush()
    best = None
    for piece in pieces:
        name = normalize_name(piece)
        core = building_core(name)
        if len(core) >= 2 and name not in _NOT_BUILDING and core not in _NOT_BUILDING:
            if best is None or len(name) > len(best[1]):
                best = (piece, name, core)
    return best


def building_requests(results: list[dict], texts: Mapping[str, str]) -> dict[str, tuple[str, str, str]]:
    """건물명을 쓸 행: 확정되지 않은 행 전부. 후보가 없으면 건물명으로 후보를 찾고(1번),
    후보가 있으면 건물명으로 고르거나 원문 번호와 다른 건물을 새 후보로 더한다(2번)."""
    out = {}
    for row in results:
        address_id = row["record"]["address_id"]
        if address_id not in texts or row["status"] not in {"no_match", "held", "lot_only"}:
            continue
        if (phrase := building_phrase(texts[address_id])) is not None:
            out[address_id] = phrase
    return out


def apply_buildings(results: list[dict], comparisons: dict, matches: Iterable[Mapping],
                    phrases: Mapping[str, tuple[str, str, str]], texts: Mapping[str, str],
                    year: int = REFERENCE_YEAR) -> Counter:
    """건물명 사전 조회(08) 결과를 반영한다. 확정하지 않는다.

    1번: 후보가 없던 행은 건물명으로 찾은 건물을 검토 후보로 단다. 시군구 안에 하나뿐인 이름이 정확·핵심·포함 일치면
         '확인 필요'(빠른 승인 가능), 그 밖에는 '일반 보류'.
    2번: 후보가 여럿이던 행은 원문 건물명과 맞는 후보가 하나뿐이면 그 후보를 1순위로 올린다."""
    # 한 건물이 이름 두 개로 등록돼 있으면(건물명 '부영사랑으로'·시군구관리건물명 '부영사랑으로 아파트') 08이 이름마다 한 행을
    # 돌려준다. 같은 주소는 후보 하나로 합치고 이름은 함께 적는다.
    # 한 건물이 법정동 경계에 걸치면 법정동마다 한 행이 온다. 지번·일련번호·행정동은 합치고, 법정동 코드처럼
    # 하나로 정해지지 않는 코드는 비운다(06·10과 같다).
    found: dict[str, dict[str, dict]] = defaultdict(dict)
    for item in matches:
        same = found[item["address_id"]].get(item["address"])
        if same is None:
            found[item["address_id"]][item["address"]] = dict(item)
            continue
        if item["building_name"] not in same["building_name"].split(" / "):
            same["building_name"] = f"{same['building_name']} / {item['building_name']}"
        for key in ("lot_addresses", "admin_dongs", "serials"):
            same[key] = sorted(set(same.get(key) or ()) | set(item.get(key) or ()))
        for key in ("legal_dong_code", "road_name_code", "administrative_area_code"):
            if same.get(key) != item.get(key):
                same[key] = None
    counts: Counter = Counter()
    for index, row in enumerate(results):
        address_id = row["record"]["address_id"]
        items = list(found.get(address_id, {}).values())
        if not items or address_id not in phrases:
            continue
        original, _, _ = phrases[address_id]
        text = texts.get(address_id) or row["record"]["raw_address"]
        kind = int(items[0]["match_kind"])
        review = row.get("review") or {}
        existing = review.get("candidates") or []
        note = {"phrase": original, "match_kind": _MATCH_KIND[kind], "buildings": len(items)}

        def evidence_for(item) -> list[str]:
            return [f"{_MATCH_KIND[kind]} · 원문 '{original}' ≈ 기준 '{item['building_name']}'",
                    f"시군구 안 같은 이름 {item['same_name_in_sigungu']}곳"]

        if existing:
            # 2번: 여러 후보 중 건물명과 맞는 후보가 하나뿐이면 1순위로 올린다.
            by_address = {item["address"]: item for item in items}
            hit = [c for c in existing if c["kind"] == "road" and c["address"] in by_address]
            strong = kind == 0 and len(items) == 1 and int(items[0]["same_name_in_sigungu"]) == 1
            if not hit and strong:
                # 원문 번호로 찾은 후보에 없는 건물을 건물명이 정확히, 시군구에 하나뿐인 이름으로 가리키면
                # (봉황동 325-52 공주시청 → 봉황로 1) 그 건물을 1순위 후보로 더하고 기존 후보는 2순위로 내린다.
                item = items[0]
                added = _road_candidate(item, [
                    f"건물명 정확 일치 · 원문 '{original}' = 기준 '{item['building_name']}' · 시군구 안 하나뿐",
                    "원문의 번호로 찾은 후보와 다른 건물 · 원문 번호와 이 건물의 번호가 다름"], year)
                added["building_name"], added["building_input"] = item["building_name"], original
                for candidate in existing:
                    candidate["rank"] = 2
                existing.append(added)
                comparisons.setdefault(address_id, {})[len(existing) - 1] = compare(text, Place.from_road(item)) + [
                    Comparison(component="건물명", input_value=original, candidate_value=item["building_name"],
                               outcome="match", evidence=_MATCH_KIND[0])]
                review.update(status="preferred_candidate",
                              tier="check_needed" if added.get("standard_address") else review.get("tier", "general"))
                if row["status"] == "no_match":
                    row["status"] = "held"
                    row["output"].update(status="held")
                counts["added_by_name"] += 1
                row["output"]["building_match"] = note
                row["traces"].append({"module_name": "building_name_match", "module_version": ENGINE_VERSION,
                                      "status": "completed", "reason_code": "building_name_candidate",
                                      "elapsed_ms": 0.0, "details": note})
                continue
            if len(hit) != 1:
                continue
            chosen = hit[0]
            for candidate in existing:
                candidate["rank"] = 1 if candidate is chosen else 2
            chosen["evidence"] = [f"건물명으로 1순위: {evidence_for(by_address[chosen['address']])[0]}",
                                  *chosen["evidence"]]
            chosen["building_name"], chosen["building_input"] = by_address[chosen["address"]]["building_name"], original
            review.update(status="preferred_candidate",
                          tier="check_needed" if chosen.get("standard_address") else review.get("tier", "general"))
            counts["narrowed"] += 1
        else:
            # 1번: 후보가 없던 행에 건물명 후보를 단다.
            items = items[:10]
            candidates = []
            for item in items:
                candidate = _road_candidate(item, evidence_for(item) + [row["output"].get("reason") or ""], year)
                candidate["building_name"], candidate["building_input"] = item["building_name"], original
                candidates.append(candidate)
            places = [Place.from_road(item) for item in items]
            # 빠른 승인('확인 필요')은 시군구 안에 하나뿐인 이름이면서, 이름이 정확히 같거나 원문에 건물 종류(아파트·빌라…)까지
            # 적혀 있어 건물명임이 분명할 때만. 마을 이름·도로명 오타(무궁화 → 무궁화연립)나 2자 부분 일치는 일반 보류로 둔다.
            _, name, core = phrases[address_id]
            typed = name != core
            unique = len(items) == 1 and int(items[0]["same_name_in_sigungu"]) == 1 and (
                kind == 0 or (typed and (kind == 1 or (kind == 2 and len(core) >= 3))))
            row["status"] = "held"
            row["output"].update(status="held")
            row["review"] = {
                "review_type": BUILDING_REVIEW,
                "status": "single_candidate" if len(candidates) == 1 else "tied_candidates",
                "base_status": review.get("base_status") or row["status"],
                "base_reason": row["output"].get("reason_code") or "", "confirmed_lot": "",
                "candidates": candidates, "standard_address": None,
                "detail_units": detail_units(row["record"]["raw_address"]),
                "tier": "check_needed" if unique and candidates[0].get("standard_address") else "general",
            }
            row["region_validation"], row["output"]["tokens"] = region_validation(text, places, address_id, year)
            comparisons[address_id] = {
                i: compare(text, place) + [Comparison(
                    component="건물명", input_value=original, candidate_value=item["building_name"],
                    outcome="match" if kind <= 1 else "unknown", evidence=_MATCH_KIND[kind])]
                for i, (place, item) in enumerate(zip(places, items))}
            counts["candidates_added"] += 1
        row["output"]["building_match"] = note
        row["traces"].append({"module_name": "building_name_match", "module_version": ENGINE_VERSION,
                              "status": "completed", "reason_code": "building_name_candidate", "elapsed_ms": 0.0,
                              "details": note})
    return counts


# ---------------------------------------------------------------- 폐지된 주소 단계

ABOLISHED_REVIEW = "폐지된 주소"
ABOLISHED_REASON = "abolished_address"


def abolished_targets(results: list[dict], texts: Mapping[str, str]) -> list[str]:
    """폐지 주소와 대조할 행: 규칙·건물명 단계 뒤에도 후보가 하나도 없는 미일치 행."""
    return [row["record"]["address_id"] for row in results
            if row["status"] == "no_match" and row["record"]["address_id"] in texts
            and not (row.get("review") or {}).get("candidates")]


def _month(value: str | None) -> str:
    return f"{value[:4]}년 {int(value[4:6])}월" if value and len(value) >= 6 and value.isdigit() else str(value or "")


RELINKED_REVIEW = "연결 지번이 빠진 건물"
RELINKED_REASON = "lot_unlinked_building"


def apply_abolished(results: list[dict], hits: Iterable[Mapping], year: int = REFERENCE_YEAR,
                    comparisons: dict | None = None, texts: Mapping[str, str] | None = None) -> Counter:
    """폐지 이력(09)과 맞은 행에 사유와 후보를 단다. 확정하지 않는다.

    - 폐지된 주소: 기록의 주소가 2025-12-31 기준자료에 없다. 상태는 미일치 그대로, 같은 지번의 지금 건물은 참고 후보.
    - 연결 지번이 빠진 건물: 원문 지번은 기준자료에 없지만, 폐지 기록은 '이 지번이 어느 건물의 연결 지번에서 빠졌다'는 것이고
      그 건물은 지금도 있다. 그 건물을 검토 후보(1순위)로 단다(검토 필요, 빠른 승인 대상 아님)."""
    found: dict[str, list[Mapping]] = defaultdict(list)
    for item in hits:
        found[item["address_id"]].append(item)
    counts: Counter = Counter()
    for row in results:
        address_id = row["record"]["address_id"]
        items = found.get(address_id)
        if not items:
            continue
        moved = [item for item in items if item.get("still_registered") and item.get("now_building")]
        if moved:
            _apply_relinked(row, moved, year, comparisons, (texts or {}).get(address_id) or row["record"]["raw_address"] or "")
            counts["lot_unlinked_building"] += 1
            continue
        gone = [{"abolished_address": item["abolished_address"], "lot_address": item.get("lot_address"),
                 "abolished_month": item.get("changed_month"), "road_name_code": item.get("road_name_code"),
                 "legal_dong_code": item.get("legal_dong_code"), "matched_as": item.get("kind")} for item in items]
        first = gone[0]
        reason = (f"폐지된 주소: {first['abolished_address']} · {_month(first['abolished_month'])} 도로명주소 변동분에서 폐지"
                  + (f" 외 {len(gone) - 1}건" if len(gone) > 1 else "") + " · 2025-12-31 기준자료에는 없음")
        current = list(dict.fromkeys(road for item in items for road in item.get("current_roads") or []))[:5]
        candidates = []
        for road in current:
            candidate = _road_candidate({"address": road, "lot_addresses": [first["lot_address"]] if first["lot_address"] else []},
                                        [f"참고: 폐지된 주소({first['abolished_address']})와 같은 지번에 지금 있는 건물"], year)
            candidate["rank"] = 2
            candidates.append(candidate)
        row["output"].update(reason_code=ABOLISHED_REASON, reason=reason, abolished=gone)
        row["review"] = {
            "review_type": ABOLISHED_REVIEW,
            "status": "no_candidate" if not candidates else "single_candidate" if len(candidates) == 1 else "tied_candidates",
            "base_status": row["status"], "base_reason": ABOLISHED_REASON, "confirmed_lot": "",
            "candidates": candidates, "standard_address": None,
            "detail_units": detail_units(row["record"]["raw_address"]), "tier": "general",
        }
        row["traces"].append({"module_name": "abolished_address", "module_version": ENGINE_VERSION,
                              "status": "completed", "reason_code": ABOLISHED_REASON, "elapsed_ms": 0.0,
                              "details": {"abolished": gone}})
        counts["abolished"] += 1
        counts["with_current_buildings"] += bool(candidates)
    return counts


def _apply_relinked(row: dict, moved: list[Mapping], year: int, comparisons: dict | None, text: str) -> None:
    candidates, compared, notes = [], {}, []
    for item in moved:
        current = dict(item["now_building"])
        if current["address"] in {c["address"] for c in candidates}:
            continue
        now_lots = ", ".join(_lot_tail(lot) for lot in current.get("lot_addresses") or []) or "없음"
        evidence = [f"원문 지번 {_lot_tail(item.get('lot_address') or '')}은 {_month(item.get('changed_month'))} 도로명주소 변동분에서 "
                    f"이 건물의 연결 지번에서 빠짐(폐지 이력)",
                    f"이 건물은 2025-12-31 기준자료에 지금도 있음 · 지금 연결 지번: {now_lots}",
                    "주의: 원문 지번이 지금은 이 건물과 연결돼 있지 않음 · 원천 자료로 확인 필요"]
        candidate = _road_candidate({**current, "road_name": None}, evidence, year)
        candidates.append(candidate)
        compared[len(candidates) - 1] = compare(text, Place.from_road(
            {**current, "road_name": re.sub(r"^.*\s(\S+)\s[0-9-]+$", r"\1", current["address"])}))
        notes.append({"lot": item.get("lot_address"), "building": current["address"], "month": item.get("changed_month")})
    reason = (f"원문 지번은 2025-12-31 기준자료에 없음 · 이 지번은 {_month(moved[0].get('changed_month'))}에 "
              f"{candidates[0]['address']}의 연결 지번에서 빠졌고 그 건물은 지금도 있음 · 사람 확인 필요")
    row["status"] = "held"
    row["output"].update(status="held", reason_code=RELINKED_REASON, reason=reason, lot_unlinked=notes)
    row["review"] = {
        "review_type": RELINKED_REVIEW, "status": "single_candidate" if len(candidates) == 1 else "tied_candidates",
        "base_status": "no_match", "base_reason": RELINKED_REASON, "confirmed_lot": "", "candidates": candidates,
        "standard_address": None, "detail_units": detail_units(row["record"]["raw_address"]), "tier": "general",
    }
    if comparisons is not None:
        comparisons[row["record"]["address_id"]] = compared
    row["traces"].append({"module_name": "lot_unlinked_building", "module_version": ENGINE_VERSION, "status": "completed",
                          "reason_code": RELINKED_REASON, "elapsed_ms": 0.0, "details": {"buildings": notes}})


# ---------------------------------------------------------------- AI(Gemini) 교정 단계

AI_REVIEW = "AI 교정 후보"
AI_REASONS = {"scope_not_found", "number_not_found"}


def ai_targets(results: list[dict]) -> list[str]:
    """AI 교정에 보낼 행: 규칙 매칭이 후보를 하나도 못 찾았고, 오타·표기 문제일 수 있는 행만 고른다.

    번호가 아예 없는 행(missing_address_number)과 '리·동은 맞는데 그 지번이 건물 기준자료에 없는' 행은
    AI가 고칠 수 있는 문제가 아니므로 보내지 않는다(번호를 지어내게 될 뿐이다)."""
    out = []
    for row in results:
        output = row.get("output") or {}
        if row["status"] not in {"no_match", "held"} or (row.get("review") or {}).get("candidates"):
            continue
        if output.get("reason_code") not in AI_REASONS or str(output.get("reason") or "").startswith("리·동은 있으나"):
            continue
        out.append(row["record"]["address_id"])
    return out


def _adopt(row: dict, built: Built, second: Mapping, text: str, comparisons: dict, *, review_type: str,
           reason_code: str, reason: str, evidence: list[str], year: int) -> tuple[dict, Place]:
    """다시 매칭해 기준자료와 정확히 맞은 해석을 이 행의 단독 검토 후보로 단다(확정하지 않음)."""
    candidate = built.row["review"]["candidates"][0]
    candidate["evidence"] = evidence + candidate["evidence"]
    place = Place.from_road(second["main_detail"])
    row["status"] = "held"
    row["output"].update(status="held", reason_code=reason_code, reason=f"{reason} · {second.get('reason') or ''}")
    row["review"] = {
        "review_type": review_type, "status": "single_candidate", "base_status": "held",
        "base_reason": reason_code, "confirmed_lot": "", "candidates": [candidate],
        "standard_address": None, "detail_units": detail_units(row["record"]["raw_address"]),
        "tier": "check_needed" if candidate.get("standard_address") else "general",
    }
    address_id = row["record"]["address_id"]
    row["region_validation"], row["output"]["tokens"] = region_validation(text, [place], address_id, year)
    row["output"]["road_lot_relation"] = built.row["output"].get("road_lot_relation")
    comparisons[address_id] = {0: compare(text, place)}
    return candidate, place


# 도로명 바로 뒤 번호가 동 번호와 붙은 입력(신호산단3로 27106동 1101호). 뒤 세 자리를 동(101~999), 앞을 건물번호로 읽는다.
# 세 자리뿐인 '수양로 107동'은 건물번호를 빠뜨린 것일 수 있어 나누지 않는다.
_GLUED = re.compile(r"(?<=[로길])(\s*)([0-9]{1,6})([1-9][0-9]{2})(\s*동)(?![가-힣])")
GLUED_REVIEW = "건물번호·동 붙여쓰기 후보"


def glued_targets(results: list[dict], texts: Mapping[str, str]) -> dict[str, tuple[str, str, str, str]]:
    """후보가 하나도 없는 행 중 번호와 동이 붙어 있는 행 → (나눠 읽은 원문, 건물번호, 동, 붙은 표기)."""
    out = {}
    for row in results:
        address_id = row["record"]["address_id"]
        text = texts.get(address_id)
        if not text or row["status"] not in {"no_match", "held"} or (row.get("review") or {}).get("candidates"):
            continue
        found = list(_GLUED.finditer(text))
        if len(found) != 1:
            continue
        m = found[0]
        variant = f"{text[:m.start()]} {m[2]} {m[3]}동{text[m.end():]}"
        out[address_id] = (variant, m[2], m[3], m[0].strip())
    return out


def apply_glued(results: list[dict], comparisons: dict, targets: Mapping[str, tuple[str, str, str, str]],
                matched: Iterable[Mapping], year: int = REFERENCE_YEAR) -> Counter:
    """나눠 읽은 원문이 기준자료와 정확히 맞으면 그 건물을 검토 후보로 단다. 확정하지 않는다."""
    by_id = {row["record"]["address_id"]: (i, row) for i, row in enumerate(results)}
    matched = {row["address_id"]: row for row in matched}
    counts: Counter = Counter()
    for address_id, (variant, main, unit, glued) in targets.items():
        second = matched.get(address_id)
        index, row = by_id[address_id]
        built = build_single(second, index, InputRecord(address_id, variant), year, text=variant) if second else None
        if not (built and built.row["status"] in {"resolved", "address_only"}):
            counts["not_found"] += 1
            continue
        _adopt(row, built, second, variant, comparisons, review_type=GLUED_REVIEW, reason_code="number_unit_glued",
               reason=f"원문 '{glued}'을(를) 건물번호 {main} + {unit}동으로 나눠 읽으면 기준자료와 일치 · 사람 확인 필요",
               evidence=[f"번호 나눠 읽기: '{glued}' → 건물번호 {main}, {unit}동 · 원문에 띄어쓰기가 없어 확인 필요"], year=year)
        row["traces"].append({"module_name": "glued_number_split", "module_version": ENGINE_VERSION,
                              "status": "completed", "elapsed_ms": 0.0, "reason_code": "number_unit_glued",
                              "details": {"glued": glued, "building_number": main, "unit": f"{unit}동"}})
        counts["candidate"] += 1
    return counts


def _lot_tail(lot: str) -> str:
    """'경상북도 경주시 내남면 용장리 산 134-3' → '용장리 산 134-3' (마지막 리·동부터)."""
    words = lot.split()
    start = max((i for i, word in enumerate(words) if re.search(r"[리동가]$", word)), default=max(len(words) - 2, 0))
    return " ".join(words[start:])


def add_parcel_evidence(results: list[dict]) -> int:
    """지번 부번 생략·불일치 행의 후보마다 그 건물이 있는 지번(원문 본번과 같은 것)을 근거 맨 앞에 적는다.

    공동 1순위 후보들의 근거가 똑같으면 어느 후보가 134-1이고 어느 후보가 134-2인지 화면에서 알 수 없다."""
    changed = 0
    for row in results:
        reason = (row.get("output") or {}).get("reason") or ""
        hit = re.search(r"지번 부번 (?:생략|불일치)\(([^)]*)\)", reason)
        main = re.search(r"[리동가로](?:산)?([0-9]+)", hit[1]) if hit else None
        if not main:
            continue
        for candidate in (row.get("review") or {}).get("candidates") or []:
            lots = [lot for lot in candidate.get("linked_lot_addresses") or []
                    if (m := re.search(r"(?:^|\s)(?:산\s*)?([0-9]+)(?:-[0-9]+)?$", lot)) and m[1] == main[1]]
            if lots:
                short = ", ".join(_lot_tail(lot) for lot in lots)
                candidate["evidence"].insert(0, f"이 후보의 지번: {short}")
                changed += 1
    return changed


TYPO_REVIEW = "번호 오타 후보"
_TYPO = {"swap": (1, "숫자 자리 바뀜"), "digit": (2, "숫자 한 자리 다름"),
         "missing": (3, "원문에 숫자 하나 빠짐"), "extra": (3, "원문에 숫자 하나 더 있음")}


def typo_targets(results: list[dict], texts: Mapping[str, str]) -> list[str]:
    """도로명·리는 기준자료에 있는데 그 번호가 없고, 다른 단계에서도 후보를 못 찾은 미일치 행."""
    return [row["record"]["address_id"] for row in results
            if row["status"] == "no_match" and row["record"]["address_id"] in texts
            and (row.get("output") or {}).get("reason_code") == "number_not_found"
            and not (row.get("review") or {}).get("candidates")]


def apply_typos(results: list[dict], comparisons: dict, items: Iterable[Mapping], texts: Mapping[str, str],
                year: int = REFERENCE_YEAR) -> Counter:
    """번호 오타 후보(10)를 단다. 번호가 원문과 다르므로 후보가 하나여도 빠른 승인 단계에 넣지 않는다."""
    found: dict[str, list[Mapping]] = defaultdict(list)
    for item in items:
        if item["address"] not in {seen["address"] for seen in found[item["address_id"]]}:
            found[item["address_id"]].append(item)
    counts: Counter = Counter()
    for row in results:
        address_id = row["record"]["address_id"]
        hits = found.get(address_id)
        if not hits:
            continue
        text = texts.get(address_id) or row["record"]["raw_address"] or ""
        candidates, compared = [], {}
        for n, item in enumerate(sorted(hits, key=lambda item: int(item["pos"]))):
            rank, label = _TYPO[item["typo"]]
            sub = f"-{item['sub']}" if int(item["sub"] or 0) else ""
            where = f"지번 {item['lot_address']}" if item["via"] == "lot" else "같은 도로"
            evidence = [f"번호 오타 후보 · {label}: 원문 {item['input_main']}{sub} → 기준자료 {item['ref_main']}{sub} ({where})",
                        "주의: 번호가 원문과 다름 · 원문 번호가 건물 없는 땅일 수도 있음 · 원천 자료로 확인 필요"]
            candidate = _road_candidate(item, evidence, year)
            candidate["rank"] = rank
            candidates.append(candidate)
            compared[n] = compare(text, Place.from_road(item))
            counts[item["typo"]] += 1
        ranks = sorted({c["rank"] for c in candidates})
        for candidate in candidates:  # 화면의 순위는 1부터 이어지게 다시 매긴다.
            candidate["rank"] = ranks.index(candidate["rank"]) + 1
        first = [c for c in candidates if c["rank"] == 1]
        # 번호를 추정한 후보라 판정은 미일치 그대로 둔다. 화면은 '번호 불일치 · 후보 있음'으로 검토 목록에 보여 준다.
        row["output"].update(reason_code="number_typo_candidate",
                             reason=f"원문 번호는 기준자료에 없음 · 번호를 잘못 적었다면 맞는 건물 {len(candidates)}곳 · 사람 확인 필요 · "
                                    + (row["output"].get("reason") or ""))
        row["review"] = {
            "review_type": TYPO_REVIEW, "status": "single_candidate" if len(candidates) == 1
            else "tied_candidates" if len(first) > 1 else "preferred_candidate",
            "base_status": "no_match", "base_reason": "number_typo_candidate", "confirmed_lot": "", "candidates": candidates,
            "standard_address": None, "detail_units": detail_units(row["record"]["raw_address"]), "tier": "general",
        }
        comparisons[address_id] = compared
        row["traces"].append({"module_name": "number_typo_candidates", "module_version": ENGINE_VERSION,
                              "status": "completed", "elapsed_ms": 0.0, "reason_code": "number_typo_candidate",
                              "details": {"candidates": len(candidates)}})
        counts["rows"] += 1
    return counts


def apply_ai(results: list[dict], comparisons: dict, corrections: Iterable[Mapping], matched: Iterable[Mapping],
             texts: Mapping[str, str], year: int = REFERENCE_YEAR, provider: str = "gemini", *,
             review_type: str = AI_REVIEW, reason_code: str = "ai_corrected",
             reason: str = "AI 교정 주소가 기준자료와 정확히 일치 · 사람 승인 필요",
             label: str = "AI 교정", note_key: str = "ai_correction") -> Counter:
    """AI 교정 결과를 결과 행에 반영한다. 교정한 주소가 규칙 매칭에서 정확히 확정되면 '검토 필요 · AI 교정 후보'로 두고
    그 주소를 승인 가능한 후보(확인 필요 단계)로 단다. AI 결과는 자동으로 확정하지 않는다.

    review_type·reason·label·note_key는 다른 AI 단계(예: address_pipeline의 LLM 원문 구분)가 같은 검증 절차를 쓰면서
    결과를 구분해 남길 때 바꾼다."""
    by_id = {row["record"]["address_id"]: (i, row) for i, row in enumerate(results)}
    matched = {row["address_id"]: row for row in matched}
    counts: Counter = Counter()
    for fix in corrections:
        address_id = fix["address_id"]
        if address_id not in by_id:
            continue
        index, row = by_id[address_id]
        corrected = fix.get("corrected_address")
        note = {"original": fix.get("original_address"), "corrected": corrected, "changes": fix.get("changes"),
                "inferred": fix.get("inferred"), "verified": False}
        second = matched.get(address_id) if corrected else None
        built = build_single(second, index, InputRecord(address_id, corrected), year, text=corrected) if second else None
        if built and built.row["status"] in {"resolved", "address_only"}:
            change = f" ({fix['changes']})" if fix.get("changes") else ""
            evidence = [f"{label}: '{fix.get('original_address')}' → '{corrected}'{change}"]
            if fix.get("inferred"):
                evidence.append(f"AI가 원문에 없던 지역을 보충함: {fix['inferred']}")
            note["verified"] = True
            text = texts.get(address_id) or row["record"]["raw_address"]
            candidate, place = _adopt(row, built, second, text, comparisons, review_type=review_type,
                                      reason_code=reason_code, reason=reason, evidence=evidence, year=year)
            # 오타가 아니라 원문에 적힌 시도·시군구 자체를 바꾼 교정(서울특별시 칠곡군 → 경상북도 칠곡군)은
            # 원문 지역이 서로 맞지 않는 입력이다. 후보로는 보여 주되 경고를 달고 빠른 승인 단계에서 뺀다.
            changed = [c for c in compare(text, place) if c["component"] in ("시도", "시군구") and c["outcome"] == "mismatch"]
            if changed:
                candidate["evidence"].insert(0, "주의: AI가 원문의 " + ", ".join(
                    f"{c['component']}({c['input_value']}→{c['candidate_value']})" for c in changed)
                    + "을(를) 바꿈 · 원문 지역이 서로 맞지 않는 입력")
                note["region_changed"] = [c["component"] for c in changed]
                row["review"]["tier"] = "general"
            counts["verified_region_changed" if changed else "verified"] += 1
        else:
            counts["unverified" if corrected else "no_correction"] += 1
            if second:
                note["bigquery_status"] = second.get("status")
        row["output"][note_key] = note
        note["provider"] = provider
        row["traces"].append({"module_name": f"{provider}_correction", "module_version": ENGINE_VERSION,
                              "status": "completed", "elapsed_ms": 0.0,
                              "reason_code": "ai_corrected_verified" if note["verified"] else "ai_corrected_unverified",
                              "details": note})
    return counts


def apply_region_notes(results: list[dict], notes: Mapping[str, list[dict]]) -> int:
    """옛 행정구역 이름을 바꿔 대조한 행에 그 내역을 남긴다(판정 사유·후보 근거·처리 이력)."""
    changed = 0
    for row in results:
        items = notes.get(row["record"]["address_id"])
        if not items:
            continue
        text = ", ".join(f"{n['old']} → {n['new']}({n['effective']})" for n in items)
        line = f"옛 행정구역 이름을 현재 이름으로 바꿔 대조: {text}"
        row["output"]["old_region"] = items
        row["output"]["reason"] = f"{line} · {row['output'].get('reason') or ''}".rstrip(" ·")
        for candidate in (row.get("review") or {}).get("candidates") or []:
            candidate["evidence"].insert(0, line)
        row["traces"].append({"module_name": "old_region_rename", "module_version": ENGINE_VERSION,
                              "status": "completed", "reason_code": "old_region_renamed", "elapsed_ms": 0.0,
                              "details": {"renamed": items}})
        changed += 1
    return changed


def refresh_summary(dataset: dict) -> None:
    """결과 행이 바뀐 뒤(AI 교정 반영) 집계를 다시 만든다."""
    results = dataset["results"]
    summary = dict(Counter(row["status"] for row in results).most_common())
    review_summary: dict[str, Counter] = defaultdict(Counter)
    for row in results:
        if row["review"]:
            review_summary[row["review"]["review_type"]][row["review"]["status"]] += 1
    relations = Counter((row["output"].get("road_lot_relation") or {}).get("kind") for row in results)
    relations.pop(None, None)
    dataset["summary"] = summary
    dataset["review_summary"] = {key: dict(value) for key, value in review_summary.items()}
    batch = dataset["batch_results"][0]
    batch.update(summary=summary, review_summary=dataset["review_summary"], road_lot_relations=dict(relations))


# ---------------------------------------------------------------- 어댑터

@dataclass
class BigQueryEngineAdapter:
    """워크벤치 어댑터 계약(run_dataset, reference_version, annotate_rows)을 BigQuery로 채운다."""
    settings: Settings = field(default_factory=Settings.from_env)
    reference_version: str = str(REFERENCE_YEAR)
    stage: BigQueryStage | None = None
    parallel_jobs = 2

    def __post_init__(self):
        self.stage = self.stage or BigQueryStage(self.settings)

    def _match_sql(self) -> str:
        routine = f"`{self.settings.table('parse_address_candidates')}`"
        parser = self.settings.sql("04_parse_function.sql").replace(
            f"CREATE OR REPLACE FUNCTION {routine}", "CREATE TEMP FUNCTION parse_address_candidates", 1,
        )
        parser = re.sub(r"^OPTIONS \(description = .*\)\n", "", parser, count=1, flags=re.MULTILINE)
        _, separator, match = self.settings.sql("05_standardize_all.sql").partition("\nAS\n")
        if not separator:
            raise ValueError("05_standardize_all.sql의 매칭 쿼리를 찾지 못했습니다.")
        match = match.replace(routine, "parse_address_candidates")
        details = self.settings.sql("06_engine_details.sql")
        return f"{parser}\nCREATE TEMP TABLE _SESSION.engine_standardized AS\n{match}\n{details}"

    @property
    def ai_enabled(self) -> bool:
        """규칙으로 못 찾은 행을 AI로 교정할지. ADDRESS_BQ_AI=0이면 끈다(기본 켬)."""
        return os.environ.get("ADDRESS_BQ_AI", "1").strip() not in {"0", "false", "off", ""}

    @property
    def ai_provider(self) -> str:
        """오타 교정에 쓸 모델. ADDRESS_BQ_AI_PROVIDER=openai(묶어서 호출, 기본) 또는 gemini(BigQuery AI.GENERATE)."""
        provider = os.environ.get("ADDRESS_BQ_AI_PROVIDER", "openai").strip().lower()
        if provider not in {"gemini", "openai"}:
            raise ValueError("ADDRESS_BQ_AI_PROVIDER는 gemini 또는 openai로 지정하세요.")
        return provider

    def _openai_fixes(self, targets: list[str], texts: Mapping[str, str], rows: list[dict], ai_batch: str,
                      chunks: list[dict] | None = None) -> list[dict]:
        """OpenAI로 묶어서 교정하고, 교정한 주소를 다시 매칭할 수 있게 input_raw의 AI 묶음에 올린다.

        요청은 끊지 않고 흘려보내고, 묶음(ADDRESS_BQ_AI_CHUNK행)이 끝날 때마다 걸린 시간을 chunks에 남긴다.
        다시 매칭은 교정이 모두 끝난 뒤 한 번만 한다(기준표를 묶음마다 다시 읽지 않게).
        """
        from .openai_correct import correct, make_generate

        start = perf_counter()

        def record(chunk: list[dict], done: int, total: int) -> None:
            if chunks is not None:
                chunks.append({"rows_done": done, "rows_total": total,
                               "corrected": sum(1 for fix in chunk if fix["corrected_address"]),
                               "failed": sum(1 for fix in chunk if fix["error"]),
                               "elapsed_sec": round(perf_counter() - start, 1)})

        size, concurrency, chunk = self.ai_openai_settings
        fixes = correct({i: texts[i] for i in targets}, make_generate(), batch_size=size,
                        concurrency=concurrency, chunk_size=chunk, on_chunk=record)
        source = {row["address_id"]: row["source_row"] for row in rows}
        upload = [{"batch": ai_batch, "address_id": fix["address_id"], "road_address": fix["corrected_address"],
                   "lot_address": "", "source_row": source[fix["address_id"]]}
                  for fix in fixes if fix["corrected_address"]]
        if upload:
            self.stage.append_rows("input_raw", upload)
        return fixes

    @property
    def ai_openai_settings(self) -> tuple[int, int, int]:
        """(요청당 주소 수, 동시 요청 수, 진행 기록 묶음 행 수). ADDRESS_BQ_AI_BATCH·_CONCURRENCY·_CHUNK, 기본 24·16·480."""
        from .openai_correct import BATCH_SIZE, CHUNK_SIZE, CONCURRENCY

        values = tuple(int(os.environ.get(f"ADDRESS_BQ_AI_{name}", str(default)))
                       for name, default in (("BATCH", BATCH_SIZE), ("CONCURRENCY", CONCURRENCY), ("CHUNK", CHUNK_SIZE)))
        if min(values) < 1:
            raise ValueError("ADDRESS_BQ_AI_BATCH·ADDRESS_BQ_AI_CONCURRENCY·ADDRESS_BQ_AI_CHUNK는 1 이상이어야 합니다.")
        return values

    @property
    def ai_max(self) -> int:
        """한 번 실행에서 AI로 교정할 최대 행 수(비용 상한). ADDRESS_BQ_AI_MAX, 기본 3000."""
        return int(os.environ.get("ADDRESS_BQ_AI_MAX", "3000"))

    def _with_parser(self, name: str) -> str:
        """원문 분석 함수(04)를 이 실행에서만 쓰는 임시 함수로 앞에 붙여 SQL 파일을 실행 가능한 스크립트로 만든다."""
        routine = f"`{self.settings.table('parse_address_candidates')}`"
        parser = self.settings.sql("04_parse_function.sql").replace(
            f"CREATE OR REPLACE FUNCTION {routine}", "CREATE TEMP FUNCTION parse_address_candidates", 1)
        parser = re.sub(r"^OPTIONS \(description = .*\)\n", "", parser, count=1, flags=re.MULTILINE)
        return f"{parser}\n{self.settings.sql(name).replace(routine, 'parse_address_candidates')}"

    def _sql(self, name: str) -> str:
        return self.settings.sql(name)

    def _tables(self) -> None:
        self.stage.run_sql_file("00_tables.sql")

    def _cleanup(self, batches: list[str]) -> None:
        self.stage.query(f"DELETE FROM `{self.settings.table('input_raw')}` WHERE batch IN UNNEST(@batches)",
                         array={"batches": ("STRING", batches)})

    def _ai_sql(self) -> str:
        sql = self.settings.sql("07_ai_correct.sql")
        endpoint = os.environ.get("ADDRESS_BQ_AI_ENDPOINT", "").strip()
        if endpoint:
            if not re.fullmatch(r"[A-Za-z0-9._-]+", endpoint):
                raise ValueError("ADDRESS_BQ_AI_ENDPOINT에는 모델 이름만 적으세요.")
            sql = sql.replace("    output_schema =>", f"    endpoint => '{endpoint}',\n    output_schema =>", 1)
        return sql

    def run_dataset(self, records: tuple[InputRecord, ...], *, run_id: str,
                    progress: Progress | None = None) -> AdapterResult:
        batch = f"wb-{run_id}"
        steps: list[dict] = []

        def step(name: str, action, **details):
            start = perf_counter()
            value = action()
            steps.append({"module_name": name, "module_version": ENGINE_VERSION, "status": "completed",
                          "reason_code": None, "elapsed_ms": round((perf_counter() - start) * 1000, 1),
                          "details": details})
            return value

        segments = {record.address_id: prepare(record.raw_address) for record in records}
        rows = []
        for i, record in enumerate(records):
            parts = segments[record.address_id]
            texts = [(f"{record.address_id}#{k}", seg) for k, seg in enumerate(parts, start=1)] if len(parts) > 1 \
                else [(record.address_id, parts[0] if parts else (record.raw_address or ""))]
            rows += [{"batch": batch, "address_id": key, "road_address": value, "lot_address": "", "source_row": i + 1}
                     for key, value in texts]
        multi = sum(1 for parts in segments.values() if len(parts) > 1)
        texts = {record.address_id: parts[0] for record in records if len(parts := segments[record.address_id]) == 1}
        ai_batch = f"{batch}-ai"
        step("bigquery_tables", self._tables)
        try:
            step("bigquery_upload", lambda: self.stage.append_rows("input_raw", rows),
                 rows=len(rows), multi_address_inputs=multi)
            found = step("bigquery_match", lambda: [dict(row.items()) for row in self.stage.query(
                self._match_sql(), {"batch": batch})])
            dataset, comparisons = build_dataset(records, found, run_id=run_id, steps=steps, segments=segments)
            add_parcel_evidence(dataset["results"])
            phrases = building_requests(dataset["results"], texts)
            if phrases:
                # 지역 확인은 건물명 조각을 뺀 원문으로 한다(남해군 … 진주시산림조합의 '진주시'로 진주시를 찾지 않게).
                reqs = [json.dumps({"id": i, "text": compact(texts[i]).replace(compact(original), " ", 1),
                                    "phrase": name, "core": core}, ensure_ascii=False)
                        for i, (original, name, core) in phrases.items()]
                matches = step("building_names", lambda: [dict(row.items()) for row in self.stage.query(
                    self._sql("08_building_candidates.sql"), array={"reqs": ("STRING", reqs)})],
                    rows=len(reqs))
                steps[-1]["details"].update(dict(apply_buildings(dataset["results"], comparisons, matches, phrases, texts)))
                refresh_summary(dataset)
            gone = abolished_targets(dataset["results"], texts)
            if gone:
                reqs = [json.dumps({"id": i, "text": texts[i]}, ensure_ascii=False) for i in gone]
                hits = step("abolished_addresses", lambda: [dict(row.items()) for row in self.stage.query(
                    self._with_parser("09_abolished_addresses.sql"), array={"reqs": ("STRING", reqs)})], rows=len(reqs))
                steps[-1]["details"].update(dict(apply_abolished(dataset["results"], hits, comparisons=comparisons,
                                                                 texts=texts)))
                refresh_summary(dataset)
            typos = typo_targets(dataset["results"], texts)
            if typos:
                reqs = [json.dumps({"id": i, "text": texts[i]}, ensure_ascii=False) for i in typos]
                hits = step("number_typo_candidates", lambda: [dict(row.items()) for row in self.stage.query(
                    self._with_parser("10_number_typo_candidates.sql"), array={"reqs": ("STRING", reqs)})], rows=len(reqs))
                steps[-1]["details"].update(dict(apply_typos(dataset["results"], comparisons, hits, texts)))
                refresh_summary(dataset)
            glued = glued_targets(dataset["results"], texts)
            if glued:
                split_batch = f"{batch}-split"
                step("glued_number_split", lambda: self.stage.append_rows("input_raw", [
                    {"batch": split_batch, "address_id": i, "road_address": variant, "lot_address": "",
                     "source_row": next(r["source_row"] for r in rows if r["address_id"] == i)}
                    for i, (variant, *_rest) in glued.items()]), rows=len(glued))
                again = [dict(row.items()) for row in self.stage.query(self._match_sql(), {"batch": split_batch})]
                steps[-1]["details"].update(dict(apply_glued(dataset["results"], comparisons, glued, again)))
                refresh_summary(dataset)
            targets =[i for i in ai_targets(dataset["results"]) if i in texts][:self.ai_max] if self.ai_enabled else []
            if targets:
                provider = self.ai_provider
                if provider == "openai":
                    size, concurrency, chunk = self.ai_openai_settings
                    chunks: list[dict] = []
                    fixes = step("ai_correct", lambda: self._openai_fixes(targets, texts, rows, ai_batch, chunks),
                                 rows=len(targets), provider=provider, batch_size=size, concurrency=concurrency,
                                 chunk_size=chunk, chunks=chunks)
                else:
                    fixes = step("ai_correct", lambda: [dict(row.items()) for row in self.stage.query(
                        self._ai_sql(), {"batch": batch, "ai_batch": ai_batch}, array={"ids": ("STRING", targets)})],
                        rows=len(targets), provider=provider)
                second = step("ai_rematch", lambda: [dict(row.items()) for row in self.stage.query(
                    self._match_sql(), {"batch": ai_batch})])
                counts = apply_ai(dataset["results"], comparisons, fixes, second, texts, provider=provider)
                steps[-1]["details"].update(dict(counts))
                refresh_summary(dataset)
        finally:
            # 워크벤치 실행분(원문·AI 교정본)은 input_raw에 남기지 않는다(원본 입력 묶음과 섞이지 않게).
            self._cleanup([batch, ai_batch, f"{batch}-split"])
        renamed = {record.address_id: notes for record in records
                   if (notes := rename_old_regions(record.raw_address)[1])}
        if renamed:
            steps.append({"module_name": "old_region_rename", "module_version": ENGINE_VERSION, "status": "completed",
                          "reason_code": None, "elapsed_ms": 0.0,
                          "details": {"rows": apply_region_notes(dataset["results"], renamed)}})
        if progress is not None:
            progress(dataset["results"], dataset["batch_results"][0])
        return AdapterResult(dataset, comparisons)

    def annotate_rows(self, rows: list[dict]) -> None:
        """제출 양식 19개 컬럼(output_fields)을 BigQuery의 raw_2025로 채운다."""
        from .output_fields import annotate

        annotate(rows, self.raw_lookup, int(self.reference_version), self.building_lookup)

    def raw_lookup(self, keys: set[tuple[int, int]]) -> dict[tuple[int, int], dict]:
        from .output_fields import _RAW_COLUMNS

        serials = sorted(serial for year, serial in keys if year == REFERENCE_YEAR)
        if not serials:
            return {}
        columns = ", ".join(f"`{name}`" for name in _RAW_COLUMNS)
        rows = self.stage.query(
            f"SELECT CAST(`일련번호` AS INT64) AS serial, {columns} FROM `{self.settings.table('raw_2025')}` "
            "WHERE CAST(`일련번호` AS INT64) IN UNNEST(@serials)", array={"serials": ("INT64", serials)})
        return {(REFERENCE_YEAR, row["serial"]): {**{name: row[name] for name in _RAW_COLUMNS}, "_year": REFERENCE_YEAR}
                for row in rows}

    def building_lookup(self, keys: set[tuple[int, int, int, int]]) -> dict[tuple[int, int, int, int], list[dict]]:
        found: dict[tuple[int, int, int, int], list[dict]] = {key: [] for key in keys}
        wanted = sorted(f"{road}-{main}-{sub}" for year, road, main, sub in keys if year == REFERENCE_YEAR)
        if not wanted:
            return found
        rows = self.stage.query(
            "SELECT CAST(`도로명코드` AS INT64) AS road, SAFE_CAST(`건물본번` AS INT64) AS main, "
            "IFNULL(SAFE_CAST(`건물부번` AS INT64), 0) AS sub, `법정동리명`, `법정동읍면동명`, `대지구분코드`, "
            f"`지번본번`, `지번부번` FROM `{self.settings.table('raw_2025')}` "
            "WHERE FORMAT('%d-%d-%d', CAST(`도로명코드` AS INT64), SAFE_CAST(`건물본번` AS INT64), "
            "IFNULL(SAFE_CAST(`건물부번` AS INT64), 0)) IN UNNEST(@keys) ORDER BY CAST(`일련번호` AS INT64)",
            array={"keys": ("STRING", wanted)})
        for row in rows:
            found.setdefault((REFERENCE_YEAR, row["road"], row["main"], row["sub"]), []).append({
                key: row[key] for key in ("법정동리명", "법정동읍면동명", "대지구분코드", "지번본번", "지번부번")})
        return found


def create_adapter() -> BigQueryEngineAdapter:
    return BigQueryEngineAdapter()
