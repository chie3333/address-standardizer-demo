"""한 번의 실행(run)에서 단계끼리 주고받는 상태."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from time import perf_counter
from typing import Callable

from address_workbench.adapter import InputRecord

from .sources import Sources

VERSION = "1"


@dataclass
class Run:
    records: tuple[InputRecord, ...]
    run_id: str
    sources: Sources
    llm: bool = True
    llm_max: int = 3000
    segments: dict[str, list[str]] = field(default_factory=dict)  # 주소 ID → 0단계에서 나눈 주소 조각
    texts: dict[str, str] = field(default_factory=dict)            # 조각이 하나인 주소 ID → 정리한 원문
    rows: list[dict] = field(default_factory=list)                 # input_raw에 올린 행
    dataset: dict | None = None
    comparisons: dict = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)                # 워크벤치 처리 이력(search_traces)
    stage_log: list[dict] = field(default_factory=list)            # 단계별 처리 건수
    generate: Callable[[str, str], str] | None = None              # LLM 호출 함수(5단계에서 처음 쓸 때 만든다)
    llm_cache: dict = field(default_factory=dict)                  # (프롬프트, 요청) → 응답. 다시 실행할 때 LLM을 또 부르지 않는다

    @property
    def batch(self) -> str:
        return f"pl-{self.run_id}"

    @property
    def results(self) -> list[dict]:
        return self.dataset["results"] if self.dataset else []

    def counts(self) -> dict:
        """지금 상태의 판정별 건수와 후보가 있는 행 수."""
        status = Counter(row["status"] for row in self.results)
        with_candidates = sum(1 for row in self.results if (row.get("review") or {}).get("candidates"))
        return {"status": dict(status), "with_candidates": with_candidates}

    def record(self, stage: str, tool: str, rules: str, action: Callable[[], dict | None]) -> dict:
        """단계(도구) 하나를 실행하고 걸린 시간, 바뀐 건수, 실행 뒤 판정 건수를 남긴다."""
        before = self.counts() if self.dataset else None
        start = perf_counter()
        details = dict(action() or {})
        elapsed = round((perf_counter() - start) * 1000, 1)
        after = self.counts() if self.dataset else None
        entry = {"stage": stage, "tool": tool, "rules": rules, "elapsed_ms": elapsed, "details": details,
                 "after": after}
        if before and after:
            entry["new_candidate_rows"] = after["with_candidates"] - before["with_candidates"]
        self.stage_log.append(entry)
        self.steps.append({"module_name": f"stage{stage}_{tool}", "module_version": VERSION, "status": "completed",
                           "reason_code": None, "elapsed_ms": elapsed,
                           "details": {"rules": rules, **details, **({"after": after} if after else {})}})
        return details
