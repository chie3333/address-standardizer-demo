-- 4단계 후보 도구 J 번호 오타. bigquery/sql/10_number_typo_candidates.sql과 같은 규칙. 도로명(또는 리·동)은 기준자료에 있는데 그 번호가 없는 미일치 행에,
-- 번호를 잘못 적었을 때 될 법한 건물을 후보로 찾는다. 확정하지 않고 판정도 미일치 그대로 둔다.
--   $reqs: JSON 문자열 목록. 각 항목 {"id": 주소 ID, "text": 원문(어순 정리본)}
-- 후보 조건: 같은 도로 또는 같은 리·동이고 그 시군구가 원문에 적혀 있다. 원문에 읍·면이 있으면 같은 읍·면.
--   부번·산 여부는 원문과 같고, 본번(두 자리 이상)만 swap / digit / missing / extra로 다르다(typo_kind 함수).
--   행마다 최대 5개. swap → digit → missing/extra 순, 같은 종류면 원문 번호와 가까운 순.
-- Python 함수는 먼저 임시 표로 뽑는다(05와 같은 이유).
CREATE OR REPLACE TEMP TABLE req_cands AS
WITH req AS (
  SELECT json_extract_string(j, '$.id') AS address_id, nfkc(json_extract_string(j, '$.text')) AS text
  FROM (SELECT unnest($reqs) AS j)
)
  SELECT p.address_id, regexp_replace(p.text, '\s+', '', 'g') AS compact,
    p.c.kind AS kind, p.c.name AS name, p.c.mountain AS mountain, p.c.main AS main, IFNULL(p.c.sub, 0) AS sub
  FROM (SELECT r.*, unnest(parse_address_candidates(r.text)) AS c FROM req r) p
  WHERE p.c.main >= 10;

-- 후보 이름과 기준자료의 조인 결과를 먼저 임시 표로 둔다. 한 쿼리 안에서 이어서 거르고 순위를 매기면
-- DuckDB 2.0이 가끔 내부 오류(dict-surviving cache … changed identity)를 냈다.
CREATE OR REPLACE TEMP TABLE typo_raw_hits AS
WITH cands AS (
  SELECT * FROM req_cands
),
road_hits AS (
  SELECT c.address_id, c.compact, 'road' AS via, c.main AS input_main, f.main_no AS ref_main, c.sub,
    typo_kind(CAST(c.main AS VARCHAR), CAST(f.main_no AS VARCHAR)) AS typo, CAST(NULL AS VARCHAR) AS lot_address,
    f.standard_road_address AS address, f.sigungu, f.sigungu_code, f.emd
  FROM cands c JOIN ref_road f ON c.kind = 'road' AND f.road_key = c.name AND f.sub_no = c.sub
),
lot_hits AS (
  SELECT address_id, compact, 'lot' AS via, input_main, ref_main, sub,
    typo_kind(CAST(input_main AS VARCHAR), CAST(ref_main AS VARCHAR)) AS typo, lot_address,
    unnest(road_addresses) AS address, sigungu, sigungu_code, emd
  FROM (
    SELECT c.address_id, c.compact, c.main AS input_main, k.lot_main AS ref_main, c.sub,
      k.standard_lot_address AS lot_address, k.road_addresses, k.sigungu, k.sigungu_code, k.emd
    FROM cands c JOIN ref_lot_keys k
      ON c.kind = 'lot' AND k.lot_key = c.name AND k.lot_mountain = c.mountain AND k.lot_sub = c.sub
  )
)
SELECT * FROM road_hits UNION ALL SELECT * FROM lot_hits;

