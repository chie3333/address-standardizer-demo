"""배치 단위로 진행 상황을 남기는 백그라운드 실행.

화면은 실행을 시작한 뒤 `snapshot(since)`로 새로 끝난 행만 받아 간다. 중간에 오류가 나도
그때까지 끝난 배치의 행은 작업에 남아 있어 화면에서 계속 보고 내려받을 수 있다.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import BoundedSemaphore, Lock
from datetime import datetime, timezone
from time import time
from uuid import uuid4

from .adapter import InputRecord, run

# 사람이 1순위 후보를 승인해 확정할 수 있는 검토 단계.
APPROVABLE_TIERS = {"near_confirmed", "check_needed"}
APPROVED = "reviewed_confirmed"


def approvable_candidate(row: dict) -> dict | None:
    """승인할 수 있으면 단독 1순위 후보를 돌려준다. 표준 주소와 코드가 있는 후보만 확정값으로 쓸 수 있다."""
    review = row.get("review") or {}
    if review.get("tier") not in APPROVABLE_TIERS or row.get("status") in {"resolved", "excluded", "failed"}:
        return None
    first = [item for item in review.get("candidates") or [] if isinstance(item, dict) and item.get("rank") == 1]
    if len(first) != 1:
        return None
    standard = first[0].get("standard_address")
    if not isinstance(standard, dict) or not standard.get("standard_road_address"):
        return None
    return first[0]


@dataclass
class Job:
    job_id: str
    run_id: str
    owner_id: str
    source: str
    reference_version: str
    total: int
    status: str = "queued"  # queued | running | done | failed
    rows: list[dict] = field(default_factory=list)  # 끝난 순서대로 쌓는다.
    batches_done: int = 0
    error: str = ""
    payload: dict | None = None  # 완료 후 검증된 전체 결과
    started_at: float = field(default_factory=time)
    finished_at: float | None = None


class JobRunner:
    """작업 소유자별로 최근 결과를 보관하고, 어댑터의 동시 실행 한도에 맞춰 처리한다."""

    def __init__(self, adapter, source: str, *, keep: int = 2):
        self.adapter = adapter
        self.source = source
        self.keep = keep
        self.jobs: dict[str, Job] = {}
        self.lock = Lock()
        self.parallel_jobs = getattr(adapter, "parallel_jobs", 1)
        self.slots = BoundedSemaphore(self.parallel_jobs)
        self.executor = ThreadPoolExecutor(max_workers=self.parallel_jobs)

    def start(self, records: tuple[InputRecord, ...], *, owner_id: str) -> Job:
        with self.lock:
            if self.parallel_jobs == 1 and any(job.status in {"queued", "running"} for job in self.jobs.values()):
                raise RuntimeError("다른 처리가 진행 중입니다. 끝난 뒤 다시 실행하세요.")
            job = Job(uuid4().hex, uuid4().hex, owner_id, self.source, self.adapter.reference_version, len(records))
            self.jobs[job.job_id] = job
            self.executor.submit(self._run, job, records)
        return job

    def run_sync(self, records: tuple[InputRecord, ...]) -> dict:
        with self.slots:
            return run(self.adapter, records, source=self.source)

    def _prune(self, owner_id: str) -> None:
        finished = [job.job_id for job in self.jobs.values()
                    if job.owner_id == owner_id and job.status in {"done", "failed"}
                    and not any((row.get("additional_analysis") or {}).get("status") in {"queued", "running"}
                                for row in job.rows)]
        for old in finished[:-self.keep]:
            del self.jobs[old]

    def _run(self, job: Job, records: tuple[InputRecord, ...]) -> None:
        def progress(rows: list[dict], batch: dict | None) -> None:
            with self.lock:
                job.rows.extend(rows)
                if batch is not None:
                    job.batches_done += 1

        try:
            with self.slots:
                with self.lock:
                    job.status = "running"
                payload = run(self.adapter, records, source=self.source, run_id=job.run_id, progress=progress)
        except Exception as exc:  # noqa: BLE001 - 화면에 원인만 알리고 끝난 행은 남긴다.
            with self.lock:
                job.status, job.error, job.finished_at = "failed", type(exc).__name__, time()
                self._prune(job.owner_id)
            return
        with self.lock:
            # 화면이 받아 간 위치(since)가 바뀌지 않도록 도착 순서는 유지하고, 내용만 검증된 최종 행으로 바꾼다.
            final = {row["record"]["address_id"]: row for row in payload["dataset"]["results"]}
            arrived = [final.pop(row["record"]["address_id"]) for row in job.rows if row["record"]["address_id"] in final]
            job.rows = arrived + list(final.values())  # 진행 보고를 안 하는 어댑터는 여기서 전부 채워진다.
            job.payload, job.status, job.finished_at = payload, "done", time()
            self._prune(job.owner_id)

    def approve(self, job_id: str, address_ids: list[str], *, owner_id: str | None,
                undo: bool = False) -> list[dict] | None:
        """검토 필요 행의 단독 1순위 후보를 '검토 후 확정'으로 표시하거나 표시를 지운다. 바뀐 행을 돌려준다.

        원문 그대로 맞은 확정(status=resolved)과 섞이지 않도록 status는 그대로 두고 approval만 붙인다.
        """
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job.owner_id != owner_id:
                return None
            if job.status != "done":
                raise RuntimeError("처리가 끝난 뒤 승인할 수 있습니다.")
            wanted = set(address_ids)
            changed = []
            for row in job.rows:
                if row["record"]["address_id"] not in wanted:
                    continue
                if undo:
                    if row.pop("approval", None) is not None:
                        changed.append(row)
                    continue
                candidate = approvable_candidate(row)
                if candidate is None or (row.get("approval") or {}).get("status") == APPROVED:
                    continue
                row["approval"] = {
                    "status": APPROVED, "tier": row["review"]["tier"],
                    "candidate_address": candidate.get("address"), "candidate_kind": candidate.get("kind"),
                    "evidence": candidate.get("evidence"), "standard_address": candidate["standard_address"],
                    "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                changed.append(row)
            # 승인·취소로 확정 주소가 바뀌었으니 제출 양식 컬럼도 다시 채운다.
            if changed and callable(getattr(self.adapter, "annotate_rows", None)):
                self.adapter.annotate_rows(changed)
            return changed

    def latest(self, *, owner_id: str | None) -> Job | None:
        with self.lock:
            return next((job for job in reversed(self.jobs.values()) if job.owner_id == owner_id), None)

    def snapshot(self, job_id: str, since: int = 0, *, owner_id: str | None) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job.owner_id != owner_id:
                return None
            done = job.status == "done"
            return {
                "job_id": job.job_id, "run_id": job.run_id, "source": job.source,
                "reference_version": job.reference_version, "status": job.status, "total": job.total,
                "processed": len(job.rows), "batches_done": job.batches_done, "error": job.error,
                "elapsed_s": round((job.finished_at or time()) - job.started_at, 1),
                "rows": job.rows[since:],
                # 완료되면 결과 행을 뺀 나머지(집계, 배치 이력)만 준다. 행은 이미 받았거나 위 rows로 받는다.
                "payload": {**job.payload, "dataset": {**job.payload["dataset"], "results": None}} if done else None,
            }
