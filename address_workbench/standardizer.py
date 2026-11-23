"""address_standardizer 파이프라인을 워크벤치 기본 어댑터로 구성한다.

파서는 같은 폴더의 parser.env에서 선택한다. 같은 이름의 환경 변수가 우선한다.
    ADDRESS_PARSER       openai 또는 ollama (기본 openai)
    ADDRESS_OLLAMA_MODEL Ollama 모델 태그 (기본 qwen3:1.7b)

경로와 기준연도는 환경 변수로 바꿀 수 있다.
    ADDRESS_CATALOG      address_catalog.duckdb 경로 (기본 data/address_reference_lite/address_catalog.duckdb)
    ADDRESS_RAW_DIR      RAW_DATA_YYYY.parquet 이 있는 폴더 (기본 data)
    ADDRESS_YEAR         기준연도 (기본 2025)
    ADDRESS_HISTORY      과거 기준연도, 쉼표 구분 (기본 "2024,2023", 빈 값이면 사용 안 함)
    ADDRESS_BATCH_SIZE   run_dataset 배치 크기 (기본 480, 화면 진행률 단위)
    ADDRESS_OPENAI_ENV   별도 OpenAI 설정 파일 (미지정 시 환경 변수 또는 address_standardizer/.env.openai)
    ADDRESS_INPUT_CLEANUP 0이면 파서 앞 원문 정리 규칙(input_cleanup)을 끈다 (기본 켬)
"""
from __future__ import annotations

import os
from pathlib import Path

from .adapter import PipelineAdapter

ROOT = Path(__file__).resolve().parent.parent
# 독립 배포본의 기준자료 기본 위치는 저장소 루트의 data/이다.
DEFAULT_RAW_DIR = ROOT / "data"
DEFAULT_CATALOG = DEFAULT_RAW_DIR / "address_reference_lite" / "address_catalog.duckdb"
DEFAULT_OPENAI_ENV = ROOT / "address_standardizer" / ".env.openai"
PARSER_SETTINGS = Path(__file__).with_name("parser.env")


def create_adapter() -> PipelineAdapter:
    from dotenv import dotenv_values

    from address_standardizer import DEFAULT_PREPROCESSOR, Pipeline, make_openai_token_only_parser
    from address_standardizer.contracts import BatchConfig
    from address_standardizer.detail_address import DetailAddressSearch
    from address_standardizer.input_cleanup import clean_for_matching
    from address_standardizer.local_ollama_token_parser import MODEL_NAME, make_local_ollama_token_parser

    settings = dotenv_values(PARSER_SETTINGS, interpolate=False) if PARSER_SETTINGS.is_file() else {}
    provider = os.environ.get("ADDRESS_PARSER", settings.get("ADDRESS_PARSER") or "openai").strip().lower()
    if provider not in {"openai", "ollama"}:
        raise ValueError("ADDRESS_PARSER는 openai 또는 ollama로 지정하세요.")

    catalog = Path(os.environ.get("ADDRESS_CATALOG", DEFAULT_CATALOG))
    raw_dir = Path(os.environ.get("ADDRESS_RAW_DIR", DEFAULT_RAW_DIR))
    configured_env = os.environ.get("ADDRESS_OPENAI_ENV")
    openai_env = Path(configured_env) if configured_env else (DEFAULT_OPENAI_ENV if DEFAULT_OPENAI_ENV.is_file() else None)
    year = int(os.environ.get("ADDRESS_YEAR", "2025"))
    history = sorted({int(y) for y in os.environ.get("ADDRESS_HISTORY", "2024,2023").split(",") if y.strip()},
                     reverse=True)
    # 화면이 진행률을 받는 단위. 배치가 작으면 배치마다 반복되는 LLM 대기(파싱→검증→재시도)와
    # 기준자료 조회(배치당 약 2초)가 늘어난다. 요청당 24·동시 배치 3·동시 요청 16과 맞춘 값이다.
    batch_size = os.environ.get("ADDRESS_BATCH_SIZE", "480")

    paths = [catalog, *(raw_dir / f"RAW_DATA_{y}.parquet" for y in (year, *history))]
    if provider == "openai" and openai_env is not None:
        paths.append(openai_env)
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("필요한 기준자료 또는 설정 파일이 없습니다: " + ", ".join(missing))

    batch_config = None
    if provider == "ollama":
        model = os.environ.get("ADDRESS_OLLAMA_MODEL", settings.get("ADDRESS_OLLAMA_MODEL") or MODEL_NAME)
        parser = make_local_ollama_token_parser(model=model)
        # 동시 요청 수는 지정하지 않고 Ollama 파서의 기본값(DEFAULT_OLLAMA_CONCURRENCY)을 따른다.
        batch_config = BatchConfig(str(year), llm_batch_size=8)
    else:
        parser = make_openai_token_only_parser(openai_env)

    def search(y: int) -> DetailAddressSearch:
        return DetailAddressSearch(catalog, raw_dir / f"RAW_DATA_{y}.parquet", year=y)

    detail_recovery_generate = None
    if os.environ.get("ADDRESS_DETAIL_RECOVERY", "0") == "1":
        if provider == "ollama":
            from address_standardizer.detail_llm_recovery import ollama_detail_generator
            detail_recovery_generate = ollama_detail_generator(model=model)
        else:
            from address_standardizer.detail_llm_recovery import openai_detail_generator
            detail_recovery_generate = openai_detail_generator(openai_env)

    pipeline = Pipeline(
        parser=parser,
        preprocessor=DEFAULT_PREPROCESSOR,
        detail_search=search(year),
        review_history=tuple(search(y) for y in history),
        detail_recovery_generate=detail_recovery_generate,
        input_cleaner=None if os.environ.get("ADDRESS_INPUT_CLEANUP", "1") == "0" else clean_for_matching,
    )
    return PipelineAdapter(pipeline, str(year), int(batch_size) if batch_size else None, batch_config=batch_config)
