"""일치한 RAW_DATA 행으로 도로명 주소와 코드를 구성한다."""

import re

from .contracts import (
    RAW_DATA_COLUMNS, HierarchyOutput, RawAddressRow, StandardAddress, StandardizationOutput,
)


def normalize_number(value: str) -> str:
    if re.fullmatch(r"[0-9]+", value):
        return value.lstrip("0") or "0"
    return value


def project_address(row: RawAddressRow, *, allow_missing_codes: bool = False) -> StandardAddress | None:
    def value(field: str) -> str:
        raw = row.columns.get(RAW_DATA_COLUMNS[field])
        return "" if raw is None else str(raw).strip()

    sido, sigungu = value("sido"), value("sigungu")
    emd = value("legal_emd")
    road, main, sub = value("road_name"), value("building_main_no"), value("building_sub_no")
    if not sido or not road or not re.fullmatch(r"[0-9]+", main):
        return None
    if sub and not re.fullmatch(r"[0-9]+", sub):
        return None

    codes = []
    for field in ("road_name_code", "legal_dong_code", "administrative_area_code"):
        code = value(field)
        if allow_missing_codes and not code:
            codes.append("")
            continue
        match = re.fullmatch(r"([0-9]+)(?:\.0+)?", code)
        if match is None:
            return None
        codes.append(match[1])

    main, sub = normalize_number(main), normalize_number(sub) if sub else ""
    if main == "0":
        return None
    number = f"{main}-{sub}" if sub not in {"", "0"} else main
    address = " ".join(part for part in (
        sido, sigungu, emd if emd.endswith(("읍", "면")) else "", road, number,
    ) if part)
    return StandardAddress(address, *codes)


def standardize(hierarchy: HierarchyOutput) -> StandardizationOutput:
    if not hierarchy.hierarchy_candidates:
        return StandardizationOutput("held", reason_code=hierarchy.search_status)

    outputs: dict[StandardAddress, set[str]] = {}
    for candidate in hierarchy.hierarchy_candidates:
        source = candidate.source_candidate
        required = ("lot_main_no",) if source.address_type == "lot" else ("road_name", "building_main_no")
        columns = {label.column for label in source.token_labels}
        if any(RAW_DATA_COLUMNS[key] not in columns for key in required):
            return StandardizationOutput("held", reason_code="missing_lookup_fields")
        if not candidate.raw_rows:
            return StandardizationOutput("held", reason_code="missing_raw_rows")
        if set(candidate.raw_record_ids) != {row.record_id for row in candidate.raw_rows}:
            return StandardizationOutput("held", reason_code="incomplete_raw_rows")
        for row in candidate.raw_rows:
            projected = project_address(row)
            if projected is None:
                return StandardizationOutput("held", reason_code="incomplete_raw_row")
            outputs.setdefault(projected, set()).add(row.record_id)

    if len(outputs) != 1:
        return StandardizationOutput("held", reason_code="multiple_standard_outputs")
    address, ids = next(iter(outputs.items()))
    return StandardizationOutput("resolved", address, raw_record_ids=tuple(sorted(ids)))
