"""행정구역 명칭과 도로명을 추출하고 라벨 판정은 DB에 맡기는 파서."""

from dataclasses import replace
import json
import os
from pathlib import Path
import re
from typing import Callable
from unicodedata import normalize

from .contracts import (
    AddressRecord, LabeledToken, LeadingNameCandidate, Module, ModuleResult, ModuleTrace, ParseOutput,
    RoadJoinCandidate, RunContext,
)
from .region_validation import SIDO_ALIASES


# 2026-11-13 4,000행 실측(요청당 24, 배치 480, 동시 배치 3): 동시 요청 4는 105초, 16은 45초, 정확도 동일.
# gpt-5.6-luna 계정 한도(분당 토큰 200만)에서 16은 분당 약 30만 토큰 수준이다.
DEFAULT_LLM_CONCURRENCY = 16
LABEL_ORDER = {
    "시도": 0,
    "시군구": 1,
    "읍면동": 2,
    "리": 3,
    "도로명": 4,
}
COMPOUND_SIGUNGU = re.compile(r"([가-힣]+시)\s*([가-힣]+구)")
ADDRESS_NUMBER = re.compile(r"(?:^|\s|[로길동면읍리구군시도가])\d+(?:\s*-\s*\d+)?\s*$")

SYSTEM_PROMPT = """너는 한국 주소에서 행정구역명과 도로명을 추출하는 파서다.
입력 주소 한 건에서 시도, 시군구, 읍면동, 리, 도로명에 해당하는 명칭을 찾아 입력에 나타난 순서대로 반환한다.

[규칙 1] 입력에 있는 명칭만 추출한다.
- 빠진 시도, 시군구, 읍면동, 리, 도로명을 추가하지 않는다.
- 약칭과 접미사 생략을 그대로 유지한다. '경북'을 '경상북도'로 바꾸거나 '구미'를 '구미시'로 바꾸지 않는다.
- '서울', '강남', '대치'도 입력된 형태 그대로 반환한다.
- '반포면 온천리'에 '충청남도'나 '공주시'를 추가하지 않는다.

[규칙 2] 명칭 단위로 나누고 상세주소는 제외한다.
- 붙어 있는 명칭은 분리한다. '고양시덕양구'는 '고양시 덕양구'로, '서울강남대치2동'은 '서울 강남 대치2동'으로 나눈다.
- 지번 본번·부번, 건물번호, 건물 동·호·층, 우편번호는 제외한다.
- 기업명, 일반 건물명, 아파트명, 괄호 기호와 구분용 특수문자는 제외한다.
- 괄호 안에 실제 행정구역명이나 도로명이 있으면 입력에 등장한 위치에 그 명칭을 남긴다.
- '대치2동', '개야도4길', '용당로331번길'처럼 명칭 자체에 포함된 숫자는 유지한다. 도로명 일부를 잘라내지 않는다.
- 한 명칭 안에 들어간 공백은 붙인다. '잿 골길'은 '잿골길', '장안산단1 로'는 '장안산단1로'로 반환한다.
- '잠실역상가'는 이 작업에서 도로명으로 취급해 남긴다.
- '남성로', '세종로', '시장북로'는 주소 명칭으로 등장하면 그대로 남긴다.

[규칙 3] 명백한 오타만 수정한다.
- 의도한 명칭이 명백한 오타만 수정해서 반환한다. '대전강역시'는 '대전광역시'로 반환한다.
- 약칭, 접미사 생략, 판단이 애매한 표기는 그대로 유지한다.

[출력 규칙]
- 추출한 명칭만 공백 하나로 구분해 한 줄로 출력한다.
- 라벨, 주소 유형, 설명, JSON, 마크다운, 따옴표는 출력하지 않는다.
- 추출할 명칭이 없으면 빈 문자열을 반환한다.

[예시]
입력: 서울강남대치2동 → 출력: 서울 강남 대치2동
입력: 반포면 온천리 → 출력: 반포면 온천리
입력: 대전강역시 유성구 → 출력: 대전광역시 유성구
입력: 경기도 수원시 팔달구 인계동 1번지 2층 305호 → 출력: 경기도 수원시 팔달구 인계동
입력: 서울특별시 마포구 상암동 월드컵북로 396 → 출력: 서울특별시 마포구 상암동 월드컵북로
입력: 테헤란로 152 (역삼동) → 출력: 테헤란로 역삼동
입력: 대치 은마아파트 → 출력: 대치
입력: 대치동 은마아파트 → 출력: 대치동
입력: 강남 논현 → 출력: 강남 논현
입력: 경북구미시원평동 100-2 → 출력: 경북 구미시 원평동
입력: 경북 구미 송정동 → 출력: 경북 구미 송정동
입력: 대구광역시 중구 남성로 → 출력: 대구광역시 중구 남성로"""
BATCH_PROMPT = (
    f"{SYSTEM_PROMPT}\n\n"
    "[묶음 요청 형식]\n"
    "입력은 id와 address를 담은 JSON 배열이다. 각 address는 독립적으로 처리하며 다른 행의 내용을 섞지 않는다. "
    "위 출력 규칙은 주소별 tokens 문자열에 적용한다. "
    '전체 응답은 [{"id":0,"tokens":"서울 강남구"}] 형식의 JSON 배열로 반환한다. '
    "입력의 정수 id를 그대로 복사하고 입력마다 결과를 정확히 한 번 반환한다."
)
RETRY_INSTRUCTIONS = (
    "\n\n[명칭 추출 재검증]\n"
    "첫 결과에서 명칭이 빠졌거나 경계가 잘못된 주소다. 입력에 있는 명칭을 원문 순서대로 다시 추출한다. "
    "붙여 쓴 주소는 끝의 번호를 제외한 명칭 구간을 끝에서부터 읽어 각 경계를 찾는다. "
    "명칭을 그대로 복사하고 특히 끝의 리 이름에서 앞 글자를 생략하지 않는다. "
    "예: 충청남도공주시반포면온천리18-7 → 충청남도 공주시 반포면 온천리. "
    "서상리와 반월중앙동은 각각 한 명칭이다. 덕남리30-2는 덕남리만 반환한다. "
    "원문 순서와 위 출력 형식을 지킨다."
)
TRAILING_NUMBER = re.compile(r"\d+(?:\s*-\s*\d+)?$")
LEADING_NAME_FRAGMENT = re.compile(r"[가-힣]+\Z")
# 이웃 토큰 사이에 이 숫자만 있으면 명칭의 일부일 수 있어 붙인 이름도 후보로 둔다(삼봉길71 번길).
JOINABLE_NUMBER = re.compile(r"\d{1,4}")
DIGIT_SPACE_DIGIT = re.compile(r"\d\s+\d")
HANGUL_RUN = re.compile(r"[가-힣]+")
SOURCE_NAME = re.compile(r"[가-힣]+(?:\d+[가-힣]+)*")
NAME_BEFORE_ADDRESS_NUMBER = re.compile(
    rf"\s*({SOURCE_NAME.pattern})\s*(?:산\s*)?\d+(?:\s*-\s*\d+)?(?![\w-]|\s*-\s*\d)"
)


