# 주소 표준화 기준자료

워크벤치 기본 파이프라인(`src/address/frontend/address_workbench/standardizer.py`)이 기본 경로로 읽는 원본 Parquet이다. 지역·도로명 카탈로그 `address_catalog.duckdb`는 같은 폴더의 [`address_reference_lite/`](address_reference_lite/)에 있다.

| 파일 | 크기 | 내용 |
|---|---|---|
| `RAW_DATA_2025.parquet` | 약 87 MB | 제공 자료 원천 주소(2025-12-31 기준). 기준연도 상세 검색에 쓴다 |
| `RAW_DATA_2024.parquet` | 약 87 MB | 과거 기준자료. `과거 기준자료 일치` 판정에 쓴다 |
| `RAW_DATA_2023.parquet` | 약 87 MB | 과거 기준자료 |
| `ADDRESS_HISTORY_202410_202512.parquet` | 약 9.5 MB | 2024년 10월~2025년 12월 도로명주소 월별 변동 이력. 신규·수정·폐지 이력 조회에 활용 |

다른 위치의 자료를 쓰려면 `ADDRESS_CATALOG`, `ADDRESS_RAW_DIR` 환경 변수로 바꾼다.

DuckDB 엔진의 기준표 파일(`address_engine.duckdb`, 약 2.5 GB)은 GitHub 파일 크기 제한(100 MB)을 넘어 올리지 않는다. 이 파일은 `RAW_DATA_2025.parquet`과 `src/address/data/ADDRESS_HISTORY_202410_202512.parquet`으로 다시 만들 수 있다.
