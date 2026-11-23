-- 도로명(또는 리·동)은 기준자료에 있는데 그 번호가 없는 미일치 행에, 번호를 잘못 적었을 때 될 법한 건물을 후보로 찾는다.
-- 확정하지 않고 판정도 미일치 그대로 둔다. 엔진은 이 결과를 "번호 오타 후보"(경고 포함, 승인 대상 아님)로 단다.
--   · 원문에 읍·면이 있으면 같은 읍·면 안에서만 찾는다.
--
-- 엔진(address_workbench.bigquery_engine)이 원문 분석 함수(04)를 임시 함수로 앞에 붙여 실행한다.
--   @reqs: JSON 문자열 배열. 각 항목 {"id": 주소 ID, "text": 원문(어순 정리본)}
-- 후보 조건:
--   · 같은 도로(도로명주소) 또는 같은 리·동(지번)이고, 그 시군구가 원문에 적혀 있다.
--   · 부번·산 여부는 원문과 같고, 본번만 아래처럼 다르다(본번 두 자리 이상).
--       swap      이웃한 두 숫자 자리 바뀜 (158 ↔ 185)
--       digit     숫자 한 자리 다름       (158 ↔ 168)
--       missing   원문에 숫자 하나 빠짐   (18 ↔ 158)
--       extra     원문에 숫자 하나 더 있음 (1558 ↔ 158)
--   · 행마다 최대 5개. swap → digit → missing/extra 순, 같은 종류면 원문 번호와 가까운 순.
CREATE TEMP FUNCTION typo_kind(a STRING, b STRING) RETURNS STRING LANGUAGE js AS r"""
  if (a === null || b === null || a === b) return null;
  if (a.length === b.length) {
    const d = [];
    for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) d.push(i);
    if (d.length === 1) return 'digit';
    if (d.length === 2 && d[1] === d[0] + 1 && a[d[0]] === b[d[1]] && a[d[1]] === b[d[0]]) return 'swap';
    return null;
  }
  if (Math.abs(a.length - b.length) !== 1) return null;
  const [s, l] = a.length < b.length ? [a, b] : [b, a];
  for (let i = 0; i < l.length; i++) if (l.slice(0, i) + l.slice(i + 1) === s) return a.length < b.length ? 'missing' : 'extra';
  return null;
""";

