# 주소 검증 워크벤치

주소 한 건 또는 CSV/XLSX를 입력하고, 표준화 결과·검토 후보·판정 근거·처리 이력을 확인하는 로컬 웹 화면입니다. 브라우저 파일은 `web/`에 있으며 별도의 프론트엔드 빌드 없이 실행합니다.

## 실행

저장소 루트에서 [설치 및 설정 안내](../README.md)에 따라 준비한 뒤 실행합니다. `run.py`는 루트 `.env`를 읽습니다.

```bash
python run.py --demo
python run.py --engine new_pipeline
python run.py --engine bigquery
python run.py --engine new_pipeline --engine bigquery --port 8770
```

위 명령은 각각 별도의 실행 예시입니다. 기본 포트는 8765이고, 마지막 명령은 8770입니다. 아무 옵션 없이 실행하면 새 파이프라인을 사용합니다. 실제 데이터가 없는 경우 `--demo`로 화면을 확인하세요.

| 엔진 | 구현 | 필요한 준비 |
|---|---|---|
| 시연 | `demo.py` | 준비된 가상 사례. API 호출 없음 |
| 새 파이프라인 | `../address_pipeline/` | RAW·폐지 이력 parquet |
| BigQuery | `bigquery_engine.py`, `../bigquery/sql/` | Google 인증, 기준자료·기준표 |

## 화면과 결과

- **단일 주소 입력 / 파일 일괄 처리**: 파일은 주소 열과 선택 ID 열을 지정합니다. XLSX는 시트도 선택합니다. UTF-8·CP949 CSV와 XLSX를 읽으며, 8 MiB·16,000행·256열까지 받습니다.
- **처리 상태**: 표준화 완료, 검토 후 확정, 검토 필요, 불일치·제외·실패를 나누어 봅니다. 검토 필요에는 후보가 있는 미일치 행도 포함될 수 있습니다.
- **주소 상세**: 원문과 표준 주소, 지역 검증 경로, 후보별 순위와 근거, 단계별 처리 이력을 확인합니다. 도로명·지번의 1:N·N:1·N:M 관계도 표시합니다.
- **검토와 승인**: 거의 확정 후보는 일괄 승인, 확인 필요 후보는 개별 승인합니다. 승인 정보는 원래 판정과 별도로 기록하며 취소할 수 있습니다. 과거 자료 일치 등 승인 제외 후보도 있습니다.
- **추가 분석**: 이미 나온 후보를 OpenAI가 비교합니다. 키·모델 설정이 필요하며, 시연 모드는 준비된 설명을 반환합니다. AI 교정 활성화 스위치와 별도로 동작합니다.
- **결과 다운로드**: CSV와 JSON을 지원합니다. Excel에서 ID·코드의 앞자리 0을 유지하려면 해당 열을 텍스트로 가져오거나 JSON을 사용합니다.

작업·승인·추가 분석 기록은 서버 메모리에 보관합니다. 새로고침하면 같은 브라우저 세션의 작업을 조회할 수 있지만 서버를 재시작하면 사라집니다. 공용 브라우저 프로필은 세션을 공유합니다.

## 주요 파일

| 파일 | 역할 |
|---|---|
| `server.py` | HTTP, 엔진 선택, 브라우저 세션, 정적 파일 |
| `adapter.py` | 입력·출력 계약과 결과 직렬화 |
| `jobs.py` | 비동기 작업, 진행 상태, 검토 승인 |
| `tabular.py` | CSV/XLSX 읽기와 입력 검증 |
| `bigquery_engine.py`, `bigquery_stage.py` | BigQuery 엔진과 SQL 실행 |
| `output_fields.py` | 제출 양식 출력 열 구성 |
| `additional_analysis.py`, `openai_correct.py` | 기존 후보 비교와 AI 표기 교정 |
| `old_regions.py` | 옛 행정구역명 정리 |
| `web/` | HTML, CSS, JavaScript 화면 |

`adapter.py`에는 기존 프로젝트와의 선택적 호환 코드가 남아 있지만, 기본 실행과 두 배포 엔진은 `address_standardizer`를 불러오지 않습니다.

## 서버와 테스트

기본 서버는 `127.0.0.1`에 바인딩합니다. 외부 접속은 별도의 HTTPS 프록시가 필요합니다. 프록시가 원래 Host를 전달하도록 구성하고 `--public-origin https://실제호스트`를 지정합니다. 주소별 데이터는 서버에서 처리하며, BigQuery 실행은 입력을 클라우드 테이블에 임시 기록하고 실행 후 삭제합니다.

```bash
python -m unittest discover -s address_workbench/tests -v
node --test address_workbench/tests/*.test.mjs
```

Node.js 18 이상은 테스트에만 사용합니다. [설계 문서](DESIGN.md)에서 어댑터와 화면 경계를 확인할 수 있습니다.
