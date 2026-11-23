"""신규 주소 정제 모듈의 공통 입출력 규격."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Semaphore
from typing import TYPE_CHECKING, Callable, Generic, Literal, TypedDict, TypeVar

from pandas import DataFrame

if TYPE_CHECKING:
    from .review_candidates import AddressReview

RAW_DATA_COLUMNS = {
    "sido": "법정동시도명",
    "sigungu": "법정동시군구명",
    "legal_emd": "법정동읍면동명",
    "ri": "법정동리명",
    "admin_dong": "행정동명",
    "land_type": "대지구분코드",
    "lot_main_no": "지번본번",
    "lot_sub_no": "지번부번",
    "road_name": "도로명",
    "building_main_no": "건물본번",
    "building_sub_no": "건물부번",
    "road_name_code": "도로명코드",
    "legal_dong_code": "법정동코드",
    "administrative_area_code": "행정구역분류코드",
}

STANDARD_ADDRESS_COLUMNS = tuple(RAW_DATA_COLUMNS[key] for key in (
    "sido", "sigungu", "legal_emd", "road_name", "building_main_no", "building_sub_no",
    "road_name_code", "legal_dong_code", "administrative_area_code",
))


_REGION_FIELDS = ("sido", "sigungu", "legal_emd", "ri", "admin_dong")
_LOT_FIELDS = ("land_type", "lot_main_no", "lot_sub_no")
_ROAD_FIELDS = ("road_name", "building_main_no", "building_sub_no")
LOOKUP_COLUMNS_BY_TYPE = {
    "lot": tuple(RAW_DATA_COLUMNS[key] for key in (*_REGION_FIELDS, *_LOT_FIELDS)),
    "road": tuple(RAW_DATA_COLUMNS[key] for key in (*_REGION_FIELDS, *_ROAD_FIELDS)),
}
LOOKUP_COLUMN_ORDER = tuple(dict.fromkeys(
    (*LOOKUP_COLUMNS_BY_TYPE["lot"], *LOOKUP_COLUMNS_BY_TYPE["road"]),
))
NUMBER_COLUMNS = frozenset(
    RAW_DATA_COLUMNS[key] for key in (*_LOT_FIELDS, "building_main_no", "building_sub_no")
)

T = TypeVar("T")
Input = TypeVar("Input", contravariant=True)
Output = TypeVar("Output")


@dataclass(frozen=True)
class AddressRecord:
    address_id: str
    raw_address: str | None
    normalized_address: str | None = None


@dataclass(frozen=True)
class BatchConfig:
    reference_version: str
    max_concurrency: int = 4
    llm_max_concurrency: int | None = None
    # 2026-11-13 오류 검증 1,000건 실측: 8·20·24·32의 정확도는 같고, 24는 8보다 주소당 토큰 약 43% 절감.
    llm_batch_size: int = 24
    # run_dataset에서 동시에 진행할 실행 배치 수. 한 배치가 DB를 조회하는 동안 다른 배치가 LLM을 호출한다.
    # LLM 동시 요청 한도(llm_max_concurrency)는 겹친 배치들이 함께 나눠 쓴다. 동시 요청 16에서는 3개여야
    # 요청 자리가 찬다(4,000행 실측: 배치 2개 70초, 3개 45초).
    parallel_batches: int = 3

    def __post_init__(self) -> None:
        if self.max_concurrency < 1 or (
            self.llm_max_concurrency is not None and self.llm_max_concurrency < 1
        ):
            raise ValueError("동시 처리 한도는 양수여야 합니다.")
        if self.llm_batch_size < 1:
            raise ValueError("LLM 요청당 주소 수는 양수여야 합니다.")
        if self.parallel_batches < 1:
            raise ValueError("동시에 진행할 배치 수는 양수여야 합니다.")


@dataclass(frozen=True)
class BatchInput:
    run_id: str
    records: tuple[AddressRecord, ...]
    config: BatchConfig


@dataclass(frozen=True)
class RunContext:
    run_id: str
    record: AddressRecord
    input_index: int
    config: BatchConfig
    _llm_slots: Semaphore = field(repr=False, compare=False)

    def call_llm(self, call: Callable[[], T]) -> T:
        """배치의 공통 LLM 동시 호출 한도를 적용한다."""
        with self._llm_slots:
            return call()


@dataclass(frozen=True)
class ModuleTrace:
    status: Literal["completed", "skipped", "failed"]
    reason_code: str | None = None
    details: dict[str, object] = field(default_factory=dict)
    module_name: str = ""
    module_version: str = ""
    elapsed_ms: float = 0


@dataclass(frozen=True)
class ModuleResult(Generic[T]):
    output: T | None
    trace: ModuleTrace


@dataclass(frozen=True)
class Module(Generic[Input, Output]):
    version: str
    run: Callable[[RunContext, Input], ModuleResult[Output]]
    run_many: Callable[[tuple[RunContext, ...]], tuple[ModuleResult[Output], ...]] | None = None
    llm_default_concurrency: int = 2
    retry_parser: Module[Input, Output] | None = None


@dataclass(frozen=True)
class PreprocessOutput:
    status: Literal["passed", "excluded", "held"]
    reason_code: Literal[
        "blank_input", "numeric_only", "po_box", "duplicate_input", "multiple_addresses",
    ] | None = None
    representative_address_id: str | None = None
    normalized_address: str | None = None
    review_tag: str | None = None


@dataclass(frozen=True)
class TokenLabel:
    token_index: int
    token: str
    column: str
    value: str


@dataclass(frozen=True)
class ParseCandidate:
    candidate_id: str
    address_type: Literal["lot", "road"]
    observed_components: dict[str, str]
    name_candidates: dict[str, tuple[str, ...]] = field(default_factory=dict)
    lot_main_no: str | None = None
    lot_sub_no: str | None = None
    is_mountain: bool | None = None
    building_main_no: str | None = None
    building_sub_no: str | None = None
    token_labels: tuple[TokenLabel, ...] = ()


class _RequiredLabeledToken(TypedDict):
    raw: str
    labels: list[str]


class LabeledToken(_RequiredLabeledToken, total=False):
    corrected: str


@dataclass(frozen=True)
class LeadingNameCandidate:
    token_index: int
    segment_index: int
    name: str


@dataclass(frozen=True)
class RoadJoinCandidate:
    token_index: int
    raw: str
    name: str


@dataclass(frozen=True)
class ParseOutput:
    screening_result: Literal["passed", "blank_input", "numeric_only", "po_box"]
    address_candidates: tuple[ParseCandidate, ...] = ()
    unresolved_segments: tuple[str, ...] = ()
    tokens: list[LabeledToken] = field(default_factory=list)
    leading_name_candidates: tuple[LeadingNameCandidate, ...] = ()
    road_join_candidates: tuple[RoadJoinCandidate, ...] = ()


@dataclass(frozen=True)
class RegionNode:
    node_id: str
    node_type: str
    name: str


@dataclass(frozen=True)
class HierarchyCandidate:
    source_candidate: ParseCandidate
    node_path: tuple[RegionNode, ...]
    raw_record_ids: tuple[str, ...] = ()
    raw_rows: tuple[RawAddressRow, ...] = ()


@dataclass(frozen=True)
class RawAddressRow:
    record_id: str
    columns: dict[str, str | int | float | None]


@dataclass(frozen=True)
class StandardAddress:
    standard_road_address: str
    road_name_code: str
    legal_dong_code: str
    administrative_area_code: str


@dataclass(frozen=True)
class StandardizationOutput:
    status: Literal["resolved", "held"]
    address: StandardAddress | None = None
    reason_code: str | None = None
    raw_record_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class HierarchyOutput:
    search_status: Literal["found", "no_match", "ambiguous", "no_label_candidates"]
    hierarchy_candidates: tuple[HierarchyCandidate, ...] = ()
    confirmed_token_labels: tuple[TokenLabel, ...] = ()


@dataclass(frozen=True)
class ScopeMatch:
    scope_id: str
    raw_record_ids: tuple[str, ...] = ()
    node_path: tuple[RegionNode, ...] = ()
    raw_rows: tuple[RawAddressRow, ...] = ()


@dataclass(frozen=True)
class LookupGroup:
    query_group_id: str
    scope_id: str | None
    column: str
    values: tuple[str, ...]
    terminal_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class HierarchySearch:
    version: str
    lookup_group: Callable[
        [BatchConfig, LookupGroup],
        ModuleResult[dict[str, ScopeMatch]],
    ]

@dataclass(frozen=True)
class ValidatedCandidate:
    hierarchy_candidate: HierarchyCandidate
    reference_record_id: str
    standard_address: str
    codes: dict[str, str]


@dataclass(frozen=True)
class ValidationOutput:
    validation_status: Literal["matched", "multiple_matches", "no_match", "conflict"]
    validated_candidates: tuple[ValidatedCandidate, ...] = ()


@dataclass(frozen=True)
class ResolutionOutput:
    resolution_status: Literal["resolved", "held"]
    selected_candidate: ValidatedCandidate | None = None
    reason_code: str | None = None


class _RequiredRegionValidationResult(TypedDict):
    address_id: str
    reference_year: int
    status: Literal["unique_path", "multiple_paths", "no_match", "no_region_tokens", "held"]
    paths: list[list[dict[str, str | None]]]


class RegionValidationResult(_RequiredRegionValidationResult, total=False):
    used_correction: bool
    db_labels: list[list[str]]
    reason_code: str
    # 전체 경로가 없을 때 성립하는 일부 경로와 남은 토큰의 유사 지명 (validate_batch(suggest=True)).
    partial_match: dict


@dataclass(frozen=True)
class DetailRecoveryHypothesis:
    address: str
    reason: str


@dataclass(frozen=True)
class DetailRecoveryEvidence:
    hypothesis: DetailRecoveryHypothesis
    validation: str  # db_matched / unverified / rejected_number_changed / lookup_failed
    reason: str = ""
    standard_address: StandardAddress | None = None
    raw_record_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AddressResult:
    record: AddressRecord
    input_index: int
    status: str
    traces: tuple[ModuleTrace, ...]
    output: PreprocessOutput | ParseOutput | HierarchyOutput | ValidationOutput | ResolutionOutput | None
    standardization: StandardizationOutput | None = None
    region_validation: RegionValidationResult | None = None
    review: AddressReview | None = None
    # 원문이 앞 행과 같아 처리하지 않고 그 대표 행의 결과를 물려받았으면 대표 행의 address_id.
    inherited_from: str | None = None
    # Hypotheses are never treated as authoritative until verified by the original DB lookup.
    detail_recovery: tuple[DetailRecoveryEvidence, ...] = ()


@dataclass(frozen=True)
class BatchResult:
    run_id: str
    reference_version: str
    results: tuple[AddressResult, ...]
    summary: dict[str, int]
    parsed_df: DataFrame = field(repr=False, compare=False)
    search_traces: tuple[ModuleTrace, ...] = ()
    review_summary: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class DatasetResult:
    run_id: str
    reference_version: str
    results: tuple[AddressResult, ...]
    summary: dict[str, int]
    batch_results: tuple[BatchResult, ...] = field(repr=False, compare=False)
    review_summary: dict[str, dict[str, int]] = field(default_factory=dict)