def _decode_tokens(
    payload: object, address: str, *, labeled: bool = True,
) -> tuple[list[LabeledToken], tuple[str, ...]]:
    if not isinstance(payload, list):
        raise ValueError("응답은 토큰 객체의 JSON 배열이어야 합니다.")

    tokens: list[LabeledToken] = []
    remaining = []
    offset = 0
    required = {"raw", "labels"} if labeled else {"raw"}
    for item in payload:
        if not isinstance(item, dict) or item.keys() not in (required, required | {"corrected"}):
            raise ValueError("토큰 응답에 필수 필드가 없거나 허용하지 않는 필드가 있습니다.")
        raw, labels = item["raw"], item.get("labels", [])
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("raw는 비어 있지 않은 문자열이어야 합니다.")
        if ADDRESS_NUMBER.search(raw):
            raise ValueError("raw에는 명칭에 딸린 주소 번호를 포함할 수 없습니다.")
        if labeled and (not isinstance(labels, list) or not 1 <= len(labels) <= 2
                or any(not isinstance(label, str) or label not in LABEL_ORDER for label in labels)
                or len(set(labels)) != len(labels)):
            raise ValueError("labels는 중복 없이 허용 라벨을 1~2개 담아야 합니다.")
        corrected = None
        if "corrected" in item:
            corrected = item["corrected"]
            if (not isinstance(corrected, str) or not corrected.strip()
                    or corrected != corrected.strip() or corrected == raw
                    or ADDRESS_NUMBER.search(corrected)):
                raise ValueError("corrected에는 원문과 다른 명칭 수정값이 필요합니다.")

        # Consume each source span once and preserve the text skipped between tokens.
        start = address.find(raw, offset)
        if start < 0:
            raise ValueError("입력 순서와 일치하지 않는 토큰입니다.")
        gap = address[offset:start].strip()
        if gap:
            remaining.append(gap)
        offset = start + len(raw)

        compound = COMPOUND_SIGUNGU.fullmatch(raw) if not labeled or labels == ["시군구"] else None
        if compound:
            corrected_compound = COMPOUND_SIGUNGU.fullmatch(corrected) if corrected else None
            if corrected and corrected_compound is None:
                raise ValueError("복합 시군구의 수정값도 시와 구로 나뉘어야 합니다.")
            corrected_parts = corrected_compound.groups() if corrected_compound else compound.groups()
            for part, corrected_part in zip(compound.groups(), corrected_parts, strict=True):
                token: LabeledToken = {"raw": part, "labels": labels.copy()}
                if corrected_part != part:
                    token["corrected"] = corrected_part
                tokens.append(token)
        else:
            token = {"raw": raw, "labels": labels}
            if corrected is not None:
                token["corrected"] = corrected
            tokens.append(token)

    tail = address[offset:].strip()
    if tail:
        remaining.append(tail)
    return tokens, tuple(remaining)


