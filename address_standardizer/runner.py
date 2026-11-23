"""주소 단위 병렬 실행과 단계별 연결."""

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from itertools import combinations, product
from threading import Semaphore
from time import perf_counter
from typing import Callable, TypeVar
from unicodedata import normalize

from pandas import DataFrame

from ._execution import invoke as _invoke
from .contracts import (
    AddressRecord, AddressResult, BatchConfig, BatchInput, BatchResult, DatasetResult,
    HierarchyOutput, HierarchySearch,
    Module, ModuleResult, ModuleTrace, ParseOutput, PreprocessOutput, RegionValidationResult,
    ResolutionOutput, RunContext, StandardizationOutput, ValidationOutput,
)
from .data_input import make_batches
from .detail_address import DetailAddressSearch, DetailInput
from .preprocessing import SPACING_PREPROCESSOR, exclude_duplicates
from .region_validation import PreLLMRegionLabeling, RegionValidator
from .review_candidates import AddressReview, review_batch, summarize_reviews, merge_detail_recovery
from .standardization import standardize


I = TypeVar("I")
O = TypeVar("O")
# 공백으로 끊긴 명칭을 한 행에서 동시에 몇 곳까지 다시 붙여 볼지(흑 석동 흑 석로9길 = 2곳).
MAX_NAME_JOINS = 3


@dataclass(frozen=True)
class Pipeline:
    parser: Module[AddressRecord, ParseOutput]
    hierarchy_search: HierarchySearch | None = None
    address_validation: Module[HierarchyOutput, ValidationOutput] | None = None
    resolution: Module[ValidationOutput, ResolutionOutput] | None = None
    preprocessor: Module[AddressRecord, PreprocessOutput] | None = None
    region_validator: RegionValidator | None = None
    detail_search: DetailAddressSearch | None = None
    review_history: tuple[DetailAddressSearch, ...] = ()
    detail_recovery_generate: Callable[[str, str], str] | None = None
    # 원문 → (정리한 원문, 바꾼 내역). 파서 앞에서 표기를 규칙으로 정리하고 결과에는 원문을 되돌린다.
    input_cleaner: Callable[[str | None], tuple[str | None, tuple[dict, ...]]] | None = None

    def __post_init__(self) -> None:
        if self.address_validation is not None and self.hierarchy_search is None:
            raise ValueError("상세 검증에는 계층 검색 모듈이 필요합니다.")
        if self.resolution is not None and self.address_validation is None:
            raise ValueError("결과 판정에는 상세 검증 모듈이 필요합니다.")
        if self.detail_search is None:
            if self.review_history:
                raise ValueError("과거 기준자료 조회에는 detail_search가 필요합니다.")
            return
        validator = self.detail_search.validator
        if self.region_validator is None:
            object.__setattr__(self, "region_validator", validator)
        elif (self.region_validator.year != validator.year
              or self.region_validator.catalog.resolve() != validator.catalog.resolve()):
            raise ValueError("지역 검증과 상세 검색은 같은 기준연도와 카탈로그를 사용해야 합니다.")
        years = {search.validator.year for search in self.review_history}
        if len(years) != len(self.review_history) or any(year >= validator.year for year in years):
            raise ValueError("과거 기준연도는 중복 없이 기준연도보다 이전이어야 합니다.")


def _parse_result(context: RunContext, result: ModuleResult[ParseOutput]) -> AddressResult:
    status = result.trace.status
    if status == "completed":
        assert result.output is not None
        if result.output.screening_result != "passed":
            status = "excluded"
        else:
            status = "parsed" if result.output.address_candidates or result.output.tokens else "parse_unresolved"
    return AddressResult(context.record, context.input_index, status, (result.trace,), result.output)


def _parse_group(
    contexts: tuple[RunContext, ...], parser: Module[AddressRecord, ParseOutput],
) -> tuple[AddressResult, ...]:
    if len(contexts) == 1 or parser.run_many is None:
        context, = contexts
        result = _invoke("parser", parser.version, lambda: parser.run(context, context.record))
        return (_parse_result(context, result),)

    run_many = parser.run_many

    def parse() -> ModuleResult[tuple[ModuleResult[ParseOutput], ...]]:
        outputs = run_many(contexts)
        if len(outputs) != len(contexts):
            raise ValueError("파서 결과 수가 요청한 주소 수와 다릅니다.")
        if any(result.trace.status == "completed" and result.output is None for result in outputs):
            raise ValueError("완료된 파싱에 출력이 없습니다.")
        return ModuleResult(outputs, ModuleTrace("completed"))

    request = _invoke("parser", parser.version, parse)
    outputs = request.output if request.output is not None else (ModuleResult(None, request.trace),) * len(contexts)
    return tuple(
        _parse_result(context, replace(output, trace=replace(
            output.trace,
            module_name=request.trace.module_name,
            module_version=request.trace.module_version,
            elapsed_ms=request.trace.elapsed_ms,
            details={**output.trace.details, "shared_elapsed": True,
                     "request_first_input_index": contexts[0].input_index},
        )))
        for context, output in zip(contexts, outputs, strict=True)
    )


def _preprocess(
    context: RunContext, module: Module[AddressRecord, PreprocessOutput], stage_name: str,
) -> AddressResult:
    filtered = _invoke(
        stage_name, module.version, lambda: module.run(context, context.record),
    )
    if filtered.trace.status != "completed" or filtered.output is None:
        return AddressResult(
            context.record, context.input_index, filtered.trace.status, (filtered.trace,), filtered.output,
        )
    status = "preprocess_passed" if filtered.output.status == "passed" else filtered.output.status
    return AddressResult(
        context.record, context.input_index, status, (filtered.trace,), filtered.output,
    )


