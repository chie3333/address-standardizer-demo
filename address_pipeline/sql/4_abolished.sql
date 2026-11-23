-- 4단계 후보 도구 I 폐지 주소. bigquery/sql/09_abolished_addresses.sql과 같은 규칙. 기준자료에서 찾지 못한 원문을 폐지된 주소와 대조한다.
-- 폐지 이력: address_history (도로명주소 한글 월변동분 2024-10~2025-12 누적, 이동사유코드 63 = 폐지)
--   $reqs: JSON 문자열 목록. 각 항목 {"id": 주소 ID, "text": 원문(어순 정리본)}
-- 대조: 도로명 + 건물번호(대표 행) 또는 법정 리·동 + 산 + 지번. 원문에 그 주소의 시군구가 있어야 하고 번호 0은 대조하지 않는다.
-- 지금도 기준자료에 있는 주소(폐지 후 다시 등록, 또는 연결 지번만 빠진 건물)는 도로명·지번으로 찾은 경우 모두 뺀다.
-- Python 함수는 먼저 임시 표로 뽑는다(05와 같은 이유).
CREATE OR REPLACE TEMP TABLE req_cands AS
WITH req AS (
  SELECT json_extract_string(j, '$.id') AS address_id, nfkc(json_extract_string(j, '$.text')) AS text
  FROM (SELECT unnest($reqs) AS j)
)
  SELECT p.address_id, regexp_replace(p.text, '\s+', '', 'g') AS compact,
    p.c.kind AS kind, p.c.name AS name, p.c.mountain AS mountain, p.c.main AS main, p.c.sub AS sub, p.c.matched AS matched
  FROM (SELECT r.*, unnest(parse_address_candidates(r.text)) AS c FROM req r) p;

