-- 건물명 사전: raw_2025에서 건물명·시군구관리건물명이 있는 행만 뽑아 이름 × 건물(도로명코드+본번+부번+법정동) 단위로 모은다.
--
-- name_norm  로컬 파이프라인의 _compare_name과 같은 규칙: NFKC, 소문자, 공백 제거, a.p.t/apt → 아파트, 제N차 → N차
-- name_core  name_norm 끝의 건물 종류(아파트·맨션·빌라 등)를 뗀 이름. 부분 일치·접미어 차이 비교용
-- same_name_in_sigungu / same_name_nationwide  같은 name_norm을 가진 서로 다른 도로명주소 수
--
-- 실행: bq query --use_legacy_sql=false < bigquery/sql/01_building_names.sql
CREATE OR REPLACE TABLE `your-gcp-project.address.building_names`
CLUSTER BY sigungu_code, name_core
OPTIONS (description = 'raw_2025 건물명 사전. 이름 × 건물 단위, 정규화 이름과 동명 수 포함')
AS
WITH named AS (
  SELECT
    x.name AS original_name,
    x.source AS name_source,
    r.`법정동시도명` AS sido,
    r.`법정동시군구명` AS sigungu,
    r.`법정동읍면동명` AS emd,
    r.`법정동리명` AS ri,
    r.`행정동명` AS admin_dong,
    CAST(CAST(r.`법정동코드` AS INT64) AS STRING) AS legal_dong_code,
    CAST(CAST(r.`도로명코드` AS INT64) AS STRING) AS road_name_code,
    CAST(CAST(r.`행정구역분류코드` AS INT64) AS STRING) AS administrative_area_code,
    r.`도로명` AS road_name,
    SAFE_CAST(r.`건물본번` AS INT64) AS main_no,
    IFNULL(SAFE_CAST(r.`건물부번` AS INT64), 0) AS sub_no,
    SAFE_CAST(r.`지번본번` AS INT64) AS lot_main,
    IFNULL(SAFE_CAST(r.`지번부번` AS INT64), 0) AS lot_sub,
    CAST(r.`대지구분코드` AS INT64) = 1 AS lot_mountain,
    CAST(r.`일련번호` AS INT64) AS serial
  FROM `your-gcp-project.address.raw_2025` r,
    UNNEST([STRUCT(r.`건물명` AS name, '건물명' AS source),
            STRUCT(r.`시군구관리건물명` AS name, '시군구관리건물명' AS source)]) x
  WHERE x.name IS NOT NULL AND TRIM(x.name) != ''
),
normed AS (
  SELECT *,
    REGEXP_REPLACE(
      REPLACE(REPLACE(REGEXP_REPLACE(LOWER(NORMALIZE(original_name, NFKC)), r'\s+', ''), 'a.p.t', '아파트'), 'apt', '아파트'),
      r'제([0-9]+)차', r'\1차') AS name_norm,
    SUBSTR(legal_dong_code, 1, 5) AS sigungu_code,
    ARRAY_TO_STRING([sido, sigungu, IF(ENDS_WITH(emd, '읍') OR ENDS_WITH(emd, '면'), emd, NULL), road_name,
      IF(sub_no > 0, FORMAT('%d-%d', main_no, sub_no), CAST(main_no AS STRING))], ' ') AS standard_road_address,
    IF(lot_main > 0, ARRAY_TO_STRING([sido, sigungu, emd, NULLIF(ri, ''), IF(lot_mountain, '산', NULL),
      IF(lot_sub > 0, FORMAT('%d-%d', lot_main, lot_sub), CAST(lot_main AS STRING))], ' '), NULL) AS lot_address
  FROM named
  WHERE main_no > 0
),
buildings AS (
  SELECT
    name_norm,
    REGEXP_REPLACE(name_norm, r'(아파트|맨션|빌라트|빌라|연립주택|연립|다세대주택|다세대|오피스텔|빌딩)$', '') AS name_core,
    ARRAY_AGG(DISTINCT original_name ORDER BY original_name) AS original_names,
    ARRAY_AGG(DISTINCT name_source ORDER BY name_source) AS name_sources,
    ANY_VALUE(sido) AS sido, ANY_VALUE(sigungu) AS sigungu, sigungu_code,
    ANY_VALUE(emd) AS emd, ANY_VALUE(ri) AS ri, legal_dong_code,
    road_name_code, ANY_VALUE(road_name) AS road_name, main_no, sub_no,
    ANY_VALUE(standard_road_address) AS standard_road_address,
    ARRAY_AGG(DISTINCT administrative_area_code IGNORE NULLS) AS administrative_area_codes,
    ARRAY_AGG(DISTINCT admin_dong IGNORE NULLS) AS admin_dongs,
    ARRAY_AGG(DISTINCT lot_address IGNORE NULLS) AS lot_addresses,
    ARRAY_AGG(DISTINCT serial ORDER BY serial) AS raw_serials
  FROM normed
  GROUP BY name_norm, sigungu_code, legal_dong_code, road_name_code, main_no, sub_no
),
in_sigungu AS (
  SELECT name_norm, sigungu_code, COUNT(DISTINCT standard_road_address) AS same_name_in_sigungu
  FROM buildings GROUP BY name_norm, sigungu_code
),
nationwide AS (
  SELECT name_norm, COUNT(DISTINCT standard_road_address) AS same_name_nationwide
  FROM buildings GROUP BY name_norm
)
SELECT b.*, CHAR_LENGTH(b.name_norm) AS name_length,
  -- 건물 고유 이름이 아니라 용도·동 표기(주택, 농막, 창고, 마을회관, 1동, 가동 등)인 이름. 매칭 근거로 쓰지 않는다.
  REGEXP_CONTAINS(b.name_norm, r'^(주택|단독주택|다세대주택|다가구주택|연립주택|공동주택|농막|창고|농협창고|마을회관|경로당|소매점|축사|우사|돈사|계사|제실|재실|제각|공중화장실|화장실|가로판매대|구두수선대|음식점|일반음식점|상가|공장|사무소|사무실|교회|민박|철거|슈퍼|정자|컨테이너|비닐하우스|상점|정미소|주유소|농협|관리사|부속건물|근린생활시설|([0-9]+|[a-z]|[가나다라마바사아자차카타파하])동|주[0-9]+동)$') AS is_generic,
  s.same_name_in_sigungu, n.same_name_nationwide
FROM buildings b
JOIN in_sigungu s USING (name_norm, sigungu_code)
JOIN nationwide n USING (name_norm);
