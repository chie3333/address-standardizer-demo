"""Browser service boundary for the address standardizer."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from importlib import import_module
from inspect import signature
from math import isfinite
from typing import TYPE_CHECKING, Callable, Literal, Protocol, TypedDict
from uuid import uuid4

if TYPE_CHECKING:
    from address_standardizer.contracts import BatchConfig


@dataclass(frozen=True)
class InputRecord:
    address_id: str
    raw_address: str | None


class Comparison(TypedDict):
    component: str
    input_value: str | None
    candidate_value: str | None
    outcome: Literal["match", "mismatch", "unknown"]
    evidence: str


@dataclass(frozen=True)
class AdapterResult:
    dataset: object
    # Address ID -> zero-based position in review.candidates -> evidence rows.
    candidate_comparisons: Mapping[str, Mapping[int, list[Comparison]]] | None = None


# 배치가 끝날 때마다 받는 행들(JSON으로 변환된 AddressResult, input_index는 전체 기준)과 배치 이력.
Progress = Callable[[list[dict], dict | None], None]


class AddressAdapter(Protocol):
    reference_version: str

    def run_dataset(self, records: tuple[InputRecord, ...], *, run_id: str) -> AdapterResult: ...


@dataclass(frozen=True)
class PipelineAdapter:
    """Use a configured Pipeline from the parent project, when available."""
    pipeline: object
    reference_version: str
    batch_size: int | None = None
    batch_config: BatchConfig | None = None

    def run_dataset(
        self, records: tuple[InputRecord, ...], *, run_id: str, progress: Progress | None = None,
    ) -> AdapterResult:
        from address_standardizer import run_dataset
        from address_standardizer.contracts import AddressRecord, BatchConfig

        from .old_regions import rename_for_matching, restore_rows

        # 옛 행정구역명(연기군 등)은 현재 이름으로 바꿔 파이프라인에 넘기고, 결과에는 원문을 되돌려 놓는다.
        renamed, inputs = {}, []
        for record in records:
            text, notes = rename_for_matching(record.raw_address)
            if notes:
                renamed[record.address_id] = (record.raw_address, notes)
            inputs.append(AddressRecord(record.address_id, text))

        def rows(results):
            converted = [json_value(result) for result in results]
            restore_rows(converted, renamed, "1")
            return converted

        def batch_json(batch):
            converted = json_value(batch)
            if renamed and isinstance(converted.get("results"), list):
                restore_rows(converted["results"], renamed, "1")
            return converted

        def report(results, batch):
            progress(rows(results), batch_json(batch) if batch is not None else None)

        dataset = run_dataset(
            tuple(inputs), self.pipeline, run_id=run_id,
            config=self.batch_config or BatchConfig(self.reference_version),
            batch_size=self.batch_size, progress=report if progress is not None else None,
        )
        if not renamed:
            return AdapterResult(dataset)
        output = json_value(dataset)
        restore_rows(output["results"], renamed, "1")
        for batch in output["batch_results"]:
            restore_rows(batch["results"], renamed, "1")
        return AdapterResult(output)

    def annotate_rows(self, rows: list[dict]) -> None:
        """결과 행에 제출 양식 19개 컬럼(`output_fields`)을 붙인다."""
        from .output_fields import annotate, building_lookup, raw_lookup

        searches = [search for search in (getattr(self.pipeline, "detail_search", None),
                                          *getattr(self.pipeline, "review_history", ()))
                    if search is not None and hasattr(search, "raw_data")]
        if not searches:
            return
        paths = {search.validator.year: search.raw_data for search in searches}
        annotate(rows, raw_lookup(paths), int(self.reference_version), building_lookup(paths))


def load_adapter(spec: str) -> AddressAdapter:
    module_name, sep, factory_name = spec.partition(":")
    if not (module_name and sep and factory_name):
        raise ValueError("어댑터를 모듈명:팩토리함수 형식으로 지정하세요.")
    factory: Callable[[], AddressAdapter] = getattr(import_module(module_name), factory_name)
    adapter = factory()
    if not callable(getattr(adapter, "run_dataset", None)) or not (
        isinstance(getattr(adapter, "reference_version", None), str) and adapter.reference_version.strip()
    ):
        raise TypeError("어댑터에 run_dataset 메서드와 reference_version 문자열이 필요합니다.")
    return adapter


def json_value(value: object):
    if isinstance(value, float):
        return value if isfinite(value) else None
    if isinstance(value, int) and abs(value) > (2**53 - 1):
        return str(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    module = type(value).__module__.split(".")[0]
    if module == "pandas" and type(value).__name__ == "DataFrame":
        return json_value(value.to_dict(orient="records"))
    if module == "pandas" and type(value).__name__ in {"NAType", "NaTType"}:
        return None
    if module == "numpy" and hasattr(value, "tolist"):
        return json_value(value.tolist())
    raise TypeError(f"JSON으로 전달할 수 없는 값: {type(value).__name__}")


def supports_progress(adapter: AddressAdapter) -> bool:
    try:
        return "progress" in signature(adapter.run_dataset).parameters
    except (TypeError, ValueError):
        return False


def run(
    adapter: AddressAdapter, records: tuple[InputRecord, ...], *, source: str,
    run_id: str | None = None, progress: Progress | None = None,
) -> dict:
    run_id = run_id or uuid4().hex
    if progress is not None and supports_progress(adapter):
        output = adapter.run_dataset(records, run_id=run_id, progress=progress)
    else:
        output = adapter.run_dataset(records, run_id=run_id)
    dataset = json_value(output.dataset)
    if not isinstance(dataset, dict) or (
        dataset.get("run_id") != run_id or dataset.get("reference_version") != adapter.reference_version
        or not isinstance(dataset.get("summary"), dict) or not isinstance(dataset.get("batch_results"), list)
        or not isinstance(dataset.get("results"), list) or len(dataset["results"]) != len(records)
    ):
        raise ValueError("어댑터가 실행 ID, 기준자료 버전 또는 DatasetResult 필드 계약을 지키지 않았습니다.")
    for index, (row, expected) in enumerate(zip(dataset["results"], records)):
        if not isinstance(row, dict) or not isinstance(row.get("record"), dict) or (
            row.get("input_index") != index or row["record"].get("address_id") != expected.address_id
            or "raw_address" not in row["record"] or row["record"]["raw_address"] != expected.raw_address
            or not isinstance(row.get("status"), str) or not isinstance(row.get("traces"), list)
        ):
            raise ValueError(f"어댑터의 {index + 1}번째 AddressResult가 원본 행과 일치하지 않습니다.")
    if callable(getattr(adapter, "annotate_rows", None)):
        adapter.annotate_rows(dataset["results"])
    return {
        "source": source,
        "dataset": dataset,
        "candidate_comparisons": json_value(output.candidate_comparisons or {}),
    }