WITH cands AS (
  SELECT * FROM req_cands
),
gone AS (
  SELECT
    h."변동연월" AS changed_month, h."효력발생일" AS effective_date, h."지번구분" AS lot_role,
    h."시도명" AS sido, NULLIF(h."시군구명", '') AS sigungu, h."법정읍면동명" AS emd, NULLIF(h."법정리명", '') AS ri,
    h."법정동코드" AS legal_dong_code, h."도로명코드" AS road_name_code, h."도로명" AS road_name,
    regexp_replace(IFNULL(h."도로명", ''), '\s+', '', 'g') AS road_key,
    regexp_replace(IFNULL(NULLIF(h."법정리명", ''), h."법정읍면동명"), '\s+', '', 'g') AS lot_key,
    TRY_CAST(h."건물본번" AS BIGINT) AS b_main, IFNULL(TRY_CAST(h."건물부번" AS BIGINT), 0) AS b_sub,
    TRY_CAST(h."지번본번" AS BIGINT) AS l_main, IFNULL(TRY_CAST(h."지번부번" AS BIGINT), 0) AS l_sub,
    IFNULL(h."산여부" = '1', FALSE) AS l_mountain
  FROM address_history h
  WHERE h."이동사유코드" = '63'
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
  WHERE (h.sigungu IS NOT NULL AND all_parts_in(h.compact, h.sigungu))
     OR (h.sigungu IS NULL AND strpos(h.compact, '세종') > 0)
),
fresh AS (
  -- 폐지 기록의 도로명주소가 지금(2025-12-31 RAW)도 같은 도로명·번호로 있으면 폐지된 주소가 아니다(still_registered).
  --   · 도로명으로 찾은 행: 원문 주소가 지금도 있는 것이므로 뺀다.
  --   · 지번으로 찾은 행: 건물은 그대로 있고 그 건물에 연결된 지번만 빠진 기록(지번구분 '관련')이다. 빼지 않고
  --     '연결 지번이 빠진 건물' 후보로 돌려준다(완도군 고금면 덕동리 581 → 충무사길 68, 지금은 덕동리 578에 있음).
  SELECT * FROM (
    SELECT s.*, EXISTS (
      SELECT 1 FROM raw_clean_all r
      WHERE r.road_key = s.road_key AND r.main_no = s.b_main AND r.sub_no = s.b_sub
        AND r.sigungu_code = substr(s.legal_dong_code, 1, 5)) AS still_registered
    FROM scoped s
  ) x
  WHERE NOT (x.still_registered AND x.kind = 'road')
),
labeled AS (
  SELECT f.*,
    if(f.road_name IS NOT NULL AND f.b_main IS NOT NULL,
       concat_ws(' ', f.sido, f.sigungu, if(ends_with(f.emd, '읍') OR ends_with(f.emd, '면'), f.emd, NULL), f.road_name,
                 if(f.b_sub > 0, printf('%d-%d', f.b_main, f.b_sub), CAST(f.b_main AS VARCHAR))), NULL) AS road_address,
    concat_ws(' ', f.sido, f.sigungu, f.emd, f.ri, if(f.l_mountain, '산', NULL),
              if(f.l_sub > 0, printf('%d-%d', f.l_main, f.l_sub), CAST(f.l_main AS VARCHAR))) AS lot_address
  FROM fresh f
),
-- 연결 지번이 빠진 건물의 지금(2025-12-31) 정보: 코드는 하나로 정해질 때만 채운다(06과 같다).
current_rows AS (
  SELECT r.road_key, r.main_no, r.sub_no, r.sigungu_code,
    min(r.standard_road_address) AS address,
    list(DISTINCT r.road_name_code) FILTER (WHERE r.road_name_code IS NOT NULL) AS road_codes,
    list(DISTINCT r.legal_dong_code) FILTER (WHERE r.legal_dong_code IS NOT NULL) AS legal_codes,
    list(DISTINCT r.administrative_area_code) FILTER (WHERE r.administrative_area_code IS NOT NULL) AS admin_codes,
    list(r.standard_lot_address) AS lots, list(r.admin_dong) AS adongs, list(r.serial) AS serials,
    min(r.sido) AS sido, min(r.sigungu) AS sigungu, min(r.emd) AS emd, min(r.ri) AS ri
  FROM raw_clean_all r
  JOIN (SELECT DISTINCT road_key, b_main, b_sub, substr(legal_dong_code, 1, 5) AS sc FROM labeled WHERE still_registered) w
    ON r.road_key = w.road_key AND r.main_no = w.b_main AND r.sub_no = w.b_sub AND r.sigungu_code = w.sc
  GROUP BY r.road_key, r.main_no, r.sub_no, r.sigungu_code
),
current_building AS (
  SELECT c.road_key, c.main_no, c.sub_no, c.sigungu_code, {
      'address': c.address,
      'road_name_code': if(len(c.road_codes) = 1, c.road_codes[1], NULL),
      'legal_dong_code': if(len(c.legal_codes) = 1, c.legal_codes[1], NULL),
      'administrative_area_code': if(len(list_distinct(c.admin_codes)) = 1, list_distinct(c.admin_codes)[1], NULL),
      'lot_addresses': list_sort(list_distinct(list_filter(c.lots, lambda x: x IS NOT NULL))),
      'admin_dongs': list_sort(list_distinct(list_filter(c.adongs, lambda x: x IS NOT NULL))),
      'serials': list_sort(list_distinct(c.serials))[1:50],
      'sido': c.sido, 'sigungu': c.sigungu, 'emd': c.emd, 'ri': c.ri, 'main_no': c.main_no, 'sub_no': c.sub_no
    } AS now_building
  FROM current_rows c
)
SELECT
  l.address_id, l.kind, any_value(l.matched) AS matched,
  IFNULL(l.road_address, l.lot_address) AS abolished_address, any_value(l.lot_address) AS lot_address,
  max(l.changed_month) AS changed_month, max(l.effective_date) AS effective_date,
  any_value(l.road_name_code) AS road_name_code, any_value(l.legal_dong_code) AS legal_dong_code,
  -- 지번으로 찾았고 그 기록의 건물이 지금도 있음: 폐지가 아니라 연결 지번이 빠진 건물(now_building = 그 건물의 지금 정보)
  bool_and(l.still_registered) AS still_registered, any_value(cb.now_building) AS now_building,
  -- 같은 지번에 지금(2025-12-31 기준) 있는 건물: 참고 후보
  flatten(list(IFNULL(k.road_addresses, []))) AS current_roads
FROM labeled l
LEFT JOIN current_building cb
  ON l.still_registered AND cb.road_key = l.road_key AND cb.main_no = l.b_main AND cb.sub_no = l.b_sub
 AND cb.sigungu_code = substr(l.legal_dong_code, 1, 5)
LEFT JOIN ref_lot k
  ON k.legal_dong_code = l.legal_dong_code AND k.lot_mountain = l.l_mountain AND k.lot_main = l.l_main AND k.lot_sub = l.l_sub
GROUP BY l.address_id, l.kind, IFNULL(l.road_address, l.lot_address)
ORDER BY l.address_id, abolished_address
