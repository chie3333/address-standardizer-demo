-- raw_2025를 매칭용 기준표 두 개로 정리한다.
--   ref_road  도로명주소 건물 단위: 도로명(공백 제거) + 건물본번 + 건물부번 → 표준 도로명주소와 코드
--   ref_lot   지번 단위: 리(없으면 읍면동) 이름 + 산 여부 + 지번본번 + 지번부번 → 그 지번에 있는 도로명주소들
-- 실행: bq query --use_legacy_sql=false < bigquery/sql/03_reference_tables.sql
CREATE OR REPLACE TABLE `your-gcp-project.address.raw_2025_clean`
CLUSTER BY sigungu_code
OPTIONS (description = 'raw_2025 형 변환본: 코드는 문자열, 번호는 정수, 표준 주소 문자열 포함')
AS
WITH typed AS (
  SELECT
    CAST(`일련번호` AS INT64) AS serial,
    `법정동시도명` AS sido, `법정동시군구명` AS sigungu, `법정동읍면동명` AS emd, NULLIF(TRIM(`법정동리명`), '') AS ri,
    `행정동명` AS admin_dong, `도로명` AS road_name,
    CAST(CAST(`도로명코드` AS INT64) AS STRING) AS road_name_code,
    CAST(CAST(`법정동코드` AS INT64) AS STRING) AS legal_dong_code,
    CAST(CAST(`행정구역분류코드` AS INT64) AS STRING) AS administrative_area_code,
    SAFE_CAST(`건물본번` AS INT64) AS main_no, IFNULL(SAFE_CAST(`건물부번` AS INT64), 0) AS sub_no,
    SAFE_CAST(`지번본번` AS INT64) AS lot_main, IFNULL(SAFE_CAST(`지번부번` AS INT64), 0) AS lot_sub,
    IFNULL(CAST(`대지구분코드` AS INT64) = 1, FALSE) AS lot_mountain,
    `건물명` AS building_name, `시군구관리건물명` AS sigungu_building_name
  FROM `your-gcp-project.address.raw_2025`
)
SELECT *,
  SUBSTR(legal_dong_code, 1, 5) AS sigungu_code,
  REGEXP_REPLACE(NORMALIZE(road_name, NFKC), r'\s+', '') AS road_key,
  REGEXP_REPLACE(NORMALIZE(IFNULL(ri, emd), NFKC), r'\s+', '') AS lot_area_key,
  IF(main_no > 0, ARRAY_TO_STRING([sido, sigungu, IF(ENDS_WITH(emd, '읍') OR ENDS_WITH(emd, '면'), emd, NULL), road_name,
    IF(sub_no > 0, FORMAT('%d-%d', main_no, sub_no), CAST(main_no AS STRING))], ' '), NULL) AS standard_road_address,
  IF(lot_main > 0, ARRAY_TO_STRING([sido, sigungu, emd, ri, IF(lot_mountain, '산', NULL),
    IF(lot_sub > 0, FORMAT('%d-%d', lot_main, lot_sub), CAST(lot_main AS STRING))], ' '), NULL) AS standard_lot_address
FROM typed;

CREATE OR REPLACE TABLE `your-gcp-project.address.ref_road`
CLUSTER BY road_key, main_no
OPTIONS (description = '도로명주소 건물 단위 기준표')
AS
SELECT road_key, main_no, sub_no, sigungu_code, legal_dong_code,
  ANY_VALUE(sido) AS sido, ANY_VALUE(sigungu) AS sigungu, ANY_VALUE(emd) AS emd, ANY_VALUE(ri) AS ri,
  ANY_VALUE(road_name_code) AS road_name_code, ANY_VALUE(standard_road_address) AS standard_road_address,
  ARRAY_AGG(DISTINCT administrative_area_code IGNORE NULLS) AS administrative_area_codes,
  ARRAY_AGG(DISTINCT standard_lot_address IGNORE NULLS) AS lot_addresses,
  ARRAY_AGG(DISTINCT admin_dong IGNORE NULLS) AS admin_dongs,
  ARRAY_AGG(serial ORDER BY serial) AS serials
