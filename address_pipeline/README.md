# 새 주소 표준화 파이프라인

규칙으로 정확히 맞는 것부터 확정하고, 남은 행만 후보 도구 → LLM 순으로 내려보내는 파이프라인입니다.
BigQuery 엔진에서 정리한 규칙(`bigquery/RULES.md`의 A~M)을 단계 순서대로 다시 짰습니다.
기준자료는 **RAW_DATA_2025.parquet(2025-12-31 기준)를 그대로 읽습니다.** 미리 만들어 두는 기준표 파일(2.5 GB)이 없습니다.
실행할 때마다 원문에 나온 이름의 RAW 행만 골라 그 범위의 기준표를 메모리에 만듭니다.

## 단계 순서 (`pipeline.py`)

| 단계 | 파일 | 하는 일 | 규칙 |
|---|---|---|---|
| 0 입력·정리 | `stage0_prepare.py` | 옛 지명, 영문·오타 시도, 반복 제거, 복수 주소 나누기, 어순 정리 | A1~A8 |
| 1 원문 분석 | `stage123_match.py`, `sql/2_match.sql` 첫 문장 | 도로명·지번 '이름 + 번호' 후보 (LLM 없이) | B1~B9 |
| 2 정확 매칭 | `sql/2_scope_reference.sql`, `sql/2_match.sql` | 필요한 RAW 행만 골라 범위 기준표 → 번호·지역 정확 일치 | C, D1~D9 |
| 3 판정 | `sql/2_match.sql`, `sql/3_details.sql` | 확정·지번만·검토·미일치, 복수 주소·지역 불완전은 검토로 | E, F, G, L1 |
| 4 후보 도구 | `stage4_candidates.py`, `sql/4_*.sql` | 건물명 → 폐지 주소·연결 지번이 빠진 건물 → 번호 오타 → 붙여쓰기 | H, I, J, K |
| 5a LLM 원문 구분 | `stage5_llm.py`, `llm_spans.py` | 후보 없는 행: 주소·건물명·동층호 구간 표시 → 규칙으로 다시 | 새 단계 |
| 5b LLM 교정 | `stage5_llm.py` | 그래도 후보 없는 행: 표기 오타 교정 문장 → 다시 매칭 | M |
| 6 결과 | `stage6_output.py` | 옛 지명 기록, 요약, 제출 양식 열 | |

- **확정은 2·3단계의 정확 일치에서만 나옵니다.** 4·5단계는 검토 후보만 답니다.
- **LLM 결과는 항상 규칙으로 다시 확인합니다.** 5a는 원문 글자 그대로인 구간만 받고(`llm_spans.verified`), 그 구간을 다시 매칭하거나 건물명 검색에 넣습니다.
- 단계마다 걸린 시간, 새로 후보가 생긴 행 수, 판정별 건수를 `batch_results[0].stage_log`에 남깁니다. 어느 규칙이 이 순서에서 실제로 몇 건을 처리하는지 볼 수 있습니다.
- 결과 반영 규칙(증거 문장·후보 순위·빠른 승인 단계)은 BigQuery 엔진에서 복사한 [rules.py](rules.py)를 씁니다. `address_workbench`의 엔진 파일(bigquery_engine·openai_correct·old_regions·duckdb_functions)은 가져오지 않고 이 폴더의 복사본을 쓰며, 워크벤치 연결 규격(`adapter`, `output_fields`)만 가져옵니다. 두 쪽은 따로 고칩니다.

## 시군구 없이 쓴 주소 (2026-11-19)

원문에 시군구가 없으면 지역 표기 불완전(F2)으로 보류하던 규칙에 예외를 두었습니다. 아래를 **모두** 만족하면 확정합니다
(`sql/2_match.sql`의 `nationwide_counts`, `unique_without_sigungu`). BigQuery·DuckDB 엔진의 05에도 같은 규칙을 넣었습니다
(그쪽은 전국 기준표 `ref_road`·`ref_lot_keys`로 전국 개수를 셉니다).