def _address_text(record: AddressRecord) -> str:
    return (record.normalized_address if record.normalized_address is not None else record.raw_address) or ""


def _blank_result() -> ModuleResult[ParseOutput]:
    return ModuleResult(ParseOutput("blank_input"), ModuleTrace("completed", "blank_input"))


def _parse_tokens(
    decode: Callable[[], tuple[list[LabeledToken], tuple[str, ...]]], *, labeled: bool = True,
) -> ModuleResult[ParseOutput]:
    try:
        tokens, remaining = decode()
    except (TypeError, ValueError) as exc:
        return ModuleResult(None, ModuleTrace(
            "failed", "invalid_label_response" if labeled else "invalid_token_response",
            {"error_type": type(exc).__name__},
        ))
    if labeled:
        tokens.sort(key=lambda token: LABEL_ORDER[token["labels"][0]])
    return ModuleResult(
        ParseOutput("passed", unresolved_segments=remaining, tokens=tokens),
        ModuleTrace("completed", details={"token_count": len(tokens)}),
    )


def _parse_many(
    contexts: tuple[RunContext, ...], generate: Callable[[str, str], str],
    prompt: str, decode: Callable[[object, str], tuple[list[LabeledToken], tuple[str, ...]]],
    *, labeled: bool,
) -> tuple[ModuleResult[ParseOutput], ...]:
    addresses = {i: _address_text(context.record) for i, context in enumerate(contexts)}
    active = {i: address for i, address in addresses.items() if address.strip()}
    results = {i: _blank_result() for i in addresses.keys() - active.keys()}
    if active:
        request = json.dumps(
            [{"id": i, "address": address} for i, address in active.items()], ensure_ascii=False,
        )
        try:
            response = contexts[0].call_llm(lambda: generate(prompt, request))
        except Exception as exc:
            failure = ModuleResult(None, ModuleTrace("failed", "module_exception", {"error_type": type(exc).__name__}))
            results.update(dict.fromkeys(active, failure))
        else:
            try:
                rows = json.loads(response)
                if not isinstance(rows, list):
                    raise ValueError("배치 응답은 JSON 배열이어야 합니다.")
            except (TypeError, ValueError) as exc:
                failure = ModuleResult(None, ModuleTrace(
                    "failed", "invalid_batch_response", {"error_type": type(exc).__name__},
                ))
                results.update(dict.fromkeys(active, failure))
            else:
                by_id = {}
                duplicates = set()
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    row_id = row.get("id")
                    if type(row_id) is not int or row_id not in active:
                        continue
                    if row_id in by_id:
                        duplicates.add(row_id)
                    by_id[row_id] = row.get("tokens") if labeled else {
                        key: value for key, value in row.items() if key != "id"
                    }
                for i, address in active.items():
                    if i in duplicates or i not in by_id:
                        reason = "duplicate_batch_id" if i in duplicates else "missing_batch_id"
                        results[i] = ModuleResult(None, ModuleTrace("failed", reason))
                    else:
                        results[i] = _parse_tokens(
                            lambda: decode(by_id[i], address), labeled=labeled,
                        )
    return tuple(results[i] for i in addresses)


