"""보류 주소별 검토 유형과 후보를 반환한다."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
import re

from pandas import DataFrame
from rapidfuzz import fuzz, process

from .contracts import RawAddressRow, StandardAddress
from .detail_address import (
    AMBIGUOUS_SUB_OR_DETAIL, BEON_ROAD_OR_SUB, DetailAddressSearch, DetailInput, DetailResult, NumberQuery,
    ReferenceMatch, _REFERENCE_COLUMNS, _mentions_building, _project_reference, trailing_sub_number, verified_names,
    glued_unit_interpretations,
)
from .preprocessing import normalize_address
from .region_validation import HISTORICAL_NAMES, SIDO_ALIASES


VERIFIED = "기본 도로명주소 확인"
NUMBER_ROLE = "건물번호·지번 역할 모호"
SUB_OR_DETAIL = "뒤 숫자의 부번·상세주소 모호"
BEON_READING = "번길 누락·부번 해석 모호"
MIXED = "도로명·지번 병기"
OMITTED_SUB = "부번 생략"
DIFFERENT_SUB = "명시된 부번 불일치"
LOT_ROADS = "한 지번에 여러 도로명주소"
BUILDING_NAME = "번호 부족·건물명/상세만"
HISTORICAL = "기준연도 번호 미일치"
MULTIPLE = "기타·복수 주소/필지"
UNINTERPRETED = "기타·지역/번호 해석 부족"
SIMILAR_REGION = "지역명 미일치·유사 지명 후보"
AMBIGUOUS_CORRECTION = "오타 수정 결과가 하나로 정해지지 않음"
TYPO_CORRECTION = "오타 교정 후보"
NUMBER_TYPO = "건물번호·지번번호 오타 후보"
NUMBER_UNIT_GLUED = "건물번호·상세 동 붙여쓰기 후보"
ADMIN_MISMATCH_PRIORITY = 5  # 정확 일치·부번 차이 후보보다 뒤
SUGGESTION_REVIEW_LIMIT = 20

_SUB_REASONS = {"sub_number_omitted", "sub_number_mismatch"}
_MULTIPLE_REASONS = {
    "multiple_addresses", "unnamed_additional_parcels", "unparsed_parcel_list", "ambiguous_parcel_list",
}
_MISSING_REASONS = {
    "missing_base_address", "missing_address_number", "missing_address_scope", "ambiguous_number_boundary",
}
# 과거 연도 번호 일치(우선순위 0, 1, …) 다음 순위가 되도록 더하는 값.
NUMBER_MISSING_BUILDING_PRIORITY = 10
# 뒤 숫자를 부번으로 읽을 때, 같은 본번 아래 부번이 이보다 많으면 호수 같은 다른 숫자가 우연히 겹쳤을
# 수 있어 거의 확정에서 확인 필요로 내린다. 대상건 실측: 띄어 쓴 부번 15건 중 부번 수 1~25개.
TRAILING_SUB_MAX_SIBLINGS = 10
# 건물명 유사 일치(rapidfuzz). 정확한 이름이 없을 때만 같은 도로·지역 안 건물명과 비교한다.
FUZZY_NAME_MIN_SCORE = 85   # fuzz.ratio 하한. 5글자 이름에서 1글자 다름(80점)은 받지 않는다.
FUZZY_NAME_MARGIN = 5       # 2등 이름과 이만큼 차이 나야 채택한다.
FUZZY_NAME_PRIORITY = 5     # 정확한 건물명 일치보다 뒤에 둔다.
# 비교 전에 맞추는 흔한 표기 차이: APT → 아파트, 제2차 → 2차.
_NAME_ALIASES = (("a.p.t", "아파트"), ("apt", "아파트"))
_ORDINAL_PREFIX = re.compile(r"제([0-9]+)차")
# 번호 뒤 상세에서 건물명으로 볼 앞부분: 쉼표·괄호·슬래시, 또는 '숫자+동/층/호/-' 앞까지.
_NAME_PHRASE = re.compile(r"(.+?)(?=\s*(?:[0-9]+\s*(?:[동층호-]|$)|[,()/])|$)")
# '같은 본번의 다른 부번'(건물명 근거 없음) 후보와 같은 순위.
SAME_NUMBER_OTHER_READING_PRIORITY = 1
_MISSING_TYPES = {"상세주소만", "건물번호 미확인", "주소번호 없음"}
_LOT_MARKED_NUMBER = re.compile(r"([0-9]{1,5})(?:-([0-9]{1,5}))?\s*번지(?![가-힣])")
_LOCAL = {"읍면동", "리"}
_KIND_LABEL = {"road": "도로명주소", "lot": "지번주소"}


@dataclass(frozen=True)
class ReviewCandidate:
    kind: str
    address: str
    rank: int
    evidence: tuple[str, ...]
    reference_years: tuple[int, ...]
    raw_records: tuple[tuple[int, int], ...]
    linked_road_addresses: tuple[str, ...] = ()
    # 도로명 후보와 같은 RAW 행의 지번주소.
    linked_lot_addresses: tuple[str, ...] = ()
    # 같은 RAW 행의 행정동명.
    admin_dongs: tuple[str, ...] = ()
    # 도로명 후보의 표준 도로명주소와 코드 3개. 검토 승인 때 그대로 확정값으로 쓴다.
    standard_address: StandardAddress | None = None
    # 건물명으로 찾았거나 좁힌 후보: 기준자료 건물명과 원문에서 읽은 표기(유사 일치면 서로 다르다).
    building_name: str = ""
    building_input: str = ""


# 검토 단계. 화면의 '검토 필요'를 이 값으로 나눠 보여준다.
TIER_NEAR_CONFIRMED = "near_confirmed"  # 유력 · 거의 확정: 단독 1순위이고 근거가 분명함
TIER_CHECK_NEEDED = "check_needed"      # 유력 · 확인 필요: 단독 1순위지만 비슷한 곳이 더 있거나 근거가 약함
TIER_GENERAL = "general"                # 일반 보류: 공동 1순위·후보 없음
TIER_HISTORICAL = "historical"          # 과거 기준자료 일치: 1순위 후보를 과거 연도 자료에서만 찾음


@dataclass(frozen=True)
class SubNumberList:
    """원문 번호의 부번이 기준자료와 다를 때, 같은 본번 아래 기준자료에 있는 부번들(0은 부번 없음)."""
    kind: str
    number: str
    input_sub: int
    sub_numbers: tuple[int, ...]


@dataclass(frozen=True)
class AddressReview:
    review_type: str
    status: str
    base_status: str
    base_reason: str
    confirmed_lot: str
    candidates: tuple[ReviewCandidate, ...]
    standard_address: StandardAddress | None = None
    detail_units: tuple[str, ...] = ()
    tier: str = ""
    sub_numbers: tuple[SubNumberList, ...] = ()


def summarize_reviews(reviews: Iterable[AddressReview]) -> dict[str, dict[str, int]]:
    counts = defaultdict(Counter)
    for review in reviews:
        counts[review.review_type][review.status] += 1
    return {review_type: dict(totals) for review_type, totals in counts.items()}


@dataclass
class _Evidence:
    priority: int
    reasons: set[str] = field(default_factory=set)
    years: set[int] = field(default_factory=set)
    records: set[tuple[int, int]] = field(default_factory=set)
    linked_roads: set[str] = field(default_factory=set)
    linked_lots: set[str] = field(default_factory=set)
    admin_dongs: set[str] = field(default_factory=set)
    standard: StandardAddress | None = None
    # 단독 1순위일 때 '거의 확정'으로 볼 만큼 분명한 근거가 있는지(예: 시군구에 하나뿐인 건물명이 같은 도로에서 일치).
    strong: bool = False
    # 원문 번호와 건물명이 같은 건물에서 함께 맞음. 공동 1순위 가운데 하나뿐이면 그 후보를 단독 1순위로 올린다.
    name_number: bool = False
    building_name: str = ""
    building_input: str = ""


@dataclass(frozen=True)
class _BuildingScope:
    tokens: tuple[str, ...]
    road: bool
    remainder: str
    verified_roads: tuple[tuple[int, int], ...] | None = None
    # 번호가 기준자료에 없는 주소의 건물명 후보는 과거 연도 번호 일치보다 뒤에 둔다.
    number_missing: bool = False


@dataclass(frozen=True)
class _ReviewPlan:
    review_type: str
    exact: tuple[NumberQuery, ...] = ()
    alternatives: tuple[NumberQuery, ...] = ()
    building: _BuildingScope | None = None
    road_name: str = ""
    explicit_lot_scope: bool = False


def _token_labels(search: DetailAddressSearch, token: str) -> dict[str, str]:
    compact = "".join(token.split())
    return search.names.get(compact[0], {}).get(compact, {}) if compact else {}


def _number_role_plan(search, tokens, rest, mark) -> _ReviewPlan:
    note = re.match(r"\s*\(\s*([^(),]+)[,)]", rest[mark.end():])
    note_tokens, note_roles, note_rest = search._prefix(note[1].strip()) if note else ((), set(), "")
    if note_rest or not note_roles.intersection(_LOCAL):
        note_tokens = ()
    road_tokens = tuple(dict.fromkeys((*tokens, *note_tokens)))
    lot_tokens = tuple(dict.fromkeys((*tokens[:-1], *note_tokens)))
    explicit = any(
        label == "리" or (label == "읍면동" and not name.endswith(("읍", "면")))
        for token in lot_tokens for label, name in _token_labels(search, token).items()
    )
    main, sub = int(mark[1]), int(mark[2] or 0)
    queries = [NumberQuery("road", road_tokens, main, sub)]
    if main <= 9999 and sub <= 9999:
        queries.append(NumberQuery(
            "lot", lot_tokens if explicit else road_tokens, main, sub, use_road_scope=not explicit,
        ))
    return _ReviewPlan(NUMBER_ROLE, tuple(queries), road_name=tokens[-1], explicit_lot_scope=explicit)


def _plan_review(search: DetailAddressSearch, item: DetailInput, result: DetailResult) -> _ReviewPlan:
    if result.status in {"resolved", "address_only"}:
        return _ReviewPlan(VERIFIED)

    matches = result.matches
    kinds = {match.query.kind for match in matches}
    exact = tuple(match.query for match in matches if match.status == "exact")
    alternatives = tuple(match.query for match in matches if match.status in _SUB_REASONS)
    reason = result.reason
    if reason == AMBIGUOUS_SUB_OR_DETAIL and len(matches) == 1:
        # 예양길 28 1: 1을 상세주소로 본 28과 부번으로 본 28-1을 함께 후보로 보여준다.
        query = matches[0].query
        sub = trailing_sub_number(result.detail_text, matches[0])
        if sub is not None:
            return _ReviewPlan(SUB_OR_DETAIL, (query, replace(query, sub=sub, sub_explicit=True)))
    if reason == BEON_ROAD_OR_SUB:
        # 태평로152번 27: 태평로152번길 27과 태평로 152-27이 둘 다 있으면 두 해석을 함께 후보로 보여준다.
        return _ReviewPlan(BEON_READING, tuple(match.query for match in matches))
    names = verified_names(item, search.validator)
    text = names.text
    if kinds == {"road", "lot"} or reason == "multiple_addresses":
        first, tail, first_issue = search._read_query(text, verified_roles=names.roles)
        second, _, second_issue = (
            search._read_query(tail.lstrip(" ,;/"), verified_roles=names.roles)
            if first and tail else (None, "", "")
        )
        if (first and second and not first_issue and not second_issue
                and second.tokens[0] in search.parent_names and first != second):
            review_type = MIXED if first.kind != second.kind else MULTIPLE
            return _ReviewPlan(review_type, (first, second), (first, second))

    tokens, roles, rest = search._prefix(text, names.roles)
    road = "도로명" in roles
    mark = _LOT_MARKED_NUMBER.match(rest) if road and len(matches) < 2 and reason != "po_box" else None
    if mark and int(mark[1]) > 0:
        plan = _number_role_plan(search, tokens, rest, mark)
        return replace(plan, exact=tuple(search.use_verified_scope(query, names) for query in plan.exact))
    if result.status == "no_match":
        # 번호가 기준자료에 없어도 원문 상세의 건물명이 같은 도로·법정동에 있으면 검토 후보로 보여준다.
        building = None
        if tokens and result.detail_text:
            scope = search.use_verified_scope(NumberQuery("road", tokens, 0, 0), names).verified_scope if road else None
            building = _BuildingScope(tokens, road, result.detail_text, scope, number_missing=True)
        return _ReviewPlan(HISTORICAL, tuple(match.query for match in matches), building=building)
    if reason == "road_lot_conflict" or kinds == {"road", "lot"}:
        return _ReviewPlan(MIXED, exact, alternatives)
    if result.status == "lot_only":
        return _ReviewPlan(LOT_ROADS, exact)
    if len(matches) == 1 and alternatives and not reason.startswith("ambiguous"):
        review_type = DIFFERENT_SUB if alternatives[0].sub_explicit else OMITTED_SUB
        return _ReviewPlan(review_type, alternatives=alternatives)
    if (not item.source_type or item.source_type in _MISSING_TYPES) and reason in _MISSING_REASONS:
        scope = search.use_verified_scope(NumberQuery("road", tokens, 0, 0), names).verified_scope if road else None
        building = _BuildingScope(tokens, road, rest, scope) if tokens else None
        return _ReviewPlan(BUILDING_NAME, building=building)
    review_type = MULTIPLE if reason in _MULTIPLE_REASONS or len(matches) > 1 else UNINTERPRETED
    return _ReviewPlan(review_type, exact)


def _add(cards, kind, address, priority, reason, year, records=(), linked_road="", linked_lot="", admin_dongs=(),
         standard=None, strong=False, name_number=False, building=("", "")):
    if not address:
        return
    key = (kind, address)
    evidence = cards.get(key)
    if evidence is None:
        evidence = cards[key] = _Evidence(priority)
    evidence.priority = min(evidence.priority, priority)
    evidence.reasons.add(reason)
    evidence.years.add(year)
    evidence.records.update((year, serial) for serial in records)
    if linked_road:
        evidence.linked_roads.add(linked_road)
    if linked_lot:
        evidence.linked_lots.add(linked_lot)
    evidence.admin_dongs.update(filter(None, admin_dongs))
    if standard is not None and evidence.standard is None:
        evidence.standard = standard
    evidence.strong = evidence.strong or strong
    evidence.name_number = evidence.name_number or name_number
    if building[0] and not evidence.building_name:
        evidence.building_name, evidence.building_input = building


def _add_hit(cards, kind, hit: ReferenceMatch, serial, priority, reason, year, strong=False, name_number=False,
             building=("", "")):
    road = hit.road.standard_road_address if hit.road else ""
    address = road if kind == "road" else hit.lot.address if hit.lot else ""
    lot = hit.lot.address if hit.lot else ""
    # 지번 후보도 같은 RAW 행의 도로명주소와 코드를 함께 둬서, 연결 도로가 하나면 승인 확정값으로 쓴다.
    _add(cards, kind, address, priority, reason, year, (serial,), road if kind == "lot" else "",
         lot if kind == "road" else "", (hit.admin_dong,), hit.road, strong, name_number, building)


def _mentioned_name(detail_text: str, hit: ReferenceMatch) -> tuple[str, str]:
    """원문 상세에 들어 있는 이 행의 건물명을 (기준 표기, 원문 표기)로 돌려준다."""
    name = next((name for name in hit.building_names if _mentions_building(detail_text, (name,))), "")
    return (name, name)


def _address_sub_number(hit: ReferenceMatch, kind: str) -> int | None:
    address = hit.road.standard_road_address if kind == "road" and hit.road else hit.lot.address if hit.lot else ""
    number = re.search(r"([0-9]+)(?:-([0-9]+))?$", address)
    return int(number[2] or 0) if number else None


def _add_number_roles(cards, plan, exact_rows, year):
    road_rows = exact_rows[plan.exact[0]]
    lot_rows = exact_rows[plan.exact[1]] if len(plan.exact) == 2 else {}
    common = road_rows.keys() & lot_rows.keys()
    if common:
        for serial in common:
            _add_hit(cards, "road", road_rows[serial], serial, 0, "두 해석이 동일 RAW 행으로 연결", year)
        return

    matching_road = {
        serial for serial, hit in lot_rows.items()
        if hit.road and hit.road.standard_road_address.rsplit(" ", 2)[-2] == plan.road_name
    }
    prefer_lot = bool(plan.explicit_lot_scope and matching_road)
    for serial, hit in road_rows.items():
        _add_hit(cards, "road", hit, serial, int(prefer_lot), "도로명·건물번호 정확 일치", year)
    for serial, hit in lot_rows.items():
        priority = int(prefer_lot and serial not in matching_road)
        reason = "원문 동·리와 지번 일치" if plan.explicit_lot_scope else "도로명 연결 지역에서 지번 일치"
        _add_hit(cards, "lot", hit, serial, priority, reason, year)


def _add_number_candidates(cards, plan, result, exact_rows, other_rows, year):
    if plan.review_type == NUMBER_ROLE:
        _add_number_roles(cards, plan, exact_rows, year)
        return
    # 여러 주소가 섞인 입력에서는 건물명 하나로 어느 쪽인지 정하지 않는다.
    mixed = plan.review_type in {MIXED, MULTIPLE}
    for query in plan.exact:
        for serial, hit in exact_rows[query].items():
            if plan.review_type == LOT_ROADS:
                # 지번이 정확히 맞고 그 지번의 건물 중 원문 건물명과 같은 건물: 지번 + 건물명 두 단서가 일치
                named = _mentions_building(result.detail_text, hit.building_names)
                reason = "같은 지번·건물명 일치" if named else "같은 지번에 연결"
                _add_hit(cards, "road", hit, serial, int(not named), reason, year, strong=named, name_number=named,
                         building=_mentioned_name(result.detail_text, hit) if named else ("", ""))
            elif plan.review_type in {SUB_OR_DETAIL, BEON_READING}:
                number = f"{query.main}-{query.sub}" if query.sub else str(query.main)
                if plan.review_type == BEON_READING:
                    reading = "뒤 숫자를 부번으로 해석" if query.sub else f"'{query.tokens[-1]}'의 '길'이 빠진 것으로 해석"
                else:
                    reading = "뒤 숫자를 부번으로 해석" if query.sub else "뒤 숫자를 상세주소로 해석"
                _add_hit(cards, query.kind, hit, serial, 0, f"{reading} · {_KIND_LABEL[query.kind]} {number} 정확 일치", year)
            else:
                # 번호가 정확히 맞고 원문 건물명도 그 건물과 같다: 번호 + 건물명 두 단서가 같은 건물을 가리킨다.
                named = not mixed and _mentions_building(result.detail_text, hit.building_names)
                reason = f"{_KIND_LABEL[query.kind]} 번호 정확 일치" + (" · 건물명 일치" if named else "")
                _add_hit(cards, query.kind, hit, serial, 0, reason, year, strong=named, name_number=named,
                         building=_mentioned_name(result.detail_text, hit) if named else ("", ""))
    # 본번 단독 건물은 없고 뒤 숫자가 실제 부번과 맞으면(예양길 28 1, 28이 없음) 뒤 숫자를 부번으로 읽을 수밖에 없다.
    trailing = None
    if plan.review_type == OMITTED_SUB and len(result.matches) == 1:
        trailing = trailing_sub_number(result.detail_text, result.matches[0])
    for query in plan.alternatives:
        if exact_rows.get(query):
            continue
        siblings = {_address_sub_number(hit, query.kind) for hit in other_rows[query].values()} - {None}
        for serial, hit in other_rows[query].items():
            named = _mentions_building(result.detail_text, hit.building_names)
            if trailing is not None and _address_sub_number(hit, query.kind) == trailing:
                few = len(siblings) <= TRAILING_SUB_MAX_SIBLINGS
                reason = f"뒤 숫자({trailing})를 부번으로 해석 · 본번 단독 건물 없음 · 같은 본번 부번 {len(siblings)}개"
                if not few:
                    reason += f" (기준 {TRAILING_SUB_MAX_SIBLINGS}개 초과)"
                _add_hit(cards, query.kind, hit, serial, 0, reason, year, strong=few)
                continue
            priority = (2 if mixed else 0) + int(not named) + int(trailing is not None)
            reason = "같은 본번·건물명 일치" if named else "같은 본번의 다른 부번"
            # 본번과 건물명이 같은 건물에서 맞으면(부번만 생략) 두 단서가 일치한 것으로 본다.
            _add_hit(cards, query.kind, hit, serial, priority, reason, year,
                     strong=named and not mixed, name_number=named and not mixed,
                     building=_mentioned_name(result.detail_text, hit) if named else ("", ""))


def _add_admin_mismatch_candidates(search, plans, base, cards, year):
    """행정동 이름으로 찾았는데 번호가 같은 법정동의 다른 행정동에만 있으면 참고 후보로 낮은 순위에 둔다."""
    queries = {
        i: [match.query for match in result.matches if match.query.admin_dong and match.status != "exact"]
        for i, (plan, result) in enumerate(zip(plans, base, strict=True)) if plan.review_type != VERIFIED
    }
    rows = search.lookup_reference_rows(
        (query for found in queries.values() for query in found), ignore_admin=True,
    )
    for i, found in queries.items():
        for query in found:
            for serial, hit in rows.get(query, {}).items():
                if hit.admin_dong == query.admin_dong:
                    continue
                reason = f"참고: 행정동 불일치 · 입력 {query.admin_dong} / 기준자료 {hit.admin_dong or '미제공'}"
                _add_hit(cards[i], query.kind, hit, serial, ADMIN_MISMATCH_PRIORITY, reason, year)


def _building_name(text: str, index: dict[str, set[str]]) -> str:
    text = text.strip(" ,(")
    letters = [(letter, i) for i, char in enumerate(text) if not char.isspace() for letter in char.lower()]
    compact = "".join(letter for letter, _ in letters)
    for name in sorted(index.get(compact[:3], ()), key=lambda value: (-len(value), value)):
        if compact.startswith(name):
            tail = text[letters[len(name) - 1][1] + 1:]
            if not tail or re.match(r"[\s,()]|[0-9]+\s*(?:[동층호]|[-/])", tail):
                return name
    return ""


def _alias_text(text: str) -> str:
    """공백은 두고 표기 차이만 맞춘다(제2차 → 2차, APT → 아파트)."""
    text = _ORDINAL_PREFIX.sub(r"\1차", normalize_address(text).lower())
    for alias, name in _NAME_ALIASES:
        text = text.replace(alias, name)
    return text


def _compare_name(text: str) -> str:
    compact = "".join(normalize_address(text).lower().split())
    for alias, name in _NAME_ALIASES:
        compact = compact.replace(alias, name)
    return _ORDINAL_PREFIX.sub(r"\1차", compact)


def _name_phrase(text: str) -> str:
    """번호 뒤 상세에서 건물명으로 쓸 앞부분을 꺼낸다(삼성APT 102동 1004호 → 삼성APT)."""
    found = _NAME_PHRASE.match(normalize_address(text).strip(" ,("))
    phrase = found[1].strip() if found else ""
    compact = _compare_name(phrase)
    return phrase if len(compact) >= 4 and re.search(r"[가-힣a-z]", compact) else ""


def _scope_building_names(db, search, scopes, wanted):
    """검색 범위(같은 도로·법정동·시군구) 안 건물명만 가져와 원문 상세와 맞는 이름을 고른다.

    RAW 전체로 이름 사전을 만들지 않는다. 먼저 상세 앞부분이 범위 안 이름과 정확히 같은지 보고(가장 긴 이름),
    없으면 rapidfuzz로 표기가 가장 비슷한 이름 하나를 고른다. 유사 일치는 점수가 충분히 높고 2등과
    차이가 날 때만 채택한다. 돌려주는 이름은 RAW 이름에서 공백만 뺀 소문자 형태라 이후 조회 SQL에 그대로 쓴다.
    """
    requests = _building_requests(search, {i: scopes[i] for i in wanted}, {i: "" for i in wanted})
    if not requests:
        return {}, {}
    db.register("name_scope", DataFrame(sorted(requests), columns=["input_index", "name", "legal", "road", "priority"]))
    rows = db.execute("""
        SELECT DISTINCT q.input_index, x.name FROM read_parquet(?) r JOIN name_scope q ON r.법정동코드 = q.legal
          AND (q.road = 0 OR r.도로명코드 = q.road), UNNEST([r.건물명, r.시군구관리건물명]) x(name)
        WHERE x.name IS NOT NULL
    """, [str(search.raw_data)]).fetchall()
    in_scope = defaultdict(dict)
    for i, name in rows:
        query_name = "".join(name.split()).lower()
        if len(query_name) >= 4 and re.search(r"[가-힣a-z]", query_name):
            in_scope[i].setdefault(query_name, name)
    exact, fuzzy = {}, {}
    for i in wanted:
        names = in_scope.get(i)
        if not names:
            continue
        # 맨 앞 일치는 표기를 맞춘 이름으로 비교하고, 찾으면 원래 조회용 이름으로 되돌린다.
        index = defaultdict(set)
        by_compare = {}
        for query_name, original in names.items():
            compare = _compare_name(original)
            index[compare[:3]].add(compare)
            by_compare.setdefault(compare, query_name)
        # 표기를 먼저 맞춰야 '청아빌라 제2차'가 짧은 이름 '청아빌라'가 아니라 '청아빌라2차'로 잡힌다.
        if name := _building_name(_alias_text(scopes[i].remainder), index):
            exact[i] = by_compare[name]
            continue
        # 맨 앞이 아니어도(101동 래미안아파트) 범위 안 이름이 경계를 지켜 들어 있으면 정확한 일치로 본다.
        if name := _name_anywhere(scopes[i].remainder, names):
            exact[i] = name
            continue
        phrase = _name_phrase(scopes[i].remainder)
        if not phrase:
            continue
        # 숫자가 다르면 다른 단지·건물이다(청솔3차 ≠ 청솔1차, 세웅하이빌602 ≠ 세웅하이빌2).
        # 이름 안의 숫자 열이 같은 이름끼리만 유사 비교한다.
        target = _compare_name(phrase)
        digits = re.findall(r"[0-9]+", target)
        compared = defaultdict(list)
        for query_name, original in names.items():
            compare = _compare_name(original)
            if re.findall(r"[0-9]+", compare) == digits:
                compared[compare].append((query_name, original))
        if not compared:
            continue
        ranked = process.extract(target, list(compared), scorer=fuzz.ratio, limit=2)
        best, score, _ = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0
        if score == 100:
            # 공백·APT 같은 표기만 다르고 같은 이름이다(우성APT = 우성아파트). 정확한 건물명 일치로 본다.
            exact[i] = compared[best][0][0]
        elif score >= FUZZY_NAME_MIN_SCORE and score - second >= FUZZY_NAME_MARGIN:
            query_name, original = compared[best][0]
            fuzzy[i] = (query_name, phrase, original, round(score))
    return exact, fuzzy


def _name_anywhere(text: str, names: dict[str, str]) -> str:
    """상세 텍스트 어디에든 범위 안 건물명이 단어 경계를 지켜 들어 있으면 가장 긴 이름을 돌려준다.

    앞은 다른 글자에 붙지 않아야 하고(1차삼성아파트 안의 삼성아파트는 제외), 뒤는 끝·공백·쉼표·괄호
    또는 '숫자+동/층/호'만 허용한다. 이름 글자 사이 공백과 APT·제2차 같은 표기 차이는 허용한다.
    """
    spaced = _alias_text(text)
    compact = "".join(spaced.split())
    hits = []
    for query_name, original in names.items():
        target = _compare_name(original)
        if len(target) < 4 or target not in compact:
            continue
        pattern = r"\s*".join(map(re.escape, target))
        if re.search(rf"(?<![가-힣a-z0-9]){pattern}(?=$|[\s,()/]|[0-9]+\s*[동층호-])", spaced):
            hits.append((len(target), query_name))
    return max(hits)[1] if hits else ""


def _building_requests(search, scopes, names):
    token_sets = []
    for i in names:
        scope = scopes[i]
        if scope.verified_roads is None:
            token_sets.append(scope.tokens)
        if scope.road and scope.tokens[:-1]:
            token_sets.append(scope.tokens[:-1])
    validated = search.validate_scopes(token_sets)
    requests = set()
    for i, name in names.items():
        scope = scopes[i]
        if scope.road:
            roads = scope.verified_roads
            if roads is None:
                query = NumberQuery("road", scope.tokens, 0, 0)
                roads = search._scope(query, validated[scope.tokens])
            for road, legal in roads:
                requests.add((i, name, legal, road, 0))
        regional = scope.tokens[:-1] if scope.road else scope.tokens
        for path in validated.get(regional, ()):
            leaf = path[-1]
            code = leaf["code_prefix"]
            if code and leaf["label"] in _LOCAL:
                legal_codes = search.legal_codes_by_prefix[code]
            elif code and leaf["label"] == "시군구" and not scope.number_missing:
                # 번호가 기준자료에 없는 주소는 같은 도로·법정동 안에서만 건물명을 찾는다.
                legal_codes = {int(k) for k in search.validator.path_legal_codes if k.startswith(code)}
            else:
                continue
            for legal in legal_codes:
                requests.add((i, name, legal, 0, int(scope.road)))
    return requests


def _add_building_candidates(search, scopes, cards, year):
    if not scopes:
        return
    import duckdb

    with duckdb.connect() as db:
        # 범위 안 건물명과 정확히 맞으면 그 이름을, 없으면 표기가 비슷한 이름(삼성APT ≈ 삼성아파트)을 쓴다.
        found, fuzzy = _scope_building_names(db, search, scopes, list(scopes))
        found.update({i: match[0] for i, match in fuzzy.items()})
        requests = _building_requests(search, scopes, found)
        if not requests:
            return
        frame = DataFrame(sorted(requests), columns=["input_index", "name", "legal", "road", "priority"])
        db.register("building_requests", frame)
        columns = ", ".join(f'r."{key}"' for key in _REFERENCE_COLUMNS)
        rows = db.execute(rf"""
            SELECT q.input_index, q.priority, CAST(r.일련번호 AS BIGINT), {columns}
            FROM read_parquet(?) r JOIN building_requests q ON r.법정동코드 = q.legal
            AND (q.road = 0 OR r.도로명코드 = q.road)
            AND q.name IN (lower(regexp_replace(r.건물명, '\s+', '', 'g')),
                           lower(regexp_replace(r.시군구관리건물명, '\s+', '', 'g')))
        """, [str(search.raw_data)]).fetchall()
        parsed = [
            (i, priority, serial, RawAddressRow(str(serial), dict(zip(_REFERENCE_COLUMNS, values, strict=True))))
            for i, priority, serial, *values in rows
        ]
        same_name = _same_name_counts(db, search, {
            (found[i], _sigungu_code(raw)) for i, _, _, raw in parsed if _sigungu_code(raw) is not None
        })
        for i, priority, serial, raw in parsed:
            count = same_name.get((found[i], _sigungu_code(raw)), 0)
            reason = "검증된 도로·지역에서 건물명 일치" if priority == 0 else "동일 지역에서 건물명 일치"
            if count:
                reason += f" · 시군구 안 같은 이름 {count}곳"
            # 같은 도로·지역에서 일치했고 그 이름이 시군구에 하나뿐이면 단독 1순위일 때 '거의 확정'으로 본다.
            strong = priority == 0 and count == 1
            if i in fuzzy:
                _, phrase, original, score = fuzzy[i]
                reason = f"건물명 유사 일치 · 입력 '{phrase}' ≈ 기준 '{original}' ({score}점) · " + reason
                priority += FUZZY_NAME_PRIORITY
                # 표기가 비슷한 건물도 후보로 제시한다. 거의 확정 기준은 정확한 건물명과 같게
                # (같은 도로·지역 + 시군구 안 유일한 이름) 둬서, 표기가 틀린 쪽이 더 확실하게 분류되지 않게 한다.
                # 근거에 입력 표기와 기준 표기, 점수를 남겨 승인 전에 확인하게 한다.
            if scopes[i].number_missing:
                reason = "입력 번호는 기준자료에 없음 · " + reason
                priority += NUMBER_MISSING_BUILDING_PRIORITY
            original = next((str(raw.columns.get(column)) for column in ("건물명", "시군구관리건물명")
                             if raw.columns.get(column) and "".join(str(raw.columns[column]).split()).lower() == found[i]), found[i])
            building = (original, fuzzy[i][1] if i in fuzzy else original)
            _add_hit(cards[i], "road", _project_reference(raw), serial, priority, reason, year, strong, building=building)


# 지역 이름으로 시작하는 공공기관 건물명(대전광역시청, 포천시청, 경상남도교육청, 정부대전청사).
# 지역이 다시 나온 것이 아니라 건물 이름이므로, 기준자료에서 같은 이름의 건물을 찾아 검토 후보로 제시한다.
_PUBLIC_BUILDING = re.compile(
    r"(?<![가-힣])((?:[가-힣]{1,10}(?:시|군|구|도)(?:청|교육청|의회))|정부[가-힣]{2,6}청사)(?![가-힣])"
)
# 다른 후보보다 앞에 둔다. 원문 번호와 다른 건물일 수 있어 확정하지 않고 승인 전 확인 대상으로 둔다.
PUBLIC_BUILDING_PRIORITY = -1
_SIDO_FORMS = defaultdict(set)
for _short, _full in SIDO_ALIASES.items():
    _SIDO_FORMS[_full].update((_short, _full))
for _full, _old in HISTORICAL_NAMES.items():
    _SIDO_FORMS[_full].update((_full, *_old))
    for _name in _old:
        _SIDO_FORMS[_name] = _SIDO_FORMS[_full]


def public_building_names(text: str) -> list[str]:
    """원문에서 지역 이름으로 시작하는 공공기관 건물명을 순서대로 꺼낸다."""
    return list(dict.fromkeys(_PUBLIC_BUILDING.findall(normalize_address(text or ""))))


def _public_name_forms(name: str) -> list[str]:
    """비교할 이름 표기들. 옛 시도명이나 약칭으로 시작하면 지금 시도명으로 바꾼 표기도 더한다
    (전라북도청·전북도청 → 전북특별자치도청)."""
    compact = "".join(name.split()).lower()
    forms = [compact]
    for full, written in _SIDO_FORMS.items():
        if full not in SIDO_ALIASES.values() and full not in HISTORICAL_NAMES:
            continue
        for form in sorted(written, key=len, reverse=True):
            if compact.startswith(form) and compact != form:
                rest = compact[len(form):]
                if len(form) == 2 and rest.startswith("도") and full.endswith("도"):
                    rest = rest[1:]   # 전북 + 도청 → 전북특별자치도 + 청
                forms.append(full + rest)
                break
    return list(dict.fromkeys(forms))


def _region_written(compact: str, sido: str, sigungu: str) -> bool:
    """그 건물의 시군구가 원문에 적혀 있고, 원문의 시도가 그 건물의 시도와 충돌하지 않는지."""
    parts = sigungu.split()
    if not parts or not all(part in compact or (len(part) >= 3 and part[:-1] in compact) for part in parts):
        return False
    stated = {full for full, forms in _SIDO_FORMS.items() for form in forms if compact.startswith(form)}
    return not stated or sido in stated or bool(_SIDO_FORMS.get(sido, set()) & {form for full in stated for form in _SIDO_FORMS[full]})


def _add_public_building_candidates(search, items, plans, cards, year):
    """원문의 공공기관 건물명이 시군구 안에서 한 건물을 가리키면 그 건물을 1순위 검토 후보로 더한다.

    번호로 찾은 후보와 다른 동에 있어도(대회산리 306 포천시청 → 포천시 중앙로 87) 찾는다. 같은 이름이
    원문 시군구에 여럿이면 더하지 않고, 원문의 시군구가 그 건물과 다르면(다른 면의 창녕군청) 쓰지 않는다.
    """
    wanted = {}
    for i, (item, plan) in enumerate(zip(items, plans, strict=True)):
        # 여러 줄에 주소를 나눠 적은 입력은 어느 주소의 건물인지 정하지 않는다.
        if plan.review_type in {VERIFIED, MULTIPLE, MIXED} or len((item.raw_address or "").strip().splitlines()) > 1:
            continue
        names = public_building_names(item.raw_address)
        if len(names) == 1:
            wanted[i] = names[0]
    if not wanted:
        return
    import duckdb

    columns = ", ".join(f'r."{key}"' for key in _REFERENCE_COLUMNS)
    frame = DataFrame([(i, form) for i, name in wanted.items() for form in _public_name_forms(name)],
                      columns=["input_index", "name"])
    with duckdb.connect() as db:
        db.register("public_requests", frame)
        # 기준자료 건물명은 '포천시청, 포천시의회'처럼 여러 이름을 쉼표로 묶어 두기도 한다. 하나씩 나눠 비교한다.
        rows = db.execute(rf"""
            SELECT DISTINCT q.input_index, CAST(r.일련번호 AS BIGINT), {columns}
            FROM read_parquet(?) r,
              UNNEST(string_split(regexp_replace(coalesce(r.건물명, '') || ',' || coalesce(r.시군구관리건물명, ''),
                                                 '[/·]', ',', 'g'), ',')) x(part)
            JOIN public_requests q ON q.name = lower(regexp_replace(x.part, '\s+', '', 'g'))
        """, [str(search.raw_data)]).fetchall()
    hits = defaultdict(list)
    for i, serial, *values in rows:
        raw = RawAddressRow(str(serial), dict(zip(_REFERENCE_COLUMNS, values, strict=True)))
        compact = "".join(normalize_address(items[i].raw_address).split())
        if _region_written(compact, str(raw.columns.get("법정동시도명") or ""), str(raw.columns.get("법정동시군구명") or "")):
            hits[i].append((serial, raw))
    for i, found in hits.items():
        matches = [(serial, _project_reference(raw), raw) for serial, raw in found]
        roads = {match.road.standard_road_address for _, match, _ in matches if match.road}
        if len(roads) != 1:
            continue
        name = wanted[i]
        serial, match, raw = matches[0]
        forms = set(_public_name_forms(name))
        reference = next((part.strip() for column in ("건물명", "시군구관리건물명")
                          for part in re.split(r"[,/·]", str(raw.columns.get(column) or ""))
                          if "".join(part.split()).lower() in forms), name)
        reason = (f"공공기관 건물명 정확 일치 · 원문 '{name}' = 기준 '{reference}' · 시군구 안 하나뿐"
                  " · 원문 번호와 다른 건물일 수 있음")
        _add_hit(cards[i], "road", match, serial, PUBLIC_BUILDING_PRIORITY, reason, year, building=(reference, name))


def _sigungu_code(raw: RawAddressRow) -> int | None:
    code = re.fullmatch(r"([0-9]{10})(?:\.0+)?", str(raw.columns.get("법정동코드") or "").strip())
    return int(code[1]) // 100000 if code else None


def _same_name_counts(db, search, keys: set[tuple[str, int]]) -> dict[tuple[str, int], int]:
    """(공백 없는 소문자 건물명, 시군구 코드)마다 같은 이름을 가진 서로 다른 도로명주소 수를 센다."""
    if not keys:
        return {}
    db.register("same_name_requests", DataFrame(sorted(keys), columns=["name", "sigungu"]))
    rows = db.execute(r"""
        SELECT q.name, q.sigungu, count(DISTINCT (r.도로명코드, r.건물본번, r.건물부번))
        FROM read_parquet(?) r JOIN same_name_requests q
          ON CAST(r.법정동코드 AS BIGINT) // 100000 = q.sigungu
         AND q.name IN (lower(regexp_replace(r.건물명, '\s+', '', 'g')),
                        lower(regexp_replace(r.시군구관리건물명, '\s+', '', 'g')))
        GROUP BY q.name, q.sigungu
    """, [str(search.raw_data)]).fetchall()
    return {(name, sigungu): count for name, sigungu, count in rows}


def _ri_building_queries(search, items, base) -> dict[int, NumberQuery]:
    """'…리 번호'를 지번으로 조회해 정확히 맞지 않은 행에서, 같은 번호를 건물번호로 다시 볼 대상을 고른다.

    도로명 칸에 도로명 없이 '삼태리 56'처럼 적힌 주소는 56이 건물번호일 수 있다. 지번이 정확히 맞으면
    지번을 우선하므로 대상에서 뺀다. 번지·산이 적힌 번호는 지번이 분명하므로 제외한다.
    """
    found = {}
    for i, (item, result) in enumerate(zip(items, base, strict=True)):
        if result.status in {"resolved", "address_only"} or len(result.matches) != 1:
            continue
        match = result.matches[0]
        query = match.query
        if query.kind != "lot" or query.land or match.status == "exact" or not query.tokens:
            continue
        if "리" not in _token_labels(search, query.tokens[-1]):
            continue
        if re.search(rf"(?<![0-9]){query.main}(?:\s*-\s*[0-9]+)?\s*번지", normalize_address(item.raw_address)):
            continue
        found[i] = query
    return found


def _add_ri_building_candidates(search, items, base, cards, year):
    found = _ri_building_queries(search, items, base)
    if not found:
        return
    import duckdb

    indices = list(found)
    legal = defaultdict(set)
    for request_index, _, _, code, *_ in search._requests([found[i] for i in indices]):
        legal[indices[request_index]].add(code)
    frame = DataFrame(
        [(i, code, found[i].main, found[i].sub) for i in indices for code in legal[i]],
        columns=["input_index", "legal", "main", "sub"],
    )
    if frame.empty:
        return
    columns = ", ".join(f'r."{key}"' for key in _REFERENCE_COLUMNS)
    with duckdb.connect() as db:
        db.register("ri_requests", frame)
        rows = db.execute(f"""
            SELECT q.input_index, CAST(r.일련번호 AS BIGINT), {columns}
            FROM read_parquet(?) r JOIN ri_requests q ON r.법정동코드 = q.legal
            AND TRY_CAST(r.건물본번 AS INTEGER) = q.main AND TRY_CAST(r.건물부번 AS INTEGER) = q.sub
        """, [str(search.raw_data)]).fetchall()
    hits = defaultdict(list)
    for i, serial, *values in rows:
        hits[i].append((serial, RawAddressRow(str(serial), dict(zip(_REFERENCE_COLUMNS, values, strict=True)))))
    for i, rows_for_input in hits.items():
        roads = {raw.columns["도로명코드"] for _, raw in rows_for_input}
        # 지번(부번 생략)과 건물번호 중 어느 쪽을 뜻했는지는 원문만으로 정할 수 없으므로
        # 같은 본번의 다른 지번 부번 후보와 같은 순위로 나란히 보여준다. 도로 수는 근거로만 남긴다.
        priority = SAME_NUMBER_OTHER_READING_PRIORITY
        reason = f"도로명 없이 건물번호로 해석 · 리 안 도로 {len(roads)}곳"
        for serial, raw in rows_for_input:
            _add_hit(cards[i], "road", _project_reference(raw), serial, priority, reason, year)


def _add_historical_candidates(history, plans, cards):
    eligible = {i: plan for i, plan in enumerate(plans) if plan.review_type == HISTORICAL}
    if not eligible:
        return
    for priority, search in enumerate(sorted(history, key=lambda source: source.validator.year, reverse=True)):
        year = search.validator.year
        found = search.lookup_reference_rows(
            replace(query, verified_scope=None) for plan in eligible.values() for query in plan.exact
        )
        for i, plan in eligible.items():
            hits = [(query, found[replace(query, verified_scope=None)]) for query in plan.exact]
            if not hits or any(not rows for _, rows in hits):
                continue
            if {query.kind for query, _ in hits} == {"road", "lot"}:
                ids = set.intersection(*(set(rows) for _, rows in hits))
                query, rows = hits[0]
                hits = [(replace(query, kind="road"), {serial: rows[serial] for serial in ids})]
            for query, rows in hits:
                for serial, hit in rows.items():
                    _add_hit(cards[i], query.kind, hit, serial, priority, "해당 연도 번호 정확 일치", year)



def main_number_typo_variants(original: int) -> tuple[tuple[str, int], ...]:
    """J: adjacent transposition, substitution, deletion, insertion (ordered).

    The values are *search hypotheses*, never a replacement for the input.
    """
    digits = str(original)
    if len(digits) < 2:
        return ()
    grouped = []
    for i in range(len(digits) - 1):
        pair = list(digits)
        pair[i], pair[i + 1] = pair[i + 1], pair[i]
        grouped.append(("transposition", "".join(pair)))
    for i, old in enumerate(digits):
        for ch in "0123456789":
            if ch != old:
                grouped.append(("substitution", digits[:i] + ch + digits[i + 1:]))
    for i in range(len(digits)):
        grouped.append(("deletion", digits[:i] + digits[i + 1:]))
    for i in range(len(digits) + 1):
        for ch in "0123456789":
            grouped.append(("insertion", digits[:i] + ch + digits[i:]))
    found = []
    seen = {original}
    for label, value in grouped:
        if not value or value[0] == "0" or len(value) > 5:
            continue
        number = int(value)
        if number > 0 and number not in seen:
            found.append((label, number))
            seen.add(number)
    return tuple(found)


def _add_number_typo_candidates(search, base, cards, year) -> set[int]:
    """J: search same scoped road/lot, same sub and mountain; no auto-approval."""
    owners = defaultdict(list)
    probes = []
    for index, result in enumerate(base):
        if cards[index] or result.status != "no_match" or result.reason != "number_not_found":
            continue
        eligible = [match.query for match in result.matches
                    if match.status == "number_not_found" and match.query.main >= 10]
        if len(eligible) != 1:
            continue
        original = eligible[0]
        for label, proposed in main_number_typo_variants(original.main):
            query = replace(original, main=proposed)
            if query not in owners:
                probes.append(query)
            owners[query].append((index, original.main, label))
    if not probes:
        return set()
    hits = search.lookup_reference_rows(probes)
    identified = set()
    count = defaultdict(int)
    for query in probes:
        for index, original, label in owners[query]:
            for serial, match in hits.get(query, {}).items():
                if count[index] >= 5:
                    break
                _add_hit(cards[index], query.kind, match, serial,
                         NUMBER_MISSING_BUILDING_PRIORITY + 5,
                         f"번호 오타 후보({label}): 원문 {original} → 기준자료 {query.main}"
                         " · 원문 번호와 다르므로 수동 검토 필요", year)
                count[index] += 1
                identified.add(index)
    return identified


def _add_glued_unit_candidates(search, items, base, cards, year) -> set[int]:
    """K: one glued number/unit boundary; confirm with the original DB engine."""
    eligible = {}
    for i, (item, result) in enumerate(zip(items, base, strict=True)):
        if cards[i] or result.status in {"resolved", "address_only", "lot_only"}:
            continue
        candidates = glued_unit_interpretations(item.raw_address)
        if len(candidates) == 1:
            eligible[i] = candidates[0]
    if not eligible:
        return set()
    found = search.search_batch([DetailInput(text) for text in eligible.values()])
    changed = set()
    for (i, interpreted), result in zip(eligible.items(), found, strict=True):
        if result.status not in {"resolved", "address_only"} or result.standard_address is None:
            continue
        _add(cards[i], "road", result.standard_address.standard_road_address,
             NUMBER_MISSING_BUILDING_PRIORITY + 5,
             f"번호·동 붙여쓰기 후보: {interpreted} · 기준자료 일치 · 수동 검토 필요",
             year, result.raw_record_ids, standard=result.standard_address)
        changed.add(i)
    return changed


def _finish_review(plan: _ReviewPlan, result: DetailResult, cards: dict, year: int | None = None) -> AddressReview:
    # 공동 1순위 가운데 번호와 건물명이 함께 맞은 후보가 하나뿐이면 그 후보를 단독 1순위로 둔다.
    best = min((evidence.priority for evidence in cards.values()), default=None)
    first = [key for key, evidence in cards.items() if evidence.priority == best]
    named = [key for key in first if cards[key].name_number]
    if len(first) > 1 and len(named) == 1:
        for key in first:
            if key != named[0]:
                cards[key].priority += 0.5
    ranks = {priority: rank for rank, priority in enumerate(sorted({e.priority for e in cards.values()}), 1)}
    candidates = tuple(sorted((
        ReviewCandidate(
            kind, address, ranks[e.priority], tuple(sorted(e.reasons)),
            tuple(sorted(e.years, reverse=True)), tuple(sorted(e.records)), tuple(sorted(e.linked_roads)),
            tuple(sorted(e.linked_lots)), tuple(sorted(e.admin_dongs)), e.standard,
            e.building_name, e.building_input,
        ) for (kind, address), e in cards.items()
    ), key=lambda candidate: (candidate.rank, candidate.kind, candidate.address)))
    if plan.review_type == VERIFIED:
        status = "verified"
    elif not candidates:
        status = "no_candidate"
    elif len(candidates) == 1:
        status = "single_candidate"
    elif candidates[1].rank != 1:
        status = "preferred_candidate"
    elif plan.review_type in {SUB_OR_DETAIL, BEON_READING} or len({candidate.kind for candidate in candidates if candidate.rank == 1}) > 1:
        status = "parallel_candidates"
    else:
        status = "tied_candidates"
    lot = result.standard_lot.address if result.standard_lot else ""
    sub_numbers = tuple(
        SubNumberList(match.query.kind, f"{'산 ' if match.query.land else ''}{match.query.main}",
                      match.query.sub, match.other_sub_numbers)
        for match in result.matches if match.status in _SUB_REASONS and match.other_sub_numbers
    )
    return AddressReview(
        plan.review_type, status, result.status, result.reason, lot, candidates,
        result.standard_address, result.detail_units, _tier(plan, candidates, cards, year, result.status),
        sub_numbers,
    )


def _tier(plan: _ReviewPlan, candidates: tuple[ReviewCandidate, ...], cards: dict, year: int | None = None,
          base_status: str = "") -> str:
    """검토 필요 주소를 거의 확정·확인 필요·일반 보류로 나눈다.

    - 거의 확정: 1순위가 하나이고, 그 후보에 분명한 근거(strong)가 있다. 지금은 시군구 안에 하나뿐인
      건물명이 같은 도로·지역에서 일치한 경우다.
    - 확인 필요: 1순위가 하나지만 분명한 근거가 없다(같은 이름이 다른 곳에도 있음, 부번 추정 등).
    - 일반 보류: 1순위가 여럿이거나 후보가 없다.
    """
    if plan.review_type == VERIFIED:
        # 주소는 확정됐지만 행정구역분류코드 등 일부 코드가 기준자료에 없는 행(address_only)은 고를 후보가 없으므로
        # 거의 확정에 두고, 사람이 승인하면 검토 후 확정으로 옮긴다.
        return TIER_NEAR_CONFIRMED if base_status == "address_only" else ""
    first = [candidate for candidate in candidates if candidate.rank == 1]
    # 1순위 후보를 기준연도가 아닌 과거 연도 자료에서만 찾았으면 따로 모은다. 지금은 없는 주소일 수 있어
    # 승인 확정 대상에서 빼고, 이전·폐지 여부를 확인하도록 한다.
    if year is not None and first and all(
        candidate.reference_years and year not in candidate.reference_years for candidate in first
    ):
        return TIER_HISTORICAL
    if len(first) != 1:
        return TIER_GENERAL
    top = first[0]
    # 지번 후보는 연결된 도로명주소가 하나일 때만 그 주소로 확정할 수 있다.
    single_road = top.kind == "road" or len(top.linked_road_addresses) <= 1
    strong = cards[(top.kind, top.address)].strong and top.standard_address is not None and single_road
    return TIER_NEAR_CONFIRMED if strong else TIER_CHECK_NEEDED


def _suggestion_inputs(item: DetailInput) -> list[tuple[dict, DetailInput]]:
    """지역 일부만 확인된 주소에서 남은 지명을 유사 지명으로 바꾼 조회 입력을 만든다."""
    region = item.region_validation or {}
    partial = region.get("partial_match") if region.get("status") == "no_match" else None
    if not partial:
        return []
    inputs = []
    for suggestion in partial["suggestions"][:SUGGESTION_REVIEW_LIMIT]:
        position = suggestion["token_index"]
        if position >= len(item.validated_tokens):
            continue
        original = item.validated_tokens[position]["raw"]
        if original not in item.raw_address:
            continue
        tokens = list(item.validated_tokens)
        tokens[position] = {key: value for key, value in tokens[position].items() if key != "corrected"}
        tokens[position]["raw"] = suggestion["name"]
        validation = {
            "address_id": region.get("address_id", ""), "reference_year": region["reference_year"],
            "status": suggestion["status"], "paths": suggestion["paths"],
            "db_labels": suggestion.get("db_labels", []),
        }
        inputs.append((suggestion, replace(
            item, raw_address=item.raw_address.replace(original, suggestion["name"], 1),
            validated_tokens=tuple(tokens), region_validation=validation, hold_reason="",
        )))
    return inputs


def _closeness(suggestion: dict) -> tuple:
    """자모 편집 거리가 작을수록, 같으면 유사도가 높을수록 앞에 둔다."""
    return suggestion.get("jamo_distance", 0), -suggestion["similarity"]


def _merge_suggestions(review: AddressReview, found: list[tuple[dict, AddressReview]]) -> AddressReview:
    """유사 지명으로 찾은 후보를 기존 후보 뒤 순위로 붙인다. 자동 확정하지 않는다."""
    seen = {(candidate.kind, candidate.address) for candidate in review.candidates}
    added = []
    for suggestion, alternative in sorted(found, key=lambda pair: _closeness(pair[0])):
        note = f"유사 지명 후보: {suggestion['raw']} → {suggestion['name']}"
        for candidate in alternative.candidates:
            if (candidate.kind, candidate.address) in seen:
                continue
            seen.add((candidate.kind, candidate.address))
            # 번지까지 그대로 확인된 후보를 부번이 다른 후보보다 앞에 둔다.
            exact = alternative.review_type == VERIFIED
            added.append(((not exact, *_closeness(suggestion), candidate.rank), replace(
                candidate, evidence=(note, *candidate.evidence),
            )))
    offset = max((candidate.rank for candidate in review.candidates), default=0)
    ranks = {key: offset + rank for rank, key in enumerate(sorted({key for key, _ in added}), 1)}
    candidates = (*review.candidates, *(replace(candidate, rank=ranks[key]) for key, candidate in added))
    if not candidates:
        status = "no_candidate"
    elif len(candidates) == 1:
        status = "single_candidate"
    elif candidates[1].rank != candidates[0].rank:
        status = "preferred_candidate"
    else:
        status = "tied_candidates"
    return replace(review, review_type=SIMILAR_REGION, status=status, candidates=tuple(candidates))


def _correction_checks(search: DetailAddressSearch, items: list[DetailInput]) -> dict[int, tuple[dict, list]]:
    """LLM 수정값으로 검증된 주소마다 원문 표기와 비슷한 다른 지명을 찾아 조회 입력을 만든다."""
    rows, owners = [], []
    for index, item in enumerate(items):
        region = item.region_validation or {}
        if not region.get("used_correction") or not region.get("paths"):
            continue
        corrected = {i: token["corrected"] for i, token in enumerate(item.validated_tokens) if token.get("corrected")}
        if not corrected:
            continue
        tokens = [{"raw": token["raw"], "labels": list(token.get("labels", []))} for token in item.validated_tokens]
        rows.append({"address_id": str(len(rows)), "tokens": tokens})
        owners.append((index, corrected, tokens))
    if not rows:
        return {}
    validations, _ = search.validator.validate_batch(rows, suggest=True)
    checks = {}
    for (index, corrected, tokens), validation in zip(owners, validations, strict=True):
        partial = validation.get("partial_match") if validation["status"] == "no_match" else None
        # LLM 수정값과 같은 지역 경로로 이어지는 후보(서울 ↔ 서울특별시)는 대안이 아니다.
        confirmed = items[index].region_validation["paths"]
        others = [
            suggestion for suggestion in (partial or {}).get("suggestions", ())
            if suggestion["token_index"] in corrected and suggestion["paths"] != confirmed
        ]
        probe = replace(items[index], validated_tokens=tuple(tokens), region_validation={
            **validation, "partial_match": {**(partial or {}), "suggestions": others},
        })
        checks[index] = (corrected, _suggestion_inputs(probe))
    return checks


def _spelling_changed(item: DetailInput, corrected: dict) -> bool:
    """LLM 수정값 중 띄어쓰기·약칭 확장이 아닌 실제 표기 교정이 있는지."""
    for index, name in corrected.items():
        raw = "".join(str(item.validated_tokens[index]["raw"]).split())
        fixed = "".join(str(name).split())
        if raw and fixed and raw not in fixed and fixed not in raw:
            return True
    return False


def _check_correction(
    review: AddressReview, item: DetailInput, corrected: dict, found: list[tuple[dict, AddressReview]],
) -> AddressReview:
    """LLM 수정값으로 번지까지 맞아도, 비슷한 다른 지명도 번지까지 맞으면 확정하지 않는다."""
    notes = tuple(f"LLM 오타 수정: {item.validated_tokens[i]['raw']} → {name}" for i, name in corrected.items())
    own = {(candidate.kind, candidate.address) for candidate in review.candidates}
    exact = [
        (suggestion, alternative) for suggestion, alternative in found
        if alternative.review_type == VERIFIED
        and any((candidate.kind, candidate.address) not in own for candidate in alternative.candidates)
    ]
    if review.review_type != VERIFIED or not exact:
        checked = ("비슷한 다른 지명 중 번지까지 일치하는 곳 없음",) if review.review_type == VERIFIED else ()
        candidates = tuple(
            replace(candidate, evidence=(*notes, *checked, *candidate.evidence)) for candidate in review.candidates
        )
        if review.review_type == VERIFIED and _spelling_changed(item, corrected):
            # 원문 표기를 고쳐야만 맞는 주소(망덕산로105번긷 → 번길)는 자동 확정하지 않고 확인 필요 후보로 둔다.
            # 띄어쓰기만 다르거나(흑 석동) 약칭을 정식 이름으로 늘린 것(서울 → 서울특별시)은 교정으로 보지 않는다.
            return replace(
                review, review_type=TYPO_CORRECTION, status="single_candidate" if len(candidates) == 1
                else review.status, base_status="held", base_reason="typo_corrected", standard_address=None,
                tier=TIER_CHECK_NEEDED, candidates=tuple(
                    replace(candidate, evidence=(*candidate.evidence, "원문 표기를 고쳐서 맞은 주소 · 검토 필요"))
                    for candidate in candidates),
            )
        return replace(review, candidates=candidates)
    candidates = [replace(candidate, rank=1, evidence=(*notes, *candidate.evidence)) for candidate in review.candidates]
    seen = {(candidate.kind, candidate.address) for candidate in candidates}
    for suggestion, alternative in exact:
        for candidate in alternative.candidates:
            if (candidate.kind, candidate.address) in seen:
                continue
            seen.add((candidate.kind, candidate.address))
            candidates.append(replace(candidate, rank=1, evidence=(
                f"유사 지명 후보: {suggestion['raw']} → {suggestion['name']}", *candidate.evidence,
            )))
    return replace(
        review, review_type=AMBIGUOUS_CORRECTION, status="tied_candidates", base_status="held",
        base_reason="ambiguous_correction", standard_address=None, candidates=tuple(candidates),
    )



def merge_detail_recovery(review: AddressReview, evidence, year: int) -> AddressReview:
    """Attach DB-verified LLM search hypotheses as secondary, never auto-approved cards."""
    existing = {(c.kind, c.address) for c in review.candidates}
    added = []
    rank = max((c.rank for c in review.candidates), default=0) + 1
    for item in evidence:
        if item.validation != "db_matched" or item.standard_address is None:
            continue
        standard = item.standard_address
        key = ("road", standard.standard_road_address)
        if key in existing:
            continue
        existing.add(key)
        serials = tuple((year, int(x)) for x in item.raw_record_ids if str(x).isdecimal())
        added.append(ReviewCandidate(
            "road", standard.standard_road_address, rank,
            (f"LLM 검색 가설: {item.hypothesis.reason}",
             f"교정 후 DB 재검색 일치: {item.hypothesis.address}",
             "원문과 다른 해석이므로 수동 검토 필요"),
            (year,), serials, standard_address=standard,
        ))
    if not added:
        return review
    cards = (*review.candidates, *added)
    # Do not override the rank of independently established DB matches.
    # All LLM-added candidates stay outside automatic near-confirmed approval.
    return replace(review, review_type="AI 교정 후보", status=(
        "single_candidate" if len(cards) == 1 else "preferred_candidate"
        if len([x for x in cards if x.rank == min(c.rank for c in cards)]) == 1
        else "tied_candidates"
    ), base_status="held", base_reason="ai_corrected",
                   standard_address=None, tier=TIER_GENERAL, candidates=cards)

def review_batch(
    search: DetailAddressSearch, items: list[DetailInput], *,
    history: tuple[DetailAddressSearch, ...] = (),
) -> list[AddressReview]:
    """기준연도 조회를 재사용하고 보류 유형에 해당하는 후보를 일괄 조회한다.

    지역 경로가 일부만 확인된 주소는 유사 지명으로 바꿔 한 번 더 조회하고 그 후보를 뒤 순위로 붙인다.
    LLM 수정값으로 확인된 주소는 원문과 비슷한 다른 지명도 조회해, 번지까지 맞는 곳이 또 있으면 보류한다.
    """
    extras = [(index, suggestion, alternative)
              for index, item in enumerate(items) for suggestion, alternative in _suggestion_inputs(item)]
    checks = _correction_checks(search, items)
    probes = [(index, suggestion, alternative)
              for index, (_, inputs) in checks.items() for suggestion, alternative in inputs]
    reviews = _review_batch(search, [*items, *(alt for _, _, alt in extras), *(alt for _, _, alt in probes)],
                            history=history)
    found, probed = defaultdict(list), defaultdict(list)
    offset = len(items)
    for (index, suggestion, _), alternative in zip(extras, reviews[offset:offset + len(extras)], strict=True):
        found[index].append((suggestion, alternative))
    for (index, suggestion, _), alternative in zip(probes, reviews[offset + len(extras):], strict=True):
        probed[index].append((suggestion, alternative))
    results = []
    for index, review in enumerate(reviews[:len(items)]):
        if index in found:
            review = _merge_suggestions(review, found[index])
        elif index in checks:
            review = _check_correction(review, items[index], checks[index][0], probed[index])
        results.append(review)
    return results


def _review_batch(
    search: DetailAddressSearch, items: list[DetailInput], *,
    history: tuple[DetailAddressSearch, ...] = (),
) -> list[AddressReview]:
    year = search.validator.year
    history_years = {source.validator.year for source in history}
    if len(history_years) != len(history) or any(previous >= year for previous in history_years):
        raise ValueError("과거 기준연도는 중복 없이 기준연도보다 이전이어야 합니다.")
    base = search.search_batch(items)
    plans = [_plan_review(search, item, result) for item, result in zip(items, base, strict=True)]
    exact_rows = search.lookup_reference_rows(
        query for plan in plans if plan.review_type != HISTORICAL for query in plan.exact
    )
    other_rows = search.lookup_reference_rows(
        (query for plan in plans for query in plan.alternatives if not exact_rows.get(query)),
        other_sub_numbers=True,
    )
    cards = [{} for _ in items]
    for i, (plan, result) in enumerate(zip(plans, base, strict=True)):
        if plan.review_type == VERIFIED:
            assert result.standard_address
            lots = (result.standard_lot,) if result.standard_lot else result.selected_lots
            lot_addresses = {lot.address for lot in lots}
            # 확정된 도로명주소에 연결된 지번을 모두 남긴다. 화면에서 도로명주소와 지번의 관계(1:N 등)를 보여준다.
            for lot_address in sorted(lot_addresses) or [""]:
                _add(cards[i], "road", result.standard_address.standard_road_address, 0,
                     "기준연도 기본 도로명주소 확인", year, result.raw_record_ids, linked_lot=lot_address,
                     admin_dongs=tuple(lot.admin_dong for lot in lots), standard=result.standard_address)
        elif plan.review_type != HISTORICAL:
            _add_number_candidates(cards[i], plan, result, exact_rows, other_rows, year)
    _add_admin_mismatch_candidates(search, plans, base, cards, year)
    _add_building_candidates(
        search, {i: plan.building for i, plan in enumerate(plans) if plan.building}, cards, year,
    )
    _add_ri_building_candidates(search, items, base, cards, year)
    _add_public_building_candidates(search, items, plans, cards, year)
    _add_historical_candidates(history, plans, cards)
    glued = _add_glued_unit_candidates(search, items, base, cards, year)
    typos = _add_number_typo_candidates(search, base, cards, year)
    reviews = [_finish_review(
        replace(plan, review_type=(NUMBER_UNIT_GLUED if i in glued else NUMBER_TYPO if i in typos
                                   else plan.review_type)), result, candidates, year)
        for i, (plan, result, candidates) in enumerate(zip(plans, base, cards, strict=True))]
    return [replace(review, tier=TIER_GENERAL) if i in glued or i in typos else review
            for i, review in enumerate(reviews)]
