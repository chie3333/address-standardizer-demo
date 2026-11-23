"""확정된 주소를 제출 양식의 19개 컬럼으로 펼친다.

값의 출처
- 시도~리, 도로명, 건물번호, 건물명: 확정 근거가 된 RAW_DATA 행
- 대지구분·지번본번·지번부번·지번목록: 같은 건물(도로명코드+건물번호)의 RAW 행 전체. 한 건물이 여러 필지에
  걸치면 지번을 모두 같은 순서로 ', '로 잇는다(확정 근거 지번이 맨 앞).
- 행정구역분류코드·법정동코드·도로명코드: 파이프라인이 확정한 표준주소의 코드(없으면 RAW 행 값)
- 건물동명·건물층명·건물호명·층구분코드: 원문에서 읽은 상세주소(동·층·호)
확정되지 않은 행(보류·불일치·제외·실패)은 모두 빈 값이다.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

# 도로명주소를 확정한 행의 한 줄 주소. 아래 개별 컬럼을 이 순서로 잇는다.
# 시도 시군구 읍면동 도로명 건물번호 건물명 동 층 호  예) 경기도 양주시 은현면 은현로312번길 158 ○○빌라 101동 2층 203호
COMBINED_COLUMN = "도로명주소"
OUTPUT_COLUMNS = (
    "법정동시도명", "법정동시군구명", "법정동읍면동명", "법정동리명",
    "행정구역분류코드", "법정동코드", "도로명코드",
    "대지구분코드", "지번본번", "지번부번", "지번목록",
    "건물명", "건물동명", "건물층명", "건물호명", "층구분코드",
    "도로명", "지하구분코드", "건물본번", "건물부번",
)
_RAW_COLUMNS = (
    "법정동시도명", "법정동시군구명", "법정동읍면동명", "법정동리명", "행정구역분류코드", "법정동코드", "도로명코드",
    "대지구분코드", "지번본번", "지번부번", "건물명", "시군구관리건물명", "도로명", "건물본번", "건물부번",
)
_CONFIRMED = {"resolved", "address_only", "lot_only"}
_CODE_KEYS = {"행정구역분류코드": "administrative_area_code", "법정동코드": "legal_dong_code", "도로명코드": "road_name_code"}

RawLookup = Callable[[set[tuple[int, int]]], dict[tuple[int, int], dict]]
BuildingKey = tuple[int, int, int, int]  # (연도, 도로명코드, 건물본번, 건물부번)
BuildingLookup = Callable[[set[BuildingKey]], dict[BuildingKey, list[dict]]]
LOT_SEPARATOR = ", "


def raw_lookup(paths: Mapping[int, Path]) -> RawLookup:
    """기준연도별 RAW_DATA에서 (연도, 일련번호) 행을 한 번에 읽는 함수를 만든다."""

    def lookup(keys: set[tuple[int, int]]) -> dict[tuple[int, int], dict]:
        import duckdb

        found = {}
        columns = ", ".join(f'"{name}"' for name in _RAW_COLUMNS)
        with duckdb.connect() as db:
            for year in sorted({year for year, _ in keys}):
                serials = sorted(serial for y, serial in keys if y == year)
                if year not in paths or not serials:
                    continue
                rows = db.execute(
                    f"SELECT CAST(일련번호 AS BIGINT), {columns} FROM read_parquet(?) "
                    "WHERE CAST(일련번호 AS BIGINT) IN (SELECT unnest(?::BIGINT[]))",
                    [str(paths[year]), serials],
                ).fetchall()
                for serial, *values in rows:
                    found[(year, int(serial))] = {**dict(zip(_RAW_COLUMNS, values, strict=True)), "_year": year}
        return found

    return lookup


def building_lookup(paths: Mapping[int, Path]) -> BuildingLookup:
    """같은 건물(도로명코드+건물본번+건물부번)의 RAW 행을 모두 읽는 함수를 만든다. 지번 목록용."""

    def lookup(keys: set[BuildingKey]) -> dict[BuildingKey, list[dict]]:
        import duckdb
        from pandas import DataFrame

        found: dict[BuildingKey, list[dict]] = {key: [] for key in keys}
        with duckdb.connect() as db:
            for year in sorted({key[0] for key in keys}):
                wanted = [key[1:] for key in keys if key[0] == year]
                if year not in paths or not wanted:
                    continue
                db.register("wanted", DataFrame(wanted, columns=["road", "main", "sub"]))
                rows = db.execute(
                    "SELECT w.road, w.main, w.sub, r.법정동리명, r.법정동읍면동명, r.대지구분코드, r.지번본번, r.지번부번 "
                    "FROM read_parquet(?) r JOIN wanted w ON CAST(r.도로명코드 AS BIGINT) = w.road "
                    "AND TRY_CAST(r.건물본번 AS INTEGER) = w.main AND TRY_CAST(r.건물부번 AS INTEGER) = w.sub "
                    "ORDER BY CAST(r.일련번호 AS BIGINT)",
                    [str(paths[year])],
                ).fetchall()
                db.unregister("wanted")
                for road, main, sub, ri, dong, land, lot_main, lot_sub in rows:
                    found[(year, int(road), int(main), int(sub))].append({
                        "법정동리명": ri, "법정동읍면동명": dong, "대지구분코드": land,
                        "지번본번": lot_main, "지번부번": lot_sub,
                    })
        return found

    return lookup


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


def _number(value) -> str:
    text = _text(value)
    return str(int(text)) if text.isdecimal() else text


def _approved_candidate(row: dict) -> dict | None:
    approval = row.get("approval") or {}
    if approval.get("status") is None:
        return None
    for candidate in (row.get("review") or {}).get("candidates") or []:
        if candidate.get("address") == approval.get("candidate_address"):
            return candidate
    return None


def _source(row: dict, year: int) -> tuple[dict, list[tuple[int, int]], str] | None:
    """(표준 코드, RAW 행 키, 확정 지번)을 고른다. 확정되지 않은 행이면 None."""
    review = row.get("review") or {}
    candidate = _approved_candidate(row)
    if candidate is not None:
        return ((row.get("approval") or {}).get("standard_address") or {},
                [tuple(key) for key in candidate.get("raw_records") or []], "")
    if row.get("status") not in _CONFIRMED:
        return None
    standardization = row.get("standardization") or {}
    if standardization.get("status") == "resolved" and standardization.get("raw_record_ids"):
        keys = [(year, int(serial)) for serial in standardization["raw_record_ids"] if str(serial).isdecimal()]
        return standardization.get("address") or {}, keys, review.get("confirmed_lot") or ""
    candidates = review.get("candidates") or []
    keys = [tuple(key) for key in candidates[0].get("raw_records") or []] if candidates else []
    return review.get("standard_address") or {}, keys, review.get("confirmed_lot") or ""


def _lot_text(raw: dict) -> str:
    main, sub = _number(raw.get("지번본번")), _number(raw.get("지번부번"))
    mount = "산 " if _text(raw.get("대지구분코드")) == "1" else ""
    return f"{mount}{main}-{sub}" if sub not in {"", "0"} else f"{mount}{main}"


def _units(row: dict) -> dict[str, str]:
    """상세주소 '101동 지하 2층 203호'를 동·층·호 숫자와 층구분(지상/지하)으로 나눈다."""
    values = {"건물동명": "", "건물층명": "", "건물호명": "", "층구분코드": ""}
    for unit in (row.get("review") or {}).get("detail_units") or []:
        match = re.fullmatch(r"(지하\s*)?([0-9]+)\s*(동|층|호)", str(unit).strip())
        if not match:
            continue
        underground, number, label = match.groups()
        column = {"동": "건물동명", "층": "건물층명", "호": "건물호명"}[label]
        values[column] = str(int(number))
        if label == "층":
            values["층구분코드"] = "1" if underground else "0"
    return values


def combined_road_address(fields: Mapping[str, str]) -> str:
    """개별 컬럼을 '시도 시군구 읍면동 도로명 건물번호 건물명 동 층 호'로 잇는다. 도로명이 없으면 빈 값."""
    if not fields.get("도로명") or not fields.get("건물본번"):
        return ""
    number = fields["건물본번"] + (f"-{fields['건물부번']}" if fields.get("건물부번") not in {"", "0"} else "")
    floor = fields.get("건물층명")
    if floor:
        floor = f"지하 {floor}층" if fields.get("층구분코드") == "1" else f"{floor}층"
    parts = (
        fields.get("법정동시도명"), fields.get("법정동시군구명"), fields.get("법정동읍면동명"),
        fields["도로명"], number, fields.get("건물명"),
        f"{fields['건물동명']}동" if fields.get("건물동명") else "", floor,
        f"{fields['건물호명']}호" if fields.get("건물호명") else "",
    )
    return " ".join(part for part in parts if part)


def _primary(row: dict, raw_rows: Mapping[tuple[int, int], dict], year: int):
    """(표준 코드, 확정 근거 RAW 행, 도로명주소 확정 여부). 확정되지 않았거나 RAW 행이 없으면 None."""
    source = _source(row, year)
    if source is None:
        return None
    standard, keys, confirmed_lot = source
    rows = [raw_rows[key] for key in keys if key in raw_rows]
    if not rows:
        return None
    # 확정 근거 RAW 행이 여럿이면 확정 지번과 같은 행을, 없으면 첫 행을 대표로 쓴다.
    raw = next((item for item in rows if confirmed_lot.endswith(" " + _lot_text(item))), rows[0])
    road_confirmed = _approved_candidate(row) is not None or (
        bool(standard.get("standard_road_address")) and row.get("status") != "lot_only")
    return standard, raw, road_confirmed


def _building_key(raw: dict) -> BuildingKey | None:
    try:
        return (int(raw["_year"]), int(float(raw["도로명코드"])), int(raw["건물본번"]), int(raw["건물부번"]))
    except (KeyError, TypeError, ValueError):
        return None


def _lot_fields(lots: list[dict]) -> dict[str, str]:
    """지번 여러 개를 같은 순서로 이어 대지구분·본번·부번·지번목록 컬럼을 만든다."""
    lots = list({(_text(lot.get("대지구분코드")), _number(lot.get("지번본번")), _number(lot.get("지번부번")),
                  _text(lot.get("법정동리명")) or _text(lot.get("법정동읍면동명"))): lot for lot in lots}.values())
    # RAW_DATA는 0(대지)·1(산)이고 제출 양식은 1(대지)·2(산)·3(특수대지)이다.
    land = [{"0": "1", "1": "2"}.get(_text(lot.get("대지구분코드")), "") for lot in lots]
    names = [" ".join(filter(None, (_text(lot.get("법정동리명")) or _text(lot.get("법정동읍면동명")), _lot_text(lot))))
             for lot in lots]
    return {
        "대지구분코드": LOT_SEPARATOR.join(land),
        "지번본번": LOT_SEPARATOR.join(_number(lot.get("지번본번")) for lot in lots),
        "지번부번": LOT_SEPARATOR.join(_number(lot.get("지번부번")) for lot in lots),
        "지번목록": LOT_SEPARATOR.join(names),
    }


def output_fields(row: dict, raw_rows: Mapping[tuple[int, int], dict], year: int,
                  building_rows: Mapping[BuildingKey, list[dict]] | None = None) -> dict[str, str]:
    fields = {COMBINED_COLUMN: "", **dict.fromkeys(OUTPUT_COLUMNS, "")}
    primary = _primary(row, raw_rows, year)
    if primary is None:
        return fields
    standard, raw, road_confirmed = primary
    lots = [raw]
    if road_confirmed and building_rows and (key := _building_key(raw)) in building_rows:
        lots += building_rows[key]  # 확정 근거 지번을 맨 앞에 두고 같은 건물의 다른 지번을 잇는다.
    fields.update({
        "법정동시도명": _text(raw.get("법정동시도명")), "법정동시군구명": _text(raw.get("법정동시군구명")),
        "법정동읍면동명": _text(raw.get("법정동읍면동명")), "법정동리명": _text(raw.get("법정동리명")),
        **_lot_fields(lots),
    })
    for column, key in _CODE_KEYS.items():
        fields[column] = _text(standard.get(key)) if standard.get(key) is not None else _text(raw.get(column))
    if road_confirmed:
        fields.update({
            "건물명": _text(raw.get("시군구관리건물명")) or _text(raw.get("건물명")),
            "도로명": _text(raw.get("도로명")),
            # RAW_DATA에 지하 여부가 없어 기준자료로 확정한 건물은 지상(0)으로 둔다.
            "지하구분코드": "0",
            "건물본번": _number(raw.get("건물본번")), "건물부번": _number(raw.get("건물부번")),
            **_units(row),
        })
        fields[COMBINED_COLUMN] = combined_road_address(fields)
    else:
        fields["도로명코드"] = ""
    return fields


def annotate(rows: Iterable[dict], lookup: RawLookup, year: int, buildings: BuildingLookup | None = None) -> None:
    """결과 행마다 `output_fields`를 붙인다. RAW 조회는 모든 행을 모아 한 번에 한다."""
    rows = list(rows)
    keys = set()
    for row in rows:
        source = _source(row, year)
        if source is not None:
            keys.update((int(y), int(serial)) for y, serial in source[1])
    raw_rows = lookup(keys) if keys else {}
    building_keys = set()
    if buildings is not None:
        for row in rows:
            primary = _primary(row, raw_rows, year)
            if primary is not None and primary[2] and (key := _building_key(primary[1])) is not None:
                building_keys.add(key)
    building_rows = buildings(building_keys) if building_keys else {}
    for row in rows:
        row["output_fields"] = output_fields(row, raw_rows, year, building_rows)
