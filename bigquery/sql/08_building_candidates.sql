-- 원문의 건물명으로 기준 건물을 찾는다(건물명 사전 address.building_names, 01로 만든 표).
-- 엔진(address_workbench.bigquery_engine)이 원문에서 건물명 조각을 뽑아 @reqs로 넘긴다.
--   @reqs: JSON 문자열 배열. 각 항목 {"id": 주소 ID, "text": 공백 지운 원문, "phrase": 정규화한 건물명, "core": 접미어 뗀 이름}
--
-- 찾는 범위: 원문에 적힌 시군구(세종은 시도) 안. 시군구가 없으면 찾지 않는다(전국 동명 건물이 너무 많다).
-- 일치 방식(작을수록 강함)
--   0 정확: 정규화한 이름이 같음                     (대림아파트 = 대림아파트)
--   1 핵심 이름: 아파트·맨션·빌라 등 접미어를 뗀 이름이 같음 (종우원에이아파트 = 종우원에이맨션)
--   2 포함: 한쪽 핵심 이름이 다른 쪽에 들어 있음, 양쪽 3자 이상 (삼영노블리안 ⊂ 삼영노블리안2차)
--   3 철자 유사: 핵심 이름이 4자 이상이고 한 글자 차이   (삼성레미안 ≈ 삼성래미안)
-- 같은 주소 ID에서 원문의 읍면동과 맞는 건물을 먼저 고르고, 그 안에서 가장 강한 방식만 남긴다.
-- 결과는 후보일 뿐 확정이 아니다. 엔진이 검토 필요 후보로 붙인다.
WITH req AS (
  SELECT JSON_VALUE(j, '$.id') AS address_id, JSON_VALUE(j, '$.text') AS compact,
         JSON_VALUE(j, '$.phrase') AS phrase, JSON_VALUE(j, '$.core') AS core
  FROM UNNEST(@reqs) j
),
sido_map AS (
  SELECT * FROM UNNEST([
    STRUCT('서울특별시' AS sido, '서울' AS short), ('부산광역시', '부산'), ('대구광역시', '대구'), ('인천광역시', '인천'),
    ('광주광역시', '광주'), ('대전광역시', '대전'), ('울산광역시', '울산'), ('세종특별자치시', '세종'),
    ('경기도', '경기'), ('강원특별자치도', '강원'), ('충청북도', '충북'), ('충청남도', '충남'),
    ('전북특별자치도', '전북'), ('전라남도', '전남'), ('경상북도', '경북'), ('경상남도', '경남'), ('제주특별자치도', '제주')])
),
-- 원문에 적힌 시군구(세종은 시도)를 먼저 고른다. 시군구가 '포항시 북구'처럼 여러 단어면 모든 단어가 원문에 있어야 한다.
regions AS (SELECT DISTINCT sido, sigungu FROM `your-gcp-project.address.building_names`),
req_scope AS (
  SELECT q.*, r.sido AS scope_sido, r.sigungu AS scope_sigungu
  FROM req q CROSS JOIN regions r
  LEFT JOIN sido_map m ON m.sido = r.sido
  -- 원문에 시도가 적혀 있으면 그 시도 안에서만 찾는다(대전 중구 ≠ 서울 중구).
  WHERE (NOT REGEXP_CONTAINS(q.compact, r'(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|충청|전북|전남|전라|경북|경남|경상|제주)([^시]|$)|특별시|광역시|특별자치')
         OR STRPOS(q.compact, r.sido) > 0 OR REGEXP_CONTAINS(q.compact, CONCAT(m.short, '([^시]|$)'))
         OR (r.sido = '전북특별자치도' AND STRPOS(q.compact, '전라북도') > 0)
         OR (r.sido = '강원특별자치도' AND STRPOS(q.compact, '강원도') > 0))
    AND ((r.sigungu IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM UNNEST(SPLIT(r.sigungu, ' ')) p
           WHERE NOT (STRPOS(q.compact, p) > 0 OR (CHAR_LENGTH(p) >= 3 AND STRPOS(q.compact, SUBSTR(p, 1, CHAR_LENGTH(p) - 1)) > 0))))
     OR (r.sigungu IS NULL AND (STRPOS(q.compact, r.sido) > 0 OR STRPOS(q.compact, m.short) > 0)))
),
scoped AS (
  SELECT q.address_id, q.phrase, q.core, q.compact AS compact_text, b.*,
    CASE
      WHEN b.name_norm = q.phrase THEN 0
      WHEN b.name_core = q.core THEN 1
      -- 포함 일치는 짧은 이름(주촌, 대저)이 아무 건물명에나 걸리지 않게 양쪽 다 3자 이상일 때만
      WHEN CHAR_LENGTH(q.core) >= 3 AND CHAR_LENGTH(b.name_core) >= 3
        AND (STRPOS(b.name_core, q.core) > 0 OR STRPOS(q.core, b.name_core) > 0) THEN 2
      -- 2자 이름(청구)은 원문에 건물 종류(아파트·빌라…)가 붙어 있고 그 건물의 읍면동이 원문에 있을 때만 포함 일치로 본다.
      WHEN CHAR_LENGTH(q.core) = 2 AND q.phrase != q.core AND STRPOS(b.name_core, q.core) > 0
        AND (STRPOS(q.compact, b.emd) > 0 OR STRPOS(q.compact, IFNULL(b.ri, '-')) > 0) THEN 2
      WHEN CHAR_LENGTH(q.core) >= 4 AND ABS(CHAR_LENGTH(b.name_core) - CHAR_LENGTH(q.core)) <= 1
        AND EDIT_DISTANCE(b.name_core, q.core) <= 1 THEN 3
    END AS match_kind,
    STRPOS(q.compact, b.emd) > 0 OR EXISTS (SELECT 1 FROM UNNEST(b.admin_dongs) d WHERE STRPOS(q.compact, d) > 0) AS emd_hit
  FROM req_scope q
  JOIN `your-gcp-project.address.building_names` b
    ON b.sido = q.scope_sido AND IFNULL(b.sigungu, '') = IFNULL(q.scope_sigungu, '')
   AND NOT b.is_generic AND CHAR_LENGTH(b.name_core) >= 2
   -- 이름 안의 숫자가 다르면 다른 단지다(진우하이빌3차 ≠ 진우하이빌2차). 원문 이름에 숫자가 있을 때만 본다.
   AND (NOT REGEXP_CONTAINS(q.phrase, r'[0-9]')
        OR ARRAY_TO_STRING(REGEXP_EXTRACT_ALL(b.name_norm, r'[0-9]+'), ',') = ARRAY_TO_STRING(REGEXP_EXTRACT_ALL(q.phrase, r'[0-9]+'), ','))
),
kept AS (
  -- 원문에 읍·면이 적혀 있으면 다른 읍·면의 건물은 받지 않는다(화산면 … 하마 ≠ 해남읍의 '하마').
  SELECT * FROM scoped
  WHERE match_kind IS NOT NULL
    AND NOT (REGEXP_CONTAINS(compact_text, r'[가-힣]{1,4}[읍면](?:[가-힣]|$)')
             AND (ENDS_WITH(emd, '읍') OR ENDS_WITH(emd, '면')) AND NOT emd_hit)
  -- 먼저 원문의 읍면동과 맞는 건물, 그 안에서 가장 강한 일치 방식만 남긴다. 두 조건을 따로 거르면
  -- (가장 강한 방식은 동이 안 맞고, 동이 맞는 건물은 더 약한 방식일 때) 후보가 하나도 남지 않았다.
  QUALIFY DENSE_RANK() OVER (PARTITION BY address_id ORDER BY CAST(emd_hit AS INT64) DESC, match_kind) = 1
)
SELECT address_id, phrase, match_kind, emd_hit,
  standard_road_address AS address, sido, sigungu, emd, ri, road_name, main_no, sub_no,
  road_name_code, legal_dong_code,
  IF(ARRAY_LENGTH(administrative_area_codes) = 1, administrative_area_codes[OFFSET(0)], NULL) AS administrative_area_code,
  admin_dongs, lot_addresses, ARRAY(SELECT x FROM UNNEST(raw_serials) x ORDER BY x LIMIT 50) AS serials,
  original_names[OFFSET(0)] AS building_name, same_name_in_sigungu,
  COUNT(DISTINCT standard_road_address) OVER (PARTITION BY address_id) AS found_buildings
FROM kept
ORDER BY address_id, standard_road_address, building_name, legal_dong_code;