def _screen_records(
    executor: ThreadPoolExecutor, contexts: tuple[RunContext, ...],
    preprocessor: Module[AddressRecord, PreprocessOutput],
) -> tuple[AddressResult, ...]:
    prepared = tuple(executor.map(
        lambda ctx: _preprocess(ctx, SPACING_PREPROCESSOR, "input_preparation"), contexts,
    ))
    screened = exclude_duplicates(prepared)

    def finish(item: tuple[RunContext, AddressResult]) -> AddressResult:
        context, result = item
        if result.status != "preprocess_passed":
            return result
        processed = _preprocess(context, preprocessor, "preprocessing")
        return replace(processed, traces=(*result.traces, *processed.traces))

    return tuple(executor.map(finish, zip(contexts, screened, strict=True)))


def _representative_of(result: AddressResult) -> str | None:
    """원문 중복으로 제외된 행이면 대표 행의 address_id를 반환한다."""
    output = result.output
    if (result.status == "excluded" and isinstance(output, PreprocessOutput)
            and output.reason_code == "duplicate_input"):
        return output.representative_address_id
    return None


def _inherit(duplicate: AddressResult, representative: AddressResult) -> AddressResult:
    """대표 행의 판정 결과를 중복 행에 복사한다. 중복 행 자신의 선별 이력은 앞에 남긴다."""
    rep_id = representative.record.address_id
    trace = ModuleTrace(
        "completed", "duplicate_inherited", {"representative_address_id": rep_id, "status": representative.status},
        module_name="duplicate_inheritance", module_version="1",
    )
    return replace(
        representative, record=duplicate.record, input_index=duplicate.input_index,
        traces=(*duplicate.traces, trace), inherited_from=rep_id,
    )


def _inherit_duplicates(results: tuple[AddressResult, ...]) -> tuple[AddressResult, ...]:
    by_id = {result.record.address_id: result for result in results}
    return tuple(
        _inherit(result, by_id[rep]) if (rep := _representative_of(result)) in by_id else result
        for result in results
    )


def _prepared_record(result: AddressResult) -> AddressRecord:
    assert isinstance(result.output, PreprocessOutput)
    return replace(result.record, normalized_address=result.output.normalized_address)


def _continue_address(
    context: RunContext, pipeline: Pipeline, address: AddressResult,
    hierarchy: HierarchyOutput,
) -> AddressResult:
    traces = list(address.traces)
    started = perf_counter()
    standardized = standardize(hierarchy)
    traces.append(ModuleTrace(
        "completed", standardized.reason_code, {"status": standardized.status},
        module_name="standardization", module_version="1",
        elapsed_ms=(perf_counter() - started) * 1000,
    ))

    def invoke(name: str, module: Module[I, O], value: I) -> ModuleResult[O]:
        result = _invoke(name, module.version, lambda: module.run(context, value))
        traces.append(result.trace)
        return result

    def finish(
        status: str,
        output: ParseOutput | HierarchyOutput | ValidationOutput | ResolutionOutput | None,
    ) -> AddressResult:
        if traces[-1].status != "completed":
            status = traces[-1].status
        return replace(
            address, status=status, traces=tuple(traces), output=output,
            standardization=standardized,
        )

    if hierarchy.search_status != "found" or pipeline.address_validation is None:
        status = standardized.status if hierarchy.hierarchy_candidates else hierarchy.search_status
        return finish(status, hierarchy)

    validation = invoke("address_validation", pipeline.address_validation, hierarchy)
    if validation.trace.status != "completed" or validation.output is None:
        return finish("failed", validation.output)
    if pipeline.resolution is None:
        return finish(validation.output.validation_status, validation.output)

    resolution = invoke("resolution", pipeline.resolution, validation.output)
    status = resolution.output.resolution_status if resolution.output is not None else "failed"
    return finish(status, resolution.output)


def _collect_candidates(results: tuple[AddressResult, ...]) -> DataFrame:
    rows = []
    for result in results:
        if result.status != "parsed":
            continue
        parsed = result.output
        assert isinstance(parsed, ParseOutput)
        for candidate in parsed.address_candidates:
            rows.append((
                len(rows), result.input_index, result.record.address_id,
                candidate.candidate_id, candidate.address_type, candidate, candidate.token_labels,
            ))
    return DataFrame(rows, columns=[
        "row_id", "input_index", "address_id", "candidate_id", "address_type", "candidate",
        "token_labels",
    ])


def _validate_regions(
    results: tuple[AddressResult, ...], validator: RegionValidator, config: BatchConfig,
    *, split_merged_names: bool = False,
) -> tuple[tuple[AddressResult, ...], tuple[ModuleTrace, ...]]:
    recovery_traces = ()
    if split_merged_names and config.reference_version == str(validator.year):
        results, recovery_traces = _recover_source_boundaries(results, validator)
    records = {
        result.record.address_id: {"address_id": result.record.address_id, "tokens": result.output.tokens}
        for result in results
        if result.status == "parsed" and isinstance(result.output, ParseOutput) and result.output.tokens
    }
    if not records:
        return results, recovery_traces

    def validate() -> ModuleResult[dict[str, RegionValidationResult]]:
        if config.reference_version != str(validator.year):
            raise ValueError("배치 기준연도와 지역 검증기 기준연도가 다릅니다.")
        validations, stats = validator.validate_batch(records.values(), suggest=True)
        by_id = {row["address_id"]: row for row in validations}
        if by_id.keys() != records.keys() or len(validations) != len(records):
            raise ValueError("지역 검증 결과의 주소 ID가 배치 입력과 일치하지 않습니다.")
        return ModuleResult(by_id, ModuleTrace("completed", details=stats))

    batch_result = _invoke("region_validation", validator.version, validate)
    validations = batch_result.output or {}
    updated = []
    for result in results:
        address_id = result.record.address_id
        if address_id not in records:
            updated.append(result)
            continue
        validation = validations.get(address_id)
        if validation is None:
            trace = replace(batch_result.trace, elapsed_ms=0)
            updated.append(replace(result, status="failed", traces=(*result.traces, trace)))
            continue
        trace = ModuleTrace(
            "completed", reason_code=validation.get("reason_code"),
            details={"status": validation["status"], "path_count": len(validation["paths"]),
                                  "used_correction": validation.get("used_correction", False)},
            module_name="region_validation", module_version=validator.version,
        )
        updated.append(replace(
            result, status=validation["status"], region_validation=validation, traces=(*result.traces, trace),
        ))
    results = tuple(updated)
    traces = (*recovery_traces, batch_result.trace)
    if split_merged_names:
        results, road_traces = _join_road_names(results, validator)
        traces += road_traces
    restored, restoration_traces = _restore_leading_names(results, validator)
    traces += restoration_traces
    if split_merged_names:
        restored, split_traces = _split_merged_names(restored, validator)
        traces += split_traces
    return restored, traces


