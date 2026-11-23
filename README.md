# 주소 표준화 워크벤치

한국 주소를 기준자료와 대조하고, 확정 결과와 검토 후보를 확인하는 로컬 웹 애플리케이션입니다. 주소 한 건 또는 CSV/XLSX를 입력하고, 판정 근거·후보 비교·처리 이력을 확인하며 결과를 CSV/JSON으로 내려받을 수 있습니다.

이 저장소는 **워크벤치 + 주소 정제·표준화 파이프라인 + BigQuery 엔진**으로 구성한 시연용 배포본입니다. 사용자가 지정한 원본 `src/address/data`도 `data/`에 함께 포함합니다. 실제 인증 정보는 포함하지 않습니다. GitHub 링크는 코드와 안내를 공유하는 용도이며, 앱은 아래 명령으로 각자의 컴퓨터에서 실행합니다.

## 1. 준비된 예시로 화면 보기

Python 3.11 이상에서 실행합니다. Git이 없다면 GitHub의 **Code → Download ZIP**으로 받아 압축을 푼 폴더에서 실행해도 됩니다.

```bash
git clone https://github.com/chie3333/address-standardizer-demo.git
cd address-standardizer-demo
python3 run.py --demo
```

Windows에서는 `python3` 대신 `python`을 사용합니다. 브라우저에서 **http://127.0.0.1:8765**를 엽니다. 종료는 터미널에서 `Ctrl+C`입니다.

`.env`가 없는 최초 상태의 시연 모드는 Python 표준 라이브러리만 사용하며, 기준자료·Google 인증·OpenAI 키가 필요 없습니다. **단일 주소 입력 → 샘플 주소 넣기** 또는 화면의 시연 데이터를 이용하세요. 준비된 가상 주소만 처리하며 다른 주소는 `skipped`로 표시합니다. XLSX 파일을 읽으려면 아래 패키지를 설치해야 합니다.

## 2. 실제 처리용 설치와 설정

저장소 루트에서 가상환경을 만듭니다.

```bash
python3 -m venv .venv
```

macOS/Linux:

```bash
source .venv/bin/activate
python -m pip install -r address_workbench/requirements-bigquery.txt
cp .env.example .env
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r address_workbench/requirements-bigquery.txt
Copy-Item .env.example .env
```

가상환경 활성화가 제한되면 `.venv` 안의 Python 실행 파일로 명령을 실행할 수 있습니다. 로컬 파이프라인만 쓸 때는 `requirements-bigquery.txt` 대신 `address_workbench/requirements.txt`만 설치해도 됩니다.

`.env`를 열어 사용할 엔진의 설정을 채웁니다. **`python run.py`가 루트 `.env`를 읽습니다.** 운영체제에 이미 설정된 환경 변수가 우선하며, 상대 경로는 저장소 루트를 기준으로 합니다. `python -m address_workbench.server`는 루트 `.env`를 자동으로 읽지 않습니다.

| 실행 모드 | 준비할 항목 |
|---|---|
| `--demo` | 준비된 가상 사례. 실제 주소 검색·AI 호출 없음 |
| `--engine pipeline` | 카탈로그 DB, 연도별 RAW parquet, OpenAI 또는 Ollama 파서 |
| `--engine bigquery` | Google 인증, 실제 GCP 프로젝트·데이터세트, 준비된 기준 테이블 |
| AI 교정·추가 분석 | 본인의 `OPENAI_API_KEY`, `OPENAI_MODEL` |

## 3. 주소 표준화 파이프라인으로 실제 주소 처리

