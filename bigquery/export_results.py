"""워크벤치 결과 JSON을 BigQuery 업로드용 CSV(주소당 한 행)로 바꾼다.

    python bigquery/export_results.py "pipeline-address-results-XXXX.json" bigquery/out/results_XXXX.csv

CSV는 BigQuery 콘솔의 '테이블 만들기 → 업로드'로 address.pipeline_results에 추가한다.
스키마는 bigquery/schema_pipeline_results.json을 쓴다.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

COLUMNS = [
    "run_id", "loaded_at", "reference_year", "address_id", "input_index", "raw_address",
    "status", "excluded_reason", "failed_reason", "review_type", "base_reason", "review_status", "tier",
    "candidate_count", "top_candidate", "confirmed_lot", "sido", "sigungu",
    "standard_road_address", "road_name_code", "legal_dong_code", "administrative_area_code",
]


def _region(row: dict) -> tuple[str, str]:
    validation = row.get("region_validation") or {}
    paths = validation.get("paths") or []
    if len(paths) != 1:
        return "", ""
    names = {region["label"]: region["name"] for region in paths[0]}
    return names.get("시도", ""), names.get("시군구", "")


def convert(row: dict, run_id: str, year: str, loaded_at: str) -> dict:
    output = row.get("output") or {}
    review = row.get("review") or {}
    candidates = review.get("candidates") or []
    top = [c for c in candidates if c.get("rank") == 1]
    address = ((row.get("standardization") or {}).get("address")
               or review.get("standard_address") or {})
    failed = next((t.get("reason_code") for t in reversed(row.get("traces") or [])
                   if t.get("reason_code")), "") if row["status"] == "failed" else ""
    sido, sigungu = _region(row)
    return {
        "run_id": run_id,
        "loaded_at": loaded_at,
        "reference_year": year,
        "address_id": row["record"]["address_id"],
        "input_index": row["input_index"],
        "raw_address": row["record"].get("raw_address") or "",
        "status": row["status"],
        "excluded_reason": output.get("reason_code") or "" if row["status"] == "excluded" else "",
        "failed_reason": failed,
        "review_type": review.get("review_type") or "",
        "base_reason": review.get("base_reason") or (output.get("reason_code") or "" if row["status"] == "held" else ""),
        "review_status": review.get("status") or "",
        "tier": review.get("tier") or "",
        "candidate_count": len(candidates),
        "top_candidate": top[0]["address"] if len(top) == 1 else "",
        "confirmed_lot": review.get("confirmed_lot") or "",
        "sido": sido,
        "sigungu": sigungu,
        "standard_road_address": address.get("standard_road_address") or "",
        "road_name_code": address.get("road_name_code") or "",
        "legal_dong_code": address.get("legal_dong_code") or "",
        "administrative_area_code": address.get("administrative_area_code") or "",
    }


def main(source: str, target: str) -> None:
    data = json.loads(Path(source).read_text(encoding="utf-8"))["dataset"]
    loaded_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, COLUMNS)
        writer.writeheader()
        for row in data["results"]:
            writer.writerow(convert(row, data["run_id"], data["reference_version"], loaded_at))
    print(f"{len(data['results'])}행 → {target}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
