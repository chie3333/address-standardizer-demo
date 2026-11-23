"""기존 파이프라인 결과에 BigQuery 단계를 덧붙인다.

파이프라인(LLM 파서 → DuckDB 검증)을 그대로 돌린 뒤, 같은 입력을 BigQuery SQL로 한 번 더 표준화해
두 결과를 행마다 나란히 놓는다. 파이프라인이 확정하지 못한 행은 BigQuery 결과를 보조 판정으로 쓴다.

    python -m address_workbench.bigquery_stage \\
        --input "(2025.12월 기준)농림어업DB AI 주소표준화 대상건(15988건).xlsx" \\
        --results pipeline-address-results-XXXX.json \\
        --batch 202512 --out bigquery/out/merged_202512.csv

순서
  1. 원본 엑셀(도로명·지번 두 열)을 address.input_raw에 올린다. 같은 batch의 기존 행은 지운다.
  2. 워크벤치 결과 JSON을 주소당 한 행으로 바꿔 address.pipeline_results에 올린다. 같은 run_id는 지운다.
  3. bigquery/sql/04(원문 분석 함수), 05(전체 매칭)를 실행해 address.sql_standardized를 만든다.
  4. 두 결과를 일련번호로 합쳐 최종 판정·판정 출처·사유를 붙인 CSV를 쓴다.

필요한 것: `pip install -r address_workbench/requirements-bigquery.txt`,
`gcloud auth application-default login`. 기준표(raw_2025_clean, ref_road, ref_lot_keys)는
bigquery/sql/03을 한 번 실행해 둔 상태여야 한다(--rebuild-reference로 다시 만들 수 있다).

환경 변수
    ADDRESS_BQ_PROJECT   GCP 프로젝트 (기본 your-gcp-project)
    ADDRESS_BQ_DATASET   데이터세트 (기본 address)
    ADDRESS_BQ_LOCATION  위치 (기본 asia-northeast3)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "bigquery" / "sql"
# SQL 파일에 적힌 프로젝트·데이터세트 이름. 실행할 때 설정값으로 바꾼다.
SQL_NAMESPACE = "your-gcp-project.address"

INPUT_HEADER = ("일련번호", "STDIZBFRADDR (도로명주소)", "STADDADDR (지번주소)")
CONFIRMED = {"resolved", "address_only"}
# 파이프라인이 정책으로 뺀 행. BigQuery가 찾아도 최종 판정은 제외로 둔다.
POLICY_EXCLUDED = {"po_box", "numeric_only"}

MERGED_COLUMNS = (
    "일련번호", "입력_도로명주소", "입력_지번주소",
    "최종_판정", "최종_도로명주소", "최종_지번주소", "도로명코드", "법정동코드", "행정구역분류코드",
    "판정_출처", "판정_사유",
    "파이프라인_판정", "파이프라인_도로명주소", "BigQuery_판정", "BigQuery_도로명주소", "BigQuery_사유",
    "두_방식_일치",
)


@dataclass(frozen=True)
class Settings:
    project: str = "your-gcp-project"
    dataset: str = "address"
    location: str = "asia-northeast3"

    @classmethod
    def from_env(cls) -> "Settings":
        default = cls()
        return cls(
            os.environ.get("ADDRESS_BQ_PROJECT", default.project),
            os.environ.get("ADDRESS_BQ_DATASET", default.dataset),
            os.environ.get("ADDRESS_BQ_LOCATION", default.location),
        )

    def table(self, name: str) -> str:
        return f"{self.project}.{self.dataset}.{name}"

    def sql(self, name: str) -> str:
        return (SQL_DIR / name).read_text(encoding="utf-8").replace(SQL_NAMESPACE, f"{self.project}.{self.dataset}")


def _text(value) -> str:
    return "" if value is None else str(value)


# ---------------------------------------------------------------- 입력과 결과 변환 (BigQuery 없이 동작)

def input_rows(path: str | Path, batch: str, sheet: str = "Data") -> list[dict]:
    """원본 엑셀을 input_raw 행으로 바꾼다. 원문은 손대지 않는다."""
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True)
    try:
        rows = workbook[sheet].iter_rows(values_only=True)
        header = tuple(next(rows)[:3])
        if header != INPUT_HEADER:
            raise ValueError(f"머리글이 예상과 다릅니다: {header}")
        return input_rows_from_values(rows, batch)
    finally:
        workbook.close()


def input_rows_from_values(rows: Iterable[tuple], batch: str, first_line: int = 2) -> list[dict]:
    seen: set[str] = set()
    out = []
    for line, (serial, road, lot, *_) in enumerate(rows, start=first_line):
        if serial is None and road is None and lot is None:
            continue
        address_id = _text(serial)
        if not address_id or address_id in seen:
            raise ValueError(f"{line}행: 일련번호가 비었거나 중복입니다 ({address_id!r}).")
        seen.add(address_id)
        out.append({"batch": batch, "address_id": address_id, "road_address": _text(road),
                    "lot_address": _text(lot), "source_row": line})
    return out


def _region(row: Mapping) -> tuple[str, str]:
    paths = (row.get("region_validation") or {}).get("paths") or []
    if len(paths) != 1:
        return "", ""
    names = {region["label"]: region["name"] for region in paths[0]}
    return names.get("시도", ""), names.get("시군구", "")


def result_row(row: Mapping, run_id: str, year: str, loaded_at: str) -> dict:
    """워크벤치 결과 JSON의 한 행을 pipeline_results 행으로 바꾼다."""
    output = row.get("output") or {}
    review = row.get("review") or {}
    candidates = review.get("candidates") or []
    top = [c for c in candidates if c.get("rank") == 1]
    address = (row.get("standardization") or {}).get("address") or review.get("standard_address") or {}
    status = row["status"]
    failed = next((t.get("reason_code") for t in reversed(row.get("traces") or []) if t.get("reason_code")), "") \
        if status == "failed" else ""
    sido, sigungu = _region(row)
    return {
        "run_id": run_id, "loaded_at": loaded_at, "reference_year": year,
        "address_id": row["record"]["address_id"], "input_index": row["input_index"],
        "raw_address": row["record"].get("raw_address") or "", "status": status,
        "excluded_reason": (output.get("reason_code") or "") if status == "excluded" else "",
        "failed_reason": failed,
        "review_type": review.get("review_type") or "",
        "base_reason": review.get("base_reason") or ((output.get("reason_code") or "") if status == "held" else ""),
        "review_status": review.get("status") or "", "tier": review.get("tier") or "",
        "candidate_count": len(candidates),
        "top_candidate": top[0]["address"] if len(top) == 1 else "",
        "confirmed_lot": review.get("confirmed_lot") or "",
        "sido": sido, "sigungu": sigungu,
        "standard_road_address": address.get("standard_road_address") or "",
        "road_name_code": address.get("road_name_code") or "",
        "legal_dong_code": address.get("legal_dong_code") or "",
        "administrative_area_code": address.get("administrative_area_code") or "",
    }


def result_rows(dataset: Mapping, loaded_at: str | None = None) -> list[dict]:
    loaded_at = loaded_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return [result_row(row, dataset["run_id"], dataset["reference_version"], loaded_at)
            for row in dataset["results"]]


def load_results_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))["dataset"]


# ---------------------------------------------------------------- 두 결과 합치기 (BigQuery 없이 동작)

def merge_row(source: Mapping, pipeline: Mapping | None, sql: Mapping | None) -> dict:
    """입력 한 행에 대해 파이프라인과 BigQuery 결과를 합쳐 최종 판정을 정한다.

    - 파이프라인 확정은 그대로 쓴다. BigQuery가 다른 주소로 확정했으면 '검토 필요'로 내린다.
    - 파이프라인이 정책으로 뺀 행(사서함, 숫자만)은 제외로 둔다.
    - 그 밖에 파이프라인이 확정하지 못한 행은 BigQuery 확정·지번만 확정을 보조 판정으로 쓴다.
    """
    pipeline = pipeline or {}
    sql = sql or {}
    p_status = pipeline.get("status", "")
    p_road = pipeline.get("standard_road_address", "") or ""
    s_status = sql.get("status", "") or ""
    s_road = sql.get("standard_road_address", "") or ""
    agree = ""
    if p_road and s_road:
        agree = "일치" if p_road == s_road else "불일치"

    def codes(row: Mapping) -> tuple[str, str, str]:
        return tuple(row.get(k, "") or "" for k in ("road_name_code", "legal_dong_code", "administrative_area_code"))

    final_lot = ""
    if p_status in CONFIRMED and agree == "불일치" and s_status == "확정":
        final, road, code, origin = "검토 필요", "", ("", "", ""), "두 방식 불일치"
        reason = f"파이프라인 {p_road} / BigQuery {s_road}"
    elif p_status in CONFIRMED:
        final, road, code, origin = "확정", p_road, codes(pipeline), "파이프라인"
        reason = "파이프라인 확정" + (" · BigQuery도 같은 주소" if agree == "일치" else "")
    elif p_status == "excluded" and pipeline.get("excluded_reason") in POLICY_EXCLUDED:
        final, road, code, origin = "제외", "", ("", "", ""), "파이프라인 정책"
        reason = f"파이프라인 제외({pipeline.get('excluded_reason')})"
    elif p_status == "lot_only":
        final, road, code, origin = "지번만 확정", "", ("", "", ""), "파이프라인"
        final_lot = pipeline.get("confirmed_lot", "") or ""
        reason = "파이프라인 지번 확정"
    elif s_status == "확정":
        final, road, code, origin = "확정", s_road, codes(sql), "BigQuery 보조"
        reason = sql.get("reason", "")
    elif s_status == "지번만 확정":
        final, road, code, origin = "지번만 확정", "", ("", "", ""), "BigQuery 보조"
        final_lot = sql.get("standard_lot_address", "") or ""
        reason = sql.get("reason", "")
    elif s_status == "입력 없음":
        final, road, code, origin = "입력 없음", "", ("", "", ""), "BigQuery"
        reason = sql.get("reason", "")
    elif s_status == "검토 필요" or p_status == "held":
        final, road, code, origin = "검토 필요", "", ("", "", ""), "BigQuery" if s_status == "검토 필요" else "파이프라인"
        reason = sql.get("reason", "") if s_status == "검토 필요" else (pipeline.get("review_type") or pipeline.get("base_reason", ""))
    else:
        final, road, code, origin = "미일치", "", ("", "", ""), "BigQuery" if sql else "파이프라인"
        reason = sql.get("reason", "") or pipeline.get("base_reason", "") or pipeline.get("failed_reason", "")

    if not final_lot and final == "확정" and origin == "BigQuery 보조":
        final_lot = sql.get("standard_lot_address", "") or ""
    return dict(zip(MERGED_COLUMNS, (
        source.get("address_id", ""), source.get("road_address", ""), source.get("lot_address", ""),
        final, road, final_lot, *code, origin, reason,
        p_status, p_road, s_status, s_road, sql.get("reason", "") or "", agree,
    ), strict=True))


def merge(inputs: list[Mapping], pipeline: Mapping[str, Mapping], sql: Mapping[str, Mapping]) -> list[dict]:
    return [merge_row(row, pipeline.get(row["address_id"]), sql.get(row["address_id"])) for row in inputs]


def write_csv(rows: list[dict], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, MERGED_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------- BigQuery 실행

class BigQueryStage:
    def __init__(self, settings: Settings | None = None, client=None):
        self.settings = settings or Settings.from_env()
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from google.cloud import bigquery

            self._client = bigquery.Client(project=self.settings.project, location=self.settings.location)
        return self._client

    def query(self, sql: str, params: Mapping[str, str] | None = None,
              array: Mapping[str, tuple[str, list]] | None = None):
        """params는 문자열 파라미터, array는 {이름: (타입, 값 목록)} 배열 파라미터."""
        from google.cloud import bigquery

        config = bigquery.QueryJobConfig(query_parameters=[
            *(bigquery.ScalarQueryParameter(name, "STRING", value) for name, value in (params or {}).items()),
            *(bigquery.ArrayQueryParameter(name, kind, values) for name, (kind, values) in (array or {}).items()),
        ])
        return self.client.query(sql, job_config=config, location=self.settings.location).result()

    def run_sql_file(self, name: str, params: Mapping[str, str] | None = None):
        return self.query(self.settings.sql(name), params)

    def replace_rows(self, table: str, key: str, value: str, rows: list[dict]) -> int:
        """같은 key 값의 기존 행을 지우고 rows를 올린다. 다시 실행해도 행이 겹치지 않는다."""
        target = self.settings.table(table)
        self.query(f"DELETE FROM `{target}` WHERE {key} = @value", {"value": value})
        return self.append_rows(table, rows)

    def append_rows(self, table: str, rows: list[dict]) -> int:
        from google.cloud import bigquery

        target = self.settings.table(table)
        config = bigquery.LoadJobConfig(
            schema=self.client.get_table(target).schema,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        )
        self.client.load_table_from_json(rows, target, job_config=config, location=self.settings.location).result()
        return len(rows)

    def fetch(self, table: str, key: str, value: str | None = None) -> dict[str, dict]:
        where = f"WHERE {key} = @value" if value is not None else ""
        rows = self.query(f"SELECT * FROM `{self.settings.table(table)}` {where}",
                          {"value": value} if value is not None else None)
        return {row["address_id"]: dict(row.items()) for row in rows}

    def run(self, input_path: str | Path, results_path: str | Path | None, batch: str, out: str | Path,
            *, sheet: str = "Data", rebuild_reference: bool = False) -> dict:
        self.run_sql_file("00_tables.sql")
        if rebuild_reference:
            self.run_sql_file("03_reference_tables.sql")
        inputs = input_rows(input_path, batch, sheet)
        self.replace_rows("input_raw", "batch", batch, inputs)

        pipeline: dict[str, dict] = {}
        run_id = None
        if results_path:
            dataset = load_results_json(results_path)
            run_id = dataset["run_id"]
            self.replace_rows("pipeline_results", "run_id", run_id, result_rows(dataset))
            pipeline = self.fetch("pipeline_results", "run_id", run_id)

        self.run_sql_file("04_parse_function.sql")
        self.run_sql_file("05_standardize_all.sql", {"batch": batch})
        sql = self.fetch("sql_standardized", "address_id")
        merged = merge(inputs, pipeline, sql)
        write_csv(merged, out)
        return summarize(merged) | {"run_id": run_id, "rows": len(merged), "out": str(out)}


def summarize(rows: list[dict]) -> dict:
    from collections import Counter

    return {
        "최종_판정": dict(Counter(row["최종_판정"] for row in rows).most_common()),
        "판정_출처": dict(Counter(row["판정_출처"] for row in rows).most_common()),
        "두_방식_일치": dict(Counter(row["두_방식_일치"] for row in rows if row["두_방식_일치"]).most_common()),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="파이프라인 결과에 BigQuery 표준화를 덧붙여 합친 CSV를 만든다.")
    parser.add_argument("--input", required=True, help="원본 엑셀(일련번호·도로명주소·지번주소)")
    parser.add_argument("--results", help="워크벤치 결과 JSON. 없으면 BigQuery 결과만 쓴다.")
    parser.add_argument("--batch", required=True, help="입력 묶음 이름, 예: 202512")
    parser.add_argument("--out", required=True, help="합친 결과 CSV 경로")
    parser.add_argument("--sheet", default="Data")
    parser.add_argument("--rebuild-reference", action="store_true", help="raw_2025로 기준표를 다시 만든다(약 1분)")
    args = parser.parse_args(argv)
    summary = BigQueryStage().run(args.input, args.results, args.batch, args.out,
                                  sheet=args.sheet, rebuild_reference=args.rebuild_reference)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
