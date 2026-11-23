-- 4단계 후보 도구 H 건물명 준비: 4_buildings.sql 첫 문장이 정한 범위(building_scope의 시도·시군구)의 건물명 사전만
-- RAW에서 임시 표 building_names로 만든다. 규칙은 bigquery/sql/01_building_names.sql과 같다.
--   name_norm  NFKC, 소문자, 공백 제거, a.p.t/apt → 아파트, 제N차 → N차
--   name_core  name_norm 끝의 건물 종류(아파트·맨션·빌라 등)를 뗀 이름
--   same_name_in_sigungu  같은 시군구에서 같은 name_norm을 가진 서로 다른 도로명주소 수
CREATE OR REPLACE TEMP TABLE building_names AS
WITH scope AS (
  SELECT DISTINCT scope_sido AS sido, IFNULL(scope_sigungu, '') AS sigungu FROM building_scope
),
named AS (
  SELECT
    x.name AS original_name, x.source AS name_source,
    r."법정동시도명" AS sido, r."법정동시군구명" AS sigungu, r."법정동읍면동명" AS emd, r."법정동리명" AS ri,
    r."행정동명" AS admin_dong,
    CAST(CAST(r."법정동코드" AS BIGINT) AS VARCHAR) AS legal_dong_code,
    CAST(CAST(r."도로명코드" AS BIGINT) AS VARCHAR) AS road_name_code,
    CAST(CAST(r."행정구역분류코드" AS BIGINT) AS VARCHAR) AS administrative_area_code,
    r."도로명" AS road_name,
    TRY_CAST(r."건물본번" AS BIGINT) AS main_no, IFNULL(TRY_CAST(r."건물부번" AS BIGINT), 0) AS sub_no,
    TRY_CAST(r."지번본번" AS BIGINT) AS lot_main, IFNULL(TRY_CAST(r."지번부번" AS BIGINT), 0) AS lot_sub,
    CAST(r."대지구분코드" AS BIGINT) = 1 AS lot_mountain,
    CAST(r."일련번호" AS BIGINT) AS serial
  FROM raw_2025 r
  JOIN scope s ON s.sido = r."법정동시도명" AND s.sigungu = IFNULL(r."법정동시군구명", ''),
    unnest([{'name': r."건물명", 'source': '건물명'}, {'name': r."시군구관리건물명", 'source': '시군구관리건물명'}]) AS u(x)
  WHERE x.name IS NOT NULL AND TRIM(x.name) != ''
),
normed AS (
  SELECT n.*,
    regexp_replace(
      replace(replace(regexp_replace(lower(coalesce(k.norm, n.original_name)), '\s+', '', 'g'), 'a.p.t', '아파트'), 'apt', '아파트'),
      '제([0-9]+)차', '\1차', 'g') AS name_norm,
    substr(n.legal_dong_code, 1, 5) AS sigungu_code,
    concat_ws(' ', n.sido, n.sigungu, if(ends_with(n.emd, '읍') OR ends_with(n.emd, '면'), n.emd, NULL), n.road_name,
      if(n.sub_no > 0, printf('%d-%d', n.main_no, n.sub_no), CAST(n.main_no AS VARCHAR))) AS standard_road_address,
    if(n.lot_main > 0, concat_ws(' ', n.sido, n.sigungu, n.emd, NULLIF(n.ri, ''), if(n.lot_mountain, '산', NULL),
      if(n.lot_sub > 0, printf('%d-%d', n.lot_main, n.lot_sub), CAST(n.lot_main AS VARCHAR))), NULL) AS lot_address
  FROM named n LEFT JOIN nfkc_changes k ON k.raw = n.original_name
  WHERE n.main_no > 0
),
buildings AS (
  SELECT
    name_norm,
    regexp_replace(name_norm, '(아파트|맨션|빌라트|빌라|연립주택|연립|다세대주택|다세대|오피스텔|빌딩)$', '') AS name_core,
    list(DISTINCT original_name ORDER BY original_name) AS original_names,
    any_value(sido) AS sido, any_value(sigungu) AS sigungu, sigungu_code,
    any_value(emd) AS emd, any_value(ri) AS ri, legal_dong_code,
    road_name_code, any_value(road_name) AS road_name, main_no, sub_no,
    any_value(standard_road_address) AS standard_road_address,
    coalesce(list(DISTINCT administrative_area_code ORDER BY administrative_area_code) FILTER (WHERE administrative_area_code IS NOT NULL), []) AS administrative_area_codes,
    coalesce(list(DISTINCT admin_dong ORDER BY admin_dong) FILTER (WHERE admin_dong IS NOT NULL), []) AS admin_dongs,
    coalesce(list(DISTINCT lot_address ORDER BY lot_address) FILTER (WHERE lot_address IS NOT NULL), []) AS lot_addresses,
    list(DISTINCT serial ORDER BY serial) AS raw_serials
  FROM normed
  GROUP BY name_norm, sigungu_code, legal_dong_code, road_name_code, main_no, sub_no
),
in_sigungu AS (
  SELECT name_norm, sigungu_code, count(DISTINCT standard_road_address) AS same_name_in_sigungu
  FROM buildings GROUP BY name_norm, sigungu_code
)
SELECT b.*,
  -- 건물 고유 이름이 아니라 용도·동 표기(주택, 농막, 창고, 마을회관, 1동, 가동 등)인 이름. 매칭 근거로 쓰지 않는다.
  regexp_matches(b.name_norm, '^(주택|단독주택|다세대주택|다가구주택|연립주택|공동주택|농막|창고|농협창고|마을회관|경로당|소매점|축사|우사|돈사|계사|제실|재실|제각|공중화장실|화장실|가로판매대|구두수선대|음식점|일반음식점|상가|공장|사무소|사무실|교회|민박|철거|슈퍼|정자|컨테이너|비닐하우스|상점|정미소|주유소|농협|관리사|부속건물|근린생활시설|([0-9]+|[a-z]|[가나다라마바사아자차카타파하])동|주[0-9]+동)$') AS is_generic,
  s.same_name_in_sigungu
FROM buildings b
JOIN in_sigungu s USING (name_norm, sigungu_code);