def _make_openai_generate(env_file: str | Path | None) -> Callable[[str, str], str]:
    if env_file is None:
        settings = os.environ
    else:
        path = Path(env_file)
        if not path.is_file():
            raise FileNotFoundError(path)
        from dotenv import dotenv_values

        settings = dotenv_values(path)

    key = (settings.get("OPENAI_API_KEY") or "").strip()
    model = (settings.get("OPENAI_MODEL") or "").strip()
    if not key or not model:
        raise ValueError("OPENAI_API_KEY와 OPENAI_MODEL을 지정하세요.")

    from openai import OpenAI

    client = OpenAI(api_key=key, base_url="https://api.openai.com/v1", timeout=60)

    def generate(prompt: str, address: str) -> str:
        return client.responses.create(
            model=model, instructions=prompt, input=address, store=False,
        ).output_text

    return generate


def _decode_compact(payload: object, _address: str) -> tuple[list[LabeledToken], tuple[str, ...]]:
    if not isinstance(payload, dict) or payload.keys() != {"tokens"}:
        raise ValueError("응답에는 명칭 문자열만 있어야 합니다.")
    return _decode_line(payload["tokens"])


def _decode_line(text: object) -> tuple[list[LabeledToken], tuple[str, ...]]:
    if not isinstance(text, str) or len(text.strip().splitlines()) > 1 or "|" in text:
        raise ValueError("명칭은 공백으로 구분한 한 줄이어야 합니다.")
    names = text.split()
    return [{"raw": name, "labels": []} for name in names], ()


def _exact_spans(
    patterns: list[re.Pattern[str]], source: str, start: int, end: int,
) -> list[tuple[int, int]] | None:
    spans = []
    for pattern in patterns:
        match = pattern.search(source, start, end)
        if match is None:
            return None
        spans.append(match.span())
        start = match.end()
    return spans