WITH req AS (
  SELECT JSON_VALUE(j, '$.id') AS address_id, NORMALIZE(JSON_VALUE(j, '$.text'), NFKC) AS text
  FROM UNNEST(@reqs) j
),
cands AS (
  SELECT r.address_id, REGEXP_REPLACE(r.text, r'\s+', '') AS compact, c.kind, c.name, c.mountain,
    CAST(c.main AS INT64) AS main, IFNULL(CAST(c.sub AS INT64), 0) AS sub
  FROM req r, UNNEST(`your-gcp-project.address.parse_address_candidates`(r.text)) c
  WHERE CAST(c.main AS INT64) >= 10
),
road_hits AS (
  SELECT c.address_id, c.compact, 'road' AS via, c.main AS input_main, f.main_no AS ref_main, c.sub,
    typo_kind(CAST(c.main AS STRING), CAST(f.main_no AS STRING)) AS typo, CAST(NULL AS STRING) AS lot_address,
    f.standard_road_address AS address, f.sigungu, f.sigungu_code, f.emd
  FROM cands c JOIN `your-gcp-project.address.ref_road` f
    ON c.kind = 'road' AND f.road_key = c.name AND f.sub_no = c.sub
),
lot_hits AS (
  SELECT c.address_id, c.compact, 'lot' AS via, c.main AS input_main, k.lot_main AS ref_main, c.sub,
    typo_kind(CAST(c.main AS STRING), CAST(k.lot_main AS STRING)) AS typo, k.standard_lot_address AS lot_address,
    road AS address, k.sigungu, k.sigungu_code, k.emd
  FROM cands c JOIN `your-gcp-project.address.ref_lot_keys` k
    ON c.kind = 'lot' AND k.lot_key = c.name AND k.lot_mountain = c.mountain AND k.lot_sub = c.sub,
    UNNEST(k.road_addresses) road
),
hits AS (
  SELECT * FROM (SELECT * FROM road_hits UNION ALL SELECT * FROM lot_hits) h
  WHERE h.typo IS NOT NULL
    -- 원문에 그 시군구가 적혀 있어야 한다(세종은 시군구가 없으므로 '세종').
    AND ((h.sigungu IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM UNNEST(SPLIT(h.sigungu, ' ')) p
           WHERE NOT (STRPOS(h.compact, p) > 0 OR (CHAR_LENGTH(p) >= 3 AND STRPOS(h.compact, SUBSTR(p, 1, CHAR_LENGTH(p) - 1)) > 0))))
      OR (h.sigungu IS NULL AND STRPOS(h.compact, '세종') > 0))
    -- 원문에 읍·면이 적혀 있으면 그 읍·면이어야 한다(장항읍 송림리 ≠ 마산면 송림리).
    AND (NOT REGEXP_CONTAINS(h.compact, r'[가-힣]{1,4}[읍면]') OR NOT REGEXP_CONTAINS(IFNULL(h.emd, ''), r'[읍면]$')
         OR STRPOS(h.compact, h.emd) > 0)
),
ranked AS (
  SELECT h.*, ROW_NUMBER() OVER (PARTITION BY h.address_id ORDER BY
      CASE h.typo WHEN 'swap' THEN 0 WHEN 'digit' THEN 1 ELSE 2 END, ABS(h.ref_main - h.input_main), h.address) AS pos
  FROM (SELECT * EXCEPT (n) FROM (
          -- 한 건물이 여러 지번에 걸치면 원문 번호에 가장 가까운 지번을 남긴다(예전에는 아무거나 남았다).
          SELECT h.*, ROW_NUMBER() OVER (PARTITION BY h.address_id, h.address ORDER BY h.via,
              CASE h.typo WHEN 'swap' THEN 0 WHEN 'digit' THEN 1 ELSE 2 END, ABS(h.ref_main - h.input_main),
              h.lot_address) AS n FROM hits h)
        WHERE n = 1) h
),
-- 법정동 경계에 걸친 건물은 기준표에 법정동마다 한 행씩 있다. 주소 하나에 한 행으로 모으고,
-- 코드는 하나로 정해질 때만 채운다(06과 같다). 예전에는 그중 아무 행이나 먼저 온 것을 썼다.
detail AS (
  SELECT r.address_id, r.pos, r.via, r.typo, r.input_main, r.ref_main, r.sub, r.lot_address, r.address,
    ARRAY_AGG(DISTINCT f.road_name_code IGNORE NULLS) AS road_codes,
    ARRAY_AGG(DISTINCT f.legal_dong_code IGNORE NULLS) AS legal_codes,
    ARRAY_CONCAT_AGG(f.administrative_area_codes) AS admin_codes,
    ARRAY_CONCAT_AGG(f.serials) AS serials, ARRAY_CONCAT_AGG(f.lot_addresses) AS lots, ARRAY_CONCAT_AGG(f.admin_dongs) AS adongs,
    ANY_VALUE(f.sido) AS sido, ANY_VALUE(f.sigungu) AS sigungu, MIN(f.emd) AS emd, MIN(f.ri) AS ri,
    ARRAY_AGG(DISTINCT f.emd IGNORE NULLS ORDER BY f.emd) AS emds, ARRAY_AGG(DISTINCT f.ri IGNORE NULLS ORDER BY f.ri) AS ris,
    ANY_VALUE(f.main_no) AS main_no, ANY_VALUE(f.sub_no) AS sub_no
  FROM ranked r
  JOIN `your-gcp-project.address.ref_road` f
    ON f.standard_road_address = r.address AND f.sigungu_code = r.sigungu_code
  WHERE r.pos <= 5
  GROUP BY r.address_id, r.pos, r.via, r.typo, r.input_main, r.ref_main, r.sub, r.lot_address, r.address
)
SELECT
  d.address_id, d.via, d.typo, d.input_main, d.ref_main, d.sub, d.lot_address, d.pos, d.address,
  IF(ARRAY_LENGTH(d.road_codes) = 1, d.road_codes[OFFSET(0)], NULL) AS road_name_code,
  IF(ARRAY_LENGTH(d.legal_codes) = 1, d.legal_codes[OFFSET(0)], NULL) AS legal_dong_code,
  (SELECT IF(COUNT(DISTINCT x) = 1, ANY_VALUE(x), NULL) FROM UNNEST(d.admin_codes) x) AS administrative_area_code,
  ARRAY(SELECT DISTINCT x FROM UNNEST(d.serials) x ORDER BY x) AS serials,
  ARRAY(SELECT DISTINCT x FROM UNNEST(d.lots) x ORDER BY x) AS lot_addresses,
  ARRAY(SELECT DISTINCT x FROM UNNEST(d.adongs) x ORDER BY x) AS admin_dongs,
  d.sido, d.sigungu, d.emd, d.ri, d.emds, d.ris,
  REGEXP_EXTRACT(d.address, r'(\S+) [0-9]+(?:-[0-9]+)?$') AS road_name, d.main_no, d.sub_no
FROM detail d
ORDER BY d.address_id, d.pos;