def _recover_source_boundaries(
    results: tuple[AddressResult, ...], validator: RegionValidator,
) -> tuple[tuple[AddressResult, ...], tuple[ModuleTrace, ...]]:
    trials = []
    owners = []
    for index, result in enumerate(results):
        if result.status != "failed":
            continue
        for trace in result.traces:
            for candidate in trace.details.get("source_boundary_candidates", ()):
                trials.append({"address_id": str(len(trials)), "tokens": candidate["tokens"]})
                owners.append((index, candidate))
    if not trials:
        return results, ()

    def validate() -> ModuleResult[list[RegionValidationResult]]:
        matches, stats = validator.validate_batch(trials)
        return ModuleResult(matches, ModuleTrace("completed", details=stats))

    checked = _invoke("region_source_boundary_validation", validator.version, validate)
    if checked.output is None:
        return results, (checked.trace,)

    hits = {}
    for (index, candidate), match in zip(owners, checked.output, strict=True):
        if match["status"] in {"unique_path", "multiple_paths", "held"}:
            hits.setdefault(index, []).append((candidate, match))

    updated = list(results)
    for index, matches in hits.items():
        result = results[index]
        candidate, match = matches[0]
        if len(matches) > 1:
            trace = ModuleTrace(
                "completed", "ambiguous_source_boundaries", {"candidate_count": len(matches)},
                module_name="region_source_boundary_validation", module_version=validator.version,
            )
            updated[index] = replace(result, status="held", traces=(*result.traces, trace))
            continue
        trace = ModuleTrace(
            "completed", "source_boundary_checked",
            {"llm_token": candidate["llm_token"], "source_token": candidate["source_token"],
             "first_status": result.status, "revalidation_status": match["status"]},
            module_name="region_source_boundary_validation", module_version=validator.version,
        )
        output = ParseOutput("passed", tokens=candidate["tokens"],
                             unresolved_segments=candidate["unresolved_segments"])
        updated[index] = replace(
            result, status=match["status"], output=output, traces=(*result.traces, trace),
            region_validation={**match, "address_id": result.record.address_id},
        )
    return tuple(updated), (checked.trace,)


def _join_road_names(
    results: tuple[AddressResult, ...], validator: RegionValidator,
) -> tuple[tuple[AddressResult, ...], tuple[ModuleTrace, ...]]:
    def piece(token) -> str:
        return "".join(normalize("NFKC", token["raw"]).split())

    pending = []
    for index, result in enumerate(results):
        output = result.output
        if result.status == "no_match" and isinstance(output, ParseOutput) and output.road_join_candidates:
            pending.append((index, output))
    if not pending:
        return results, ()

    # 두 조각이 모두 이미 있는 명칭이면(인동 + 중앙로) 붙이지 않는다. 붙인 이름이 우연히
    # 다른 명칭(인동중앙로)과 같아도 원래 두 명칭을 다른 주소로 바꾸지 않기 위해서다.
    pieces = {piece(token) for _, output in pending for token in output.tokens}
    roads, _, _, _ = validator._lookup_roads(pieces)

    def known(name: str) -> bool:
        return validator._known_region(name) or bool(roads.get(name))

    choices = {}
    for index, output in pending:
        usable = []
        for candidate in output.road_join_candidates:
            pair = output.tokens[candidate.token_index:candidate.token_index + 2]
            if len(pair) == 2 and not all(known(piece(token)) for token in pair):
                usable.append(candidate)
        if usable:
            choices[index] = sorted(usable, key=lambda candidate: candidate.token_index)
    if not choices:
        return results, ()

    def joined(output: ParseOutput, combo) -> tuple[list, list[int]]:
        """겹치지 않는 후보들을 한꺼번에 합치고, 합친 토큰의 새 위치를 반환한다."""
        tokens = output.tokens.copy()
        for candidate in reversed(combo):
            position = candidate.token_index
            tokens[position:position + 2] = [{"raw": candidate.name, "labels": []}]
        return tokens, [candidate.token_index - offset for offset, candidate in enumerate(combo)]

    # 한 곳만 끊긴 경우부터 시험하고, 같은 크기에서 경로가 나온 조합이 하나뿐일 때만 반영한다.
    # 흑 석동 흑 석로9길처럼 두 곳이 끊긴 경우는 두 후보를 함께 붙인 조합으로 확인한다.
    updated = list(results)
    traces = []
    for size in range(1, MAX_NAME_JOINS + 1):
        trials, owners = [], []
        for index, usable in choices.items():
            for combo in combinations(usable, size):
                if any(b.token_index - a.token_index < 2 for a, b in zip(combo, combo[1:])):
                    continue
                tokens, positions = joined(results[index].output, combo)
                trials.append({"address_id": str(len(trials)), "tokens": tokens})
                owners.append((index, combo, positions))
        if not trials:
            break

        def validate() -> ModuleResult[list[RegionValidationResult]]:
            matches, stats = validator.validate_batch(trials)
            return ModuleResult(matches, ModuleTrace("completed", details={**stats, "joined_names": size}))

        checked = _invoke("region_road_join_validation", validator.version, validate)
        traces.append(checked.trace)
        if checked.output is None:
            break

        found = defaultdict(list)
        for trial, (index, combo, positions), match in zip(trials, owners, checked.output, strict=True):
            names = [candidate.name for candidate in combo]
            # 합친 명칭이 모두 같은 경로에 실제로 나타나야 한다.
            paths = [path for path in match["paths"]
                     if all(any(node["name"] == name for node in path) for name in names)]
            if paths:
                found[index].append((trial, combo, positions, match, paths))
        for index, accepted in found.items():
            del choices[index]
            if len(accepted) != 1:
                continue
            trial, combo, positions, match, paths = accepted[0]
            result = results[index]
            output = result.output
            assert isinstance(output, ParseOutput)
            tokens = trial["tokens"]
            db_labels = match["db_labels"].copy()
            joins = []
            for candidate, position in zip(combo, positions, strict=True):
                labels = sorted({node["label"] for path in paths for node in path if node["name"] == candidate.name})
                tokens[position] = {"raw": candidate.raw, "labels": []}
                if candidate.raw != candidate.name:
                    tokens[position]["corrected"] = candidate.name
                db_labels[position] = labels
                joins.append({"raw": candidate.raw, "joined_name": candidate.name, "labels": labels})
            status = "unique_path" if len(paths) == 1 else "multiple_paths"
            only_roads = all(join["labels"] == ["도로명"] for join in joins)
            trace = ModuleTrace(
                "completed", "road_name_joined" if only_roads else "region_name_joined",
                {"joins": joins, "path_count": len(paths), "revalidation_status": status},
                module_name="region_road_join_validation", module_version=validator.version,
            )
            updated[index] = replace(
                result, status=status, traces=(*result.traces, trace),
                output=replace(output, tokens=tokens, leading_name_candidates=(), road_join_candidates=()),
                region_validation={**match, "address_id": result.record.address_id, "status": status,
                                   "paths": paths, "db_labels": db_labels,
                                   "used_correction": any(c.raw != c.name for c in combo)
                                   or match.get("used_correction", False)},
            )
        if not choices:
            break
    return tuple(updated), tuple(traces)