1. 도로명(또는 리·동·산) + 본번 + 부번이 기준자료와 정확히 일치한다.
2. 원문에 시군구·시 이름은 없지만, 시도 이름이나 읍면동 **전체 이름**이 적혀 있다. 끝 글자를 뗀 형태는 인정하지 않는다(상남로의 '상남' ≠ 상남면).
3. 적힌 지역이 하나도 어긋나지 않는다. 다른 시도·시군구가 적혀 있으면 안 되고, 읍·면이 적혀 있으면 같은 읍·면이어야 한다.
4. 같은 이름 + 번호인 건물(지번)이 **전국에 하나뿐**이다. 원문 지역과 상관없이 RAW 전체에서 센다(엄격).
5. 복수 주소가 아니다.

확정 사유에는 "원문에 시군구 없음: … 전국에 하나뿐 … 확정"이 붙습니다.
- 정답셋 1,000건: 정확 확정 650 → 660 / 666, 틀린 확정 0, 보류가 정답인 행의 확정 31(변화 없음)
- 전체 15,988건: 18건이 새로 확정

## 실행

저장소 루트에서 패키지 설치와 `.env` 설정을 마친 뒤 실행합니다. 새 파이프라인은 로컬 DuckDB 라이브러리를 사용하며 별도 `duckdb/` 코드 폴더나 미리 만든 기준표 파일은 필요하지 않습니다.

```bash
python run.py --engine new_pipeline --engine bigquery --port 8770
```

워크벤치 **처리 엔진**에서 "새 파이프라인"을 고릅니다. 필요한 파일은 RAW parquet(87 MB)와 폐지 이력 parquet(`data/ADDRESS_HISTORY_202410_202512.parquet`, 9.5 MB)뿐입니다. 두 데이터 파일은 저장소에 포함하지 않으며 별도로 준비합니다. 경로와 설정은 [루트 실행 안내](../README.md)를 따릅니다. BigQuery를 함께 선택하려면 해당 엔진의 인증·기준표도 준비해야 합니다. 로컬 파이프라인만 실행하려면 `python run.py --engine new_pipeline`을 사용합니다.

| 환경 변수 | 기본값 |
|---|---|
| `ADDRESS_RAW_PARQUET` | `data/RAW_DATA_2025.parquet` |
| `ADDRESS_HISTORY_PARQUET` | `data/ADDRESS_HISTORY_202410_202512.parquet` |
| `ADDRESS_PIPELINE_LLM` | 코드 기본값 `1`, 배포용 `.env.example`은 `0`. 0이면 5단계를 건너뜀. `run.py`로 실행하면 루트 `.env`의 `OPENAI_API_KEY`, `OPENAI_MODEL` 사용 가능 |
| `ADDRESS_PIPELINE_LLM_MAX` | `3000` (5a·5b 각각 LLM으로 보낼 최대 행 수) |
| `ADDRESS_PIPELINE_MEMORY` | `4GB` (DuckDB 메모리 상한) |

테스트:

```bash
python -m unittest discover -s address_pipeline/tests -t .
```

## DuckDB 2.0 주의

DuckDB 2.0은 큰 쿼리 안에서 조인 결과를 이어서 거를 때 가끔 내부 오류(`dict-surviving cache … changed identity`)를 내고 연결을 못 쓰게 만듭니다.
- 오류가 나던 곳(매칭 조인 `match_hits`, 번호 오타 조인 `typo_raw_hits`)은 조인 결과를 먼저 임시 표로 두게 나눴습니다. 그 뒤 정답셋 10번, 전체 데이터 3번 실행에서 한 번도 나지 않았습니다.
- 그래도 나면 연결을 새로 열고 그 실행을 처음부터 다시 합니다(최대 3번, `batch_results[0].attempts`·`retried_statements`). LLM 응답은 저장해 두었다가 다시 쓰므로 LLM을 또 부르지 않습니다.

- **09 연결 지번이 빠진 건물 (후보로 보여 줌)**: 지번으로 찾은 폐지 기록의 건물이 지금(2025-12-31 RAW)도 있으면, 폐지된 주소로 보지 않고 그 건물을 **'연결 지번이 빠진 건물'** 검토 후보(1순위, 검토 필요, 빠른 승인 대상 아님)로 답니다. 근거에 원문 지번이 언제 연결 지번에서 빠졌는지와 그 건물의 지금 연결 지번을 적습니다. 예: 완도군 고금면 덕동리 581 → 충무사길 68(지금 덕동리 578). 전체 15,988건에서 1건입니다.