FROM `your-gcp-project.address.raw_2025_clean`
WHERE main_no > 0 AND road_key IS NOT NULL
GROUP BY road_key, main_no, sub_no, sigungu_code, legal_dong_code;

CREATE OR REPLACE TABLE `your-gcp-project.address.ref_lot`
CLUSTER BY lot_area_key, lot_main
OPTIONS (description = '지번 단위 기준표. 건물이 있는 지번만 있다(RAW가 건물 기준 자료)')
AS
SELECT lot_area_key, lot_mountain, lot_main, lot_sub, sigungu_code, legal_dong_code,
  ANY_VALUE(sido) AS sido, ANY_VALUE(sigungu) AS sigungu, ANY_VALUE(emd) AS emd, ANY_VALUE(ri) AS ri,
  ANY_VALUE(standard_lot_address) AS standard_lot_address,
  ARRAY_AGG(DISTINCT standard_road_address IGNORE NULLS) AS road_addresses,
  ARRAY_AGG(DISTINCT road_name_code IGNORE NULLS) AS road_name_codes,
  ARRAY_AGG(DISTINCT administrative_area_code IGNORE NULLS) AS administrative_area_codes,
  ARRAY_AGG(serial ORDER BY serial) AS serials
FROM `your-gcp-project.address.raw_2025_clean`
WHERE lot_main > 0
GROUP BY lot_area_key, lot_mountain, lot_main, lot_sub, sigungu_code, legal_dong_code;

-- 지번 조회 키를 세 단계로 둔다. 원문이 리를 빼고 읍·면만 쓰거나(주문진읍 295-4), 법정동 대신 행정동(일도1동)을 써도 찾는다.
--   법정리 / 법정동  리 이름(없으면 법정동 이름)   ← ref_lot과 같은 키
--   읍면            리가 있는 행의 읍·면 이름        ← 같은 지번이 여러 리에 있으면 복수 후보가 된다
--   행정동          행정동 이름(법정동 이름과 다를 때)
CREATE OR REPLACE TABLE `your-gcp-project.address.ref_lot_keys`
CLUSTER BY lot_key, lot_main
OPTIONS (description = '지번 조회 키(법정리·법정동·읍면·행정동) 단위 기준표')
AS
WITH keyed AS (
  SELECT r.*, k.lot_key, k.key_type
  FROM `your-gcp-project.address.raw_2025_clean` r,
    UNNEST([
      STRUCT(r.lot_area_key AS lot_key, IF(r.ri IS NULL, '법정동', '법정리') AS key_type),
      STRUCT(IF(r.ri IS NOT NULL, REGEXP_REPLACE(NORMALIZE(r.emd, NFKC), r'\s+', ''), NULL), '읍면'),
      STRUCT(IF(r.admin_dong IS NOT NULL AND r.admin_dong != r.emd,
                REGEXP_REPLACE(NORMALIZE(r.admin_dong, NFKC), r'\s+', ''), NULL), '행정동')
    ]) k
  WHERE r.lot_main > 0 AND k.lot_key IS NOT NULL
)
SELECT lot_key, key_type, lot_mountain, lot_main, lot_sub, sigungu_code, legal_dong_code,
  ANY_VALUE(sido) AS sido, ANY_VALUE(sigungu) AS sigungu, ANY_VALUE(emd) AS emd, ANY_VALUE(ri) AS ri,
  ANY_VALUE(standard_lot_address) AS standard_lot_address,
  ARRAY_AGG(DISTINCT standard_road_address IGNORE NULLS) AS road_addresses
FROM keyed
GROUP BY lot_key, key_type, lot_mountain, lot_main, lot_sub, sigungu_code, legal_dong_code;
