-- 0단계 준비: 프로그램이 DuckDB에 처음 연결할 때 한 번 실행한다. 표를 파일로 저장하지 않는다.
-- 기준자료는 RAW_DATA_2025.parquet(2025-12-31 기준)와 폐지 이력 parquet를 그대로 읽는다.
-- {raw_parquet}, {history_parquet}, 표 nfkc_changes(이름 → NFKC 정규화가 바뀌는 이름만)는 프로그램이 채운다.

CREATE OR REPLACE VIEW raw_2025 AS SELECT * FROM read_parquet('{raw_parquet}');

CREATE OR REPLACE VIEW address_history AS SELECT * FROM read_parquet('{history_parquet}');

-- RAW 한 행 = 건물 한 동의 지번 하나. 형 변환, 조회 키, 표준 주소 문자열을 붙인다(BigQuery 03의 raw_2025_clean과 같은 규칙).
-- 조회 키: 도로명(공백 제거), 지번 지역(리, 없으면 읍면동), 읍면동, 행정동. NFKC로 바뀌는 이름은 nfkc_changes로만 바꾼다.
CREATE OR REPLACE VIEW raw_clean_all AS
WITH typed AS (
  SELECT
    CAST("일련번호" AS BIGINT) AS serial,
    "법정동시도명" AS sido, "법정동시군구명" AS sigungu, "법정동읍면동명" AS emd, NULLIF(TRIM("법정동리명"), '') AS ri,
    "행정동명" AS admin_dong, "도로명" AS road_name,
    CAST(CAST("도로명코드" AS BIGINT) AS VARCHAR) AS road_name_code,
    CAST(CAST("법정동코드" AS BIGINT) AS VARCHAR) AS legal_dong_code,
    CAST(CAST("행정구역분류코드" AS BIGINT) AS VARCHAR) AS administrative_area_code,
    TRY_CAST("건물본번" AS BIGINT) AS main_no, IFNULL(TRY_CAST("건물부번" AS BIGINT), 0) AS sub_no,
    TRY_CAST("지번본번" AS BIGINT) AS lot_main, IFNULL(TRY_CAST("지번부번" AS BIGINT), 0) AS lot_sub,
    IFNULL(CAST("대지구분코드" AS BIGINT) = 1, FALSE) AS lot_mountain,
    "건물명" AS building_name, "시군구관리건물명" AS sigungu_building_name
  FROM raw_2025
)
SELECT t.*,
  substr(t.legal_dong_code, 1, 5) AS sigungu_code,
  regexp_replace(coalesce(cr.norm, t.road_name), '\s+', '', 'g') AS road_key,
  regexp_replace(coalesce(ca.norm, IFNULL(t.ri, t.emd)), '\s+', '', 'g') AS lot_area_key,
  regexp_replace(coalesce(ce.norm, t.emd), '\s+', '', 'g') AS emd_key,
  regexp_replace(coalesce(cd.norm, t.admin_dong), '\s+', '', 'g') AS admin_dong_key,
  if(t.main_no > 0, concat_ws(' ', t.sido, t.sigungu, if(ends_with(t.emd, '읍') OR ends_with(t.emd, '면'), t.emd, NULL),
    t.road_name, if(t.sub_no > 0, printf('%d-%d', t.main_no, t.sub_no), CAST(t.main_no AS VARCHAR))), NULL) AS standard_road_address,
  if(t.lot_main > 0, concat_ws(' ', t.sido, t.sigungu, t.emd, t.ri, if(t.lot_mountain, '산', NULL),
    if(t.lot_sub > 0, printf('%d-%d', t.lot_main, t.lot_sub), CAST(t.lot_main AS VARCHAR))), NULL) AS standard_lot_address
FROM typed t
LEFT JOIN nfkc_changes cr ON cr.raw = t.road_name
LEFT JOIN nfkc_changes ca ON ca.raw = IFNULL(t.ri, t.emd)
LEFT JOIN nfkc_changes ce ON ce.raw = t.emd
LEFT JOIN nfkc_changes cd ON cd.raw = t.admin_dong;

-- 전국 시군구 이름의 단어들(고흥군, 남구 …). 원문에 '다른 시군구'가 적혀 있는지 볼 때 쓴다(2단계 매칭).
CREATE OR REPLACE TABLE sigungu_parts AS
SELECT DISTINCT p AS name
FROM (SELECT DISTINCT unnest(string_split("법정동시군구명", ' ')) AS p FROM raw_2025
      WHERE "법정동시군구명" IS NOT NULL AND TRY_CAST("지번본번" AS BIGINT) > 0)
WHERE length(p) >= 2;

-- 건물명이 있는 시도·시군구 목록. 건물명 후보(4단계)의 찾는 범위를 고를 때 쓴다.
CREATE OR REPLACE TABLE building_regions AS
SELECT DISTINCT "법정동시도명" AS sido, "법정동시군구명" AS sigungu FROM raw_2025
WHERE TRY_CAST("건물본번" AS BIGINT) > 0
  AND (TRIM(IFNULL("건물명", '')) != '' OR TRIM(IFNULL("시군구관리건물명", '')) != '');

-- 시군구 코드별 시도·시군구 이름과, 원문에서 그 시군구를 알아보는 표기(단어, 3자 이상이면 끝 글자 뺀 형태도).
-- 실행 범위(2_scope_reference)를 '원문에 적힌 지역'으로 좁힐 때 쓴다. 2단계 매칭의 시군구 확인(any_part_in)과 같은 규칙이다.
CREATE OR REPLACE TABLE sigungu_regions AS
SELECT DISTINCT substr(CAST(CAST("법정동코드" AS BIGINT) AS VARCHAR), 1, 5) AS sigungu_code,
  "법정동시도명" AS sido, "법정동시군구명" AS sigungu
FROM raw_2025;

CREATE OR REPLACE TABLE sigungu_forms AS
SELECT DISTINCT sigungu_code, form FROM (
  SELECT r.sigungu_code, unnest([p, if(length(p) >= 3, substr(p, 1, length(p) - 1), NULL)]) AS form
  FROM (SELECT sigungu_code, unnest(string_split(sigungu, ' ')) AS p FROM sigungu_regions WHERE sigungu IS NOT NULL) r
) WHERE form IS NOT NULL;
