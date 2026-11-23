-- 입력 원문 전체(input_raw)를 SQL 한 번으로 표준화한다. 입력값 옆에 표준주소·코드·판정·판정 사유를 붙인다.
--
-- 1) parse_address_candidates로 도로명 열·지번 열에서 '이름 + 번호' 후보를 모두 뽑는다.
-- 2) ref_road(도로명+건물번호), ref_lot(리·동+산+지번)과 정확 조인한다. 같은 본번의 다른 부번도 함께 가져온다.
-- 3) 기준자료의 시도·시군구·읍면동이 원문에 적혀 있는지로 지역을 확인한다. 시군구가 맞거나 시도+읍면동이 맞아야 한다.
-- 4) 도로명 결과와 지번 결과를 함께 보고 판정한다. 둘 다 있으면 서로 같은 건물인지 교차 확인한다.
--
-- 실행: bq query --use_legacy_sql=false --parameter=batch::202512 < bigquery/sql/05_standardize_all.sql
--       (파이썬에서는 address_workbench.bigquery_stage가 @batch를 넘겨 실행한다)
CREATE OR REPLACE TABLE `your-gcp-project.address.sql_standardized`
OPTIONS (description = 'input_raw 전체를 BigQuery SQL로 표준화한 결과. 원문 옆에 표준주소·코드·판정·사유')
AS
WITH
inp AS (
  SELECT * FROM `your-gcp-project.address.input_raw` WHERE batch = @batch
),
texts AS (
  SELECT address_id, '도로명 열' AS src, road_address AS text FROM inp WHERE TRIM(IFNULL(road_address, '')) != ''
  UNION ALL
  SELECT address_id, '지번 열' AS src, lot_address AS text FROM inp WHERE TRIM(IFNULL(lot_address, '')) != ''
),
cands AS (
  SELECT t.address_id, t.src, REGEXP_REPLACE(NORMALIZE(t.text, NFKC), r'\s+', '') AS compact,
    NORMALIZE(t.text, NFKC) AS spaced,
    c.kind, c.name, c.mountain, CAST(c.main AS INT64) AS main, CAST(c.sub AS INT64) AS sub, c.matched
  -- BigQuery의 JavaScript 함수는 유니코드 정규화를 하지 않으므로(전각 숫자 ８６０) SQL에서 NFKC로 바꿔 넘긴다.
  FROM texts t, UNNEST(`your-gcp-project.address.parse_address_candidates`(NORMALIZE(t.text, NFKC))) c
),
nationwide_counts AS (
  -- 전국 유일 확인: 원문 후보(이름 + 번호)와 같은 건물·지번이 전국 기준표에 몇 곳인지 센다(adjusted의 unique_without_sigungu).
  SELECT k.kind, k.name, k.matched, COUNT(DISTINCT r.standard_road_address) AS n
  FROM (SELECT DISTINCT kind, name, mountain, main, IFNULL(sub, 0) AS sub, matched FROM cands) k
  JOIN `your-gcp-project.address.ref_road` r ON k.kind = 'road' AND r.road_key = k.name AND r.main_no = k.main AND r.sub_no = k.sub
  GROUP BY k.kind, k.name, k.matched
  UNION ALL
  SELECT k.kind, k.name, k.matched, COUNT(DISTINCT l.standard_lot_address) AS n
  FROM (SELECT DISTINCT kind, name, mountain, main, IFNULL(sub, 0) AS sub, matched FROM cands) k
  JOIN `your-gcp-project.address.ref_lot_keys` l ON k.kind = 'lot' AND l.lot_key = k.name AND l.lot_mountain = k.mountain
   AND l.lot_main = k.main AND l.lot_sub = k.sub
  GROUP BY k.kind, k.name, k.matched
),
sido_map AS (
  SELECT * FROM UNNEST([
    STRUCT('서울특별시' AS sido, '서울' AS short, '서울시' AS alt), ('부산광역시', '부산', '부산시'), ('대구광역시', '대구', '대구시'),
    ('인천광역시', '인천', '인천시'), ('광주광역시', '광주', '광주광역시'), ('대전광역시', '대전', '대전시'), ('울산광역시', '울산', '울산시'),
    ('세종특별자치시', '세종', '세종시'), ('경기도', '경기', '경기도'), ('강원특별자치도', '강원', '강원도'),
    ('충청북도', '충북', '충청북도'), ('충청남도', '충남', '충청남도'), ('전북특별자치도', '전북', '전라북도'),
    ('전라남도', '전남', '전라남도'), ('경상북도', '경북', '경상북도'), ('경상남도', '경남', '경상남도'), ('제주특별자치도', '제주', '제주도')])
),
hits AS (
  SELECT c.address_id, c.src, c.compact, c.kind, c.name, c.matched, c.sub IS NULL AS sub_missing,
    r.sub_no AS ref_sub, r.sub_no = IFNULL(c.sub, 0) AS exact,
    r.sido, r.sigungu, r.emd, r.standard_road_address AS road_addr, CAST(NULL AS STRING) AS lot_addr
  FROM cands c
  JOIN `your-gcp-project.address.ref_road` r
    ON c.kind = 'road' AND r.road_key = c.name AND r.main_no = c.main
  UNION ALL
  SELECT c.address_id, c.src, c.compact, c.kind, c.name, c.matched, c.sub IS NULL,
    l.lot_sub, l.lot_sub = IFNULL(c.sub, 0),
    l.sido, l.sigungu, l.emd, ra, l.standard_lot_address
  FROM cands c
  JOIN `your-gcp-project.address.ref_lot_keys` l
    ON c.kind = 'lot' AND l.lot_key = c.name AND l.lot_mountain = c.mountain AND l.lot_main = c.main
  LEFT JOIN UNNEST(l.road_addresses) ra
),
-- 원문에 실제 시군구 이름(예: 고흥군, 순천시, 남구)이 적혀 있는지. 적혀 있는데 후보의 시군구와 다르면 그 후보는 받지 않는다.
sigungu_names AS (
  SELECT DISTINCT p AS name
  FROM (SELECT DISTINCT sigungu FROM `your-gcp-project.address.ref_lot` WHERE sigungu IS NOT NULL),
    UNNEST(SPLIT(sigungu, ' ')) p
  WHERE CHAR_LENGTH(p) >= 2
),
stated AS (
  SELECT t.address_id, t.src, LOGICAL_OR(STRPOS(t.compact, n.name) > 0) AS sigungu_any
  FROM (SELECT DISTINCT address_id, src, compact FROM cands) t CROSS JOIN sigungu_names n
  GROUP BY t.address_id, t.src
),
-- 원문의 각 '이름+번호' 위치(matched)마다, 이 지역 기준자료에 실제로 있는 가장 긴 이름의 길이
longest AS (
  -- 원문에 띄어쓰기 없이 그대로 적힌 이름을 우선한다(동점동 태백로 → '태백로', 공백을 지워 생긴 '동태백로'가 아님).
  SELECT c.address_id, c.kind, c.matched,
    COALESCE(MAX(IF(STRPOS(c.spaced, c.name) > 0, CHAR_LENGTH(c.name), NULL)), MAX(CHAR_LENGTH(c.name))) AS max_len
  FROM cands c
  JOIN (SELECT DISTINCT road_key AS k, 'road' AS kind, sigungu FROM `your-gcp-project.address.ref_road`
        UNION ALL
        SELECT DISTINCT lot_key, 'lot', sigungu FROM `your-gcp-project.address.ref_lot_keys`) a
    ON a.kind = c.kind AND a.k = c.name
  WHERE a.sigungu IS NULL
     OR EXISTS (SELECT 1 FROM UNNEST(SPLIT(a.sigungu, ' ')) p WHERE STRPOS(c.compact, p) > 0
                OR (CHAR_LENGTH(p) >= 3 AND STRPOS(c.compact, SUBSTR(p, 1, CHAR_LENGTH(p) - 1)) > 0))
  GROUP BY c.address_id, c.kind, c.matched
),
scored AS (
  SELECT h.*,
    -- 시군구가 '포항시 남구'처럼 여러 단어면 모든 단어가 원문에 있어야 한다(부산 남구 ≠ 포항시 남구).
    -- 마지막 단어가 4자 이상이면(마산합포구) 그것만 있어도 된다. 단어마다 끝 글자(시·군·구)를 뺀 형태도 받는다.
    (NOT EXISTS (SELECT 1 FROM UNNEST(SPLIT(h.sigungu, ' ')) p
                 WHERE NOT (STRPOS(h.compact, p) > 0 OR (CHAR_LENGTH(p) >= 3 AND STRPOS(h.compact, SUBSTR(p, 1, CHAR_LENGTH(p) - 1)) > 0)))
     OR (CHAR_LENGTH(ARRAY_REVERSE(SPLIT(h.sigungu, ' '))[OFFSET(0)]) >= 4
         AND STRPOS(h.compact, ARRAY_REVERSE(SPLIT(h.sigungu, ' '))[OFFSET(0)]) > 0)) AND h.sigungu IS NOT NULL AS sigungu_hit,
    -- 시군구의 첫 단어(시)만 적은 원문(부천시 경인로 → 부천시 소사구)은 한 단계 낮은 점수로 받는다.
    IFNULL(ARRAY_LENGTH(SPLIT(h.sigungu, ' ')) >= 2 AND STRPOS(h.compact, SPLIT(h.sigungu, ' ')[OFFSET(0)]) > 0, FALSE) AS city_hit,
    -- 시도 약칭은 뒤에 '시'가 붙으면 시군구 이름이다(제주시, 광주시는 제주도·광주광역시가 아님).
    (STRPOS(h.compact, h.sido) > 0 OR STRPOS(h.compact, m.alt) > 0
     OR REGEXP_CONTAINS(h.compact, CONCAT(m.short, '([^시]|$)'))) AS sido_hit,
    STRPOS(h.compact, h.emd) > 0 AS emd_full_hit,
    (STRPOS(h.compact, h.emd) > 0 OR (CHAR_LENGTH(h.emd) >= 3 AND STRPOS(h.compact, SUBSTR(h.emd, 1, CHAR_LENGTH(h.emd) - 1)) > 0)) AS emd_hit,
    REGEXP_CONTAINS(h.compact, r'(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|충청|전북|전남|전라|경북|경남|경상|제주)([^시]|$)|특별시|광역시|특별자치') AS sido_any,
    -- 원문에 읍·면이 적혀 있는데 지번의 읍·면과 다르면(관산읍 삼산리 → 장흥읍의 삼산리) 받지 않는다.
    REGEXP_CONTAINS(h.compact, r'[가-힣]{1,4}[읍면](?:[가-힣]|$)') AS has_eup_myeon,
    IFNULL(st.sigungu_any, FALSE) AS sigungu_any
  FROM hits h LEFT JOIN sido_map m ON m.sido = h.sido
  LEFT JOIN stated st ON st.address_id = h.address_id AND st.src = h.src
),
valid AS (
  -- 점수: 시군구 16, 시군구의 시 이름만 8, 시도 2, 읍면동 1. 16 이상이면 시군구 확인, 8~15면 시 이름으로 확인, 0이면 지역 표기 없음.
  SELECT s.*, 16 * CAST(sigungu_hit AS INT64) + 8 * CAST(city_hit AND NOT sigungu_hit AS INT64)
           + 2 * CAST(sido_hit AS INT64) + CAST(emd_hit AS INT64) AS score,
    -- 정확 일치: 시군구 전체 이름이 원문에 있고(세종은 시도), 원문에 적힌 읍·면이 기준자료의 읍·면과 같다.
    (sigungu_hit OR (s.sigungu IS NULL AND sido_hit))
      AND NOT (has_eup_myeon AND (ENDS_WITH(s.emd, '읍') OR ENDS_WITH(s.emd, '면')) AND NOT emd_full_hit) AS region_exact,
    -- 원문에 적힌 읍·면이 기준자료와 같다(적혀 있지 않으면 참).
    NOT (has_eup_myeon AND (ENDS_WITH(s.emd, '읍') OR ENDS_WITH(s.emd, '면')) AND NOT emd_full_hit) AS eup_ok,
    -- 원문에 이 후보의 지역이 실제로 적혀 있다: 시도, 또는 읍면동 전체 이름(상남면 → '상남'은 상남로에도 있어 인정하지 않음).
    sido_hit OR emd_full_hit AS region_written
  FROM scored s
  -- 원문의 같은 위치에서 더 긴 이름이 이 지역의 기준자료에 있으면, 그 끝부분만 맞는 짧은 이름은 쓰지 않는다(한림중앙로 ≠ 중앙로).
  LEFT JOIN longest lg ON lg.address_id = s.address_id AND lg.kind = s.kind AND lg.matched = s.matched
  WHERE CHAR_LENGTH(s.name) >= IFNULL(lg.max_len, 0)
    AND NOT (sido_any AND NOT sido_hit)                          -- 원문에 다른 시도가 적혀 있으면 받지 않는다
    AND NOT (sigungu_any AND NOT sigungu_hit AND NOT city_hit)   -- 원문에 다른 시군구가 적혀 있으면 받지 않는다(고흥군 … ≠ 목포시)
    AND NOT (s.kind = 'lot' AND has_eup_myeon AND NOT emd_hit AND (ENDS_WITH(emd, '읍') OR ENDS_WITH(emd, '면')))
    AND (sigungu_hit
      OR city_hit
      OR NOT sido_any                                            -- 지역 표기가 전혀 없으면 점수 0으로 받고, 전국에 하나뿐일 때만 확정된다
      OR (sido_hit AND emd_hit)
      OR (sigungu IS NULL AND sido_hit)                          -- 세종은 시군구가 없으므로 시도만 맞으면 된다
      OR (NOT sido_any AND emd_full_hit AND CHAR_LENGTH(emd) >= 3)) -- 시도·시군구 없이 읍면동 전체 이름만 적은 원문(장항읍 옥남길)
),
exact_best AS (
  SELECT address_id, kind, MAX(score) AS best FROM valid WHERE exact GROUP BY address_id, kind
),
exact_sets AS (
  SELECT v.address_id, v.kind,
    ARRAY_AGG(DISTINCT v.road_addr IGNORE NULLS) AS roads,
    ARRAY_AGG(DISTINCT v.lot_addr IGNORE NULLS) AS lots,
    STRING_AGG(DISTINCT v.matched, ', ') AS matched,
    ANY_VALUE(b.best) AS best_score,
    LOGICAL_AND(v.region_exact) AS region_exact,
    LOGICAL_AND(v.eup_ok) AS eup_ok,
    LOGICAL_AND(v.region_written) AS region_written,
    LOGICAL_AND(IFNULL(nw.n, 0) = 1) AS unique_nationwide   -- 같은 이름 + 번호의 건물(지번)이 전국에 하나뿐
  FROM valid v JOIN exact_best b ON b.address_id = v.address_id AND b.kind = v.kind AND v.score = b.best
  LEFT JOIN nationwide_counts nw ON nw.kind = v.kind AND nw.name = v.name AND nw.matched = v.matched
  WHERE v.exact
  GROUP BY v.address_id, v.kind
),
near AS (
  SELECT v.address_id, v.kind, STRING_AGG(DISTINCT v.matched, ', ') AS matched, LOGICAL_OR(v.sub_missing) AS sub_missing,
    ARRAY_AGG(DISTINCT v.ref_sub ORDER BY v.ref_sub LIMIT 12) AS subs,
    ARRAY_AGG(DISTINCT IFNULL(v.road_addr, v.lot_addr) IGNORE NULLS ORDER BY IFNULL(v.road_addr, v.lot_addr) LIMIT 5) AS samples
  FROM valid v
  LEFT JOIN exact_best b ON b.address_id = v.address_id AND b.kind = v.kind
  WHERE NOT v.exact AND b.address_id IS NULL
  GROUP BY v.address_id, v.kind
),
parsed AS (
  SELECT DISTINCT address_id FROM cands
),
-- 한 원문에 주소가 여러 개 적힌 경우(시도가 둘 이상, 줄바꿈 뒤 새 주소, '우편물·본점·주소:', '외 N필지')는 하나로 확정하지 않는다.
multi AS (
  SELECT t.address_id,
    COUNT(DISTINCT IF(STRPOS(t.text, m.sido) > 0 OR STRPOS(t.text, m.alt) > 0
                      OR REGEXP_CONTAINS(t.text, CONCAT(r'(^|[\s,(])', m.short, r'(\s|$)')), m.sido, NULL)) >= 2
      OR LOGICAL_OR(REGEXP_CONTAINS(t.text, r'우편물|우편\s*[:(]|본점|지점\s*:|센터\s*:|주소\s*:|외\s*[0-9]+\s*필지')
                    -- 같은 시·군 이름이 두 번 나오면 주소가 둘 이상 적힌 것으로 본다(홍성군 판교리 산 125-2 충남 홍성군 홍성읍 …)
                    OR (SELECT COUNT(*) - COUNT(DISTINCT x) FROM UNNEST(REGEXP_EXTRACT_ALL(t.text, r'([가-힣]{1,5}[시군])(?:\s|$)')) x) > 0
                    OR REGEXP_CONTAINS(t.text, r'[0-9][^\n]*\n[^\n]*[가-힣](?:로|길|리|동)\s*[0-9]')) AS multi_address
  FROM texts t CROSS JOIN sido_map m
  GROUP BY t.address_id
),
known_names AS (
  SELECT DISTINCT c.address_id, c.kind, c.matched
  FROM cands c
  JOIN (SELECT DISTINCT lot_key AS k, sigungu FROM `your-gcp-project.address.ref_lot_keys`) a
    ON c.kind = 'lot' AND a.k = c.name
  WHERE (a.sigungu IS NULL AND STRPOS(c.compact, '세종') > 0)
     OR EXISTS (SELECT 1 FROM UNNEST(SPLIT(a.sigungu, ' ')) p WHERE STRPOS(c.compact, p) > 0
                OR (CHAR_LENGTH(p) >= 3 AND STRPOS(c.compact, SUBSTR(p, 1, CHAR_LENGTH(p) - 1)) > 0))
  UNION DISTINCT
  SELECT DISTINCT c.address_id, c.kind, c.matched
  FROM cands c
  JOIN (SELECT DISTINCT road_key AS k, sigungu FROM `your-gcp-project.address.ref_road`) a
    ON c.kind = 'road' AND a.k = c.name
  WHERE (a.sigungu IS NULL AND STRPOS(c.compact, '세종') > 0)
     OR EXISTS (SELECT 1 FROM UNNEST(SPLIT(a.sigungu, ' ')) p WHERE STRPOS(c.compact, p) > 0
                OR (CHAR_LENGTH(p) >= 3 AND STRPOS(c.compact, SUBSTR(p, 1, CHAR_LENGTH(p) - 1)) > 0))
),
known AS (
  SELECT address_id,
    STRING_AGG(DISTINCT IF(kind = 'lot', matched, NULL), ', ') AS lot_name_only,
    STRING_AGG(DISTINCT IF(kind = 'road', matched, NULL), ', ') AS road_name_only
  FROM known_names GROUP BY address_id
),
per AS (
  SELECT i.address_id, i.source_row, i.road_address, i.lot_address,
    IFNULL(r.roads, []) AS r_roads, r.matched AS r_matched, r.best_score AS r_score, l.best_score AS l_score,
    r.region_exact AS r_exact, l.region_exact AS l_exact,
    r.unique_nationwide AND r.eup_ok AND r.region_written AS r_unique,
    l.unique_nationwide AND l.eup_ok AND l.region_written AS l_unique,
    IFNULL(l.roads, []) AS l_roads, IFNULL(l.lots, []) AS l_lots, l.matched AS l_matched,
    nr.matched AS nr_matched, nr.sub_missing AS nr_sub_missing, nr.subs AS nr_subs, nr.samples AS nr_samples,
    nl.matched AS nl_matched, nl.sub_missing AS nl_sub_missing, nl.subs AS nl_subs, nl.samples AS nl_samples,
    k.lot_name_only, k.road_name_only,
    pa.address_id IS NOT NULL AS parsed_any,
    IFNULL(mu.multi_address, FALSE) AS multi_address,
    TRIM(IFNULL(i.road_address, '')) = '' AND TRIM(IFNULL(i.lot_address, '')) = '' AS blank
  FROM inp i
  LEFT JOIN exact_sets r ON r.address_id = i.address_id AND r.kind = 'road'
  LEFT JOIN exact_sets l ON l.address_id = i.address_id AND l.kind = 'lot'
  LEFT JOIN near nr ON nr.address_id = i.address_id AND nr.kind = 'road'
  LEFT JOIN near nl ON nl.address_id = i.address_id AND nl.kind = 'lot'
  LEFT JOIN known k ON k.address_id = i.address_id
  LEFT JOIN parsed pa ON pa.address_id = i.address_id
  LEFT JOIN multi mu ON mu.address_id = i.address_id
),
decided AS (
  SELECT p.*,
    ARRAY(SELECT x FROM UNNEST(p.r_roads) x WHERE x IN UNNEST(p.l_roads)) AS common,
    -- 원문에 기준자료의 도로명이 있는데 번호가 안 맞으면, 지번만으로 다른 도로의 건물을 확정하지 않는다.
    COALESCE(p.nr_matched, p.road_name_only) AS road_evidence
  FROM per p
),
final AS (
  SELECT d.*,
    CASE
      WHEN blank THEN '입력 없음'
      WHEN ARRAY_LENGTH(r_roads) = 1 AND (ARRAY_LENGTH(l_roads) = 0 OR r_roads[OFFSET(0)] IN UNNEST(l_roads)) THEN '확정'
      WHEN ARRAY_LENGTH(r_roads) > 1 AND ARRAY_LENGTH(common) = 1 THEN '확정'
      WHEN ARRAY_LENGTH(r_roads) >= 1 THEN '검토 필요'
      WHEN ARRAY_LENGTH(l_lots) = 1 AND ARRAY_LENGTH(l_roads) = 1 AND road_evidence IS NULL THEN '확정'
      WHEN ARRAY_LENGTH(l_lots) = 1 AND ARRAY_LENGTH(l_roads) = 1 THEN '검토 필요'
      WHEN ARRAY_LENGTH(l_lots) = 1 THEN '지번만 확정'
      WHEN ARRAY_LENGTH(l_lots) > 1 THEN '검토 필요'
      WHEN nr_matched IS NOT NULL OR nl_matched IS NOT NULL THEN '검토 필요'
      ELSE '미일치'
    END AS status,
    CASE
      WHEN ARRAY_LENGTH(r_roads) = 1 AND (ARRAY_LENGTH(l_roads) = 0 OR r_roads[OFFSET(0)] IN UNNEST(l_roads)) THEN r_roads[OFFSET(0)]
      WHEN ARRAY_LENGTH(r_roads) > 1 AND ARRAY_LENGTH(common) = 1 THEN common[OFFSET(0)]
      WHEN ARRAY_LENGTH(r_roads) = 0 AND ARRAY_LENGTH(l_lots) = 1 AND ARRAY_LENGTH(l_roads) = 1 AND road_evidence IS NULL
        THEN l_roads[OFFSET(0)]
    END AS standard_road_address,
    IF(ARRAY_LENGTH(l_lots) = 1, l_lots[OFFSET(0)], NULL) AS standard_lot_address,
    CASE
      WHEN blank THEN '도로명·지번 열이 모두 비어 있음'
      WHEN ARRAY_LENGTH(r_roads) = 1 AND ARRAY_LENGTH(l_roads) > 0 AND r_roads[OFFSET(0)] IN UNNEST(l_roads)
        THEN FORMAT('도로명 일치(%s) + 지번 일치(%s): 같은 건물로 교차 확인', r_matched, l_matched)
      WHEN ARRAY_LENGTH(r_roads) = 1 AND ARRAY_LENGTH(l_roads) = 0 AND l_matched IS NOT NULL
        THEN FORMAT('도로명 일치(%s), 지번(%s)은 건물 기준자료에 없음', r_matched, l_matched)
      WHEN ARRAY_LENGTH(r_roads) = 1 AND ARRAY_LENGTH(l_roads) = 0
        THEN FORMAT('도로명 일치(%s), 기준자료의 시군구가 원문과 같음', r_matched)
      WHEN ARRAY_LENGTH(r_roads) = 1
        THEN FORMAT('도로명(%s)과 지번(%s)이 서로 다른 건물을 가리킴: 도로명 %s / 지번 %s', r_matched, l_matched,
                    r_roads[OFFSET(0)], ARRAY_TO_STRING(l_roads, ', '))
      WHEN ARRAY_LENGTH(r_roads) > 1 AND ARRAY_LENGTH(common) = 1
        THEN FORMAT('도로명(%s) 후보 %d곳 중 지번(%s)으로 하나 확정', r_matched, ARRAY_LENGTH(r_roads), l_matched)
      WHEN ARRAY_LENGTH(r_roads) > 1
        THEN FORMAT('같은 도로명·번호(%s)가 %d곳에 있음: %s', r_matched, ARRAY_LENGTH(r_roads), ARRAY_TO_STRING(r_roads, ' / '))
      WHEN ARRAY_LENGTH(l_lots) = 1 AND ARRAY_LENGTH(l_roads) = 1 AND road_evidence IS NULL
        THEN FORMAT('지번 일치(%s): 그 지번의 건물 도로명주소가 하나뿐', l_matched)
      WHEN ARRAY_LENGTH(l_lots) = 1 AND ARRAY_LENGTH(l_roads) = 1
        THEN FORMAT('지번(%s)은 %s를 가리키나 원문의 도로명(%s)은 기준자료 번호와 맞지 않음',
                    l_matched, l_roads[OFFSET(0)], road_evidence)
      WHEN ARRAY_LENGTH(l_lots) = 1
        THEN FORMAT('지번 일치(%s), 그 지번에 건물 %d개: %s', l_matched, ARRAY_LENGTH(l_roads), ARRAY_TO_STRING(l_roads, ' / '))
      WHEN ARRAY_LENGTH(l_lots) > 1
        THEN FORMAT('같은 지번(%s)이 %d곳에 있음: %s', l_matched, ARRAY_LENGTH(l_lots), ARRAY_TO_STRING(l_lots, ' / '))
      WHEN nr_matched IS NOT NULL
        THEN FORMAT('%s(%s): 같은 본번의 기준자료 부번 %s · 예 %s',
                    IF(nr_sub_missing, '도로명 부번 생략', '도로명 부번 불일치'), nr_matched,
                    ARRAY_TO_STRING(ARRAY(SELECT CAST(s AS STRING) FROM UNNEST(nr_subs) s), ','), ARRAY_TO_STRING(nr_samples, ' / '))
      WHEN nl_matched IS NOT NULL
        THEN FORMAT('%s(%s): 같은 본번의 기준자료 부번 %s · 예 %s',
                    IF(nl_sub_missing, '지번 부번 생략', '지번 부번 불일치'), nl_matched,
                    ARRAY_TO_STRING(ARRAY(SELECT CAST(s AS STRING) FROM UNNEST(nl_subs) s), ','), ARRAY_TO_STRING(nl_samples, ' / '))
      WHEN lot_name_only IS NOT NULL
        THEN FORMAT('리·동은 있으나 그 지번(%s)이 건물 기준자료에 없음: 건물 없는 토지일 가능성', lot_name_only)
      WHEN road_name_only IS NOT NULL
        THEN FORMAT('도로명은 있으나 그 건물번호(%s)가 기준자료에 없음', road_name_only)
      WHEN parsed_any THEN '번호는 읽었으나 원문의 지역·도로명이 기준자료와 맞지 않음(오타·옛 지명·지역 누락 가능성)'
      ELSE '원문에서 도로명+번호 또는 리·동+지번을 찾지 못함'
    END AS reason,
    -- 위 사유를 파이프라인(address_standardizer)의 사유 코드로 옮긴 값. 확정이면 NULL.
    CASE
      WHEN blank THEN 'blank_input'
      WHEN ARRAY_LENGTH(r_roads) = 1 AND (ARRAY_LENGTH(l_roads) = 0 OR r_roads[OFFSET(0)] IN UNNEST(l_roads)) THEN NULL
      WHEN ARRAY_LENGTH(r_roads) = 1 THEN 'road_lot_conflict'
      WHEN ARRAY_LENGTH(r_roads) > 1 AND ARRAY_LENGTH(common) = 1 THEN NULL
      WHEN ARRAY_LENGTH(r_roads) > 1 THEN 'multiple_standard_outputs'
      WHEN ARRAY_LENGTH(l_lots) = 1 AND ARRAY_LENGTH(l_roads) = 1 AND road_evidence IS NULL THEN NULL
      WHEN ARRAY_LENGTH(l_lots) = 1 AND ARRAY_LENGTH(l_roads) = 1 THEN 'road_lot_conflict'
      WHEN ARRAY_LENGTH(l_lots) = 1 THEN 'multiple_road_addresses'
      WHEN ARRAY_LENGTH(l_lots) > 1 THEN 'multiple_standard_outputs'
      WHEN nr_matched IS NOT NULL THEN IF(nr_sub_missing, 'sub_number_omitted', 'sub_number_mismatch')
      WHEN nl_matched IS NOT NULL THEN IF(nl_sub_missing, 'sub_number_omitted', 'sub_number_mismatch')
      WHEN lot_name_only IS NOT NULL OR road_name_only IS NOT NULL THEN 'number_not_found'
      WHEN parsed_any THEN 'scope_not_found'
      ELSE 'missing_address_number'
    END AS reason_code,
    ARRAY_TO_STRING(ARRAY_CONCAT(r_roads, ARRAY(SELECT x FROM UNNEST(l_roads) x WHERE x NOT IN UNNEST(r_roads))), ' / ') AS candidates,
    -- 검토 화면에 보일 후보: 도로명주소(정확 일치·지번의 건물·같은 본번의 다른 부번 예)와 지번
    ARRAY(SELECT DISTINCT x FROM UNNEST(ARRAY_CONCAT(r_roads, l_roads, IFNULL(nr_samples, []), IFNULL(nl_samples, []))) x) AS candidate_roads,
    l_lots AS candidate_lots
  FROM decided d
),
adjusted AS (
  -- 확정을 두 경우에 다시 검토로 내린다. ① 한 원문에 주소가 여러 개 ② 지역 표기가 기준자료와 정확히 일치하지 않음
  -- 단 ②에서 시군구만 빠졌고 같은 이름 + 번호가 전국에 하나뿐이면 확정한다(unique_without_sigungu).
  --   (구 이름 없이 시만 적음, 지역 표기 없음, 읍면동만 적음, 원문의 읍·면이 기준자료와 다름)
  SELECT * REPLACE (
    IF(demote IS NOT NULL, '검토 필요', status) AS status,
    IF(demote IS NOT NULL, NULL, standard_road_address) AS standard_road_address,
    IF(demote IS NOT NULL, NULL, standard_lot_address) AS standard_lot_address,
    CASE
      WHEN demote = 'multiple_addresses'
        THEN CONCAT('원문에 주소가 여러 개 있어 확정하지 않음 · ', reason, ' · 후보 ', IFNULL(standard_road_address, standard_lot_address))
      WHEN demote = 'region_partial'
        THEN CONCAT('번호는 일치하나 지역 표기가 기준자료와 정확히 일치하지 않아 확정하지 않음(',
                    CASE WHEN match_score = 0 THEN '원문에 지역 표기 없음'
                         WHEN match_score BETWEEN 8 AND 15 THEN '시군구 중 시 이름만 일치'
                         WHEN match_score < 8 THEN '시군구 표기 없음·불일치'
                         ELSE '원문의 읍·면이 기준자료와 다름' END,
                    ') · ', reason, ' · 후보 ', IFNULL(standard_road_address, standard_lot_address))
      WHEN unique_without_sigungu
        THEN CONCAT(reason, ' · 원문에 시군구 없음: 같은 도로명·번호(지번)가 전국에 하나뿐이고 적힌 시도·읍면동과 맞아 확정')
      ELSE reason
    END AS reason,
    IFNULL(demote, reason_code) AS reason_code)
  FROM (
    SELECT *,
      CASE
        WHEN status NOT IN ('확정', '지번만 확정') THEN NULL
        WHEN multi_address THEN 'multiple_addresses'
        WHEN NOT IFNULL(IF(ARRAY_LENGTH(r_roads) > 0, r_exact, l_exact), FALSE) AND NOT unique_without_sigungu THEN 'region_partial'
      END AS demote
    FROM (
      SELECT *,
        IF(ARRAY_LENGTH(r_roads) > 0, r_score, l_score) AS match_score,
        -- 시군구 없이 쓴 주소의 확정(엄격): 시군구·시 이름은 없고(점수 1~7) 시도나 읍면동 전체 이름이 적혀 있으며,
        -- 적힌 지역이 하나도 어긋나지 않고(다른 시도·시군구는 valid에서 이미 뺐고 읍·면은 eup_ok),
        -- 같은 이름 + 번호가 전국에 하나뿐(unique_nationwide).
        NOT IFNULL(IF(ARRAY_LENGTH(r_roads) > 0, r_exact, l_exact), FALSE)
          AND IFNULL(IF(ARRAY_LENGTH(r_roads) > 0, r_score, l_score), 0) BETWEEN 1 AND 7
          AND IFNULL(IF(ARRAY_LENGTH(r_roads) > 0, r_unique, l_unique), FALSE) AS unique_without_sigungu
      FROM final
    )
  )
),
codes AS (
  SELECT r.standard_road_address,
    ARRAY_AGG(DISTINCT r.road_name_code IGNORE NULLS) AS road_codes,
    ARRAY_AGG(DISTINCT r.legal_dong_code IGNORE NULLS) AS legal_codes,
    ARRAY_CONCAT_AGG(r.administrative_area_codes) AS admin_codes
  FROM `your-gcp-project.address.ref_road` r
  JOIN (SELECT DISTINCT standard_road_address FROM adjusted WHERE standard_road_address IS NOT NULL) w
    ON w.standard_road_address = r.standard_road_address
  GROUP BY r.standard_road_address
)
SELECT
  f.address_id, f.source_row, f.road_address AS input_road_address, f.lot_address AS input_lot_address,
  f.status, f.standard_road_address, f.standard_lot_address,
  IF(ARRAY_LENGTH(c.road_codes) = 1, c.road_codes[OFFSET(0)], NULL) AS road_name_code,
  IF(ARRAY_LENGTH(c.legal_codes) = 1, c.legal_codes[OFFSET(0)], NULL) AS legal_dong_code,
  (SELECT IF(COUNT(DISTINCT x) = 1, ANY_VALUE(x), NULL) FROM UNNEST(c.admin_codes) x) AS administrative_area_code,
  f.reason, NULLIF(f.candidates, '') AS candidates,
  f.reason_code, f.candidate_roads, f.candidate_lots
FROM adjusted f LEFT JOIN codes c USING (standard_road_address);