def _spacing_spans(names: list[str], source: str) -> list[tuple[int, int]] | None:
    """같은 문자가 같은 순서로 나타나는 유일한 원문 구간을 찾는다."""
    first = []
    options = []
    offset = 0
    for name in names:
        # 명칭 내 숫자 묶음은 유지한다. 예를 들어 12와 3을 123으로 합치지 않는다.
        pieces = re.findall(r"\d+|[^\d\s]", name)
        if not pieces:
            return None
        pattern = r"\s*".join(map(re.escape, pieces))
        matches = [match.span(1) for match in re.finditer(rf"(?=({pattern}))", source)]
        span = next((match for match in matches if match[0] >= offset), None)
        if span is None:
            return None
        first.append(span)
        options.append(matches)
        offset = span[1]

    # 처음부터 찾은 결과와 끝에서 찾은 결과가 다르면 원문 위치가 모호하다.
    offset = len(source)
    for span, matches in zip(reversed(first), reversed(options), strict=True):
        last = next(match for match in reversed(matches) if match[1] <= offset)
        if span != last:
            raise ValueError("공백을 제외한 명칭을 입력 원문의 한 위치로 확인할 수 없습니다.")
        offset = span[0]

    for i, (start, end) in enumerate(first):
        raw = source[start:end]
        if not any(char.isspace() for char in raw):
            continue
        left = first[i - 1][1] if i else 0
        right = first[i + 1][0] if i + 1 < len(first) else len(source)
        if (start > left and source[start - 1].isalnum()
                or end < right and source[end - 1].isdigit() and source[end].isdigit()
                or end < right and source[end].isalpha()):
            raise ValueError("공백이 있는 명칭의 원문 경계가 명확하지 않습니다.")
    return first


def _unordered_exact_spans(
    patterns: list[re.Pattern[str]], source: str, *, blocked: tuple[int, int] | None = None,
) -> list[tuple[int, int]] | None:
    def attached_sides(span: tuple[int, int]) -> int:
        start, end = span
        return int(start > 0 and source[start - 1].isalnum()) + int(
            end < len(source) and source[end].isalnum()
        )

    options = [
        sorted((match.span() for match in pattern.finditer(source)), key=attached_sides)
        for pattern in patterns
    ]
    order = sorted(range(len(patterns)), key=lambda index: len(options[index]))
    chosen = [(0, 0)] * len(patterns)
    occupied = [] if blocked is None else [blocked]

    def assign(depth: int) -> bool:
        if depth == len(order):
            return True
        index = order[depth]
        for start, end in options[index]:
            if any(start < other_end and other_start < end for other_start, other_end in occupied):
                continue
            chosen[index] = (start, end)
            occupied.append((start, end))
            if assign(depth + 1):
                return True
            occupied.pop()
        return False

    return chosen if assign(0) else None


def _is_close_typo(raw: str, name: str) -> bool:
    if len(raw) != len(name) or len(raw) < 3 or not re.fullmatch(r"[가-힣]+", name):
        return False
    differences = [(a, b) for a, b in zip(raw, name, strict=True) if a != b]
    if len(differences) != 1:
        return False
    original, replacement = (normalize("NFD", char) for char in differences[0])
    if len(original) == len(replacement):
        return sum(a != b for a, b in zip(original, replacement, strict=True)) == 1
    # 받침 하나를 더 쓰거나 빠뜨린 오타(선울 ↔ 서울)도 자모 하나 차이로 본다.
    shorter, longer = sorted((original, replacement), key=len)
    return len(longer) - len(shorter) == 1 and any(
        longer[:i] + longer[i + 1:] == shorter for i in range(len(longer))
    )


def _typo_spans(
    source: str, names: list[str], patterns: list[re.Pattern[str]],
    *, ordered: bool = True,
) -> tuple[list[tuple[int, int]], int]:
    candidates = []
    words = list(re.finditer(r"[가-힣]+", source))
    for index, name in enumerate(names):
        for word in words:
            if not _is_close_typo(word.group(), name):
                continue
            if ordered:
                left = _exact_spans(patterns[:index], source, 0, word.start())
                right = _exact_spans(patterns[index + 1:], source, word.end(), len(source))
                if left is None or right is None:
                    continue
                spans = [*left, word.span(), *right]
            else:
                other_spans = _unordered_exact_spans(
                    patterns[:index] + patterns[index + 1:], source, blocked=word.span(),
                )
                if other_spans is None:
                    continue
                spans = [*other_spans[:index], word.span(), *other_spans[index:]]
                left = sorted(span for span in other_spans if span[1] <= word.start())
                right = sorted(span for span in other_spans if span[0] >= word.end())
            # Require a neighbouring exact name, or a single name occupying the whole address.
            next_to_left = left and not any(char.isalnum() for char in source[left[-1][1]:word.start()])
            next_to_right = right and not any(char.isalnum() for char in source[word.end():right[0][0]])
            anchored = next_to_left or next_to_right or source.strip() == word.group()
            candidates.append((spans, index, anchored))
    if len(candidates) != 1 or not candidates[0][2]:
        raise ValueError("입력 원문에서 수정 후보의 위치를 하나로 확인할 수 없습니다.")
    spans, index, _ = candidates[0]
    return spans, index


