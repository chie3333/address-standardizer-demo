-- BigQuery 단계가 쓰는 데이터세트와 적재 테이블. 이미 있으면 그대로 둔다.
-- 실행: bq query --use_legacy_sql=false < bigquery/sql/00_tables.sql
CREATE SCHEMA IF NOT EXISTS `your-gcp-project.address`
OPTIONS (location = 'asia-northeast3', description = '주소 표준화 캡스톤: 파이프라인 실행 결과와 분석');

CREATE TABLE IF NOT EXISTS `your-gcp-project.address.input_raw` (
  batch STRING NOT NULL OPTIONS (description = '입력 묶음, 예: 202512'),
  address_id STRING NOT NULL OPTIONS (description = '원본 일련번호'),
  road_address STRING OPTIONS (description = '원본 STDIZBFRADDR(도로명주소) 그대로'),
  lot_address STRING OPTIONS (description = '원본 STADDADDR(지번주소) 그대로'),
  source_row INT64 OPTIONS (description = '원본 엑셀 행 번호')
)
CLUSTER BY batch
OPTIONS (description = '농림어업DB 주소표준화 대상 원문. 묶음(batch)마다 추가');

CREATE TABLE IF NOT EXISTS `your-gcp-project.address.pipeline_results` (
  run_id STRING NOT NULL, loaded_at TIMESTAMP, reference_year STRING, address_id STRING, input_index INT64,
  raw_address STRING, status STRING, excluded_reason STRING, failed_reason STRING, review_type STRING,
  base_reason STRING, review_status STRING, tier STRING, candidate_count INT64, top_candidate STRING,
  confirmed_lot STRING, sido STRING, sigungu STRING, standard_road_address STRING, road_name_code STRING,
  legal_dong_code STRING, administrative_area_code STRING
)
CLUSTER BY run_id, status
OPTIONS (description = '워크벤치 실행 결과. 주소당 한 행, 실행(run_id)마다 추가');
