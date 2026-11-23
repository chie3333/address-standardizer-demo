# BigQuery 엔진 준비

워크벤치의 BigQuery 엔진은 SQL로 주소를 대조하고, 건물명·폐지 이력·번호 오타 후보를 검토 결과로 표시합니다. [규칙 문서](RULES.md)에 판정 기준을 정리했습니다.

## 1. 프로젝트와 인증

BigQuery가 활성화되고 결제가 연결된 GCP 프로젝트를 준비합니다. 아래 예시의 프로젝트 `your-gcp-project`, 데이터세트 `address`, 리전 `asia-northeast3`를 본인 환경에 맞춥니다. 앱은 루트 `.env`의 `ADDRESS_BQ_PROJECT`, `ADDRESS_BQ_DATASET`, `ADDRESS_BQ_LOCATION`을 읽습니다.

```bash
gcloud auth application-default login
```

실행 계정에는 쿼리 작업 실행 권한과 데이터세트 읽기·쓰기 권한이 필요합니다. 워크벤치가 입력 테이블을 생성·적재·정리하기 때문입니다. 테이블을 이미 준비한 다른 프로젝트를 사용할 때도 이 권한이 필요합니다.

## 2. 기준자료와 테이블 준비 (처음 한 번)

이 저장소에는 원본 데이터가 없습니다. 다음 순서로 **BigQuery 콘솔**에서 준비합니다.

1. [sql/00_tables.sql](sql/00_tables.sql)의 `your-gcp-project.address`를 실제 프로젝트·데이터세트로 바꿔 실행합니다. 데이터세트와 입력·결과 테이블을 만듭니다. 새 데이터세트 생성 리전은 SQL 안의 `location = 'asia-northeast3'`도 맞춰야 합니다. 기존 데이터세트의 리전과 앱의 리전을 일치시키세요.
2. 원본 `RAW_DATA_2025.parquet`을 같은 데이터세트의 **`raw_2025`** 테이블로 적재합니다. 원본의 한글 열 이름과 자료형을 유지합니다. Parquet 적재를 사용하고 CSV로 변환하지 않습니다. 로컬 업로드가 제한되면 같은 리전의 Cloud Storage를 통해 적재합니다.
3. `ADDRESS_HISTORY_202410_202512.parquet`을 **`address_history`** 테이블로 적재합니다. 도로명주소 월변동분의 원본 이력 스키마가 필요합니다.
4. [sql/03_reference_tables.sql](sql/03_reference_tables.sql)의 프로젝트·데이터세트를 바꾸고 실행합니다. 매칭용 `raw_2025_clean`, `ref_road`, `ref_lot`, `ref_lot_keys`를 만듭니다.
5. [sql/01_building_names.sql](sql/01_building_names.sql)의 프로젝트·데이터세트를 바꾸고 실행합니다. `building_names`를 만듭니다.

03·01 SQL은 기준표를 `CREATE OR REPLACE`로 다시 생성합니다. 기준자료를 교체할 때 다시 실행합니다. 기존 공유 환경에서는 관리 담당자와 기준표 갱신 시점을 맞추세요.

| 객체 | 역할 |
|---|---|
| `raw_2025` | RAW 원본 기준자료 |
| `address_history` | 폐지·변동 이력 |
| `raw_2025_clean` | 형 변환한 기준자료 |
| `ref_road` | 도로명·건물번호 기준표 |
| `ref_lot`, `ref_lot_keys` | 지번 기준표와 검색 키 |
| `building_names` | 정규화한 건물명 사전 |
| `input_raw`, `pipeline_results` | 실행 입력과 보조 결과 적재 |

원본 자료의 열 구성은 `03_reference_tables.sql`, `01_building_names.sql`, `09_abolished_addresses.sql`의 참조 열을 기준으로 합니다. 이름만 같은 다른 parquet는 사용할 수 없습니다.

SQL 파일은 배포용 기본 이름 `your-gcp-project.address`를 사용합니다. **앱에서 실행되는 SQL은 `.env`의 프로젝트·데이터세트로 자동 치환합니다.** 콘솔에서 직접 실행하는 준비 SQL에는 위 설명대로 실제 이름을 넣어야 합니다. 워크벤치 엔진은 04의 파서를 실행별 임시 함수로 만들기 때문에 파서 함수를 별도로 배포할 필요가 없습니다.

## 3. 워크벤치 실행

저장소 루트에서 [설치](../README.md)를 마치고 실행합니다.

```bash
python run.py --engine bigquery
```

기본 포트는 8765입니다. 실제 주소를 입력해 판정과 후보를 확인합니다. 연결 확인은 실제 입력 처리 시 이루어지므로, 화면이 열리는 것만으로 인증·기준표 준비가 검증되지는 않습니다.

입력은 `input_raw`에 `wb-실행ID` 묶음으로 잠시 적재하며, 엔진은 처리가 끝나면 이번 실행의 입력을 정리합니다. 실행 계정은 기준표 읽기와 입력 적재·삭제가 가능해야 합니다. 쿼리·저장 및 선택적 AI 호출 비용은 연결한 프로젝트와 API 계정에 발생합니다.

## 선택적 AI 교정

`.env`의 기본 예시는 `ADDRESS_BQ_AI=0`입니다. OpenAI 교정은 `OPENAI_API_KEY`, `OPENAI_MODEL`을 채우고 `ADDRESS_BQ_AI=1`, `ADDRESS_BQ_AI_PROVIDER=openai`로 실행합니다. 교정한 주소를 다시 기준자료로 검증하고 자동 확정 대신 검토 후보로 제시합니다.

`ADDRESS_BQ_AI_MAX`는 실행당 AI 교정 최대 행 수입니다. `ADDRESS_BQ_AI_BATCH`, `ADDRESS_BQ_AI_CONCURRENCY`, `ADDRESS_BQ_AI_CHUNK`의 기본값은 각각 24·16·480입니다. Gemini 방식(`gemini`)은 [07 SQL](sql/07_ai_correct.sql)을 사용하며 별도의 BigQuery AI 연결·권한·모델 준비가 필요합니다.

## SQL 및 보조 도구

| 파일 | 역할 |
|---|---|
| `sql/00_tables.sql` | 데이터세트·입출력 테이블 |
| `sql/01_building_names.sql` | 건물명 사전 |
| `sql/02_building_lookup_examples.sql` | 건물명 조회 예시 |
| `sql/03_reference_tables.sql` | 매칭용 기준표 |
| `sql/04_parse_function.sql` | 원문 후보 추출 함수 |
| `sql/05_standardize_all.sql`, `sql/06_engine_details.sql` | 주소 매칭과 상세 근거 |
| `sql/07_ai_correct.sql` | 선택적 Gemini 교정 |
| `sql/08_building_candidates.sql` | 건물명 후보 |
| `sql/09_abolished_addresses.sql` | 폐지 주소·연결 지번 이력 |
| `sql/10_number_typo_candidates.sql` | 번호 오타 후보 |
| `export_input.py`, `export_results.py` | 입력 XLSX·워크벤치 결과 JSON의 CSV 변환 |
| `schema_pipeline_results.json` | 보조 결과 적재 스키마 |

실제 입력·결과 파일은 저장소에 포함하지 않습니다. 규칙·직렬화 단위 테스트는 실제 BigQuery 연결 없이 실행합니다.
