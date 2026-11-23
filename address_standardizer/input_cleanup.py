"""LLM 파서 앞에서 원문 표기를 규칙으로 정리한다(BigQuery 엔진 RULES.md A 규칙에서 가져옴).

정리한 문장으로 파싱·지역 검증·번호 조회를 하고, 결과에는 원문을 되돌려 놓는다(`runner.run_dataset`).
바꾼 내용은 행마다 `input_cleanup` 처리 이력에 남긴다.

- 영문 시도(Chungnam, Seoul-si) → 한글 정식 이름(충청남도, 서울특별시)
- 시도 정식 이름과 한 글자만 다른 표기(경상남독) → 그 이름
- 번호 앞에서 지역 이름만 감싼 괄호(충청남도 (태안군) …) → 괄호 제거
- 바로 이어서 반복된 지역 낱말(부산광역시 부산광역시)·지역 묶음 → 하나만
- 역순·뒤섞인 어순(141-1 상촌길 … 경상남도, 김해시 경원로11번길 경상남도 내동 17-10)
  → 시도 → 시군구 → 읍면동 → 리 → 도로명 → 번호 → 그 밖 순서. 시도가 맨 앞이 아니거나 번호로 시작할 때만 바꾼다.

줄이 여러 개인 원문은 줄마다 따로 정리한다(복수 주소 판정은 그대로 파이프라인이 한다).
"""
from __future__ import annotations

import re
from unicodedata import normalize

SIDO = (
    ("서울특별시", "서울", "서울시"), ("부산광역시", "부산", "부산시"), ("대구광역시", "대구", "대구시"),
    ("인천광역시", "인천", "인천시"), ("광주광역시", "광주"), ("대전광역시", "대전", "대전시"),
    ("울산광역시", "울산", "울산시"), ("세종특별자치시", "세종", "세종시"), ("경기도", "경기"),
    ("강원특별자치도", "강원", "강원도"), ("충청북도", "충북"), ("충청남도", "충남"),
    ("전북특별자치도", "전북", "전라북도"), ("전라남도", "전남"), ("경상북도", "경북"),
    ("경상남도", "경남"), ("제주특별자치도", "제주", "제주도"),
)
SIDO_FORMS = {form: full for full, *rest in SIDO for form in (full, *rest)}
_SIDO_FULL = tuple(full for full, *_ in SIDO)
_EN_SIDO = {
    "seoul": "서울특별시", "busan": "부산광역시", "daegu": "대구광역시", "incheon": "인천광역시", "gwangju": "광주광역시",
    "daejeon": "대전광역시", "ulsan": "울산광역시", "sejong": "세종특별자치시", "gyeonggi": "경기도", "gangwon": "강원특별자치도",
    "chungbuk": "충청북도", "chungcheongbuk": "충청북도", "chungnam": "충청남도", "chungcheongnam": "충청남도",
    "jeonbuk": "전북특별자치도", "jeollabuk": "전북특별자치도", "jeonnam": "전라남도", "jeollanam": "전라남도",
    "gyeongbuk": "경상북도", "gyeongsangbuk": "경상북도", "gyeongnam": "경상남도", "gyeongsangnam": "경상남도",
    "jeju": "제주특별자치도",
}
_EN_SUFFIX = re.compile(r"(?:-?(?:si|do|teukbyeolsi|gwangyeoksi))$")
_REGION_WORD = r"[가-힣]+(?:특별자치도|특별자치시|특별시|광역시|도|시|군|구|읍|면|동|리|가)"
_REGION_IN_BRACKETS = re.compile(rf"\(\s*({_REGION_WORD}(?:\s+{_REGION_WORD})*)\s*\)")
_NUMBER = re.compile(r"(?:산)?[0-9][0-9-]*(?:번지)?(?:[0-9]+호)?")


def _english_sido(word: str) -> str | None:
    key = re.sub(r"[^a-z-]", "", word.lower())
    if not key or key != word.lower().strip(",."):
        return None
    key = _EN_SUFFIX.sub("", key).replace("-", "")
    return _EN_SIDO.get(key)