def _sido_alias_spans(
    patterns: list[re.Pattern[str]], names: list[str], source: str,
) -> list[tuple[int, int]] | None:
    alias_patterns = []
    for name, pattern in zip(names, patterns, strict=True):
        aliases = sorted(
            (alias for alias, official in SIDO_ALIASES.items() if official == name),
            key=lambda alias: (-len(alias), alias),
        )
        alias_patterns.append(re.compile("|".join([pattern.pattern, *map(re.escape, aliases)])) if aliases else pattern)
    spans = _exact_spans(alias_patterns, source, 0, len(source))
    if spans is None:
        return None
    for i, ((start, end), name) in enumerate(zip(spans, names, strict=True)):
        if source[start:end] == name:
            continue
        before = spans[i - 1][1] if i else 0
        after = spans[i + 1][0] if i + 1 < len(spans) else len(source)
        # An alias may touch another matched name, but cannot cut through an unmatched word.
        if (start > before and source[start - 1].isalnum()) or (end < after and source[end].isalpha()):
            return None
    return spans


def _align_to_source(
    names: list[str], address: str, *, ordered: bool = True,
    match_sido_aliases: bool = False, leading_candidates: list[LeadingNameCandidate] | None = None,
    matched_aliases: list[dict[str, str]] | None = None,
    road_join_candidates: list[RoadJoinCandidate] | None = None,
) -> tuple[list[LabeledToken], tuple[str, ...]]:
    source = normalize("NFKC", address)
    normalized_names = [normalize("NFKC", name) for name in names]
    patterns = [re.compile(r"\s+".join(map(re.escape, name.split()))) for name in normalized_names]
    spans = (_exact_spans(patterns, source, 0, len(source)) if ordered
             else _unordered_exact_spans(patterns, source))
    if spans is None and match_sido_aliases:
        spans = _sido_alias_spans(patterns, normalized_names, source)
    if spans is None and ordered:
        spans = _spacing_spans(normalized_names, source)
    corrected_index = None
    if spans is None:
        spans, corrected_index = _typo_spans(source, normalized_names, patterns, ordered=ordered)

    boundaries = None
    if source != address:
        # Map only prefixes that end at a real boundary after Unicode normalization.
        boundaries = {}
        for end in range(len(address) + 1):
            prefix = normalize("NFKC", address[:end])
            if source.startswith(prefix):
                boundaries[len(prefix)] = end

    tokens: list[LabeledToken] = []
    remaining = []
    offset = 0
    previous_start = 0
    for index, (start, end) in sorted(enumerate(spans), key=lambda item: item[1]):
        if boundaries is not None:
            if start not in boundaries or end not in boundaries:
                raise ValueError("정규화된 명칭을 입력 원문 구간에 대응할 수 없습니다.")
            start, end = boundaries[start], boundaries[end]
        raw = address[start:end]
        if matched_aliases is not None and SIDO_ALIASES.get(normalize("NFKC", raw)) == normalized_names[index]:
            matched_aliases.append({"llm": names[index], "raw": raw})
        skipped = address[offset:start]
        gap = skipped.strip()
        if leading_candidates is not None and index != corrected_index:
            # 공백으로 떨어진 앞 글자도 후보로 본다(산 성대로215번길 → 산성대로215번길). 채택은 DB 경로로 판정한다.
            prefix = LEADING_NAME_FRAGMENT.search(skipped.rstrip())
            if prefix is not None:
                leading_candidates.append(LeadingNameCandidate(len(tokens), len(remaining), prefix[0] + raw))
        if gap:
            remaining.append(gap)
        item = {"raw": raw}
        if index == corrected_index:
            item["corrected"] = normalized_names[index]
        decoded, _ = _decode_tokens([item], raw, labeled=False)
        if (
            road_join_candidates is not None and tokens and len(decoded) == 1
            and "corrected" not in item and "corrected" not in tokens[-1]
            and (not gap or JOINABLE_NUMBER.fullmatch(normalize("NFKC", gap)))
            # 숫자와 숫자 사이의 공백(진흥로2 2나길)은 상세 검색처럼 붙이지 않고 보류한다.
            and not DIGIT_SPACE_DIGIT.search(normalize("NFKC", address[previous_start:end]))
        ):
            # 원문에서 공백만 사이에 둔 이웃 토큰은 한 명칭이 잘못 끊긴 것일 수 있다(흑 석로9길).
            # 사이에 숫자만 남았으면 명칭 끝 숫자를 번호로 떼어 낸 경우일 수 있다(삼봉길71 번길).
            # 채택 여부는 지역 검증이 실패한 행에서 DB 경로로만 판정한다.
            road_join_candidates.append(RoadJoinCandidate(
                len(tokens) - 1, address[previous_start:end],
                "".join(normalize("NFKC", tokens[-1]["raw"] + gap + raw).split()),
            ))
        tokens.extend(decoded)
        previous_start = end - len(tokens[-1]["raw"])
        offset = end
    tail = address[offset:].strip()
    if tail:
        remaining.append(tail)
    return tokens, tuple(remaining)


