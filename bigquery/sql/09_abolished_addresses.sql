-- 규칙·건물명 단계 뒤에도 기준자료(2025-12-31)에서 찾지 못한 원문을 폐지된 주소와 대조한다.
-- 폐지 이력: address.address_history (도로명주소 한글 월변동분 2024-10~2025-12 누적, 이동사유코드 63 = 폐지).
--   bq load --source_format=PARQUET --replace address.address_history ADDRESS_HISTORY_202410_202512.parquet
--
-- 엔진(address_workbench.bigquery_engine)이 원문 분석 함수(04)를 임시 함수로 앞에 붙여 실행한다.
--   @reqs: JSON 문자열 배열. 각 항목 {"id": 주소 ID, "text": 원문(어순 정리본)}
-- 대조: 도로명 + 건물번호(대표 행) 또는 법정 리·동 + 산 + 지번(대표·관련 행). 원문에 그 주소의 시군구가 있어야 하고,
-- 번호 0(0-0 같은 빈 번호)은 대조하지 않는다.
-- 지금도 기준자료에 있는 주소(폐지 후 다시 등록, 또는 연결 지번만 빠진 건물)는 도로명·지번으로 찾은 경우 모두 뺀다. 결과는 확정이 아니라 판정 사유와 참고 후보다.
WITH req AS (
  SELECT JSON_VALUE(j, '$.id') AS address_id, NORMALIZE(JSON_VALUE(j, '$.text'), NFKC) AS text
  FROM UNNEST(@reqs) j
),
cands AS (
  SELECT r.address_id, REGEXP_REPLACE(r.text, r'\s+', '') AS compact, c.kind, c.name, c.mountain,
    CAST(c.main AS INT64) AS main, CAST(c.sub AS INT64) AS sub, c.matched
  FROM req r, UNNEST(`your-gcp-project.address.parse_address_candidates`(r.text)) c
),
gone AS (
  SELECT
    h.`변동연월` AS changed_month, h.`효력발생일` AS effective_date, h.`지번구분` AS lot_role,
    h.`시도명` AS sido, NULLIF(h.`시군구명`, '') AS sigungu, h.`법정읍면동명` AS emd, NULLIF(h.`법정리명`, '') AS ri,
    h.`법정동코드` AS legal_dong_code, h.`도로명코드` AS road_name_code, h.`도로명` AS road_name,
    REGEXP_REPLACE(IFNULL(h.`도로명`, ''), r'\s+', '') AS road_key,
    REGEXP_REPLACE(IFNULL(NULLIF(h.`법정리명`, ''), h.`법정읍면동명`), r'\s+', '') AS lot_key,
    SAFE_CAST(h.`건물본번` AS INT64) AS b_main, IFNULL(SAFE_CAST(h.`건물부번` AS INT64), 0) AS b_sub,
    SAFE_CAST(h.`지번본번` AS INT64) AS l_main, IFNULL(SAFE_CAST(h.`지번부번` AS INT64), 0) AS l_sub,
    IFNULL(h.`산여부` = '1', FALSE) AS l_mountain
  FROM `your-gcp-project.address.address_history` h
  WHERE h.`이동사유코드` = '63'
),
hits AS (
  SELECT c.address_id, c.compact, c.kind, c.matched, g.*
  FROM cands c JOIN gone g
    ON c.kind = 'road' AND c.main > 0 AND g.lot_role = '대표' AND g.road_key = c.name AND g.b_main = c.main AND g.b_sub = IFNULL(c.sub, 0)
  UNION ALL
  SELECT c.address_id, c.compact, c.kind, c.matched, g.*
  FROM cands c JOIN gone g
    ON c.kind = 'lot' AND c.main > 0 AND g.lot_key = c.name AND g.l_main = c.main AND g.l_sub = IFNULL(c.sub, 0)
   AND g.l_mountain = c.mountain
),
scoped AS (
  SELECT * FROM hits h
  WHERE (h.sigungu IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM UNNEST(SPLIT(h.sigungu, ' ')) p
           WHERE NOT (STRPOS(h.compact, p) > 0 OR (CHAR_LENGTH(p) >= 3 AND STRPOS(h.compact, SUBSTR(p, 1, CHAR_LENGTH(p) - 1)) > 0))))
     OR (h.sigungu IS NULL AND STRPOS(h.compact, '세종') > 0)
),
-- 폐지 후 같은 주소로 다시 등록돼 지금도 기준자료에 있는 경우는 폐지 주소로 보지 않는다.
-- 지번으로 맞은 행도 그 이력의 건물(도로명+건물번호)이 지금 있으면 뺀다. 건물이 그 지번에서 빠진 기록일 뿐
-- 주소가 없어진 것이 아니다(덕동리 581 → 충무사길 68의 관련 지번에서 빠짐, 충무사길 68은 지금도 있음).
still_road AS (
  SELECT DISTINCT road_key, main_no, sub_no, sigungu_code FROM `your-gcp-project.address.ref_road`
),
fresh AS (
  -- 폐지 기록의 도로명주소가 지금(2025-12-31 RAW)도 같은 도로명·번호로 있으면 폐지된 주소가 아니다(still_registered).
  --   · 도로명으로 찾은 행: 원문 주소가 지금도 있는 것이므로 뺀다.
  --   · 지번으로 찾은 행: 건물은 그대로 있고 그 건물에 연결된 지번만 빠진 기록(지번구분 '관련')이다. 빼지 않고
  --     '연결 지번이 빠진 건물' 후보로 돌려준다(완도군 고금면 덕동리 581 → 충무사길 68, 지금은 덕동리 578에 있음).
  SELECT * FROM (
    SELECT s.*, EXISTS (
      SELECT 1 FROM still_road r
      WHERE r.road_key = s.road_key AND r.main_no = s.b_main AND r.sub_no = s.b_sub
        AND r.sigungu_code = SUBSTR(s.legal_dong_code, 1, 5)) AS still_registered
    FROM scoped s
  ) x
  WHERE NOT (x.still_registered AND x.kind = 'road')
),
labeled AS (
  SELECT f.*,
    IF(f.road_name IS NOT NULL AND f.b_main IS NOT NULL,
       ARRAY_TO_STRING([f.sido, f.sigungu, IF(ENDS_WITH(f.emd, '읍') OR ENDS_WITH(f.emd, '면'), f.emd, NULL), f.road_name,
                        IF(f.b_sub > 0, FORMAT('%d-%d', f.b_main, f.b_sub), CAST(f.b_main AS STRING))], ' '), NULL) AS road_address,
    ARRAY_TO_STRING([f.sido, f.sigungu, f.emd, f.ri, IF(f.l_mountain, '산', NULL),
                     IF(f.l_sub > 0, FORMAT('%d-%d', f.l_main, f.l_sub), CAST(f.l_main AS STRING))], ' ') AS lot_address
  FROM fresh f
),
-- 연결 지번이 빠진 건물의 지금(2025-12-31) 정보: 코드는 하나로 정해질 때만 채운다(06과 같다).
current_rows AS (
  SELECT r.road_key, r.main_no, r.sub_no, r.sigungu_code,
    ANY_VALUE(r.standard_road_address) AS address,
    ARRAY_AGG(DISTINCT r.road_name_code IGNORE NULLS) AS road_codes,
    ARRAY_AGG(DISTINCT r.legal_dong_code IGNORE NULLS) AS legal_codes,
    ARRAY_CONCAT_AGG(r.administrative_area_codes) AS admin_codes,
    ARRAY_CONCAT_AGG(r.lot_addresses) AS lots, ARRAY_CONCAT_AGG(r.admin_dongs) AS adongs, ARRAY_CONCAT_AGG(r.serials) AS serials,
    MIN(r.sido) AS sido, MIN(r.sigungu) AS sigungu, MIN(r.emd) AS emd, MIN(r.ri) AS ri
  FROM `your-gcp-project.address.ref_road` r
  JOIN (SELECT DISTINCT road_key, b_main, b_sub, SUBSTR(legal_dong_code, 1, 5) AS sc FROM labeled WHERE still_registered) w
    ON r.road_key = w.road_key AND r.main_no = w.b_main AND r.sub_no = w.b_sub AND r.sigungu_code = w.sc
  GROUP BY r.road_key, r.main_no, r.sub_no, r.sigungu_code
),
current_building AS (
  SELECT c.road_key, c.main_no, c.sub_no, c.sigungu_code, STRUCT(
      c.address AS address,
      IF(ARRAY_LENGTH(c.road_codes) = 1, c.road_codes[OFFSET(0)], NULL) AS road_name_code,
      IF(ARRAY_LENGTH(c.legal_codes) = 1, c.legal_codes[OFFSET(0)], NULL) AS legal_dong_code,
      (SELECT IF(COUNT(DISTINCT x) = 1, ANY_VALUE(x), NULL) FROM UNNEST(c.admin_codes) x) AS administrative_area_code,
      ARRAY(SELECT DISTINCT x FROM UNNEST(c.lots) x ORDER BY x) AS lot_addresses,
      ARRAY(SELECT DISTINCT x FROM UNNEST(c.adongs) x ORDER BY x) AS admin_dongs,
      ARRAY(SELECT DISTINCT x FROM UNNEST(c.serials) x ORDER BY x LIMIT 50) AS serials,
      c.sido AS sido, c.sigungu AS sigungu, c.emd AS emd, c.ri AS ri, c.main_no AS main_no, c.sub_no AS sub_no) AS now_building
  FROM current_rows c
)
SELECT
  l.address_id, l.kind, ANY_VALUE(l.matched) AS matched,
  IFNULL(l.road_address, l.lot_address) AS abolished_address, ANY_VALUE(l.lot_address) AS lot_address,
  MAX(l.changed_month) AS changed_month, MAX(l.effective_date) AS effective_date,
  ANY_VALUE(l.road_name_code) AS road_name_code, ANY_VALUE(l.legal_dong_code) AS legal_dong_code,
  -- 지번으로 찾았고 그 기록의 건물이 지금도 있음: 폐지가 아니라 연결 지번이 빠진 건물(now_building = 그 건물의 지금 정보)
  LOGICAL_AND(l.still_registered) AS still_registered, ANY_VALUE(cb.now_building) AS now_building,
  -- 같은 지번에 지금(2025-12-31 기준) 있는 건물: 참고 후보
  ARRAY_CONCAT_AGG(IFNULL(k.road_addresses, [])) AS current_roads
FROM labeled l
LEFT JOIN current_building cb
  ON l.still_registered AND cb.road_key = l.road_key AND cb.main_no = l.b_main AND cb.sub_no = l.b_sub
 AND cb.sigungu_code = SUBSTR(l.legal_dong_code, 1, 5)
LEFT JOIN `your-gcp-project.address.ref_lot` k
  ON k.legal_dong_code = l.legal_dong_code AND k.lot_mountain = l.l_mountain AND k.lot_main = l.l_main AND k.lot_sub = l.l_sub
GROUP BY l.address_id, l.kind, abolished_address
ORDER BY l.address_id, abolished_address;