def _near_sido(word: str) -> str | None:
    """시도 정식 이름과 길이가 같고 한 글자만 다른 표기(경상남독·경기독 → 경상남도·경기도). 정상 표기는 바꾸지 않는다.

    끝 글자가 지역 접미사(시·군·구·읍·면·동·리·가·도)인 낱말은 다른 실제 지명일 수 있어 바꾸지 않는다."""
    if word in SIDO_FORMS or len(word) < 3 or word[-1] in "시군구읍면동리가도":
        return None
    near = [full for full in _SIDO_FULL if len(full) == len(word) and sum(a != b for a, b in zip(full, word)) == 1]
    return near[0] if len(near) == 1 else None


def _is_region_word(word: str) -> bool:
    return word in SIDO_FORMS or bool(re.fullmatch(r"[가-힣]+(?:시|군|구|읍|면|동|리|도)", word))


def _drop_repeated_block(words: list[str]) -> list[str]:
    """번호 없는 같은 낱말 묶음이 바로 이어서 두 번 나오면 하나만 남긴다."""
    for size in range(len(words) // 2, 1, -1):
        for start in range(len(words) - 2 * size + 1):
            block = words[start:start + size]
            if block == words[start + size:start + 2 * size] and not any(ch.isdigit() for ch in "".join(block)):
                return _drop_repeated_block(words[:start + size] + words[start + 2 * size:])
    return words


def _word_class(word: str) -> int:
    """어순 정리 단계: 0 시도, 1 시군구, 2 읍면동, 3 리, 4 도로명, 5 번호, 6 그 밖(상세·건물명)."""
    core = word.strip(",.")
    if core in SIDO_FORMS:
        return 0
    if _NUMBER.fullmatch(core):
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


def _reorder(words: list[str]) -> list[str]:
    classes = [_word_class(word) for word in words]
    known = [c for c in classes if c != 6]
    if not known or not (known[0] == 5 or (0 in known and known.index(0) > 0)):
        return words
    # 지역·도로·번호가 아닌 낱말(건물명 등)이 있으면 어디에 붙은 말인지 알 수 없으므로 순서를 바꾸지 않는다.
    if 6 in classes:
        return words
    return [word for _, _, word in sorted(zip(classes, range(len(words)), words))]


def _clean_line(line: str, changes: list[dict]) -> str:
    # 번호 뒤 괄호(… 2-6 (연산면))는 참고 표기라 그대로 두고, 번호 앞 괄호만 푼다.
    def unwrap(match: re.Match) -> str:
        return f" {match.group(1)} " if not re.search(r"[0-9]", line[:match.start()]) else match.group(0)
    text, count = _REGION_IN_BRACKETS.subn(unwrap, line)
    count = sum(1 for m in _REGION_IN_BRACKETS.finditer(line) if not re.search(r"[0-9]", line[:m.start()]))
    if count:
        changes.append({"rule": "region_brackets", "count": count})
    words: list[str] = []
    for word in text.split():
        fixed = _english_sido(word) or _near_sido(word)
        if fixed:
            changes.append({"rule": "english_sido" if _english_sido(word) else "sido_typo", "from": word, "to": fixed})
            word = fixed
        if words and words[-1] == word and _is_region_word(word):
            changes.append({"rule": "repeated_region", "word": word})
            continue
        words.append(word)
    deduped = _drop_repeated_block(words)
    if deduped != words:
        changes.append({"rule": "repeated_region_block"})
    ordered = _reorder(deduped)
    if ordered != deduped:
        changes.append({"rule": "reorder"})
    return " ".join(ordered)


def clean_for_matching(text: str | None) -> tuple[str | None, tuple[dict, ...]]:
    """파이프라인에 넘길 정리된 원문과 바꾼 내역. 바꾼 게 없으면 원문을 그대로 돌려준다(줄 구분도 유지)."""
    if text is None or not str(text).strip():
        return text, ()
    changes: list[dict] = []
    lines = [_clean_line(line, changes) for line in normalize("NFKC", str(text)).splitlines()]
    if not changes:
        return text, ()
    return "\n".join(line for line in lines if line), tuple(changes)
