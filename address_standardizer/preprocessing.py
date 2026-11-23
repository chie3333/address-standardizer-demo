"""주소를 정규화하고 파싱 전에 제외·보류 대상을 선별한다."""

from dataclasses import replace
import re
from unicodedata import normalize

from pandas import isna

from .contracts import (
    AddressRecord, AddressResult, Module, ModuleResult, ModuleTrace, PreprocessOutput, RunContext,
)


_REGION = r"(?:[가-힣]+도 )?(?:[가-힣]+[시군구] ){1,2}(?:[가-힣]+[읍면] )?"
_ADDRESS_NUMBER = r"[0-9]+(?:-[0-9]+)?"
_LOT_ADDRESS = rf"{_REGION}[가-힣0-9·]+[동리가] (?:산 *)?{_ADDRESS_NUMBER}(?: *번지)?"
_ROAD_DETAIL = r"(?:(?: *, *| +)(?:지하 *)?[0-9]+ *(?:동|층|호))*"
_ROAD_ADDRESS = rf"{_REGION}[가-힣0-9·]+(?:대로|로|길) {_ADDRESS_NUMBER}{_ROAD_DETAIL}"
_ADDRESS_SEPARATOR = r"(?: +| *[,/;(] *)"
_COMBINED_ADDRESS = re.compile(
    rf"(?:{_LOT_ADDRESS}{_ADDRESS_SEPARATOR}{_ROAD_ADDRESS}"
    rf"|{_ROAD_ADDRESS}{_ADDRESS_SEPARATOR}{_LOT_ADDRESS})(?=$|[ ,/;()])"
)


def normalize_spacing(raw: str) -> str:
    """공백과 숫자 사이 하이픈 표기만 정리한다."""
    text = " ".join(raw.split())
    return re.sub(r"(?<=\d)\s*[-‐‑‒–−]\s*(?=\d)", "-", text)


def normalize_address(raw: str) -> str:
    return normalize_spacing(normalize("NFKC", raw))


def _screen_text(text: str, *, check_combined_address: bool = False) -> ModuleResult[PreprocessOutput]:
    output = PreprocessOutput("passed", normalized_address=text)
    if not text:
        output = PreprocessOutput("excluded", "blank_input")
    elif text.isdecimal():
        output = PreprocessOutput("excluded", "numeric_only")
    elif re.search(r"사\s*서\s*함", text):
        output = PreprocessOutput("excluded", "po_box")
    elif check_combined_address and _COMBINED_ADDRESS.match(text):
        output = PreprocessOutput(
            "held", "multiple_addresses", normalized_address=text, review_tag="복수의 주소 확인 필요",
        )
    details = {"screening_status": output.status}
    if output.review_tag is not None:
        details["review_tag"] = output.review_tag
    return ModuleResult(output, ModuleTrace("completed", output.reason_code, details))


def prepare_address(
    context: RunContext, record: AddressRecord,
) -> ModuleResult[PreprocessOutput]:
    """중복 판정 전에는 원문의 공백과 하이픈 표기를 정리한다."""
    raw = record.raw_address
    text = "" if isna(raw) else normalize_spacing(str(raw))
    return _screen_text(text)


def preprocess_address(
    context: RunContext, record: AddressRecord,
) -> ModuleResult[PreprocessOutput]:
    """중복 선별 후 정규화하고 지번·도로명 병기 행을 보류한다."""
    raw = record.raw_address
    text = "" if isna(raw) else normalize_address(str(raw))
    return _screen_text(text, check_combined_address=True)


SPACING_PREPROCESSOR = Module("1", prepare_address)
DEFAULT_PREPROCESSOR = Module("6", preprocess_address)


def exclude_duplicates(results: tuple[AddressResult, ...]) -> tuple[AddressResult, ...]:
    """원문의 공백·하이픈을 정리해 비교하고 입력 순서상 첫 주소를 유지한다."""
    representatives: dict[str, str] = {}
    screened = []
    for result in results:
        if result.status == "preprocess_passed":
            record = result.record
            output = result.output
            assert isinstance(output, PreprocessOutput)
            key = normalize_spacing(str(record.raw_address))
            representative = representatives.setdefault(key, record.address_id)
            if representative != record.address_id:
                trace = replace(
                    result.traces[-1], reason_code="duplicate_input",
                    details={
                        **result.traces[-1].details, "screening_status": "excluded",
                        "representative_address_id": representative,
                    },
                )
                result = replace(
                    result, status="excluded", traces=(*result.traces[:-1], trace),
                    output=PreprocessOutput("excluded", "duplicate_input", representative),
                )
        screened.append(result)
    return tuple(screened)