[capstone_preparation의 `src/address/pipeline`](https://github.com/chie3333/capstone_preparation/tree/be7b3c2cd1ab3740d7eaafa4d18ab58a2dffeb62/src/address/pipeline)을 가져왔습니다. 원본 Python 코드의 내용을 유지하고, 워크벤치의 import 이름에 맞춰 **`address_standardizer/`**에 배치했습니다. 원본 기준 커밋은 `be7b3c2cd1ab3740d7eaafa4d18ab58a2dffeb62`입니다.

지정한 원본의 `src/address/data` 폴더를 그대로 `data/`에 포함했습니다. 파이프라인에서 사용하는 카탈로그와 연도별 RAW는 아래 위치에 있으며, 폐지 이력·검증 사례도 원본 폴더에 포함된 상태로 함께 제공합니다.

```text
data/
├── address_reference_lite/
│   └── address_catalog.duckdb
├── RAW_DATA_2025.parquet
├── RAW_DATA_2024.parquet
├── RAW_DATA_2023.parquet
├── ADDRESS_HISTORY_202410_202512.parquet
└── test/
```

`.env`의 `ADDRESS_CATALOG`, `ADDRESS_RAW_DIR`, `ADDRESS_YEAR`로 경로와 기준연도를 지정합니다. 기본 `ADDRESS_HISTORY=2024,2023`은 과거 연도 조회를 함께 사용합니다. 과거 자료가 없다면 **`ADDRESS_HISTORY=`로 비워** 기준연도 RAW와 카탈로그만 사용합니다. 이 파이프라인에는 별도의 폐지 이력 parquet가 필요하지 않습니다.

파서는 다음 중 하나를 준비합니다. 이 파이프라인은 주소 명칭 추출에 LLM을 사용하므로 실제 처리에 파서 연결이 필요합니다.

- **OpenAI**: `.env`에 `ADDRESS_PARSER=openai`, 본인의 `OPENAI_API_KEY`와 `OPENAI_MODEL`을 지정합니다. 요청에 따른 API 비용이 발생할 수 있습니다.
- **Ollama**: Ollama 서버를 실행하고 사용할 모델을 내려받은 뒤 `ADDRESS_PARSER=ollama`, `ADDRESS_OLLAMA_MODEL=설치한모델태그`로 지정합니다. 기본 서버 주소는 `127.0.0.1:11434`입니다.

```bash
python run.py --engine pipeline
```

실행 순서는 입력 정리·전처리 → LLM 명칭 추출 → 지역 검증 → 상세주소 검색 → 표준화·검토 후보 구성입니다. `ADDRESS_INPUT_CLEANUP=0`이면 선택적 입력 정리를 끄고, `ADDRESS_DETAIL_RECOVERY=1`이면 상세 단계 LLM 검색 가설을 추가로 사용합니다. 상세 단계 복구는 기본값 `0`입니다.

## 4. BigQuery 엔진으로 실제 주소 처리

Google Cloud CLI를 설치하고, BigQuery가 활성화되고 결제가 연결된 프로젝트 및 데이터세트 사용 권한을 준비합니다. 로컬 앱의 인증은 다음 명령으로 설정합니다.

```bash
gcloud auth application-default login
```

`.env`의 `ADDRESS_BQ_PROJECT`를 실제 프로젝트 ID로 바꾸고, `ADDRESS_BQ_DATASET`과 `ADDRESS_BQ_LOCATION`을 지정합니다. 새 프로젝트에는 기준자료 적재와 기준표 생성이 먼저 필요합니다. **[BigQuery 준비 안내](bigquery/README.md)**에 순서와 SQL 적용 방법을 정리했습니다. 이미 준비된 공유 데이터세트는 접근 권한을 받은 뒤 연결할 수 있습니다.

```bash
python run.py --engine bigquery
```

BigQuery는 조회와 입력 테이블 기록·정리에 필요한 권한을 사용하며 실행에 따른 비용이 발생할 수 있습니다. 기본 예시는 `ADDRESS_BQ_AI=0`으로 AI 교정을 끕니다. OpenAI 교정은 키·모델 설정 후 `ADDRESS_BQ_AI=1`로 켭니다. Gemini 방식은 별도 BigQuery 연결 권한과 모델 설정이 필요합니다.

두 엔진을 한 화면에서 비교하려면 각각의 준비를 마친 뒤 실행합니다.

```bash
python run.py --engine pipeline --engine bigquery --port 8770
```

http://127.0.0.1:8770 에서 **처리 엔진**을 선택합니다. 아무 엔진 옵션 없이 실행하면 주소 표준화 파이프라인이 기본입니다. 시연 모드와 실제 엔진은 한 번에 함께 선택하지 않습니다.

## 5. 워크벤치 사용

1. **단일 주소 입력**에 주소를 넣거나 **파일 일괄 처리**에서 CSV/XLSX를 선택합니다.
2. 파일의 주소 열과 필요하면 ID 열·시트를 고릅니다. 파일 크기는 8 MiB, 데이터는 머리글 외 16,000행·256열까지입니다.
3. 처리 상태에서 확정·검토 필요·미일치 등을 고르고 주소별 근거와 후보를 확인합니다.
4. 검토 후보를 승인하거나 CSV/JSON으로 결과를 내려받습니다. ID와 코드의 앞자리 0을 유지하려면 JSON 또는 Excel의 텍스트 가져오기를 사용합니다.

**추가 분석**은 기존 후보를 AI가 비교하는 별도 기능으로, AI 교정 스위치와 별개로 OpenAI 키와 모델이 필요합니다. 시연 모드는 준비된 설명을 사용합니다.

실행 기록과 승인은 서버 메모리에 보관하므로 서버를 종료하면 사라집니다. 필요한 결과는 먼저 내려받으세요. 서버는 기본적으로 `127.0.0.1`에 바인딩합니다.

## 구성

```text
address_workbench/   웹 화면, HTTP 서버, 작업·승인, 결과 변환, BigQuery 어댑터
address_standardizer/ 지정한 원본 pipeline 코드: LLM 파싱·지역 검증·상세주소 검색
bigquery/           BigQuery SQL, 규칙 문서, 입출력 보조 도구
data/               지정한 원본의 기준자료·카탈로그·검증 사례
run.py              루트 .env를 읽는 실행 스크립트
.env.example        경로·엔진·AI 설정 예시
```

파이프라인은 지정한 원본을 `address_standardizer`라는 패키지 이름으로 사용합니다. 세부 설명은 [워크벤치](address_workbench/README.md), [파이프라인](address_standardizer/README.md), [BigQuery](bigquery/README.md)를 참고하세요.

## 확인과 문제 해결

```bash
python -m unittest discover -s address_workbench/tests -v
node --test address_workbench/tests/*.test.mjs
```

Node.js는 프론트엔드 테스트에만 필요합니다. 별도 기준자료가 필요한 테스트는 파일이 없으면 건너뜁니다. 지정한 원본 pipeline 폴더에는 테스트 파일이 포함되어 있지 않습니다. 실제 기준자료·Google 인증 없이 실행한 단위 테스트가 BigQuery 실서비스 연결을 보장하지는 않습니다.

| 증상 | 확인할 내용 |
|---|---|
| `기준자료 파일이 없습니다` | GitHub에서 data 폴더까지 받았는지와 `.env`의 경로 확인 |
| BigQuery 인증·권한·테이블 오류 | ADC 로그인, 프로젝트 ID, 리전, 테이블 준비·권한 확인 |
| OpenAI 키·모델 오류 | `.env` 또는 `ADDRESS_OPENAI_ENV`의 설정 확인. 별도 설정 파일을 지정했다면 그 파일의 값 사용 |
| 설정을 바꿨는데 반영되지 않음 | `python run.py`로 실행했는지 확인하고 서버 재시작. 기존 운영체제 환경 변수도 확인 |
| 포트가 이미 사용 중 | `python run.py --demo --port 8771`처럼 다른 포트 지정 |

실제 `.env`, API 키, 서비스 계정 JSON과 새로 생성한 처리 결과는 `.gitignore`로 제외합니다. `data/`에는 사용자가 요청한 원본 기준자료와 검증 사례만 명시적으로 포함합니다.
