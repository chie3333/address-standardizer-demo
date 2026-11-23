-- 4단계 후보 도구 H 건물명. bigquery/sql/08_building_candidates.sql과 같은 규칙. 원문의 건물명으로 기준 건물을 찾는다(building_names, 범위 임시 표).
--   $reqs: JSON 문자열 목록. 각 항목 {"id": 주소 ID, "text": 공백 지운 원문, "phrase": 정규화한 건물명, "core": 접미어 뗀 이름}
-- 찾는 범위: 원문에 적힌 시군구(세종은 시도) 안. 일치 방식(작을수록 강함)
--   0 정확 · 1 핵심 이름 · 2 포함(양쪽 3자 이상) · 3 철자 유사(핵심 이름 4자 이상, 한 글자 차이)
--   한 글자 차이는 within_one_edit 매크로로 본다(DuckDB levenshtein은 바이트를 센다).
-- 결과는 후보일 뿐 확정이 아니다.
-- 먼저 요청마다 찾을 시도·시군구 범위를 정한다. 프로그램이 이 범위의 건물명 사전만 RAW에서 만든 뒤(4_building_scope)
-- 아래 문장을 실행한다.
CREATE OR REPLACE TEMP TABLE building_scope AS
WITH req AS (
  SELECT json_extract_string(j, '$.id') AS address_id, json_extract_string(j, '$.text') AS compact,
         json_extract_string(j, '$.phrase') AS phrase, json_extract_string(j, '$.core') AS core
  FROM (SELECT unnest($reqs) AS j)
),
sido_map AS (
  SELECT * FROM (VALUES
    ('서울특별시', '서울'), ('부산광역시', '부산'), ('대구광역시', '대구'), ('인천광역시', '인천'),
    ('광주광역시', '광주'), ('대전광역시', '대전'), ('울산광역시', '울산'), ('세종특별자치시', '세종'),
    ('경기도', '경기'), ('강원특별자치도', '강원'), ('충청북도', '충북'), ('충청남도', '충남'),
    ('전북특별자치도', '전북'), ('전라남도', '전남'), ('경상북도', '경북'), ('경상남도', '경남'), ('제주특별자치도', '제주')
  ) v(sido, short)
),
regions AS (SELECT sido, sigungu FROM building_regions),   -- 전국 목록(0_sources)
req_scope AS (
  SELECT q.*, r.sido AS scope_sido, r.sigungu AS scope_sigungu
  FROM req q CROSS JOIN regions r
  LEFT JOIN sido_map m ON m.sido = r.sido
  -- 원문에 시도가 적혀 있으면 그 시도 안에서만 찾는다(대전 중구 ≠ 서울 중구).
  WHERE (NOT regexp_matches(q.compact, '(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|충청|전북|전남|전라|경북|경남|경상|제주)([^시]|$)|특별시|광역시|특별자치')
         OR strpos(q.compact, r.sido) > 0 OR regexp_matches(q.compact, m.short || '([^시]|$)')
         OR (r.sido = '전북특별자치도' AND strpos(q.compact, '전라북도') > 0)
         OR (r.sido = '강원특별자치도' AND strpos(q.compact, '강원도') > 0))
    AND ((r.sigungu IS NOT NULL AND all_parts_in(q.compact, r.sigungu))
     OR (r.sigungu IS NULL AND (strpos(q.compact, r.sido) > 0 OR strpos(q.compact, m.short) > 0)))
)
SELECT * FROM req_scope;

WITH req_scope AS (
  SELECT * FROM building_scope
),
scoped AS (
  SELECT q.address_id, q.phrase, q.core, q.compact AS compact_text, b.*,
    CASE
      WHEN b.name_norm = q.phrase THEN 0
      WHEN b.name_core = q.core THEN 1
      WHEN length(q.core) >= 3 AND length(b.name_core) >= 3
        AND (strpos(b.name_core, q.core) > 0 OR strpos(q.core, b.name_core) > 0) THEN 2
      -- 2자 이름(청구)은 원문에 건물 종류가 붙어 있고 그 건물의 읍면동이 원문에 있을 때만 포함 일치로 본다.
      WHEN length(q.core) = 2 AND q.phrase != q.core AND strpos(b.name_core, q.core) > 0
        AND (strpos(q.compact, b.emd) > 0 OR strpos(q.compact, IFNULL(b.ri, '-')) > 0) THEN 2
      WHEN length(q.core) >= 4 AND abs(length(b.name_core) - length(q.core)) <= 1
        AND within_one_edit(b.name_core, q.core) THEN 3
    END AS match_kind,
    strpos(q.compact, b.emd) > 0 OR len(list_filter(b.admin_dongs, lambda d: strpos(q.compact, d) > 0)) > 0 AS emd_hit
  FROM req_scope q
  JOIN building_names b
    ON b.sido = q.scope_sido AND IFNULL(b.sigungu, '') = IFNULL(q.scope_sigungu, '')
   AND NOT b.is_generic AND length(b.name_core) >= 2
   -- 이름 안의 숫자가 다르면 다른 단지다(진우하이빌3차 ≠ 진우하이빌2차).
   AND (NOT regexp_matches(q.phrase, '[0-9]')
        OR array_to_string(regexp_extract_all(b.name_norm, '[0-9]+'), ',') = array_to_string(regexp_extract_all(q.phrase, '[0-9]+'), ','))
),
kept AS (
  -- 원문에 읍·면이 적혀 있으면 다른 읍·면의 건물은 받지 않는다.
  SELECT * FROM scoped
  WHERE match_kind IS NOT NULL
    AND NOT (regexp_matches(compact_text, '[가-힣]{1,4}[읍면](?:[가-힣]|$)')
             AND (ends_with(emd, '읍') OR ends_with(emd, '면')) AND NOT emd_hit)
  -- 먼저 원문의 읍면동과 맞는 건물, 그 안에서 가장 강한 일치 방식만 남긴다. 두 조건을 따로 거르면
  -- (가장 강한 방식은 동이 안 맞고, 동이 맞는 건물은 더 약한 방식일 때) 후보가 하나도 남지 않았다.
  QUALIFY dense_rank() OVER (PARTITION BY address_id ORDER BY CAST(emd_hit AS BIGINT) DESC, match_kind) = 1
)
SELECT address_id, phrase, match_kind, emd_hit,
  standard_road_address AS address, sido, sigungu, emd, ri, road_name, main_no, sub_no,
  road_name_code, legal_dong_code,
  if(len(administrative_area_codes) = 1, administrative_area_codes[1], NULL) AS administrative_area_code,
  admin_dongs, lot_addresses, list_sort(raw_serials)[1:50] AS serials,
  original_names[1] AS building_name, same_name_in_sigungu,
  count(DISTINCT standard_road_address) OVER (PARTITION BY address_id) AS found_buildings
FROM kept
ORDER BY address_id, standard_road_address, building_name, legal_dong_code
