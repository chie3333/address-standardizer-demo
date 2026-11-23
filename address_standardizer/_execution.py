"""파이프라인 단계 호출과 실행이력 수집."""

from dataclasses import replace
from time import perf_counter
from typing import Callable, TypeVar

from .contracts import ModuleResult, ModuleTrace


O = TypeVar("O")


def invoke(
    name: str, version: str, call: Callable[[], ModuleResult[O]],
) -> ModuleResult[O]:
    started = perf_counter()
    try:
        result = call()
        if result.trace.status == "completed" and result.output is None:
            raise ValueError("완료된 모듈은 output을 반환해야 합니다.")
        output, trace = result.output, result.trace
    except Exception as exc:
        output = None
        trace = ModuleTrace("failed", "module_exception", {"error_type": type(exc).__name__})
    return ModuleResult(output, replace(
        trace, module_name=name, module_version=version,
        elapsed_ms=(perf_counter() - started) * 1000,
    ))

