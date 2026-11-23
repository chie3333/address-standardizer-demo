# 주소 정제·표준화 파이프라인

출처: [capstone_preparation/src/address/pipeline](https://github.com/chie3333/capstone_preparation/tree/be7b3c2cd1ab3740d7eaafa4d18ab58a2dffeb62/src/address/pipeline)

기준 커밋: `be7b3c2cd1ab3740d7eaafa4d18ab58a2dffeb62`. 이 폴더의 Python 파일은 해당 원본과 같은 내용입니다. 워크벤치에서 사용하는 Python 패키지 이름을 유지하기 위해 원본 `pipeline/`을 `address_standardizer/`에 배치했습니다. 세부 알고리즘 설명과 원본 환경의 예시는 [원본 README](https://github.com/chie3333/capstone_preparation/blob/be7b3c2cd1ab3740d7eaafa4d18ab58a2dffeb62/src/address/pipeline/README.md)를 참고하세요.

## 실행

저장소 루트의 [실행 안내](../README.md)에 따라 설치하고 `.env.example`을 `.env`로 복사합니다. 파이프라인은 주소 명칭 추출에 OpenAI 또는 Ollama를 사용합니다. 기준자료와 파서 연결을 준비한 뒤 실행합니다.

```bash
python run.py --engine pipeline
```

워크벤치 어댑터는 `address_workbench/standardizer.py`입니다. 루트 `.env`에서 읽은 환경 변수를 기본 OpenAI 설정으로 사용하며, `ADDRESS_OPENAI_ENV`를 지정하면 해당 dotenv 파일을 사용합니다. `ADDRESS_PARSER=ollama`이면 로컬 Ollama 서버와 `ADDRESS_OLLAMA_MODEL`에 지정한 모델을 사용합니다.

| 설정 | 역할 |
|---|---|
| `ADDRESS_CATALOG` | `address_catalog.duckdb` 기준 카탈로그 |
| `ADDRESS_RAW_DIR` | `RAW_DATA_YYYY.parquet` 파일이 있는 폴더 |
| `ADDRESS_YEAR` | 기준연도, 기본 2025 |
| `ADDRESS_HISTORY` | 과거 조회 연도, 기본 `2024,2023`. 빈 값은 과거 조회 안 함 |
| `ADDRESS_PARSER` | `openai` 또는 `ollama` |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | OpenAI 파서를 사용할 때 필요한 키와 모델 |
| `ADDRESS_BATCH_SIZE` | 워크벤치 처리 배치 크기, 기본 480 |
| `ADDRESS_INPUT_CLEANUP` | 입력 정리 사용 여부, 기본 1 |
| `ADDRESS_DETAIL_RECOVERY` | 상세 단계 LLM 검색 가설 사용 여부, 기본 0 |

## 주요 파일

| 파일 | 역할 |
|---|---|
| `runner.py`, `contracts.py`, `_execution.py` | 단계 연결, 입출력 계약과 실행 이력 |
| `data_input.py`, `input_cleanup.py`, `preprocessing.py` | 입력, 파서 앞 정리, 전처리 |
| `token_only_parser.py`, `local_ollama_token_parser.py` | LLM 명칭 추출과 원문 대조 |
| `region_validation.py` | 기준 카탈로그의 명칭과 지역 경로 검증 |
| `detail_address.py` | 번호·상세주소의 RAW 대조 |
| `standardization.py`, `review_candidates.py` | 표준주소와 검토 후보 구성 |
| `detail_llm_recovery.py` | 선택적 LLM 검색 가설과 DB 재검증 |

원본 폴더에 없는 legacy·Excel 도구·문서·테스트 파일은 추가로 가져오지 않았습니다. 워크벤치와의 연결·직렬화 테스트는 루트에서 `python -m unittest discover -s address_workbench/tests -v`로 실행합니다.