def _split_merged_names(
    results: tuple[AddressResult, ...], validator: RegionValidator,
) -> tuple[tuple[AddressResult, ...], tuple[ModuleTrace, ...]]:
    trials = []
    owners = []
    for index, result in enumerate(results):
        output = result.output
        if result.status != "no_match" or not isinstance(output, ParseOutput):
            continue
        source = result.record.raw_address or result.record.normalized_address or ""
        choices = []
        for token in output.tokens:
            raw = token["raw"]
            splits = ()
            if not token["labels"] and not token.get("corrected") and raw in source:
                splits = validator.region_name_splits(raw, limit=None)
            choices.append(splits or ((),))
        for splits in product(*choices):
            if not any(splits):
                continue
            tokens = [
                part
                for token, names in zip(output.tokens, splits, strict=True)
                for part in ([{"raw": name, "labels": []} for name in names] if names else [token])
            ]
            trials.append({"address_id": str(len(trials)), "tokens": tokens})
            owners.append((index, tuple(names for names in splits if names)))
    if not trials:
        return results, ()

    def validate() -> ModuleResult[list[RegionValidationResult]]:
        matches, stats = validator.validate_batch(trials)
        return ModuleResult(matches, ModuleTrace("completed", details=stats))

    checked = _invoke("region_merged_name_validation", validator.version, validate)
    if checked.output is None:
        return results, (checked.trace,)

    candidates = {}
    for trial, (index, splits), match in zip(trials, owners, checked.output, strict=True):
        paths = [
            path for path in match["paths"]
            if all(validator.matches_consecutive_names(path, names) for names in splits)
        ]
        if paths or match["status"] == "held":
            candidates.setdefault(index, []).append((trial["tokens"], match, paths))

    updated = list(results)
    for index, hits in candidates.items():
        result = results[index]
        output = result.output
        assert isinstance(output, ParseOutput)
        paths = {}
        for _, _, matched in hits:
            for path in matched:
                paths[tuple((node["label"], node["name"], node["node_id"], node["code_prefix"]) for node in path)] = path
        held = next((match for _, match, _ in hits if match["status"] == "held"), None)
        if held is not None:
            validation = {**held, "db_labels": [[] for _ in output.tokens]}
        elif len(paths) > 1:
            validation = {**hits[0][1], "status": "multiple_paths", "paths": list(paths.values()),
                          "db_labels": [[] for _ in output.tokens]}
        elif len(hits) == 1:
            tokens, match, _ = hits[0]
            validation = {**match, "status": "unique_path", "paths": list(paths.values())}
            output = replace(output, tokens=tokens, leading_name_candidates=(), road_join_candidates=())
        else:
            continue
        trace = ModuleTrace(
            "completed", "merged_names_checked",
            {"first_status": result.status, "revalidation_status": validation["status"],
             "path_count": len(validation["paths"])},
            module_name="region_merged_name_validation", module_version=validator.version,
        )
        updated[index] = replace(
            result, status=validation["status"], output=output, traces=(*result.traces, trace),
            region_validation={**validation, "address_id": result.record.address_id},
        )
    return tuple(updated), (checked.trace,)


def _restore_leading_names(
    results: tuple[AddressResult, ...], validator: RegionValidator,
) -> tuple[tuple[AddressResult, ...], tuple[ModuleTrace, ...]]:
    trials = []
    owners = []
    for index, result in enumerate(results):
        if result.status not in {"no_match", "unique_path"} or not isinstance(result.output, ParseOutput):
            continue
        for candidate in result.output.leading_name_candidates:
            if candidate.token_index == 0:
                continue
            tokens = result.output.tokens.copy()
            tokens[candidate.token_index] = {"raw": candidate.name, "labels": tokens[candidate.token_index]["labels"]}
            trials.append({"address_id": str(len(trials)), "tokens": tokens})
            owners.append((index, candidate))
    if not trials:
        return results, ()

    def validate() -> ModuleResult[list[RegionValidationResult]]:
        matches, stats = validator.validate_batch(trials)
        return ModuleResult(matches, ModuleTrace("completed", details=stats))

    checked = _invoke("region_boundary_validation", validator.version, validate)
    if checked.output is None:
        return results, (checked.trace,)

    accepted = {}
    for trial, (index, candidate), match in zip(trials, owners, checked.output, strict=True):
        original = results[index].region_validation
        assert original is not None
        if validator.matches_restored_parent(trial["tokens"], candidate.token_index, match, original):
            accepted.setdefault(index, []).append((candidate, trial["tokens"], match))

    updated = list(results)
    for index, hits in accepted.items():
        if len(hits) != 1:
            continue
        candidate, tokens, match = hits[0]
        result = results[index]
        output = result.output
        assert isinstance(output, ParseOutput)
        previous_name = output.tokens[candidate.token_index]["raw"]
        prefix = candidate.name[:-len(previous_name)]
        segments = list(output.unresolved_segments)
        remaining = segments[candidate.segment_index].removesuffix(prefix).rstrip()
        segments[candidate.segment_index:candidate.segment_index + 1] = [remaining] if remaining else []
        trace = ModuleTrace(
            "completed", "leading_name_restored",
            {"first_status": result.status, "raw_token": previous_name, "restored_token": candidate.name,
             "revalidation_status": match["status"]},
            module_name="region_boundary_validation", module_version=validator.version,
        )
        updated[index] = replace(
            result, status=match["status"], traces=(*result.traces, trace),
            output=replace(output, tokens=tokens, unresolved_segments=tuple(segments), leading_name_candidates=()),
            region_validation={**match, "address_id": result.record.address_id},
        )
    return tuple(updated), (checked.trace,)


