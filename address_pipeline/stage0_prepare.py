"""0단계 입력·정리 (규칙 A1~A8). 원문은 바꾸지 않고 정리한 사본으로 다음 단계를 돈다.

- A1 옛 행정구역 이름 → 지금 이름 (연기군 → 세종 등, address_pipeline/old_regions.py)
- A2 영문 시도 → 한글, A3 시도 정식 이름과 한 글자만 다른 표기 고치기
- A4 바로 이어 반복된 지역 이름, A5 반복된 낱말 묶음 하나로
- A6 한 칸의 복수 주소를 조각으로 나누기 (줄바꿈·'우편물·본점·주소:'·시도/같은 시군 재등장)
- A7 어순 정리, A8 NFKC·HTML 표기
조각이 둘 이상인 원문은 조각마다 따로 매칭하고, 3단계에서 '기타·복수 주소/필지'로 모은다.
"""
from __future__ import annotations

from .rules import prepare
from .old_regions import rename_old_regions

from .state import Run


def run(r: Run) -> dict:
    r.segments = {record.address_id: prepare(record.raw_address) for record in r.records}
    rows = []
    for i, record in enumerate(r.records):
        parts = r.segments[record.address_id]
        keyed = [(f"{record.address_id}#{k}", part) for k, part in enumerate(parts, start=1)] if len(parts) > 1 \
            else [(record.address_id, parts[0] if parts else (record.raw_address or ""))]
        rows += [{"batch": r.batch, "address_id": key, "road_address": text, "lot_address": "", "source_row": i + 1}
                 for key, text in keyed]
    r.rows = rows
    r.texts = {record.address_id: parts[0] for record in r.records if len(parts := r.segments[record.address_id]) == 1}
    r.sources.append_rows("input_raw", rows)
    return {
        "inputs": len(r.records), "uploaded_rows": len(rows),
        "multi_address_inputs": sum(1 for parts in r.segments.values() if len(parts) > 1),
        "old_region_renamed": sum(1 for record in r.records if rename_old_regions(record.raw_address or "")[1]),
    }