def _source_runs_with_omission(names: list[str], source: str) -> list[str]:
    unmatched = {name for name in names if HANGUL_RUN.fullmatch(name) and name not in source}
    runs = set()
    for match in HANGUL_RUN.finditer(source):
        raw = match[0]
        for name in unmatched:
            if len(raw) != len(name) + 1:
                continue
            first_difference = next((i for i, (a, b) in enumerate(zip(raw, name)) if a != b), len(name))
            if raw[first_difference + 1:] == name[first_difference:]:
                runs.add(raw)
    return sorted(runs)


def _source_boundary_candidates(names: list[str], address: str) -> list[dict[str, object]]:
    source = normalize("NFKC", address)
    normalized_names = [normalize("NFKC", name) for name in names]
    patterns = [re.compile(re.escape(name)) for name in normalized_names]
    candidates = []
    if len(names) < 2:
        return candidates
    for index, name in enumerate(normalized_names):
        left = _exact_spans(patterns[:index], source, 0, len(source))
        if left is None:
            continue
        start = left[-1][1] if left else 0
        right = _exact_spans(patterns[index + 1:], source, start, len(source))
        if right is None:
            continue
        if right:
            raw_name = source[start:right[0][0]].strip()
            if not SOURCE_NAME.fullmatch(raw_name):
                continue
        else:
            match = NAME_BEFORE_ADDRESS_NUMBER.match(source, start)
            if match is None:
                continue
            raw_name = match[1]
        if raw_name == name:
            continue
        recovered_names = [*normalized_names[:index], raw_name, *normalized_names[index + 1:]]
        try:
            tokens, remaining = _align_to_source(recovered_names, address)
        except ValueError:
            continue
        if any("corrected" in token for token in tokens):
            continue
        # 원문 표기로 검증되지 않으면 LLM이 읽은 명칭으로 다시 검증하도록 수정값으로 남긴다.
        for token in tokens:
            if token["raw"] == raw_name and SOURCE_NAME.fullmatch(normalized_names[index]):
                token["corrected"] = normalized_names[index]
                break
        candidates.append({
            "llm_token": names[index], "source_token": raw_name,
            "tokens": tokens, "unresolved_segments": remaining,
        })
    return candidates


