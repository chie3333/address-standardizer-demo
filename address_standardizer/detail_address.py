"""원문 주소의 건물번호·지번을 기준자료에서 조회하고 도로명 주소를 반환한다."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
import re

from pandas import DataFrame

from .contracts import (
    STANDARD_ADDRESS_COLUMNS, LabeledToken, RawAddressRow, RegionValidationResult, StandardAddress,
)
from .preprocessing import normalize_address
from .region_validation import RegionValidator
from .standardization import project_address


_NUMBER = re.compile(r"(?P<mount>산\s*)?(?P<main>[0-9]+)(?:-(?P<sub>[0-9]+))?(?P<label>\s*번지)?")
_DETAIL_SUFFIX = re.compile(r"\s*(?:(?:호동|[동층호실통반])(?![가-힣])|[/~∼-]|\.[0-9])")
_PARCEL_SEPARATOR = re.compile(r"\s*[,，]\s*(?=(?:산\s*)?[0-9]|-\s*[0-9])")
# '771-3, -6, -7'처럼 본번을 생략하고 부번만 적은 지번. 앞 지번의 본번을 이어 쓴다.
_SHORT_SUB = re.compile(r"-\s*(?P<sub>[0-9]+)(?P<label>\s*번지)?")
_EXTRA_PARCELS = re.compile(r"(?:외|등)\s*[0-9]*\s*필지|외\s*[0-9]+\s*(?:곳|건)")
_PARENTHESIS = re.compile(r"\(([^()]*)\)")
_UNIT = re.compile(r"(?:^|(?<=[ ,(/])|(?<=[0-9][동층호]))((?:지하\s*)?[0-9]+)\s*(동|층|호)(?![가-힣])")
_LOT_LABEL_SUB = re.compile(r"-\s*([0-9]+)")
_OLD_LOT_SUB = re.compile(r"([0-9]{1,4})\s*호(?:(?![가-힣])|(?=(?:외|등)\s*[0-9]*\s*필지))")
AMBIGUOUS_LOT_UNIT = "ambiguous_lot_sub_or_unit"
LEGAL_DONG_MISMATCH = "legal_dong_mismatch"
AMBIGUOUS_SUB_OR_DETAIL = "ambiguous_sub_number_or_detail"
# 도로명 뒤 'N번'(외금곡길 57번): 번호가 '번'에서 끝났으므로 부번 없는 건물번호 N으로 읽는다.
_BEON = re.compile(r"번(?=$|[\s,)])")
# 'N번 M'(태평로152번 27): 'N번길'의 '길'이 빠졌거나(태평로152번길 27) 'N-M'을 띄어 쓴 것(태평로 152-27)일 수 있다.
_BEON_THEN_NUMBER = re.compile(r"([0-9]+)번\s+([0-9]+(?:-[0-9]+)?)(?=$|[\s,])")
BEON_ROAD_OR_SUB = "beon_road_or_sub_number"
# 본번 뒤에 단위 없이 붙은 숫자: 28 1, 28, 1, 28 (1), 28 1번, 28 28-1. 1층·1동처럼 단위가 붙으면 제외한다.
# 지번은 '601 1호'의 호가 부번(601-1)일 수도, 건물 호수일 수도 있어 지번 조회에서는 '호'도 후보로 본다.
# 'N번지 M호'처럼 번지가 앞에 있으면 _read_query에서 이미 부번 M으로 읽으므로 여기까지 오지 않는다.
# 도로명 주소의 '28 1호'는 건물 호수로 둔다.
_TRAILING_NUMBER = re.compile(r"\(?\s*(?P<main>[0-9]{1,5})(?:\s*-\s*(?P<sub>[0-9]{1,5}))?\s*\)?\s*번?(?=$|[\s,)])")
_TRAILING_LOT_NUMBER = re.compile(r"\(?\s*(?P<main>[0-9]{1,5})(?:\s*-\s*(?P<sub>[0-9]{1,5}))?\s*\)?\s*[번호]?(?=$|[\s,)])")


def trailing_sub_number(detail_text: str, match: "NumberMatch") -> int | None:
    """부번 없이 조회한 번호 뒤의 단위 없는 숫자가 같은 본번의 실제 부번이면 그 부번을 반환한다.

    `예양길 28 1`의 1은 28-1을 띄어 쓴 것일 수도, 상세주소일 수도 있다. 28-1이 실제로 있으면
    본번만으로 확정하지 않고 두 해석을 검토하도록 넘긴다.
    """
    query = match.query
    if query.sub or query.sub_explicit or not match.other_sub_numbers:
        return None
    found = (_TRAILING_LOT_NUMBER if query.kind == "lot" else _TRAILING_NUMBER).match(detail_text)
    if found is None:
        return None
    if found["sub"]:
        if int(found["main"]) != query.main:
            return None
        sub = int(found["sub"])
    else:
        sub = int(found["main"])
    return sub if sub in match.other_sub_numbers else None
_LOCAL = {"읍면동", "리"}
_REFERENCE_COLUMNS = (*STANDARD_ADDRESS_COLUMNS, "법정동리명", "지번본번", "지번부번", "대지구분코드",
                      "건물명", "시군구관리건물명", "행정동명")


def normalize_search_text(raw_address: str) -> str:
    text = normalize_address(raw_address)
    text = re.sub(r"^(?:우편번호\s*)?\([0-9]{5}\)\s*", "", text)
    text = re.sub(r"(?<=[0-9])\.(?=[0-9]+(?:대로|로|길))", "·", text)
    text = re.sub(r"^전북특별자치시", "전북특별자치도", text)
    return re.sub(r"^울상광역시", "울산광역시", text)


@dataclass(frozen=True)
class DetailInput:
    raw_address: str
    source_type: str = ""
    review_note: str = ""
    source_sheet: str = ""
    # 예전에는 'N번지 M호'를 발췌 지번과 대조하는 데 썼다. 지금은 번지 뒤 호를 항상 부번으로 읽어 쓰지 않으며,
    # 위치 인자로 넘기는 기존 호출(인풋 주소 분석/detail_workbook.py)과의 호환을 위해 남겨 둔다.
    lot_hint: str = ""
    validated_tokens: tuple[LabeledToken, ...] = ()
    region_validation: RegionValidationResult | None = None
    hold_reason: str = ""


@dataclass(frozen=True)
class NumberQuery:
    kind: str
    tokens: tuple[str, ...]
    main: int
    sub: int
    land: int = 0
    use_road_scope: bool = False
    sub_explicit: bool = False
    verified_scope: tuple[tuple[int, int], ...] | None = None
    # 법정동 대신 행정동 이름(예: 대학동 → 법정동 신림동)으로 찾았으면 RAW 행정동명도 일치해야 한다.
    admin_dong: str = ""


@dataclass(frozen=True)
class _VerifiedNames:
    text: str
    roles: dict[str, set[str]]
    paths: list[list[dict[str, str | None]]]


def verified_names(item: DetailInput, validator: RegionValidator) -> _VerifiedNames:
    """Reuse the names, accepted corrections and paths from region validation."""
    text = normalize_search_text(item.raw_address)
    validation = item.region_validation
    if not validation or not validation["paths"]:
        return _VerifiedNames(text, {}, [])
    paths = validation["paths"]
    roles = {}
    for token, labels in zip(item.validated_tokens, validation.get("db_labels", ())):
        name = normalize_search_text(token["raw"])
        if not name or not labels:
            continue
        corrected = token.get("corrected")
        if corrected and validation.get("used_correction") and text.count(name) == 1:
            # LLM 수정값이 약칭(서울, 서대문)이어도 지역 검증과 같은 약칭 표로 정식 명칭을 찾아 넣는다.
            def accepted(node):
                return node["label"] in labels and node["name"] in validator.official_names(corrected, node["label"])
            matches = [{node["name"] for node in path if accepted(node)} for path in paths]
            official = set().union(*matches)
            if all(matches) and len(official) == 1:
                corrected, = official
                text = text.replace(name, corrected, 1)
                name = corrected
        roles.setdefault("".join(name.split()), set()).update(labels)
    return _VerifiedNames(text, roles, paths)


@dataclass(frozen=True)
class DetailPlan:
    queries: tuple[NumberQuery, ...] = ()
    detail_text: str = ""
    issue: str = ""
    # 원문 하나를 두 가지로 읽어 함께 조회할 때(태평로152번 27): (해석 이름, 그 해석의 조회 계획)
    readings: tuple[tuple[str, "DetailPlan"], ...] = ()


@dataclass(frozen=True)
class NumberMatch:
    query: NumberQuery
    status: str
    raw_record_ids: tuple[int, ...] = ()
    address_candidates: tuple[StandardAddress, ...] = ()
    lot_candidates: tuple[str, ...] = ()
    other_sub_numbers: tuple[int, ...] = ()


@dataclass(frozen=True)
class StandardLot:
    address: str
    legal_dong_code: str
    administrative_area_code: str
    admin_dong: str = ""  # 기준자료 행의 행정동명


@dataclass(frozen=True)
class ReferenceMatch:
    road: StandardAddress | None
    lot: StandardLot | None
    building_names: tuple[str, ...]
    admin_dong: str = ""


@dataclass(frozen=True)
class DetailResult:
    status: str
    reason: str = ""
    standard_address: StandardAddress | None = None
    detail_text: str = ""
    detail_units: tuple[str, ...] = ()
    raw_record_ids: tuple[int, ...] = ()
    matches: tuple[NumberMatch, ...] = ()
    standard_lot: StandardLot | None = None
    # 선택된 기준자료 행의 지번주소와 행정동(도로명으로 찾은 경우에도 채운다).
    selected_lots: tuple[StandardLot, ...] = ()


def _project_reference(raw: RawAddressRow) -> ReferenceMatch:
    def value(column: str) -> str:
        return str(raw.columns[column]).strip() if raw.columns[column] is not None else ""

    def code(column: str) -> str:
        match = re.fullmatch(r"([0-9]+)(?:\.0+)?", value(column))
        return match[1] if match else ""

    main, sub = value("지번본번"), value("지번부번")
    legal = code("법정동코드")
    lot = None
    if main.isascii() and main.isdecimal() and int(main) > 0 and sub.isascii() and sub.isdecimal() and legal:
        number = f"{int(main)}-{int(sub)}" if int(sub) else str(int(main))
        mount = "산" if code("대지구분코드") == "1" else ""
        region = tuple(value(column) for column in ("법정동시도명", "법정동시군구명", "법정동읍면동명", "법정동리명"))
        lot = StandardLot(" ".join(filter(None, (*region, mount, number))), legal, code("행정구역분류코드"),
                          value("행정동명"))
    names = tuple(filter(None, (value("건물명"), value("시군구관리건물명"))))
    return ReferenceMatch(project_address(raw, allow_missing_codes=True), lot, names, value("행정동명"))


def _mentions_building(detail: str, names: tuple[str, ...]) -> bool:
    for name in names:
        parts = normalize_address(name).split()
        if sum(map(len, parts)) < 4:
            continue
        literal = r"\s*".join(map(re.escape, parts))
        if re.search(rf"(?<!\w){literal}(?=$|[\s,()]|[0-9]+\s*[동층호](?![가-힣]))", detail, re.I):
            return True
    return False



def glued_unit_interpretations(text: str) -> tuple[str, ...]:
    """Suggest number+three-digit dong separation; never assert correctness.

    e.g. '수양로27106동' -> '수양로 27 106동', subject to DB lookup.
    A bare '수양로107동' remains untouched: its building number is unknown.
    """
    pattern = re.compile(
        r"(?P<road>[가-힣0-9·]+(?:대로|로|길))\s*(?P<joined>[0-9]{4,8})동(?![가-힣])"
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        return ()
    hit = matches[0]
    joined = hit["joined"]
    main, unit = joined[:-3], joined[-3:]
    if not main or not int(main) or not 101 <= int(unit) <= 999 or len(main) > 5:
        return ()
    corrected = (
        text[:hit.start()] + hit["road"] + " " + str(int(main)) + " "
        + str(int(unit)) + "동" + text[hit.end():]
    )
    return (corrected,)


class DetailAddressSearch:
    """같은 기준연도의 지역 카탈로그와 RAW_DATA로 배치 검색한다."""

    def __init__(self, catalog: str | Path, raw_data: str | Path, year: int = 2025):
        import duckdb

        self.raw_data = Path(raw_data)
        self.validator = RegionValidator(catalog, year)
        self.names = defaultdict(dict)
        self.parent_names = set()
        for path, options in self.validator.paths:
            for region, aliases in zip(path, options, strict=True):
                for label, name in aliases | {(region.label, region.name)}:
                    self._add_name(name, label)
        for name in self.validator.aliases:
            self._add_name(name, "시도")
        for name in self.validator.sigungu_aliases:
            self._add_name(name, "시군구")
        with duckdb.connect(str(catalog), read_only=True) as db:
            source = db.execute(
                "SELECT file_name, file_size_bytes FROM sources WHERE 기준연도 = ?", [year],
            ).fetchone()
            if source != (self.raw_data.name, self.raw_data.stat().st_size):
                raise ValueError("선택한 RAW_DATA가 카탈로그의 기준자료와 다릅니다.")
            for name, in db.execute(
                "SELECT DISTINCT unnest(road_names) FROM road_catalog WHERE 기준연도 = ?", [year],
            ).fetchall():
                self._add_name(name, "도로명")
        self.legal_codes_by_prefix = defaultdict(set)
        for code in self.validator.path_legal_codes:
            self.legal_codes_by_prefix[code].add(int(code))
            self.legal_codes_by_prefix[code[:8]].add(int(code))

    def _add_name(self, name: str, label: str) -> None:
        compact = "".join(normalize_address(name).split())
        if compact:
            self.names[compact[0]].setdefault(compact, {})[label] = name
        if label in {"시도", "시군구"}:
            self.parent_names.add(name)

    def _prefix(self, text: str, verified_roles=None) -> tuple[tuple[str, ...], set[str], str]:
        """기준 명칭의 최장 접두어를 찾고 남은 번호 문자열을 반환한다."""
        positions = [i for i, char in enumerate(text) if not char.isspace()]
        compact = "".join(text[i] for i in positions)
        tokens = []
        roles: set[str] = set()
        cursor = 0
        while cursor < len(compact):
            choices = self.names.get(compact[cursor], {})
            name = max(
                (name for name in choices if compact.startswith(name, cursor)),
                key=len, default="",
            )
            if not name:
                break
            if verified_roles and not (choices[name].keys() & verified_roles.get(name, set())):
                # 이미 확인한 명칭의 공백 경계가 있으면 다음 명칭의 글자를 가져오지 않는다.
                verified = max((
                    prefix for prefix, labels in verified_roles.items()
                    if prefix != name and name.startswith(prefix)
                    and choices.get(prefix, {}).keys() & labels
                    and positions[cursor + len(prefix) - 1] + 1 < positions[cursor + len(prefix)]
                ), key=len, default="")
                if verified:
                    name = verified
            if any(
                compact[i].isdigit() and compact[i + 1].isdigit() and positions[i] + 1 < positions[i + 1]
                for i in range(cursor, cursor + len(name) - 1)
            ):
                # 도로명 안의 숫자가 나뉘어 있으면 짧은 도로명과 다른 건물번호로 해석하지 않는다.
                return tuple(tokens), set(), text[positions[cursor]:]
            entries = choices[name]
            roles = set(entries)
            if verified_roles and name in verified_roles:
                roles = roles.intersection(verified_roles[name]) or roles
            tokens.append(next(value for label, value in entries.items() if label in roles))
            cursor += len(name)
            if "도로명" in roles:
                break
        rest = text[positions[cursor]:] if cursor < len(positions) else ""
        return tuple(tokens), roles, rest

    def _read_query(
        self, text: str, inherited: tuple[str, ...] = (), *, bare_lot: bool = False,
        marked_lot: bool = False, verified_roles=None,
    ) -> tuple[NumberQuery | None, str, str]:
        tokens, roles, rest = self._prefix(text, verified_roles)
        kind = "road" if "도로명" in roles else "lot"
        if not roles.intersection(_LOCAL | {"도로명"}) and not (bare_lot and not tokens):
            return None, text, "missing_address_scope"
        if kind == "road" and re.match(r"지하\s*[0-9]", rest):
            return None, text, "underground_road"
        qualifier = ""
        if enclosed := _PARENTHESIS.match(rest):
            if re.fullmatch(r"[가-힣 ]+", enclosed[1]) and _NUMBER.match(rest[enclosed.end():].lstrip()):
                qualifier = enclosed[0]
                rest = rest[enclosed.end():].lstrip()
        match = _NUMBER.match(rest)
        if match is None:
            return None, text, "missing_address_number"
        if roles.intersection(_LOCAL) and (match["label"] or match["mount"]):
            kind = "lot"
        if kind == "road" and (match["mount"] or match["label"]):
            return None, text, "ambiguous_number_type"
        if bare_lot and not tokens and not (marked_lot or match["label"] or match["mount"]):
            return None, text, "missing_address_scope"
        main, sub = match["main"], match["sub"] or "0"
        beon = _BEON.match(rest, match.end()) if kind == "road" and not match["label"] else None
        if beon and re.match(r"[0-9]+(?:-[0-9]+)?(?=$|[\s,])", rest[beon.end():].lstrip()):
            beon = None  # 태평로152번 27: '번길'의 '길'이 빠진 도로명일 수 있다(3층·101동처럼 단위가 붙으면 상세)
        tail = rest[beon.end() if beon else match.end():].lstrip()
        labeled_sub = _LOT_LABEL_SUB.match(tail) if match["label"] and not match["sub"] else None
        if labeled_sub:
            sub = labeled_sub[1]
            tail = tail[labeled_sub.end():].lstrip()
        if _DETAIL_SUFFIX.match(tail):
            return None, text, "ambiguous_number_boundary"
        attached = None if beon else re.match(r"[가-힣A-Za-z]", rest[match.end():])
        if attached and not tail.startswith(("외", "등")):
            note_tokens, note_roles, note_rest = self._prefix(tail)
            if kind == "road" and note_roles.intersection(_LOCAL) and not re.match(r"[0-9]", note_rest):
                tokens = (*tokens, *note_tokens)
                tail = note_rest
            else:
                return None, text, "ambiguous_number_boundary"
        limit = 5 if kind == "road" else 4
        if len(main) > limit or len(sub) > limit or int(main) == 0:
            return None, text, "invalid_address_number"
        issue = ""
        ho_sub = False
        # 지번 'N번지 M호'의 호는 지번 부번이다(번지 뒤의 호만 해당). 123번지 4호 → 123-4
        if (kind == "lot" and not match["sub"] and not labeled_sub and match["label"]
                and (old_sub := _OLD_LOT_SUB.match(tail))):
            if not int(old_sub[1]):
                return None, text, AMBIGUOUS_LOT_UNIT
            sub = old_sub[1]
            tail = tail[old_sub.end():].lstrip()
            ho_sub = True
        # 반복된 동일 지역명은 한 번만 검증하고, 서로 다른 지역명은 모두 검증한다.
        scope = tuple(dict.fromkeys((*inherited, *tokens)))
        query = NumberQuery(
            kind, scope, int(main), int(sub), int(bool(match["mount"])),
            sub_explicit=bool(match["sub"] or labeled_sub or ho_sub),
        )
        return query, " ".join(filter(None, (qualifier, tail))), issue

    def _beon_readings(self, text: str, found: re.Match, roles) -> tuple[tuple[str, "DetailPlan"], ...]:
        """'N번 M'을 'N번길 M'(번길의 길 누락)과 'N-M'(부번을 띄어 씀) 두 가지로 읽은 조회 계획."""
        before, after = text[:found.start()], text[found.end():]
        variants = [("road_suffix", f"{before}{found[1]}번길 {found[2]}{after}")]
        if "-" not in found[2]:
            variants.append(("sub_number", f"{before}{found[1]}-{found[2]}{after}"))
        readings = []
        for name, variant in variants:
            query, tail, issue = self._read_query(variant, verified_roles=roles)
            if query is not None and not issue and query.kind == "road":
                readings.append((name, DetailPlan((query,), tail)))
        return tuple(readings)

    def _choose_reading(self, plan: DetailPlan, results: dict) -> DetailResult:
        """번길로 읽은 주소만 있으면 그 주소로 확정하고, 부번으로 읽은 주소도 있으면 둘 다 후보로 보류한다."""
        found = [(name, result) for name, reading in plan.readings
                 if (result := self._resolve(reading, results)).status in {"resolved", "address_only"}]
        if [name for name, _ in found] == ["road_suffix"]:
            return found[0][1]
        if not found:
            return self._resolve(replace(plan, readings=()), results)
        return DetailResult("held", BEON_ROAD_OR_SUB, detail_text=plan.detail_text,
                            matches=tuple(match for _, result in found for match in result.matches))

    def _plan(self, item: DetailInput) -> DetailPlan:
        text = normalize_address(item.raw_address)
        if re.search(r"사\s*서\s*함", text):
            return DetailPlan(detail_text=text, issue="po_box")
        if "주소가 여러 개" in item.review_note:
            return DetailPlan(detail_text=text, issue="multiple_addresses")
        names = verified_names(item, self.validator)
        text = names.text
        primary, tail, issue = self._read_query(text, verified_roles=names.roles)
        if primary is None and issue == "ambiguous_number_boundary" and (found := _BEON_THEN_NUMBER.search(text)):
            readings = self._beon_readings(text, found, names.roles)
            if readings:
                return DetailPlan(detail_text=text, issue=issue, readings=readings)
        if primary is None:
            if item.source_type in {"상세주소만", "건물번호 미확인", "주소번호 없음"} and issue in {
                "missing_address_scope", "missing_address_number", "ambiguous_number_boundary",
            }:
                issue = "missing_base_address"
            return DetailPlan(detail_text=text, issue=issue)

        queries = [primary]
        if primary.kind == "lot":
            while tail:
                separator = _PARCEL_SEPARATOR.match(tail)
                raw = tail[separator.end():] if separator else tail
                if separator and (short := _SHORT_SUB.match(raw)):
                    if _DETAIL_SUFFIX.match(raw[short.end():]):
                        break
                    if len(short["sub"]) > 4 or not int(short["sub"]):
                        issue = "invalid_address_number"
                        break
                    previous = queries[-1]
                    queries.append(NumberQuery(
                        "lot", primary.tokens, previous.main, int(short["sub"]), previous.land, sub_explicit=True,
                    ))
                    tail = raw[short.end():].lstrip()
                    continue
                match = _NUMBER.match(raw)
                if not match or _DETAIL_SUFFIX.match(raw[match.end():]):
                    break
                if not separator and (
                    item.source_type != "복수 필지" or not match["sub"] or int(match["main"]) != primary.main
                ):
                    break
                if primary.sub and not (match["sub"] or match["label"] or match["mount"]):
                    issue = "ambiguous_parcel_list"
                    break
                if any(len(value) > 4 for value in (match["main"], match["sub"] or "0")) or not int(match["main"]):
                    issue = "invalid_address_number"
                    break
                queries.append(NumberQuery(
                    "lot", primary.tokens, int(match["main"]), int(match["sub"] or 0),
                    1 if match["mount"] else primary.land,
                    sub_explicit=bool(match["sub"]),
                ))
                tail = raw[match.end():].lstrip()

        # 보조 주소는 괄호 안이나 대표 주소 바로 뒤에서만 해석한다.
        parents = tuple(token for token in primary.tokens if token in self.parent_names)
        secondary_tail = secondary_text = tail.lstrip(", ")
        enclosed = _PARENTHESIS.match(secondary_text)
        marked_lot = False
        if enclosed:
            secondary_text = enclosed[1].strip()
            marked_lot = bool(re.match(r"^(?:구\s*주소|,)", secondary_text))
            secondary_text = re.sub(r"^(?:구\s*주소\s*[:：]?|,)\s*", "", secondary_text)
        secondary, remainder, secondary_issue = self._read_query(
            secondary_text, parents, bare_lot=primary.kind == "road", marked_lot=marked_lot,
            verified_roles=names.roles,
        )
        if secondary is not None:
            if secondary.kind != primary.kind and len(queries) == 1:
                if secondary.kind == "lot" and secondary.tokens == parents:
                    secondary = replace(secondary, tokens=primary.tokens, use_road_scope=True)
                queries.append(secondary)
                tail = remainder
                if enclosed:
                    tail = (tail + " " + secondary_tail[enclosed.end():]).strip()
            elif secondary == primary:
                tail = secondary_tail[enclosed.end():].strip() if enclosed else remainder
            else:
                issue = "multiple_addresses"
        elif secondary_issue in {AMBIGUOUS_LOT_UNIT, "underground_road", "ambiguous_number_type"}:
            issue = secondary_issue
        elif enclosed and primary.kind == "road":
            note_tokens, note_roles, note_rest = self._prefix(secondary_text)
            if note_roles.intersection(_LOCAL) and (not note_rest or note_rest.startswith(",")):
                scope = tuple(dict.fromkeys((*primary.tokens, *note_tokens)))
                queries[0] = replace(primary, tokens=scope)
        # 상세 표기 뒤에 또 다른 주소가 시작되면 같은 셀의 대표 주소를 임의로 고르지 않는다.
        for marker in re.finditer(r"(?:^|[ (,]+)(?=[가-힣])", tail):
            extra, _, _ = self._read_query(tail[marker.end():])
            if extra and extra not in queries:
                issue = issue or "multiple_addresses"
                break

        if issue != AMBIGUOUS_LOT_UNIT:
            if _EXTRA_PARCELS.search(tail):
                issue = "unnamed_additional_parcels"
            elif item.source_type == "복수 필지" and len(queries) == 1:
                issue = issue or "unparsed_parcel_list"
            elif item.source_type == "도로명·지번 혼합" and len({query.kind for query in queries}) != 2:
                issue = issue or "unparsed_mixed_address"
        queries = [self.use_verified_scope(query, names) for query in queries]
        return DetailPlan(tuple(dict.fromkeys(queries)), tail.strip(" ,"), issue)

    def use_verified_scope(self, query: NumberQuery, names: _VerifiedNames) -> NumberQuery:
        # Mixed addresses can carry separate regions. Reuse the whole path only
        # for a query covering the same set of names as region validation.
        if names.paths and {"".join(token.split()) for token in query.tokens} == names.roles.keys():
            return replace(query, verified_scope=tuple(sorted(self._scope(query, names.paths))),
                           admin_dong=self._admin_dong(query, names.paths))
        return query

    def _admin_dong(self, query: NumberQuery, paths: list) -> str:
        """조회 토큰이 경로의 법정동이 아니라 그 안의 행정동 이름이면 그 행정동명을 반환한다."""
        found = set()
        for path in paths:
            nodes = [node for node in path if node["label"] == "읍면동"]
            if not nodes:
                return ""
            node = nodes[-1]
            admin = {
                name for name in ("".join(token.split()) for token in query.tokens)
                if name != node["name"] and name in self.validator.emd_admin_names.get(node["code_prefix"], ())
            }
            if len(admin) != 1:
                return ""
            found |= admin
        return found.pop() if len(found) == 1 else ""

    def _scope(self, query: NumberQuery, paths: list) -> set[tuple[int, int]]:
        keys = set()
        for path in paths:
            end = path[-1]
            if query.kind == "road" and end["label"] == "도로명":
                # 도로 경로에는 이 도로가 실제로 연결된 법정동·리까지 들어 있다.
                legal = path[-2]["code_prefix"]
                if legal and path[-2]["label"] in _LOCAL:
                    keys.add((int(end["code_prefix"]), int(legal.ljust(10, "0"))))
            elif query.kind == "lot" and not query.use_road_scope and end["label"] in _LOCAL:
                keys.update((0, code) for code in self.legal_codes_by_prefix[end["code_prefix"]])
            elif query.use_road_scope and end["label"] == "도로명":
                # 지역명 없이 붙은 '898번지'는 같은 도로 경로의 법정지역 안에서 조회한다.
                legal = path[-2]["code_prefix"]
                if legal and path[-2]["label"] in _LOCAL:
                    keys.add((0, int(legal.ljust(10, "0"))))
        return keys

    def validate_scopes(self, token_sets):
        scopes = dict.fromkeys(token_sets)
        if not scopes:
            return {}
        validations, _ = self.validator.validate_batch(
            {"address_id": str(i), "tokens": [{"raw": name, "labels": []} for name in tokens]}
            for i, tokens in enumerate(scopes)
        )
        return dict(zip(scopes, (v["paths"] for v in validations), strict=True))

    def _requests(self, queries, *, ignore_admin=False):
        paths_by_tokens = self.validate_scopes(query.tokens for query in queries if query.verified_scope is None)
        return [
            (index, query.kind, road, legal, query.land, query.main, query.sub,
             "" if ignore_admin else query.admin_dong or (
                 self._admin_dong(query, paths_by_tokens[query.tokens]) if query.verified_scope is None else ""))
            for index, query in enumerate(queries)
            for road, legal in (
                query.verified_scope if query.verified_scope is not None
                else sorted(self._scope(query, paths_by_tokens[query.tokens]))
            )
        ]

    def lookup_reference_rows(
        self, queries, *, other_sub_numbers=False, ignore_admin=False,
    ) -> dict[NumberQuery, dict[int, ReferenceMatch]]:
        """조회 키별로 일치 행 또는 같은 본번의 다른 부번 행을 반환한다.

        `ignore_admin=True`면 행정동 이름 조건 없이 법정동 범위 전체에서 찾는다(참고 후보용).
        """
        queries = tuple(dict.fromkeys(queries))
        rows, *_ = self._lookup(self._requests(queries, ignore_admin=ignore_admin), other_sub_numbers=other_sub_numbers)
        return {query: rows.get(index, {}) for index, query in enumerate(queries)}


    def resolve_compound_input(self, raw: str) -> tuple[str, DetailInput | None, tuple[str, ...]]:
        """Validate explicitly separated full addresses against a common RAW building.

        Returns ('not_compound' | 'same_building' | 'multiple_addresses'
        | 'ambiguous_multiple_addresses', representative, fragments).
        A single matching string is not treated as a multi-address input.
        """
        from .preprocessing import _ROAD_ADDRESS, _LOT_ADDRESS

        # Do not split commas inside an ordinary detail field unless each piece is a full address.
        original = str(raw or "").strip()
        line_parts = [x.strip(" ,;") for x in re.split(r"[\n;]+", original) if x.strip(" ,;")]
        if len(line_parts) >= 2:
            fragments = line_parts
        else:
            full = re.compile(rf"(?:{_ROAD_ADDRESS})|(?:{_LOT_ADDRESS})")
            matches = list(full.finditer(normalize_address(original)))
            fragments = [m.group().strip() for m in matches] if len(matches) >= 2 else []
            if len(fragments) >= 2:
                consumed = full.sub("", normalize_address(original))
                if re.sub(r"[\s,;/()]+", "", consumed):
                    fragments = []
        if not 2 <= len(fragments) <= 6:
            return "not_compound", None, ()
        # A single address split across two lines (region on line 1, road on line 2)
        # must not be treated as two independently complete addresses.
        if not all(self._plan(DetailInput(piece)).queries for piece in fragments):
            return "not_compound", None, ()
        separate = self.search_batch([DetailInput(piece) for piece in fragments])
        verified = [r for r in separate if r.status in {"resolved", "address_only"} and r.standard_address]
        if len(verified) != len(fragments):
            return "ambiguous_multiple_addresses", None, tuple(fragments)
        addresses = {r.standard_address.standard_road_address for r in verified}
        shared = set(verified[0].raw_record_ids)
        for result in verified[1:]:
            shared.intersection_update(result.raw_record_ids)
        if len(addresses) == 1 and shared:
            return "same_building", DetailInput(fragments[0]), tuple(fragments)
        if len(addresses) > 1:
            return "multiple_addresses", None, tuple(fragments)
        return "ambiguous_multiple_addresses", None, tuple(fragments)

    def search_batch(self, items: list[DetailInput]) -> list[DetailResult]:
        plans = [self._plan(item) for item in items]
        query_ids = dict.fromkeys(
            query for plan in plans for each in (plan, *(reading for _, reading in plan.readings))
            for query in each.queries
        )
        requests = self._requests(query_ids)
        rows, other_subs, other_legal = self._lookup(requests)
        results = {}
        scoped_ids = {request[0] for request in requests}
        for index, query in enumerate(query_ids):
            hits = rows.get(index, {})
            evidence = [hits[serial] for serial in sorted(hits)]
            candidates = tuple(dict.fromkeys(row.road for row in evidence if row.road))
            lots = tuple(dict.fromkeys(row.lot.address for row in evidence if row.lot))
            sub_candidates = tuple(sorted(other_subs.get(index, ())))
            if hits:
                status = "exact"
            elif index in other_legal:
                status = LEGAL_DONG_MISMATCH
            elif sub_candidates:
                status = "sub_number_mismatch" if query.sub_explicit else "sub_number_omitted"
            elif index in scoped_ids:
                status = "number_not_found"
            else:
                status = "scope_not_found"
            results[query] = (NumberMatch(query, status, tuple(sorted(hits)), candidates, lots, sub_candidates), hits)
        resolved = [self._choose_reading(plan, results) if plan.readings else self._resolve(plan, results)
                    for plan in plans]
        return [
            replace(result, status="held", reason=item.hold_reason, standard_address=None,
                    standard_lot=None, raw_record_ids=())
            if item.hold_reason and result.status in {"resolved", "address_only", "lot_only"} else result
            for item, result in zip(items, resolved, strict=True)
        ]

    def _lookup(self, requests: list[tuple], *, other_sub_numbers=False):
        """번호 체계별로 Parquet을 한 번씩 조회하고 같은 본번의 부번 차이를 기록한다."""
        import duckdb

        hits = defaultdict(dict)
        other_subs = defaultdict(set)
        other_legal = set()
        if not requests:
            return hits, other_subs, other_legal
        frame = DataFrame(requests, columns=["qid", "kind", "road", "legal", "land", "main", "sub", "admin"])
        evidence = "n.in_scope AND " + (
            "NOT n.exact AND n.sub_number IS NOT NULL" if other_sub_numbers else "n.exact"
        )
        columns = ", ".join(f'CASE WHEN {evidence} THEN r."{name}" END' for name in _REFERENCE_COLUMNS)
        with duckdb.connect() as db:
            db.register("requests", frame)
            # 도로명은 도로명코드·건물번호로 건물을 찾고, 법정동코드는 범위 안인지 판정하는 데만 쓴다.
            # 같은 번호가 범위 밖 법정동에만 있으면 번호 없음과 구분해 동명 불일치로 기록한다.
            for kind, prefix, extra, in_scope in (
                ("road", "건물", 'r."도로명코드" = q.road', "r.법정동코드 = q.legal"),
                ("lot", "지번", 'r."대지구분코드" = q.land AND r.법정동코드 = q.legal', "true"),
            ):
                if kind not in frame["kind"].values:
                    continue
                found = db.execute(f"""
                    SELECT DISTINCT q.qid, CASE WHEN {evidence} THEN r.일련번호 END,
                        CASE WHEN n.in_scope AND NOT n.exact THEN n.sub_number END,
                        NOT n.in_scope AND n.exact, {columns}
                    FROM read_parquet(?) r
                    JOIN requests q ON q.kind = ?
                        AND {extra} AND TRY_CAST(r.{prefix}본번 AS INTEGER) = q.main
                    CROSS JOIN LATERAL (
                        SELECT TRY_CAST(r.{prefix}부번 AS INTEGER) AS sub_number,
                            COALESCE(sub_number = q.sub, false) AS exact,
                            COALESCE(({in_scope}) AND (q.admin = '' OR r.행정동명 = q.admin), false) AS in_scope
                    ) n
                """, [str(self.raw_data), kind]).fetchall()
                for index, serial, other_sub, outside, *values in found:
                    if serial is not None:
                        raw = RawAddressRow(str(int(serial)), dict(zip(_REFERENCE_COLUMNS, values, strict=True)))
                        hits[index][int(serial)] = _project_reference(raw)
                    if other_sub is not None:
                        other_subs[index].add(other_sub)
                    if outside:
                        other_legal.add(index)
        return hits, other_subs, other_legal

    @staticmethod
    def _resolve(plan: DetailPlan, results: dict) -> DetailResult:
        matches = tuple(results[query][0] for query in plan.queries)
        units = defaultdict(list)
        if plan.queries:
            for value, label in _UNIT.findall(plan.detail_text):
                units[label].append(normalize_address(value) + label)
        detail_units = tuple(units[label][0] for label in "동층호" if len(units[label]) == 1)
        detail = {"detail_text": plan.detail_text, "detail_units": detail_units, "matches": matches}
        if plan.issue:
            return DetailResult("held", plan.issue, **detail)
        failures = tuple(dict.fromkeys(match.status for match in matches if match.status != "exact"))
        if failures:
            no_match = failures == ("number_not_found",) and all(m.status != "exact" for m in matches)
            status = "no_match" if no_match else "held"
            return DetailResult(status, "+".join(failures), **detail)
        if len(matches) == 1 and trailing_sub_number(plan.detail_text, matches[0]) is not None:
            return DetailResult("held", AMBIGUOUS_SUB_OR_DETAIL, **detail)
        hits = [results[query][1] for query in plan.queries]
        if len({query.kind for query in plan.queries}) > 1:
            common = set.intersection(*(set(rows) for rows in hits))
            if not common:
                return DetailResult("held", "road_lot_conflict", **detail)
            hits = [{serial: hits[0][serial] for serial in common}]
        selected = {serial: row for group in hits for serial, row in group.items()}
        if len(hits) == 1 and len(selected) > 1:
            named = {
                serial: row for serial, row in selected.items()
                if _mentions_building(plan.detail_text, row.building_names)
            }
            selected = named or selected
        evidence = {
            "raw_record_ids": tuple(sorted(selected)),
            "selected_lots": tuple(dict.fromkeys(row.lot for _, row in sorted(selected.items()) if row.lot)),
            **detail,
        }
        standard_lot = None
        if all(query.kind == "lot" for query in plan.queries):
            lots = {row.lot for row in selected.values()}
            if None not in lots and len({(lot.address, lot.legal_dong_code) for lot in lots}) == 1:
                sample = next(iter(lots))
                admin_codes = {lot.administrative_area_code for lot in lots}
                admin = next(iter(admin_codes)) if len(admin_codes) == 1 else ""
                admin_names = {lot.admin_dong for lot in lots}
                admin_name = next(iter(admin_names)) if len(admin_names) == 1 else ""
                standard_lot = StandardLot(sample.address, sample.legal_dong_code, admin, admin_name)
        addresses = {row.road for row in selected.values()}
        base_addresses = {address.standard_road_address for address in addresses if address}
        if None in addresses:
            if standard_lot:
                return DetailResult("lot_only", "incomplete_reference_road", standard_lot=standard_lot, **evidence)
            return DetailResult("held", "incomplete_reference_row", **detail)
        if len(base_addresses) != 1:
            if standard_lot:
                return DetailResult("lot_only", "multiple_road_addresses", standard_lot=standard_lot, **evidence)
            reason = "multiple_parcel_addresses" if len(hits) > 1 else "multiple_standard_outputs"
            return DetailResult("held", reason, **detail)
        codes = []
        for key in ("road_name_code", "legal_dong_code", "administrative_area_code"):
            values = {getattr(address, key) for address in addresses}
            codes.append(next(iter(values)) if len(values) == 1 else "")
        if not codes[0]:
            if standard_lot:
                return DetailResult("lot_only", "conflicting_road_codes", standard_lot=standard_lot, **evidence)
            return DetailResult("held", "conflicting_road_codes", **detail)
        status, reason = ("resolved", "") if all(codes) else ("address_only", "codes_missing_or_conflicting")
        return DetailResult(
            status, reason, StandardAddress(next(iter(base_addresses)), *codes),
            standard_lot=standard_lot, **evidence,
        )
