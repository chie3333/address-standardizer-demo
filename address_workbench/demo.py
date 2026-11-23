"""Scripted synthetic scenarios. This adapter never queries reference data."""
from collections import Counter, defaultdict
from dataclasses import asdict

from .adapter import AdapterResult, InputRecord

SAMPLE_ADDRESSES = (
    "가온시 서림구 은하로 152-2",
    "가온시 서림구 은하로 150",
    "한별시 새빛구 달빛길 24",
    "가온시 서림구 은하로 999",
    "한별시 새빛구 새벽길 70",
    "가온시 서림구 다온동 11",
    "한별시 새빛구 달빛길 24",
    "",
    "가온시 서림구 숲길 37",
    "한별시 새빛구 별빛로 7",
)

PARTS = {
    0: ("가온시", "서림구", "은하로", "152"),
    1: ("가온시", "서림구", "은하로", "150"),
    2: ("한별시", "새빛구", "달빛길", "24"),
    3: ("가온시", "서림구", "은하로", "999"),
    4: ("한별시", "새빛구", "새벽길", "70"),
    5: ("가온시", "서림구", "다온동", "11"),
    8: ("가온시", "서림구", "숲길", "37"),
    9: ("한별시", "새빛구", "별빛로", "7"),
}


def trace(name, status="completed", reason=None, **details):
    return {
        "status": status, "reason_code": reason, "details": details,
        "module_name": name, "module_version": "demo", "elapsed_ms": 0,
    }


def standard(address, road="000000000001", dong="0000000002", area="000003"):
    return {
        "standard_road_address": address, "road_name_code": road,
        "legal_dong_code": dong, "administrative_area_code": area,
    }


def candidate(address, serial, evidence, *, year=2025, rank=1):
    return {
        "kind": "road", "address": address, "rank": rank, "evidence": [evidence],
        "reference_years": [year], "raw_records": [[year, serial]], "linked_road_addresses": [],
    }