WITH hits AS (
  SELECT * FROM typo_raw_hits h
  WHERE h.typo IS NOT NULL
    -- 원문에 그 시군구가 적혀 있어야 한다(세종은 시군구가 없으므로 '세종').
    AND ((h.sigungu IS NOT NULL AND all_parts_in(h.compact, h.sigungu))
      OR (h.sigungu IS NULL AND strpos(h.compact, '세종') > 0))
    -- 원문에 읍·면이 적혀 있으면 그 읍·면이어야 한다(장항읍 송림리 ≠ 마산면 송림리).
    AND (NOT regexp_matches(h.compact, '[가-힣]{1,4}[읍면]') OR NOT regexp_matches(IFNULL(h.emd, ''), '[읍면]$')
         OR strpos(h.compact, h.emd) > 0)
),
ranked AS (
  SELECT h.*, row_number() OVER (PARTITION BY h.address_id ORDER BY
      CASE h.typo WHEN 'swap' THEN 0 WHEN 'digit' THEN 1 ELSE 2 END, abs(h.ref_main - h.input_main), h.address) AS pos
  FROM (SELECT * EXCLUDE (n) FROM (
          -- 한 건물이 여러 지번에 걸치면 원문 번호에 가장 가까운 지번을 남긴다(예전에는 아무거나 남았다).
          SELECT h.*, row_number() OVER (PARTITION BY h.address_id, h.address ORDER BY h.via,
              CASE h.typo WHEN 'swap' THEN 0 WHEN 'digit' THEN 1 ELSE 2 END, abs(h.ref_main - h.input_main),
              h.lot_address) AS n FROM hits h)
        WHERE n = 1) h
),
-- 법정동 경계에 걸친 건물은 기준표에 법정동마다 한 행씩 있다. 주소 하나에 한 행으로 모으고,
-- 코드는 하나로 정해질 때만 채운다(06과 같다).
detail AS (
  SELECT r.address_id, r.pos, r.via, r.typo, r.input_main, r.ref_main, r.sub, r.lot_address, r.address,
    list(DISTINCT f.road_name_code) FILTER (WHERE f.road_name_code IS NOT NULL) AS road_codes,
    list(DISTINCT f.legal_dong_code) FILTER (WHERE f.legal_dong_code IS NOT NULL) AS legal_codes,
    list_distinct(flatten(list(f.administrative_area_codes))) AS admin_codes,
    flatten(list(f.serials)) AS serials, flatten(list(f.lot_addresses)) AS lots, flatten(list(f.admin_dongs)) AS adongs,
    any_value(f.sido) AS sido, any_value(f.sigungu) AS sigungu, min(f.emd) AS emd, min(f.ri) AS ri,
    coalesce(list(DISTINCT f.emd ORDER BY f.emd) FILTER (WHERE f.emd IS NOT NULL), []) AS emds,
    coalesce(list(DISTINCT f.ri ORDER BY f.ri) FILTER (WHERE f.ri IS NOT NULL), []) AS ris,
    any_value(f.main_no) AS main_no, any_value(f.sub_no) AS sub_no
  FROM ranked r
  JOIN ref_road f ON f.standard_road_address = r.address AND f.sigungu_code = r.sigungu_code
  WHERE r.pos <= 5
  GROUP BY r.address_id, r.pos, r.via, r.typo, r.input_main, r.ref_main, r.sub, r.lot_address, r.address
)
SELECT
  d.address_id, d.via, d.typo, d.input_main, d.ref_main, d.sub, d.lot_address, d.pos, d.address,
  if(len(d.road_codes) = 1, d.road_codes[1], NULL) AS road_name_code,
  if(len(d.legal_codes) = 1, d.legal_codes[1], NULL) AS legal_dong_code,
  if(len(d.admin_codes) = 1, d.admin_codes[1], NULL) AS administrative_area_code,
  list_sort(list_distinct(d.serials)) AS serials,
  list_sort(list_distinct(d.lots)) AS lot_addresses,
  list_sort(list_distinct(d.adongs)) AS admin_dongs,
  d.sido, d.sigungu, d.emd, d.ri, d.emds, d.ris,
  regexp_extract(d.address, '(\S+) [0-9]+(?:-[0-9]+)?$', 1) AS road_name, d.main_no, d.sub_no
FROM detail d
ORDER BY d.address_id, d.pos