def _llm_limit(config: BatchConfig, pipeline: Pipeline) -> int:
    return config.llm_max_concurrency or pipeline.parser.llm_default_concurrency


def _make_contexts(
    run_id: str, records: tuple[AddressRecord, ...], config: BatchConfig, pipeline: Pipeline,
    llm_slots: Semaphore | None = None,
) -> tuple[RunContext, ...]:
    limit = _llm_limit(config, pipeline)
    config = replace(config, llm_max_concurrency=limit)
    llm_slots = llm_slots or Semaphore(limit)
    return tuple(RunContext(run_id, record, i, config, llm_slots) for i, record in enumerate(records))


def _tokenization_retry_reasons(
    results: tuple[AddressResult, ...], validator: RegionValidator,
) -> tuple[dict[str, str], dict[str, str]]:
    rows = []
    failed_alignments = {}
    spacing_hints = {}
    for result in results:
        if result.status == "failed":
            for trace in result.traces:
                runs = trace.details.get("source_runs_with_omission")
                if trace.reason_code != "invalid_token_response" or not isinstance(runs, list):
                    continue
                address_id = result.record.address_id
                source = result.record.normalized_address or result.record.raw_address or ""
                for run in runs:
                    splits = validator.region_name_splits(run)
                    if not splits:
                        continue
                    failed_alignments[address_id] = "raw_alignment_merged_name_omission"
                    if len(splits) == 1 and source.count(run) == 1:
                        before, _, after = source.partition(run)
                        source = before + " ".join(splits[0]) + (" " if after[:1].isdigit() else "") + after
                        spacing_hints[address_id] = source
            continue
        if result.status not in {"unique_path", "no_match", "parse_unresolved"}:
            continue
        output = result.output
        if not isinstance(output, ParseOutput):
            continue
        paths = result.region_validation["paths"] if result.region_validation else []
        rows.append({
            "address_id": result.record.address_id, "tokens": output.tokens, "remaining": output.unresolved_segments,
            "address": result.record.raw_address or result.record.normalized_address or "",
            "status": result.status, "path": paths[0] if result.status == "unique_path" else (),
        })
    return {**validator.tokenization_retry_reasons(rows), **failed_alignments}, spacing_hints


def _retry_tokenization(
    results: tuple[AddressResult, ...], contexts: tuple[RunContext, ...],
    parser: Module[AddressRecord, ParseOutput], validator: RegionValidator,
    config: BatchConfig, executor: ThreadPoolExecutor,
) -> tuple[tuple[AddressResult, ...], tuple[ModuleTrace, ...]]:
    reasons, spacing_hints = _tokenization_retry_reasons(results, validator)
    if not reasons:
        return results, ()
    selected = tuple(
        replace(ctx, record=replace(ctx.record, normalized_address=spacing_hints[ctx.record.address_id]))
        if ctx.record.address_id in spacing_hints else ctx
        for ctx in contexts if ctx.record.address_id in reasons
    )
    size = config.llm_batch_size if parser.run_many is not None else 1
    groups = (selected[i:i + size] for i in range(0, len(selected), size))
    parsed = tuple(row for group in executor.map(lambda ctxs: _parse_group(ctxs, parser), groups) for row in group)
    checked, traces = _validate_regions(parsed, validator, config, split_merged_names=True)
    unresolved, _ = _tokenization_retry_reasons(checked, validator)
    retried = {row.record.address_id: row for row in checked}
    final = []
    for first in results:
        address_id = first.record.address_id
        second = retried.get(address_id)
        if second is None:
            final.append(first)
            continue
        accepted = second.status in {"unique_path", "multiple_paths", "held"} and address_id not in unresolved
        chosen = second if accepted else first
        if not accepted and first.status == "unique_path":
            assert first.region_validation is not None
            chosen = replace(first, status="held", region_validation={
                **first.region_validation, "status": "held", "paths": [],
                "db_labels": [[] for _ in first.region_validation["db_labels"]],
                "reason_code": "unresolved_tokenization",
            })
        parser_trace = second.traces[0]
        retry_trace = replace(parser_trace, module_name="parser_retry", reason_code=reasons[address_id],
                              details={**parser_trace.details, "first_status": first.status,
                                       "first_failure_reason": reasons[address_id],
                                       "retry_parse_reason": parser_trace.reason_code,
                                       "revalidation_status": second.status,
                                       "revalidation_issue": unresolved.get(address_id),
                                       "final_status": chosen.status})
        final.append(replace(chosen, record=first.record, traces=(*first.traces, retry_trace, *second.traces[1:])))
    return tuple(final), tuple(
        replace(trace, module_name="region_revalidation") if trace.module_name == "region_validation" else trace
        for trace in traces
    )