class DemoAdapter:
    reference_version = "2025"

    def run_dataset(self, records: tuple[InputRecord, ...], *, run_id: str) -> AdapterResult:
        rows, comparisons, seen = [], {}, {}
        for index, record in enumerate(records):
            raw = (record.raw_address or "").strip()
            row = {
                "record": {**asdict(record), "normalized_address": None},
                "input_index": index, "status": "skipped", "traces": [],
                "output": None, "standardization": None, "region_validation": None, "review": None,
            }
            rows.append(row)
            reason = "blank_input" if not raw else "duplicate_input" if raw in seen else None
            if reason:
                representative = seen.get(raw)
                row.update(status="excluded", output={
                    "status": "excluded", "reason_code": reason, "normalized_address": None,
                    "representative_address_id": representative, "review_tag": None,
                }, traces=[trace("input_preparation", reason=reason, representative_address_id=representative)])
                continue
            if raw not in SAMPLE_ADDRESSES:
                row["traces"] = [trace("demo_lookup", "skipped", "demo_no_fixture")]
                continue
            seen[raw] = record.address_id
            scenario = SAMPLE_ADDRESSES.index(raw)
            self.fill(row, raw, scenario)
            if scenario == 0:
                comparisons[record.address_id] = {
                    position: [
                        {"component": "도로명", "input_value": "은하로", "candidate_value": "은하로",
                         "outcome": "match", "evidence": "시연 데이터의 도로명 조건 일치"},
                        {"component": "건물본번", "input_value": "152", "candidate_value": "152",
                         "outcome": "match", "evidence": "시연 데이터의 본번 조건 일치"},
                        {"component": "건물부번", "input_value": "2", "candidate_value": value,
                         "outcome": "mismatch", "evidence": "시연 데이터에서 명시 부번과 다른 값"},
                    ]
                    for position, value in enumerate(("1", "3"))
                }
        review_summary = defaultdict(Counter)
        for row in rows:
            if review := row["review"]:
                review_summary[review["review_type"]][review["status"]] += 1
        summary = Counter(row["status"] for row in rows)
        batch_rows = [{**row, "input_index": index} for index, row in enumerate(
            row for row in rows if row["status"] not in {"excluded", "skipped"}
        )]
        batches = [{
            "run_id": f"{run_id}:1", "reference_version": self.reference_version,
            "results": batch_rows, "summary": Counter(row["status"] for row in batch_rows), "parsed_df": [],
            "search_traces": [trace("region_validation", input_count=len(batch_rows))],
            "review_summary": review_summary,
        }] if batch_rows else []
        return AdapterResult({
            "run_id": run_id, "reference_version": self.reference_version,
            "results": rows, "summary": summary, "batch_results": batches, "review_summary": review_summary,
        }, comparisons)

    def fill(self, row, raw, scenario):
        labels = ("시도", "시군구", "읍면동" if scenario == 5 else "도로명")
        *names, number = PARTS[scenario]
        row["record"]["normalized_address"] = raw
        row["output"] = {
            "screening_result": "passed", "address_candidates": [], "leading_name_candidates": [],
            "tokens": [{"raw": name, "labels": [label]} for name, label in zip(names, labels)],
            "unresolved_segments": [raw.rsplit(" ", 1)[1]],
        }
        row["traces"] = [trace("input_preparation"), trace("preprocessing"), trace("parser", token_count=3)]
        if scenario == 8:
            row.update(status="failed", output=None)
            row["traces"][-1] = trace("parser", "failed", "demo_parser_error")
            return

        row["region_validation"] = {
            "address_id": row["record"]["address_id"], "reference_year": 2025, "status": "unique_path",
            "paths": [[{"label": label, "name": name, "node_id": f"DEMO-{scenario}-{i}", "code_prefix": None}
                       for i, (label, name) in enumerate(zip(labels, names))]],
            "db_labels": [[label] for label in labels],
        }
        row["traces"].append(trace("region_validation", path_count=1))
        standard_text = "가온특별시 서림구 은하로 150" if scenario == 1 else raw
        if scenario == 1:
            row["region_validation"]["paths"][0][0]["name"] = "가온특별시"
        status, kind, decision, reason, candidates = {
            0: ("held", "명시된 부번 불일치", "tied_candidates", "sub_number_mismatch", [
                candidate("가온시 서림구 은하로 152-1", 900001, "도로명·본번 일치, 명시 부번 2 ≠ 후보 부번 1"),
                candidate("가온시 서림구 은하로 152-3", 900002, "도로명·본번 일치, 명시 부번 2 ≠ 후보 부번 3"),
            ]),
            1: ("resolved", "기본 도로명주소 확인", "verified", "", [
                candidate(standard_text, 900003, "도로명·건물번호 정확 일치"),
            ]),
            2: ("resolved", "기본 도로명주소 확인", "verified", "", [
                candidate(raw, 900004, "도로명·건물번호 정확 일치"),
            ]),
            3: ("no_match", "기준연도 번호 미일치", "no_candidate", "number_not_found", []),
            4: ("no_match", "기준연도 번호 미일치", "single_candidate", "number_not_found", [
                candidate(raw, 800001, "과거 기준자료에서만 번호 일치", year=2024),
            ]),
            5: ("lot_only", "한 지번에 여러 도로명주소", "tied_candidates", "multiple_road_addresses", [
                candidate("가온시 서림구 고운로 10", 900005, "같은 지번에 연결"),
                candidate("가온시 서림구 고운로 12", 900006, "같은 지번에 연결"),
            ]),
            9: ("address_only", "기본 도로명주소 확인", "verified", "missing_codes", [
                candidate(raw, 900007, "도로명·건물번호 정확 일치"),
            ]),
        }[scenario]
        address = standard(standard_text) if status == "resolved" else standard(raw, dong="", area="") if status == "address_only" else None
        row.update(status=status, review={
            "review_type": kind, "status": decision, "base_status": status, "base_reason": reason,
            "candidates": candidates, "confirmed_lot": raw if status == "lot_only" else "",
            "standard_address": address, "detail_units": [],
        })
        row["traces"].append(trace("address_review", reason=reason or None, candidate_count=len(candidates)))
        if status == "resolved":
            row["standardization"] = {
                "status": "resolved", "address": address, "reason_code": None,
                "raw_record_ids": [str(candidates[0]["raw_records"][0][1])],
            }