def _reconcile(
    result: ModuleResult[ParseOutput], record: AddressRecord,
) -> ModuleResult[ParseOutput]:
    output = result.output
    if output is None or output.screening_result != "passed":
        return result
    source = record.raw_address if record.raw_address is not None else record.normalized_address or ""
    names = [token["raw"] for token in output.tokens]
    normalized = normalize("NFKC", source)
    detached = []
    standalone = []
    aligned_names = []
    for name in names:
        if TRAILING_NUMBER.fullmatch(name):
            standalone.append(name)
            continue
        normalized_name = normalize("NFKC", name)
        if ADDRESS_NUMBER.search(normalized_name):
            number = TRAILING_NUMBER.search(normalized_name)
            assert number is not None
            head = normalized_name[:number.start()].rstrip()
            if re.search(rf"{re.escape(head)}\s*{re.escape(number[0])}(?![\w-]|\s*-\s*\d)", normalized):
                detached.append(name)
                name = head
        aligned_names.append(name)
    leading_candidates: list[LeadingNameCandidate] = []
    aliases: list[dict[str, str]] = []
    road_join_candidates: list[RoadJoinCandidate] = []
    reconciled = _parse_tokens(
        lambda: _align_to_source(
            aligned_names, source, match_sido_aliases=True, leading_candidates=leading_candidates,
            matched_aliases=aliases,
            road_join_candidates=road_join_candidates,
        ),
        labeled=False,
    )
    details = dict(reconciled.trace.details)
    if detached or standalone:
        details.update(first_failure_reason="number_attached_to_name", removed_number_tokens=detached,
                       raw_revalidation_status=reconciled.trace.status)
    if standalone:
        details["ignored_number_tokens"] = standalone
        if not detached:
            details["first_failure_reason"] = "standalone_address_number"
    output = reconciled.output
    if output is None:
        candidates = _source_boundary_candidates(aligned_names, source)
        if candidates:
            details["source_boundary_candidates"] = candidates
        runs = _source_runs_with_omission(aligned_names, normalized)
        if runs:
            details["source_runs_with_omission"] = runs
    else:
        if aliases:
            details["matched_sido_aliases"] = aliases
        output = replace(output, leading_name_candidates=tuple(leading_candidates),
                         road_join_candidates=tuple(road_join_candidates))
    return replace(reconciled, output=output, trace=replace(reconciled.trace, details=details))


def make_token_only_parser(
    generate: Callable[[str, str], str],
    *,
    generate_batch: Callable[[str, str], str] | None = None,
) -> Module[AddressRecord, ParseOutput]:
    def build(single_prompt: str, batch_prompt: str) -> Module[AddressRecord, ParseOutput]:
        def parse(context: RunContext, record: AddressRecord) -> ModuleResult[ParseOutput]:
            address = _address_text(record)
            if not address.strip():
                return _blank_result()
            response = context.call_llm(lambda: generate(single_prompt, address))
            return _reconcile(_parse_tokens(lambda: _decode_line(response), labeled=False), record)

        if generate_batch is None:
            return Module("9", parse, llm_default_concurrency=DEFAULT_LLM_CONCURRENCY)

        def parse_many(contexts: tuple[RunContext, ...]) -> tuple[ModuleResult[ParseOutput], ...]:
            outputs = _parse_many(contexts, generate_batch, batch_prompt, _decode_compact, labeled=False)
            return tuple(
                _reconcile(result, context.record)
                for context, result in zip(contexts, outputs, strict=True)
            )

        return Module("9", parse, parse_many, llm_default_concurrency=DEFAULT_LLM_CONCURRENCY)

    retry = build(SYSTEM_PROMPT + RETRY_INSTRUCTIONS, BATCH_PROMPT + RETRY_INSTRUCTIONS)
    return replace(build(SYSTEM_PROMPT, BATCH_PROMPT), retry_parser=retry)


def make_openai_token_only_parser(env_file: str | Path | None = None) -> Module[AddressRecord, ParseOutput]:
    generate = _make_openai_generate(env_file)
    return make_token_only_parser(generate, generate_batch=generate)
