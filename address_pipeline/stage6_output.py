"""6단계 결과: 옛 행정구역 바꿈 기록을 붙이고 요약을 다시 센다.

제출 양식 19개 열(output_fields)은 워크벤치가 결과를 내보낼 때 파이프라인의 annotate_rows로 채운다.
"""
from __future__ import annotations

from . import rules as bq
from .old_regions import rename_old_regions

from .state import Run

ENGINE = "address_pipeline"


def run(r: Run) -> dict:
    renamed = {record.address_id: notes for record in r.records
               if (notes := rename_old_regions(record.raw_address or "")[1])}
    noted = bq.apply_region_notes(r.results, renamed) if renamed else 0
    for row in r.results:
        if (row.get("output") or {}).get("engine") == bq.ENGINE:
            row["output"]["engine"] = ENGINE
        for trace in row.get("traces") or []:
            if trace.get("module_name") == bq.ENGINE:
                trace["module_name"] = ENGINE
    bq.refresh_summary(r.dataset)
    return {"old_region_rows": noted}