def _review_addresses(
    results: tuple[AddressResult, ...], parsed: tuple[AddressResult, ...],
    pipeline: Pipeline, config: BatchConfig,
) -> tuple[tuple[AddressResult, ...], tuple[ModuleTrace, ...]]:
    search = pipeline.detail_search
    if search is None:
        return results, ()
    selected = {}
    compound = {}
    for result, upstream in zip(results, parsed, strict=True):
        if result.status in {"excluded", "failed", "skipped"}:
            continue
        compound_status, representative, pieces = search.resolve_compound_input(result.record.raw_address or "")
        if compound_status != "not_compound":
            compound[result.input_index] = (compound_status, pieces)
            if representative is None:
                continue  # A compound whose identity cannot be proven remains held.
            selected[result.input_index] = representative
            continue
        output = upstream.output
        region = upstream.region_validation
        reason = "" if region and region["paths"] and result.status != "held" else "region_unconfirmed"
        if result.status == "held":
            if isinstance(output, PreprocessOutput):
                reason = output.reason_code or reason
            elif region:
                reason = region.get("reason_code") or reason
        selected[result.input_index] = DetailInput(
            result.record.raw_address or result.record.normalized_address or "",
            validated_tokens=tuple(output.tokens) if isinstance(output, ParseOutput) else (),
            region_validation=region,
            hold_reason=reason,
        )
    if not selected and not compound:
        return results, ()

    def lookup() -> ModuleResult[dict[int, AddressReview]]:
        if config.reference_version != str(search.validator.year):
            raise ValueError("배치 기준연도와 상세 검색 기준연도가 다릅니다.")
        reviews = review_batch(search, list(selected.values()), history=pipeline.review_history)
        return ModuleResult(
            dict(zip(selected, reviews, strict=True)),
            ModuleTrace("completed", details={"input_count": len(reviews), "by_type": summarize_reviews(reviews)}),
        )

    batch = (_invoke("address_review", "1", lookup) if selected else
             ModuleResult({}, ModuleTrace("completed", details={"input_count": 0})))
    reviews = batch.output or {}
    recovery = {}
    if pipeline.detail_recovery_generate is not None:
        from .detail_llm_recovery import recover_detail
        for index, item in selected.items():
            if index in compound:
                continue
            review = reviews.get(index)
            if review is None or review.base_status not in {"held", "no_match", "lot_only"}:
                continue
            if review.base_reason not in {
                "number_not_found", "scope_not_found", "missing_address_scope",
                "missing_address_number", "ambiguous_number_boundary", "ambiguous_number_type",
                "sub_number_mismatch", "sub_number_omitted",
            }:
                continue
            # Avoid a network call when existing exact evidence already resolves the row.
            if review.standard_address is not None:
                continue
            try:
                evidence = recover_detail(
                    search, item.raw_address, review.base_reason,
                    review.candidates, pipeline.detail_recovery_generate,
                )
            except Exception as exc:
                # Recovery is optional. Failure must never disrupt verified matching.
                evidence = ()
                recovery[index] = (evidence, type(exc).__name__)
            else:
                recovery[index] = (evidence, "")
                reviews[index] = merge_detail_recovery(review, evidence, search.validator.year)
    updated = []
    for result in results:
        if result.input_index in compound and result.input_index not in selected:
            kind, pieces = compound[result.input_index]
            from .review_candidates import MULTIPLE, TIER_GENERAL
            reason = "multiple_addresses"
            review = AddressReview(MULTIPLE, "no_candidate", "held", reason, "", (), tier=TIER_GENERAL)
            trace = ModuleTrace("completed", reason, {"fragments": pieces, "validation": kind},
                                module_name="compound_address_check", module_version="1")
            updated.append(replace(result, status="held", review=review,
                                   standardization=None, traces=(*result.traces, trace)))
            continue
        if result.input_index not in selected:
            updated.append(result)
            continue
        review = reviews.get(result.input_index)
        trace = replace(batch.trace, elapsed_ms=0)
        status = result.status
        standardized = result.standardization
        if review is None:
            status = "failed"
        else:
            trace = replace(trace, reason_code=review.base_reason or None, details={
                "review_type": review.review_type, "status": review.status, "candidate_count": len(review.candidates),
            })
            if status != "held" and not any(t.module_name == "hierarchy_search" for t in result.traces):
                status = review.base_status
                if status == "resolved":
                    standardized = StandardizationOutput(
                        "resolved", review.standard_address,
                        raw_record_ids=tuple(str(serial) for _, serial in review.candidates[0].raw_records),
                    )
        extra = []
        final_output = result.output
        if result.input_index in compound:
            kind, pieces = compound[result.input_index]
            if kind == "same_building" and review is not None and review.base_status in {"resolved", "address_only"}:
                status = review.base_status
                if isinstance(final_output, PreprocessOutput):
                    final_output = replace(final_output, status="passed", reason_code=None, review_tag=None)
                if status == "resolved" and review.standard_address is not None:
                    standardized = StandardizationOutput(
                        "resolved", review.standard_address,
                        raw_record_ids=tuple(str(serial) for candidate in review.candidates
                                             for _, serial in candidate.raw_records),
                    )
                extra.append(ModuleTrace("completed", "same_building", {"fragments": pieces},
                                         module_name="compound_address_check", module_version="1"))
            else:
                status = "held"
                standardized = None
        if result.input_index in recovery:
            evidence, error = recovery[result.input_index]
            extra.append(ModuleTrace("failed" if error else "completed",
                                     "detail_recovery_failed" if error else "detail_recovery_checked",
                                     {"hypotheses": len(evidence), "error_type": error},
                                     module_name="detail_llm_recovery", module_version="1"))
            if any(e.validation == "db_matched" for e in evidence):
                status = "held"
                standardized = None
        updated.append(replace(
            result, status=status, traces=(*result.traces, trace, *extra), review=review,
            standardization=standardized, output=final_output,
            detail_recovery=recovery.get(result.input_index, ((), ""))[0],
        ))
    return tuple(updated), (batch.trace,)


