"""1~3단계: 원문 분석 → 정확 매칭 → 판정. SQL 한 번(sql/2_match.sql + 3_details.sql)으로 돈다.

1단계 원문 분석 (B1~B9)  parse_address_candidates: 도로명·지번 '이름 + 번호' 후보를 LLM 없이 뽑는다.
2단계 정확 매칭 (C, D1~D9) 후보 이름에 해당하는 RAW 행만 골라 범위 기준표를 만들고(sql/2_scope_reference.sql)
                         번호·지역을 정확히 맞춘다. 지역 점수, 가장 긴 이름 우선, 다른 시도·시군구·읍면 제외.
3단계 판정 (E, F, G)     확정 / 지번만 / 검토 / 미일치. 복수 주소·지역 표기 불완전은 검토로 내린다.
                         결과 상세(코드 3개, 연결 지번, 일련번호, 같은 본번의 다른 부번 주의)를 붙인다.
L1(후보마다 그 후보의 지번)도 여기서 붙인다.
"""
from __future__ import annotations

from .rules import add_parcel_evidence, build_dataset

from .sources import Sources, read_sql, statements
from .state import Run


def match_statements() -> list[str]:
    """원문 후보 뽑기 → 범위 기준표 → 정확 매칭·판정 → 결과 상세 순서의 문장 목록."""
    parse, *match = statements(read_sql("2_match.sql"))
    return [parse, *statements(read_sql("2_scope_reference.sql")), *match, read_sql("3_details.sql")]


def match_batch(sources: Sources, batch: str) -> list[dict]:
    """input_raw의 한 묶음(batch)을 매칭해 행마다 판정·후보·상세를 돌려준다. 다시 매칭할 때(4·5단계)도 쓴다."""
    return sources.query(match_statements(), {"batch": batch})


def run(r: Run) -> dict:
    found = match_batch(r.sources, r.batch)
    r.dataset, r.comparisons = build_dataset(r.records, found, run_id=r.run_id, steps=r.steps, segments=r.segments)
    parcels = add_parcel_evidence(r.dataset["results"])
    return {"matched_rows": len(found), "parcel_evidence_rows": parcels}
