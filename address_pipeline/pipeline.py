"""새 주소 표준화 파이프라인. 단계 순서는 run_dataset 한 곳에서 보인다.

    0 입력·정리         stage0_prepare        A1~A8
    1 원문 분석         stage123_match        B1~B9 (LLM 없이)
    2 정확 매칭         stage123_match        C, D  (RAW parquet에서 필요한 행만 골라 범위 기준표)
    3 판정              stage123_match        E, F, G, L1
    4 후보 도구         stage4_candidates     H 건물명 → I 폐지 → J 번호 오타 → K 붙여쓰기
    5 LLM (후보 없는 행) stage5_llm            5a 원문 구분 → 5b 교정(M)
    6 결과              stage6_output

확정은 2·3단계의 정확 일치에서만 나온다. 4·5단계는 검토 후보만 단다.
워크벤치 어댑터 계약(run_dataset, reference_version, annotate_rows)을 따른다.

환경 변수
    ADDRESS_PIPELINE_LLM      0이면 5단계를 건너뛴다(기본 켬). OpenAI 설정은 address_pipeline/.env.openai
    ADDRESS_PIPELINE_LLM_MAX  5a·5b 각각 한 번에 LLM으로 보낼 최대 행 수(기본 3000)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import rules as bq
from address_workbench.adapter import AdapterResult, InputRecord, Progress

from . import stage0_prepare, stage123_match, stage4_candidates, stage5_llm, stage6_output
from .sources import Sources, is_broken_connection
from .state import Run

REFERENCE_YEAR = bq.REFERENCE_YEAR
ATTEMPTS = 3   # DuckDB 내부 오류가 나면 연결을 새로 열고 다시 실행하는 최대 횟수


def _llm_enabled() -> bool:
    return os.environ.get("ADDRESS_PIPELINE_LLM", "1").strip().lower() not in {"0", "false", "off", ""}


@dataclass
class AddressPipeline:
    sources: Sources = field(default_factory=Sources)
    reference_version: str = str(REFERENCE_YEAR)
    parallel_jobs = 2

    def run_dataset(self, records: tuple[InputRecord, ...], *, run_id: str,
                    progress: Progress | None = None) -> AdapterResult:
        cache: dict = {}
        retries: list[str] = []
        for attempt in range(1, ATTEMPTS + 1):
            try:
                r = self._run(records, run_id, cache)
                break
            except Exception as exc:
                if not is_broken_connection(exc) or attempt == ATTEMPTS:
                    raise
                # DuckDB 2.0 내부 오류: 연결을 새로 열고 처음부터 다시 돈다(LLM 응답은 cache로 다시 쓴다).
                retries.append(" / ".join(getattr(exc, "__notes__", []))[:200] or str(exc)[:200])
                self.sources.reset()
        r.dataset["batch_results"][0].update(attempts=attempt, retried_statements=retries)
        if progress is not None:
            progress(r.results, r.dataset["batch_results"][0])
        return AdapterResult(r.dataset, r.comparisons)

    def _run(self, records: tuple[InputRecord, ...], run_id: str, cache: dict) -> Run:
        r = Run(records, run_id, self.sources, llm=_llm_enabled(),
                llm_max=int(os.environ.get("ADDRESS_PIPELINE_LLM_MAX", "3000")), llm_cache=cache)
        try:
            r.record("0", "prepare", "A1-A8", lambda: stage0_prepare.run(r))
            r.record("123", "match", "B, C, D, E, F, G, L1", lambda: stage123_match.run(r))
            r.record("4", "buildings", "H", lambda: stage4_candidates.buildings(r))
            r.record("4", "abolished", "I", lambda: stage4_candidates.abolished(r))
            r.record("4", "number_typos", "J", lambda: stage4_candidates.number_typos(r))
            r.record("4", "glued_numbers", "K", lambda: stage4_candidates.glued_numbers(r))
            if r.llm:
                r.record("5a", "llm_spans", "LLM 원문 구분", lambda: stage5_llm.spans(r))
                r.record("5b", "llm_correction", "M", lambda: stage5_llm.correction(r))
            r.record("6", "output", "결과", lambda: stage6_output.run(r))
        finally:
            try:
                self.sources.delete_batches([r.batch, f"{r.batch}-split", f"{r.batch}-span", f"{r.batch}-ai"])
            except Exception as exc:  # noqa: BLE001 - 연결이 망가졌으면 지울 것도 없다(다시 열면 비어 있다).
                if not is_broken_connection(exc):
                    raise
        r.dataset["batch_results"][0]["stage_log"] = r.stage_log
        return r

    # ------------------------------------------------------------ 제출 양식 열(RAW 원본 조회)

    def annotate_rows(self, rows: list[dict]) -> None:
        from address_workbench.output_fields import annotate

        annotate(rows, self.raw_lookup, int(self.reference_version), self.building_lookup)

    def raw_lookup(self, keys: set[tuple[int, int]]) -> dict[tuple[int, int], dict]:
        from address_workbench.output_fields import _RAW_COLUMNS

        serials = sorted(serial for year, serial in keys if year == REFERENCE_YEAR)
        if not serials:
            return {}
        columns = ", ".join(f'"{name}"' for name in _RAW_COLUMNS)
        rows = self.sources.query(
            f'SELECT CAST("일련번호" AS BIGINT) AS serial, {columns} FROM raw_2025 '
            'WHERE list_contains($serials, CAST("일련번호" AS BIGINT))', array={"serials": ("INT64", serials)})
        return {(REFERENCE_YEAR, row["serial"]): {**{name: row[name] for name in _RAW_COLUMNS}, "_year": REFERENCE_YEAR}
                for row in rows}

    def building_lookup(self, keys: set[tuple[int, int, int, int]]) -> dict[tuple[int, int, int, int], list[dict]]:
        found: dict[tuple[int, int, int, int], list[dict]] = {key: [] for key in keys}
        wanted = sorted(f"{road}-{main}-{sub}" for year, road, main, sub in keys if year == REFERENCE_YEAR)
        if not wanted:
            return found
        rows = self.sources.query(
            'SELECT CAST("도로명코드" AS BIGINT) AS road, TRY_CAST("건물본번" AS BIGINT) AS main, '
            'IFNULL(TRY_CAST("건물부번" AS BIGINT), 0) AS sub, "법정동리명", "법정동읍면동명", "대지구분코드", '
            '"지번본번", "지번부번" FROM raw_2025 '
            "WHERE list_contains($keys, printf('%d-%d-%d', CAST(\"도로명코드\" AS BIGINT), TRY_CAST(\"건물본번\" AS BIGINT), "
            'IFNULL(TRY_CAST("건물부번" AS BIGINT), 0))) ORDER BY CAST("일련번호" AS BIGINT)',
            array={"keys": ("STRING", wanted)})
        for row in rows:
            found.setdefault((REFERENCE_YEAR, row["road"], row["main"], row["sub"]), []).append({
                key: row[key] for key in ("법정동리명", "법정동읍면동명", "대지구분코드", "지번본번", "지번부번")})
        return found


def create_adapter() -> AddressPipeline:
    return AddressPipeline()