def _screen_before_llm(
    contexts: tuple[RunContext, ...], validator: RegionValidator, config: BatchConfig,
) -> tuple[dict[int, AddressResult], tuple[ModuleTrace, ...]]:
    if config.reference_version != str(validator.year):
        return {}, ()

    def label() -> ModuleResult[dict[str, PreLLMRegionLabeling]]:
        labeled, stats = validator.label_before_llm(context.record for context in contexts)
        return ModuleResult(labeled, ModuleTrace("completed", details=stats))

    checked = _invoke("pre_llm_region_validation", validator.version, label)
    if checked.output == {} and not checked.trace.details:
        return {}, ()
    labeled = checked.output or {}
    accepted = {}
    for context in contexts:
        row = labeled.get(context.record.address_id)
        if row is None or not row.fully_labeled:
            continue
        match = row.validation
        numbers = row.remaining_numbers
        reason = "only_address_numbers_remaining" if numbers else "missing_address_number"
        trace = replace(checked.trace, elapsed_ms=0, reason_code=reason, details={
            "llm_skipped": True, "region_status": match["status"], "path_count": len(match["paths"]),
            "remaining_numbers": numbers,
        })
        if not numbers:
            match = {**match, "reason_code": reason}
        accepted[context.input_index] = AddressResult(
            context.record, context.input_index, match["status"] if numbers else "held", (trace,),
            ParseOutput("passed", unresolved_segments=numbers, tokens=row.tokens), region_validation=match,
        )
    return accepted, (checked.trace,)


def run_batch(batch: BatchInput, pipeline: Pipeline, *, llm_slots: Semaphore | None = None) -> BatchResult:
    """설정된 모듈을 실행하고 입력 행 순서로 결과를 반환한다.

    `llm_slots`를 주면 여러 배치가 같은 LLM 동시 요청 한도를 나눠 쓴다.
    """
    ids = {record.address_id for record in batch.records}
    if len(ids) != len(batch.records) or "" in ids:
        raise ValueError("address_id는 배치에서 비어 있지 않고 고유해야 합니다.")
    contexts = _make_contexts(batch.run_id, batch.records, batch.config, pipeline, llm_slots)
    with ThreadPoolExecutor(max_workers=batch.config.max_concurrency) as executor:
        preprocessor = pipeline.preprocessor
        screened = _screen_records(executor, contexts, preprocessor) if preprocessor is not None else ()
        ready = contexts
        if preprocessor is not None:
            ready = tuple(
                replace(context, record=_prepared_record(result))
                for context, result in zip(contexts, screened, strict=True)
                if result.status == "preprocess_passed"
            )
        search_traces = ()
        prevalidated = {}
        if (pipeline.region_validator is not None and pipeline.parser.retry_parser is not None
                and pipeline.hierarchy_search is None):
            prevalidated, search_traces = _screen_before_llm(ready, pipeline.region_validator, batch.config)
            ready = tuple(context for context in ready if context.input_index not in prevalidated)
        size = batch.config.llm_batch_size if pipeline.parser.run_many is not None else 1
        groups = (ready[i:i + size] for i in range(0, len(ready), size))
        parsed_by_index = {
            result.input_index: result
            for results in executor.map(lambda group: _parse_group(group, pipeline.parser), groups)
            for result in results
        }
        parsed_by_index.update(prevalidated)
        if preprocessor is not None:
            parsed = tuple(
                replace(parsed_by_index[i], traces=(*result.traces, *parsed_by_index[i].traces))
                if i in parsed_by_index else result
                for i, result in enumerate(screened)
            )
        else:
            parsed = tuple(parsed_by_index[context.input_index] for context in contexts)
        parsed_df = _collect_candidates(parsed)
        results = parsed
        if pipeline.region_validator is not None:
            results, validation_traces = _validate_regions(
                results, pipeline.region_validator, batch.config,
                split_merged_names=pipeline.parser.retry_parser is not None,
            )
            search_traces = (*search_traces, *validation_traces)
            if pipeline.parser.retry_parser is not None:
                retry_rows = tuple(result for result in results if result.input_index not in prevalidated)
                retried, retry_traces = _retry_tokenization(
                    retry_rows, ready, pipeline.parser.retry_parser, pipeline.region_validator, batch.config, executor,
                )
                by_index = {result.input_index: result for result in retried}
                results = tuple(by_index.get(result.input_index, result) for result in results)
                search_traces = (*search_traces, *retry_traces)
        region_results = results
        if pipeline.hierarchy_search is not None:
            from .legacy.db_validation import validate_batch

            hierarchy, hierarchy_traces = validate_batch(batch.config, parsed_df, pipeline.hierarchy_search)
            search_traces = (*search_traces, *hierarchy_traces)

            def finish(context: RunContext) -> AddressResult:
                address = results[context.input_index]
                if address.status == "held":
                    return address
                result = hierarchy.get(context.input_index)
                if result is None:
                    return address
                address = replace(address, traces=(*address.traces, result.trace))
                if result.output is None:
                    return replace(address, status=result.trace.status)
                return _continue_address(context, pipeline, address, result.output)

            results = tuple(executor.map(finish, contexts))
        results, review_traces = _review_addresses(results, region_results, pipeline, batch.config)
        search_traces = (*search_traces, *review_traces)
    results = _inherit_duplicates(results)
    summary = dict(Counter(result.status for result in results))
    return BatchResult(
        batch.run_id, batch.config.reference_version, results, summary, parsed_df, search_traces,
        summarize_reviews(result.review for result in results if result.review is not None),
    )


