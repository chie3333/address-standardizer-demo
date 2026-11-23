# 워크벤치 구조

브라우저는 로컬 HTTP 서버에 주소 또는 파일을 제출합니다. 서버가 선택한 엔진의 어댑터를 호출하고, 공통 결과 형식으로 상태·근거·검토 후보를 표시합니다.

```text
web/ → server.py → tabular.py / InputRecord
                  → jobs.py → DemoAdapter / AddressPipeline / BigQueryEngineAdapter
                  → 결과·승인·추가 분석 → CSV / JSON
```

## 어댑터 계약

- 입력은 `InputRecord(address_id, raw_address)`의 튜플입니다.
- `run_dataset(records, *, run_id)`는 `AdapterResult(dataset, candidate_comparisons)`를 반환합니다.
- `reference_version`으로 기준자료 버전을 표시합니다. 엔진에 따라 진행 콜백과 `annotate_rows`를 추가로 제공합니다.
- 엔진은 원본 ID·주소·행 순서를 보존합니다. 화면은 판정과 검토 승인 정보를 따로 관리합니다.
- 선택적 `candidate_comparisons`는 주소 ID와 후보 인덱스로 구성요소별 일치·불일치·미확인 근거를 연결합니다.

## 엔진 경계

`server.py`의 `ENGINES`에 새 파이프라인과 BigQuery 엔진을 등록합니다. 시연 모드는 준비된 가상 결과만 반환합니다. 새 파이프라인은 로컬 parquet와 DuckDB 라이브러리를, BigQuery 엔진은 클라우드 테이블과 `bigquery/sql/`을 사용합니다. 두 엔진 모두 같은 화면·승인·내려받기를 사용합니다.

`adapter.py`의 기존 `PipelineAdapter`는 선택적 호환 코드이며 기본 실행에 쓰이지 않습니다. 해당 코드가 요구하는 기존 `address_standardizer`는 이 저장소에 포함하지 않습니다.

## 작업과 세션

`jobs.py`는 서버 메모리에 작업 결과와 승인을 보관합니다. 브라우저는 서버가 발급한 서명된 세션 쿠키로 작업 상태를 조회합니다. 같은 세션의 새로고침은 실행 중인 작업을 이어서 조회하고, 서버 재시작은 기록을 초기화합니다.

HTTP 서버는 루프백에 바인딩하고 동일 출처 요청을 확인합니다. 정적 화면은 외부 CDN 없이 실행합니다. 입력 파일을 영구 저장하지 않으며 엔진에 따라 BigQuery·OpenAI로 입력이나 후보를 전송합니다. 로컬 사용 설정과 외부 HTTPS 프록시의 연결 설정은 [실행 안내](../README.md)를 따릅니다.
