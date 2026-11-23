-- 3단계 결과 상세. bigquery/sql/06_engine_details.sql과 같은 규칙. 05가 만든 임시 표 engine_standardized의 확정 주소와 후보마다
-- 코드 3개, 지역 구성요소, 연결된 지번·도로명주소, RAW 일련번호를 붙여 돌려준다.
WITH s AS (
  SELECT * FROM engine_standardized
),
addrs AS (
  SELECT address_id, standard_road_address AS address, -1 AS pos FROM s WHERE standard_road_address IS NOT NULL
  UNION ALL
  SELECT address_id, a.address, a.pos
  FROM (SELECT address_id, unnest(list_transform(candidate_roads, lambda x, i: {'address': x, 'pos': i - 1})) AS a FROM s)
),
roads AS (
  SELECT r.standard_road_address AS address,
    min(r.sido) AS sido, min(r.sigungu) AS sigungu, min(r.emd) AS emd, min(r.ri) AS ri,
    -- 한 건물이 여러 법정동·리에 걸치면(필지가 경계에 있음) 그 이름을 모두 둔다.
    coalesce(list(DISTINCT r.emd ORDER BY r.emd) FILTER (WHERE r.emd IS NOT NULL), []) AS emds,
    coalesce(list(DISTINCT r.ri ORDER BY r.ri) FILTER (WHERE r.ri IS NOT NULL), []) AS ris,
    min(r.road_key) AS road_name, min(r.main_no) AS main_no, min(r.sub_no) AS sub_no,
    min(r.sigungu_code) AS sigungu_code,
    list(DISTINCT r.road_name_code) FILTER (WHERE r.road_name_code IS NOT NULL) AS road_codes,
    list(DISTINCT r.legal_dong_code) FILTER (WHERE r.legal_dong_code IS NOT NULL) AS legal_codes,
    flatten(list(r.administrative_area_codes)) AS admin_codes,
    flatten(list(r.lot_addresses)) AS lots,
    flatten(list(r.admin_dongs)) AS admin_dongs,
    flatten(list(r.serials)) AS serials
  FROM ref_road r
  JOIN (SELECT DISTINCT address FROM addrs) w ON w.address = r.standard_road_address
  GROUP BY r.standard_road_address
),
-- 부번 없는 주소로 확정할 때 같은 도로·본번에 부번이 붙은 건물이 있으면 주의 표시용으로 그 부번들을 붙인다.
siblings AS (
  SELECT d.address, list(DISTINCT r.sub_no ORDER BY r.sub_no) AS sibling_subs
  FROM roads d
  JOIN ref_road r
    ON r.road_key = d.road_name AND r.main_no = d.main_no AND r.sigungu_code = d.sigungu_code AND r.sub_no > 0
  WHERE d.sub_no = 0
  GROUP BY d.address
),
detail AS (
  SELECT a.address_id, a.pos, {
      'address': a.address,
      'sido': d.sido, 'sigungu': d.sigungu, 'emd': d.emd, 'ri': d.ri, 'emds': d.emds, 'ris': d.ris,
      'road_name': d.road_name, 'main_no': d.main_no, 'sub_no': d.sub_no,
      'road_name_code': if(len(d.road_codes) = 1, d.road_codes[1], NULL),
      'legal_dong_code': if(len(d.legal_codes) = 1, d.legal_codes[1], NULL),
      'administrative_area_code': if(len(list_distinct(d.admin_codes)) = 1, list_distinct(d.admin_codes)[1], NULL),
      'lot_addresses': list_sort(list_distinct(d.lots)),
      'admin_dongs': list_sort(list_distinct(d.admin_dongs)),
      'serials': list_sort(d.serials)[1:50],
      'sibling_subs': IFNULL(sb.sibling_subs, [])
    } AS item
  FROM addrs a LEFT JOIN roads d USING (address) LEFT JOIN siblings sb USING (address)
),
-- 지번 후보는 그 지번의 구성요소와 그 지번에 있는 도로명주소를 붙인다.
lot_addrs AS (
  SELECT address_id, a.lot AS l, a.pos
  FROM (SELECT address_id, unnest(list_transform(candidate_lots, lambda x, i: {'lot': x, 'pos': i - 1})) AS a FROM s)
),
lots AS (
  SELECT k.standard_lot_address AS lot, min(k.sido) AS sido, min(k.sigungu) AS sigungu,
    min(k.emd) AS emd, min(k.ri) AS ri, min(k.lot_mountain) AS mountain,
    min(k.lot_main) AS lot_main, min(k.lot_sub) AS lot_sub, min(k.legal_dong_code) AS legal_dong_code,
    flatten(list(k.road_addresses)) AS road_addresses
  FROM ref_lot k
  JOIN (SELECT DISTINCT l FROM lot_addrs) w ON w.l = k.standard_lot_address
  GROUP BY k.standard_lot_address
),
lot_detail AS (
  SELECT a.address_id, a.pos, {
      'lot': a.l, 'sido': d.sido, 'sigungu': d.sigungu, 'emd': d.emd, 'ri': d.ri, 'mountain': d.mountain,
      'lot_main': d.lot_main, 'lot_sub': d.lot_sub, 'legal_dong_code': d.legal_dong_code,
      'road_addresses': list_sort(list_distinct(d.road_addresses))
    } AS item
  FROM lot_addrs a LEFT JOIN lots d ON d.lot = a.l
),
grouped AS (
  SELECT address_id,
    list(item ORDER BY pos) FILTER (WHERE pos >= 0) AS candidate_details,
    first(item) FILTER (WHERE pos = -1) AS main_detail
  FROM detail
  GROUP BY address_id
),
lot_grouped AS (
  SELECT address_id, list(item ORDER BY pos) AS lot_details FROM lot_detail GROUP BY address_id
)
SELECT s.*, IFNULL(g.candidate_details, []) AS candidate_details, g.main_detail,
  IFNULL(lg.lot_details, []) AS lot_details
FROM s LEFT JOIN grouped g USING (address_id) LEFT JOIN lot_grouped lg USING (address_id)
ORDER BY s.source_row, s.address_id