def run_dataset(
    records: tuple[AddressRecord, ...],
    pipeline: Pipeline,
    *,
    run_id: str,
    config: BatchConfig,
    batch_size: int | None = None,
    progress: Callable[[tuple[AddressResult, ...], BatchResult | None], None] | None = None,
) -> DatasetResult:
    """배치 이력을 보존하고 전체 데이터셋의 행 순서로 결과를 반환한다.

    `progress`를 주면 전처리에서 끝난 행(제외·보류)을 먼저, 이후 배치가 끝날 때마다 그 배치의 행을 넘긴다.
    넘기는 행의 `input_index`는 데이터셋 전체 기준 순서다.
    """
    ids = {record.address_id for record in records}
    if len(ids) != len(records) or "" in ids:
        raise ValueError("address_id는 데이터셋에서 비어 있지 않고 고유해야 합니다.")
    if batch_size is not None and batch_size < 1:
        raise ValueError("batch_size는 양수여야 합니다.")
    if pipeline.input_cleaner is not None:
        return _run_cleaned(records, pipeline, run_id=run_id, config=config, batch_size=batch_size, progress=progress)

    by_id: dict[str, AddressResult] = {}
    processing_pipeline = pipeline
    accepted = records
    if records and pipeline.preprocessor is not None:
        contexts = _make_contexts(run_id, records, config, pipeline)
        with ThreadPoolExecutor(max_workers=config.max_concurrency) as executor:
            screened = _screen_records(executor, contexts, pipeline.preprocessor)
        held = tuple(result for result in screened if result.status == "held")
        reviewed, _ = _review_addresses(held, held, pipeline, config)
        by_id = {result.record.address_id: result for result in screened}
        by_id.update((result.record.address_id, result) for result in reviewed)
        accepted = tuple(
            _prepared_record(result) for result in screened
            if result.status == "preprocess_passed"
        )
        processing_pipeline = replace(pipeline, preprocessor=None)
    positions = {record.address_id: index for index, record in enumerate(records)}
    accepted_ids = {record.address_id for record in accepted}
    # 원문 중복 행은 대표 행이 끝난 뒤 그 결과를 물려받는다. 대표 행이 전처리에서 이미 끝났으면 바로 물려받는다.
    duplicates = defaultdict(list)
    for address_id, result in list(by_id.items()):
        representative = _representative_of(result)
        if representative is None:
            continue
        if representative in accepted_ids:
            duplicates[representative].append(address_id)
        elif representative in by_id:
            by_id[address_id] = _inherit(result, by_id[representative])
    waiting = {address_id for ids in duplicates.values() for address_id in ids}

    def with_duplicates(results: tuple[AddressResult, ...]) -> tuple[AddressResult, ...]:
        return (*results, *(
            _inherit(by_id[address_id], result)
            for result in results for address_id in duplicates.get(result.record.address_id, ())
        ))

    if progress is not None:
        finished = tuple(
            replace(result, input_index=positions[address_id])
            for address_id, result in by_id.items() if address_id not in accepted_ids | waiting
        )
        if finished:
            progress(finished, None)

    batches = [batch for batch in make_batches(accepted, run_id=run_id, config=config, batch_size=batch_size)
               if batch.records]
    # 배치를 겹쳐 실행해도 LLM 동시 요청 한도는 하나를 함께 쓴다.
    llm_slots = Semaphore(_llm_limit(config, processing_pipeline))

    def finish(batch: BatchInput) -> BatchResult:
        batch_result = run_batch(batch, processing_pipeline, llm_slots=llm_slots)
        if by_id:
            batch_result = replace(batch_result, results=tuple(
                replace(result, traces=(*by_id[result.record.address_id].traces, *result.traces))
                for result in batch_result.results
            ))
        return batch_result

    batch_results: list[BatchResult | None] = [None] * len(batches)
    with ThreadPoolExecutor(max_workers=min(config.parallel_batches, max(len(batches), 1))) as pool:
        pending = {pool.submit(finish, batch): index for index, batch in enumerate(batches)}
        for future in as_completed(pending):
            batch_result = future.result()
            batch_results[pending[future]] = batch_result  # 최종 결과의 배치 이력은 입력 순서를 유지한다.
            if progress is not None:  # 진행 보고는 끝난 순서대로 한다.
                progress(tuple(
                    replace(result, input_index=positions[result.record.address_id])
                    for result in with_duplicates(batch_result.results)
                ), batch_result)

    for batch_result in batch_results:
        by_id.update((result.record.address_id, result) for result in with_duplicates(batch_result.results))
    ordered_results = tuple(
        replace(by_id[record.address_id], input_index=index)
        for index, record in enumerate(records)
    )
    return DatasetResult(
        run_id, config.reference_version, ordered_results,
        dict(Counter(result.status for result in ordered_results)), tuple(batch_results),
        summarize_reviews(result.review for result in ordered_results if result.review is not None),
    )


def _run_cleaned(
    records: tuple[AddressRecord, ...], pipeline: Pipeline, *, run_id: str, config: BatchConfig,
    batch_size: int | None,
    progress: Callable[[tuple[AddressResult, ...], BatchResult | None], None] | None,
) -> DatasetResult:
    """원문을 정리해 파이프라인을 돌리고, 결과의 원문을 되돌린 뒤 바꾼 내역을 처리 이력 맨 앞에 남긴다."""
    originals: dict[str, tuple[str | None, str | None, tuple[dict, ...]]] = {}
    cleaned = []
    for record in records:
        text, changes = pipeline.input_cleaner(record.raw_address)
        if changes:
            originals[record.address_id] = (record.raw_address, text, changes)
            record = replace(record, raw_address=text, normalized_address=None)
        cleaned.append(record)

    def restore(result: AddressResult) -> AddressResult:
        entry = originals.get(result.record.address_id)
        if entry is None:
            return result
        original, text, changes = entry
        trace = ModuleTrace("completed", "input_cleaned", {"cleaned_address": text, "changes": list(changes)},
                            module_name="input_cleanup", module_version="1")
        return replace(result, record=replace(result.record, raw_address=original), traces=(trace, *result.traces))

    def restore_batch(batch: BatchResult) -> BatchResult:
        return replace(batch, results=tuple(restore(result) for result in batch.results))

    report = None
    if progress is not None:
        def report(results, batch):
            progress(tuple(restore(result) for result in results), restore_batch(batch) if batch is not None else None)

    dataset = run_dataset(tuple(cleaned), replace(pipeline, input_cleaner=None), run_id=run_id, config=config,
                          batch_size=batch_size, progress=report)
    if not originals:
        return dataset
    return replace(dataset, results=tuple(restore(result) for result in dataset.results),
                   batch_results=tuple(restore_batch(batch) for batch in dataset.batch_results))
