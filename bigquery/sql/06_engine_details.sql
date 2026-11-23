-- BigQuery 엔진(address_workbench.bigquery_engine)이 워크벤치 결과 형식을 만들 때 쓰는 조회.
-- 이 스크립트 실행에서만 보이는 매칭 결과의 확정 주소와 후보마다
-- 코드 3개, 지역 구성요소(시도·시군구·읍면동·리·도로명·번호), 연결된 지번·도로명주소, RAW 일련번호를 붙여 돌려준다.
-- 테이블을 만들지 않고 결과 행만 돌려준다.
WITH s AS (
  SELECT * FROM _SESSION.engine_standardized
),
addrs AS (
  SELECT address_id, standard_road_address AS address, -1 AS pos FROM s WHERE standard_road_address IS NOT NULL
  UNION ALL
  SELECT address_id, a, pos FROM s, UNNEST(candidate_roads) a WITH OFFSET pos
),
roads AS (
  SELECT r.standard_road_address AS address,
    ANY_VALUE(r.sido) AS sido, ANY_VALUE(r.sigungu) AS sigungu, ANY_VALUE(r.emd) AS emd, ANY_VALUE(r.ri) AS ri,
    -- 한 건물이 여러 법정동·리에 걸치면(필지가 경계에 있음) 그 이름을 모두 둔다.
    ARRAY_AGG(DISTINCT r.emd IGNORE NULLS) AS emds, ARRAY_AGG(DISTINCT r.ri IGNORE NULLS) AS ris,
    ANY_VALUE(r.road_key) AS road_name, ANY_VALUE(r.main_no) AS main_no, ANY_VALUE(r.sub_no) AS sub_no,
    ANY_VALUE(r.sigungu_code) AS sigungu_code,
    ARRAY_AGG(DISTINCT r.road_name_code IGNORE NULLS) AS road_codes,
    ARRAY_AGG(DISTINCT r.legal_dong_code IGNORE NULLS) AS legal_codes,
    ARRAY_CONCAT_AGG(r.administrative_area_codes) AS admin_codes,
    ARRAY_CONCAT_AGG(r.lot_addresses) AS lots,
    ARRAY_CONCAT_AGG(r.admin_dongs) AS admin_dongs,
    ARRAY_CONCAT_AGG(r.serials) AS serials
  FROM `your-gcp-project.address.ref_road` r
  JOIN (SELECT DISTINCT address FROM addrs) w ON w.address = r.standard_road_address
  GROUP BY r.standard_road_address
),
-- 부번 없는 주소(예: 남부순환로 1756)로 확정할 때, 같은 도로·본번에 부번이 붙은 건물(1756-1 …)이 있으면
-- 원문에서 부번이 빠졌을 수 있으므로 그 부번들을 주의 표시용으로 붙인다.
siblings AS (
  SELECT d.address, ARRAY_AGG(DISTINCT r.sub_no ORDER BY r.sub_no) AS sibling_subs
  FROM roads d
  JOIN `your-gcp-project.address.ref_road` r
    ON r.road_key = d.road_name AND r.main_no = d.main_no AND r.sigungu_code = d.sigungu_code AND r.sub_no > 0
  WHERE d.sub_no = 0
  GROUP BY d.address
),
detail AS (
  SELECT a.address_id, a.pos, STRUCT(
      a.address AS address,
      d.sido, d.sigungu, d.emd, d.ri, d.emds, d.ris, d.road_name, d.main_no, d.sub_no,
      IF(ARRAY_LENGTH(d.road_codes) = 1, d.road_codes[OFFSET(0)], NULL) AS road_name_code,
      IF(ARRAY_LENGTH(d.legal_codes) = 1, d.legal_codes[OFFSET(0)], NULL) AS legal_dong_code,
      (SELECT IF(COUNT(DISTINCT x) = 1, ANY_VALUE(x), NULL) FROM UNNEST(d.admin_codes) x) AS administrative_area_code,
      ARRAY(SELECT DISTINCT x FROM UNNEST(d.lots) x ORDER BY x) AS lot_addresses,
      ARRAY(SELECT DISTINCT x FROM UNNEST(d.admin_dongs) x ORDER BY x) AS admin_dongs,
      ARRAY(SELECT x FROM UNNEST(d.serials) x ORDER BY x LIMIT 50) AS serials,
      IFNULL(sb.sibling_subs, []) AS sibling_subs
    ) AS item
  FROM addrs a LEFT JOIN roads d USING (address) LEFT JOIN siblings sb USING (address)
),
-- 지번 후보(같은 지번이 여러 곳에 있을 때 등)는 그 지번의 구성요소와 그 지번에 있는 도로명주소를 붙인다.
lot_addrs AS (
  SELECT address_id, l, pos FROM s, UNNEST(candidate_lots) l WITH OFFSET pos
),
lots AS (
  SELECT k.standard_lot_address AS lot, ANY_VALUE(k.sido) AS sido, ANY_VALUE(k.sigungu) AS sigungu,
    ANY_VALUE(k.emd) AS emd, ANY_VALUE(k.ri) AS ri, ANY_VALUE(k.lot_mountain) AS mountain,
    ANY_VALUE(k.lot_main) AS lot_main, ANY_VALUE(k.lot_sub) AS lot_sub, ANY_VALUE(k.legal_dong_code) AS legal_dong_code,
    ARRAY_CONCAT_AGG(k.road_addresses) AS road_addresses
  FROM `your-gcp-project.address.ref_lot` k
  JOIN (SELECT DISTINCT l FROM lot_addrs) w ON w.l = k.standard_lot_address
  GROUP BY k.standard_lot_address
),
lot_detail AS (
  SELECT a.address_id, a.pos, STRUCT(
      a.l AS lot, d.sido, d.sigungu, d.emd, d.ri, d.mountain, d.lot_main, d.lot_sub, d.legal_dong_code,
      ARRAY(SELECT DISTINCT x FROM UNNEST(d.road_addresses) x ORDER BY x) AS road_addresses
    ) AS item
  FROM lot_addrs a LEFT JOIN lots d ON d.lot = a.l
),
grouped AS (
  SELECT address_id,
    ARRAY_AGG(IF(pos >= 0, item, NULL) IGNORE NULLS ORDER BY pos) AS candidate_details,
    ARRAY_AGG(IF(pos = -1, item, NULL) IGNORE NULLS LIMIT 1) AS main_detail
  FROM detail
  GROUP BY address_id
),
lot_grouped AS (
  SELECT address_id, ARRAY_AGG(item ORDER BY pos) AS lot_details FROM lot_detail GROUP BY address_id
)
SELECT s.*, IFNULL(g.candidate_details, []) AS candidate_details,
  IF(ARRAY_LENGTH(g.main_detail) = 1, g.main_detail[OFFSET(0)], NULL) AS main_detail,
  IFNULL(lg.lot_details, []) AS lot_details
FROM s LEFT JOIN grouped g USING (address_id) LEFT JOIN lot_grouped lg USING (address_id)
ORDER BY s.source_row;
