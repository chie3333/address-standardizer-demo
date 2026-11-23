-- 2단계 정확 매칭 + 3단계 판정: 원문 후보와 기준자료를 정확히 맞추고 확정·보류·미일치를 정한다.
-- 규칙은 bigquery/sql/05_standardize_all.sql과 같다(DuckDB 문법). 전국 기준표 대신 이번 입력 범위의 임시 표(2_scope_reference)를 읽는다.
-- 첫 문장(input_cands) 다음에 프로그램이 2_scope_reference.sql을 실행해 범위 임시 표를 만든다.
--   1) parse_address_candidates로 '이름 + 번호' 후보를 모두 뽑는다.
--   2) ref_road(도로명+건물번호), ref_lot_keys(리·동+산+지번)와 정확 조인한다. 같은 본번의 다른 부번도 함께 가져온다.
--   3) 기준자료의 시도·시군구·읍면동이 원문에 적혀 있는지로 지역을 확인한다.
--   4) 도로명 결과와 지번 결과를 함께 보고 판정한다.
-- BigQuery와 다른 표기: 배열 첫 원소 [1], x IN UNNEST(a) → list_contains(a, x), 'UNNEST(SPLIT(시군구))' 확인 → 매크로
-- all_parts_in / any_part_in, 목록의 중복 제거 → 매크로 uniq(처음 나온 순서 유지).
-- Python 함수(nfkc, parse_address_candidates)는 큰 쿼리 안에서 부르지 않고 먼저 임시 표로 뽑는다
-- (DuckDB 2.0에서 Python 함수가 복잡한 실행 계획 중간에 있으면 내부 오류가 난 적이 있다).
CREATE OR REPLACE TEMP TABLE input_cands AS
WITH normed AS (
  SELECT address_id, src, nfkc(text) AS spaced FROM (
    SELECT address_id, '도로명 열' AS src, road_address AS text FROM input_raw
    WHERE batch = $batch AND TRIM(IFNULL(road_address, '')) != ''
    UNION ALL
    SELECT address_id, '지번 열' AS src, lot_address AS text FROM input_raw
    WHERE batch = $batch AND TRIM(IFNULL(lot_address, '')) != '')
)
SELECT p.address_id, p.src, regexp_replace(p.spaced, '\s+', '', 'g') AS compact, p.spaced,
  p.c.kind AS kind, p.c.name AS name, p.c.mountain AS mountain, p.c.main AS main, p.c.sub AS sub, p.c.matched AS matched
FROM (SELECT n.*, unnest(parse_address_candidates(n.spaced)) AS c FROM normed n) p;

-- 후보와 기준자료의 조인 결과(hits)도 임시 표로 둔다. 이것을 한 쿼리 안에서 작은 표와 LEFT JOIN하면
-- DuckDB 2.0이 내부 오류(dict-surviving cache … changed identity)를 냈다.
-- 전국 유일 확인: 원문 후보(이름 + 번호)와 같은 건물·지번이 전국에 몇 곳인지 RAW 전체에서 센다.
-- 매칭은 원문에 적힌 지역 범위만 읽으므로, 시군구 없이 쓴 주소를 확정해도 되는지는 이 표로 따로 본다(아래 adjusted).
CREATE OR REPLACE TEMP TABLE nationwide_counts AS
WITH keys AS (
  SELECT DISTINCT kind, name, mountain, main, IFNULL(sub, 0) AS sub, matched FROM input_cands
),
road AS (
  SELECT k.kind, k.name, k.matched, count(DISTINCT r.standard_road_address) AS n
  FROM keys k JOIN raw_clean_all r ON k.kind = 'road' AND r.road_key = k.name AND r.main_no = k.main AND r.sub_no = k.sub
  GROUP BY k.kind, k.name, k.matched
),
lot_rows AS (
  SELECT k.kind, k.name, k.matched, r.standard_lot_address FROM keys k JOIN raw_clean_all r
    ON k.kind = 'lot' AND r.lot_area_key = k.name AND r.lot_mountain = k.mountain AND r.lot_main = k.main AND r.lot_sub = k.sub
  UNION ALL
  SELECT k.kind, k.name, k.matched, r.standard_lot_address FROM keys k JOIN raw_clean_all r
    ON k.kind = 'lot' AND r.ri IS NOT NULL AND r.emd_key = k.name AND r.lot_mountain = k.mountain AND r.lot_main = k.main AND r.lot_sub = k.sub
  UNION ALL
  SELECT k.kind, k.name, k.matched, r.standard_lot_address FROM keys k JOIN raw_clean_all r
    ON k.kind = 'lot' AND r.admin_dong != r.emd AND r.admin_dong_key = k.name AND r.lot_mountain = k.mountain
   AND r.lot_main = k.main AND r.lot_sub = k.sub
),
lot AS (
  SELECT kind, name, matched, count(DISTINCT standard_lot_address) AS n FROM lot_rows GROUP BY kind, name, matched
)
SELECT * FROM road UNION ALL SELECT * FROM lot;

CREATE OR REPLACE TEMP TABLE match_hits AS
WITH cands AS (
  SELECT * FROM input_cands
)
  SELECT c.address_id, c.src, c.compact, c.kind, c.name, c.matched, c.sub IS NULL AS sub_missing,
    r.sub_no AS ref_sub, r.sub_no = IFNULL(c.sub, 0) AS exact,
    r.sido, r.sigungu, r.emd, r.standard_road_address AS road_addr, CAST(NULL AS VARCHAR) AS lot_addr
  FROM cands c
  JOIN ref_road r ON c.kind = 'road' AND r.road_key = c.name AND r.main_no = c.main
  UNION ALL
  -- 그 지번에 건물 도로명주소가 없어도 지번 행은 남긴다(BigQuery의 LEFT JOIN UNNEST).
  SELECT address_id, src, compact, kind, name, matched, sub_missing, lot_sub, lot_sub = IFNULL(sub, 0),
    sido, sigungu, emd, unnest(if(len(road_addresses) = 0, [NULL::VARCHAR], road_addresses)), standard_lot_address
  FROM (
    SELECT c.address_id, c.src, c.compact, c.kind, c.name, c.matched, c.sub IS NULL AS sub_missing, c.sub,
      l.lot_sub, l.sido, l.sigungu, l.emd, l.road_addresses, l.standard_lot_address
    FROM cands c
    JOIN ref_lot_keys l ON c.kind = 'lot' AND l.lot_key = c.name AND l.lot_mountain = c.mountain AND l.lot_main = c.main
  );

CREATE OR REPLACE TEMP TABLE engine_standardized AS
WITH
inp AS (
  SELECT * FROM input_raw WHERE batch = $batch
),
texts AS (
  SELECT address_id, '도로명 열' AS src, road_address AS text FROM inp WHERE TRIM(IFNULL(road_address, '')) != ''
  UNION ALL
  SELECT address_id, '지번 열' AS src, lot_address AS text FROM inp WHERE TRIM(IFNULL(lot_address, '')) != ''
),
cands AS (
  SELECT * FROM input_cands
),
sido_map AS (
  SELECT * FROM (VALUES
    ('서울특별시', '서울', '서울시'), ('부산광역시', '부산', '부산시'), ('대구광역시', '대구', '대구시'),
    ('인천광역시', '인천', '인천시'), ('광주광역시', '광주', '광주광역시'), ('대전광역시', '대전', '대전시'), ('울산광역시', '울산', '울산시'),
    ('세종특별자치시', '세종', '세종시'), ('경기도', '경기', '경기도'), ('강원특별자치도', '강원', '강원도'),
    ('충청북도', '충북', '충청북도'), ('충청남도', '충남', '충청남도'), ('전북특별자치도', '전북', '전라북도'),
    ('전라남도', '전남', '전라남도'), ('경상북도', '경북', '경상북도'), ('경상남도', '경남', '경상남도'), ('제주특별자치도', '제주', '제주도')
  ) v(sido, short, alt)
),
hits AS (
  SELECT * FROM match_hits
),
-- 원문에 실제 시군구 이름(예: 고흥군, 순천시, 남구)이 적혀 있는지. 적혀 있는데 후보의 시군구와 다르면 그 후보는 받지 않는다.
sigungu_names AS (
  SELECT name FROM sigungu_parts   -- 전국 시군구 단어(0_sources). 범위 임시 표가 아니라 전국 목록이어야 한다.
),
stated AS (
  SELECT t.address_id, t.src, bool_or(strpos(t.compact, n.name) > 0) AS sigungu_any
  FROM (SELECT DISTINCT address_id, src, compact FROM cands) t CROSS JOIN sigungu_names n
  GROUP BY t.address_id, t.src
),
-- 원문의 각 '이름+번호' 위치(matched)마다, 이 지역 기준자료에 실제로 있는 가장 긴 이름의 길이
known_keys AS (
  SELECT DISTINCT road_key AS k, 'road' AS kind, sigungu FROM ref_road
  UNION ALL
  SELECT DISTINCT lot_key, 'lot', sigungu FROM ref_lot_keys
),
longest AS (
  -- 원문에 띄어쓰기 없이 그대로 적힌 이름을 우선한다(동점동 태백로 → '태백로', 공백을 지워 생긴 '동태백로'가 아님).
  SELECT c.address_id, c.kind, c.matched,
    COALESCE(max(if(strpos(c.spaced, c.name) > 0, length(c.name), NULL)), max(length(c.name))) AS max_len
  FROM cands c
  JOIN known_keys a ON a.kind = c.kind AND a.k = c.name
  WHERE a.sigungu IS NULL OR any_part_in(c.compact, a.sigungu)
  GROUP BY c.address_id, c.kind, c.matched
),
scored AS (
  SELECT h.*,
    -- 시군구가 '포항시 남구'처럼 여러 단어면 모든 단어가 원문에 있어야 한다. 마지막 단어가 4자 이상이면(마산합포구) 그것만 있어도 된다.
    (all_parts_in(h.compact, h.sigungu)
     OR (length(string_split(h.sigungu, ' ')[-1]) >= 4 AND strpos(h.compact, string_split(h.sigungu, ' ')[-1]) > 0))
      AND h.sigungu IS NOT NULL AS sigungu_hit,
    -- 시군구의 첫 단어(시)만 적은 원문(부천시 경인로 → 부천시 소사구)은 한 단계 낮은 점수로 받는다.
    IFNULL(len(string_split(h.sigungu, ' ')) >= 2 AND strpos(h.compact, string_split(h.sigungu, ' ')[1]) > 0, FALSE) AS city_hit,
    -- 시도 약칭은 뒤에 '시'가 붙으면 시군구 이름이다(제주시, 광주시는 제주도·광주광역시가 아님).
    (strpos(h.compact, h.sido) > 0 OR strpos(h.compact, m.alt) > 0
     OR regexp_matches(h.compact, m.short || '([^시]|$)')) AS sido_hit,
    strpos(h.compact, h.emd) > 0 AS emd_full_hit,
    (strpos(h.compact, h.emd) > 0 OR (length(h.emd) >= 3 AND strpos(h.compact, substr(h.emd, 1, length(h.emd) - 1)) > 0)) AS emd_hit,
    regexp_matches(h.compact, '(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|충청|전북|전남|전라|경북|경남|경상|제주)([^시]|$)|특별시|광역시|특별자치') AS sido_any,
    -- 원문에 읍·면이 적혀 있는데 지번의 읍·면과 다르면(관산읍 삼산리 → 장흥읍의 삼산리) 받지 않는다.
    regexp_matches(h.compact, '[가-힣]{1,4}[읍면](?:[가-힣]|$)') AS has_eup_myeon,
    IFNULL(st.sigungu_any, FALSE) AS sigungu_any
  FROM hits h LEFT JOIN sido_map m ON m.sido = h.sido
  LEFT JOIN stated st ON st.address_id = h.address_id AND st.src = h.src
),
valid AS (
  -- 점수: 시군구 16, 시군구의 시 이름만 8, 시도 2, 읍면동 1.
  SELECT s.*, 16 * CAST(sigungu_hit AS BIGINT) + 8 * CAST(city_hit AND NOT sigungu_hit AS BIGINT)
           + 2 * CAST(sido_hit AS BIGINT) + CAST(emd_hit AS BIGINT) AS score,
    (sigungu_hit OR (s.sigungu IS NULL AND sido_hit))
      AND NOT (has_eup_myeon AND (ends_with(s.emd, '읍') OR ends_with(s.emd, '면')) AND NOT emd_full_hit) AS region_exact,
    -- 원문에 적힌 읍·면이 기준자료와 같다(적혀 있지 않으면 참).
    NOT (has_eup_myeon AND (ends_with(s.emd, '읍') OR ends_with(s.emd, '면')) AND NOT emd_full_hit) AS eup_ok,
    -- 원문에 이 후보의 지역이 실제로 적혀 있다: 시도, 또는 읍면동 전체 이름. 끝 글자를 뗀 형태(상남면 → '상남')는
    -- 도로명 안에도 들어 있을 수 있어(상남로) 인정하지 않는다.
    sido_hit OR emd_full_hit AS region_written
  FROM scored s
  -- 원문의 같은 위치에서 더 긴 이름이 이 지역의 기준자료에 있으면, 그 끝부분만 맞는 짧은 이름은 쓰지 않는다(한림중앙로 ≠ 중앙로).
  LEFT JOIN longest lg ON lg.address_id = s.address_id AND lg.kind = s.kind AND lg.matched = s.matched
  WHERE length(s.name) >= IFNULL(lg.max_len, 0)
    AND NOT (sido_any AND NOT sido_hit)
    AND NOT (sigungu_any AND NOT sigungu_hit AND NOT city_hit)
    AND NOT (s.kind = 'lot' AND has_eup_myeon AND NOT emd_hit AND (ends_with(emd, '읍') OR ends_with(emd, '면')))
    AND (sigungu_hit
      OR city_hit
      OR NOT sido_any
      OR (sido_hit AND emd_hit)
      OR (sigungu IS NULL AND sido_hit)
      OR (NOT sido_any AND emd_full_hit AND length(emd) >= 3))
),
exact_best AS (
  SELECT address_id, kind, max(score) AS best FROM valid WHERE exact GROUP BY address_id, kind
),
exact_sets AS (
  SELECT v.address_id, v.kind,
    coalesce(list(DISTINCT v.road_addr ORDER BY v.road_addr) FILTER (WHERE v.road_addr IS NOT NULL), []) AS roads,
    coalesce(list(DISTINCT v.lot_addr ORDER BY v.lot_addr) FILTER (WHERE v.lot_addr IS NOT NULL), []) AS lots,
    string_agg(DISTINCT v.matched, ', ' ORDER BY v.matched) AS matched,
    any_value(b.best) AS best_score,
    bool_and(v.region_exact) AS region_exact,
    bool_and(v.eup_ok) AS eup_ok,
    bool_and(v.region_written) AS region_written,
    -- 같은 이름 + 번호의 건물(지번)이 전국에 하나뿐
    bool_and(IFNULL(nw.n, 0) = 1) AS unique_nationwide
  FROM valid v JOIN exact_best b ON b.address_id = v.address_id AND b.kind = v.kind AND v.score = b.best
  LEFT JOIN nationwide_counts nw ON nw.kind = v.kind AND nw.name = v.name AND nw.matched = v.matched
  WHERE v.exact
  GROUP BY v.address_id, v.kind
),
near AS (
  SELECT v.address_id, v.kind, string_agg(DISTINCT v.matched, ', ' ORDER BY v.matched) AS matched,
    bool_or(v.sub_missing) AS sub_missing,
    list(DISTINCT v.ref_sub ORDER BY v.ref_sub)[1:12] AS subs,
    (list(DISTINCT IFNULL(v.road_addr, v.lot_addr) ORDER BY IFNULL(v.road_addr, v.lot_addr))
       FILTER (WHERE IFNULL(v.road_addr, v.lot_addr) IS NOT NULL))[1:5] AS samples
  FROM valid v
  LEFT JOIN exact_best b ON b.address_id = v.address_id AND b.kind = v.kind
  WHERE NOT v.exact AND b.address_id IS NULL
  GROUP BY v.address_id, v.kind
),
parsed AS (
  SELECT DISTINCT address_id FROM cands
),
-- 한 원문에 주소가 여러 개 적힌 경우는 하나로 확정하지 않는다.
multi AS (
  SELECT t.address_id,
    count(DISTINCT if(strpos(t.text, m.sido) > 0 OR strpos(t.text, m.alt) > 0
                      OR regexp_matches(t.text, '(^|[\s,(])' || m.short || '(\s|$)'), m.sido, NULL)) >= 2
      OR bool_or(regexp_matches(t.text, '우편물|우편\s*[:(]|본점|지점\s*:|센터\s*:|주소\s*:|외\s*[0-9]+\s*필지')
                 -- 같은 시·군 이름이 두 번 나오면 주소가 둘 이상 적힌 것으로 본다
                 OR len(regexp_extract_all(t.text, '([가-힣]{1,5}[시군])(?:\s|$)', 1))
                    - len(list_distinct(regexp_extract_all(t.text, '([가-힣]{1,5}[시군])(?:\s|$)', 1))) > 0
                 OR regexp_matches(t.text, '[0-9][^\n]*\n[^\n]*[가-힣](?:로|길|리|동)\s*[0-9]')) AS multi_address
  FROM texts t CROSS JOIN sido_map m
  GROUP BY t.address_id
),
known_names AS (
  SELECT DISTINCT c.address_id, c.kind, c.matched
  FROM cands c
  JOIN (SELECT DISTINCT lot_key AS k, sigungu FROM ref_lot_keys) a ON c.kind = 'lot' AND a.k = c.name
  WHERE (a.sigungu IS NULL AND strpos(c.compact, '세종') > 0) OR any_part_in(c.compact, a.sigungu)
  UNION
  SELECT DISTINCT c.address_id, c.kind, c.matched
  FROM cands c
  JOIN (SELECT DISTINCT road_key AS k, sigungu FROM ref_road) a ON c.kind = 'road' AND a.k = c.name
  WHERE (a.sigungu IS NULL AND strpos(c.compact, '세종') > 0) OR any_part_in(c.compact, a.sigungu)
),
known AS (
  SELECT address_id,
    string_agg(DISTINCT if(kind = 'lot', matched, NULL), ', ' ORDER BY if(kind = 'lot', matched, NULL)) AS lot_name_only,
    string_agg(DISTINCT if(kind = 'road', matched, NULL), ', ' ORDER BY if(kind = 'road', matched, NULL)) AS road_name_only
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
    list_filter(p.r_roads, lambda x: list_contains(p.l_roads, x)) AS common,
    -- 원문에 기준자료의 도로명이 있는데 번호가 안 맞으면, 지번만으로 다른 도로의 건물을 확정하지 않는다.
    COALESCE(p.nr_matched, p.road_name_only) AS road_evidence
  FROM per p
),
final AS (
  SELECT d.*,
    CASE
      WHEN blank THEN '입력 없음'
      WHEN len(r_roads) = 1 AND (len(l_roads) = 0 OR list_contains(l_roads, r_roads[1])) THEN '확정'
      WHEN len(r_roads) > 1 AND len(common) = 1 THEN '확정'
      WHEN len(r_roads) >= 1 THEN '검토 필요'
      WHEN len(l_lots) = 1 AND len(l_roads) = 1 AND road_evidence IS NULL THEN '확정'
      WHEN len(l_lots) = 1 AND len(l_roads) = 1 THEN '검토 필요'
      WHEN len(l_lots) = 1 THEN '지번만 확정'
      WHEN len(l_lots) > 1 THEN '검토 필요'
      WHEN nr_matched IS NOT NULL OR nl_matched IS NOT NULL THEN '검토 필요'
      ELSE '미일치'
    END AS status,
    CASE
      WHEN len(r_roads) = 1 AND (len(l_roads) = 0 OR list_contains(l_roads, r_roads[1])) THEN r_roads[1]
      WHEN len(r_roads) > 1 AND len(common) = 1 THEN common[1]
      WHEN len(r_roads) = 0 AND len(l_lots) = 1 AND len(l_roads) = 1 AND road_evidence IS NULL THEN l_roads[1]
    END AS standard_road_address,
    if(len(l_lots) = 1, l_lots[1], NULL) AS standard_lot_address,
    CASE
      WHEN blank THEN '도로명·지번 열이 모두 비어 있음'
      WHEN len(r_roads) = 1 AND len(l_roads) > 0 AND list_contains(l_roads, r_roads[1])
        THEN printf('도로명 일치(%s) + 지번 일치(%s): 같은 건물로 교차 확인', r_matched, l_matched)
      WHEN len(r_roads) = 1 AND len(l_roads) = 0 AND l_matched IS NOT NULL
        THEN printf('도로명 일치(%s), 지번(%s)은 건물 기준자료에 없음', r_matched, l_matched)
      WHEN len(r_roads) = 1 AND len(l_roads) = 0
        THEN printf('도로명 일치(%s), 기준자료의 시군구가 원문과 같음', r_matched)
      WHEN len(r_roads) = 1
        THEN printf('도로명(%s)과 지번(%s)이 서로 다른 건물을 가리킴: 도로명 %s / 지번 %s', r_matched, l_matched,
                    r_roads[1], array_to_string(l_roads, ', '))
      WHEN len(r_roads) > 1 AND len(common) = 1
        THEN printf('도로명(%s) 후보 %d곳 중 지번(%s)으로 하나 확정', r_matched, len(r_roads), l_matched)
      WHEN len(r_roads) > 1
        THEN printf('같은 도로명·번호(%s)가 %d곳에 있음: %s', r_matched, len(r_roads), array_to_string(r_roads, ' / '))
      WHEN len(l_lots) = 1 AND len(l_roads) = 1 AND road_evidence IS NULL
        THEN printf('지번 일치(%s): 그 지번의 건물 도로명주소가 하나뿐', l_matched)
      WHEN len(l_lots) = 1 AND len(l_roads) = 1
        THEN printf('지번(%s)은 %s를 가리키나 원문의 도로명(%s)은 기준자료 번호와 맞지 않음',
                    l_matched, l_roads[1], road_evidence)
      WHEN len(l_lots) = 1
        THEN printf('지번 일치(%s), 그 지번에 건물 %d개: %s', l_matched, len(l_roads), array_to_string(l_roads, ' / '))
      WHEN len(l_lots) > 1
        THEN printf('같은 지번(%s)이 %d곳에 있음: %s', l_matched, len(l_lots), array_to_string(l_lots, ' / '))
      WHEN nr_matched IS NOT NULL
        THEN printf('%s(%s): 같은 본번의 기준자료 부번 %s · 예 %s',
                    if(nr_sub_missing, '도로명 부번 생략', '도로명 부번 불일치'), nr_matched,
                    array_to_string(nr_subs, ','), array_to_string(nr_samples, ' / '))
      WHEN nl_matched IS NOT NULL
        THEN printf('%s(%s): 같은 본번의 기준자료 부번 %s · 예 %s',
                    if(nl_sub_missing, '지번 부번 생략', '지번 부번 불일치'), nl_matched,
                    array_to_string(nl_subs, ','), array_to_string(nl_samples, ' / '))
      WHEN lot_name_only IS NOT NULL
        THEN printf('리·동은 있으나 그 지번(%s)이 건물 기준자료에 없음: 건물 없는 토지일 가능성', lot_name_only)
      WHEN road_name_only IS NOT NULL
        THEN printf('도로명은 있으나 그 건물번호(%s)가 기준자료에 없음', road_name_only)
      WHEN parsed_any THEN '번호는 읽었으나 원문의 지역·도로명이 기준자료와 맞지 않음(오타·옛 지명·지역 누락 가능성)'
      ELSE '원문에서 도로명+번호 또는 리·동+지번을 찾지 못함'
    END AS reason,
    CASE
      WHEN blank THEN 'blank_input'
      WHEN len(r_roads) = 1 AND (len(l_roads) = 0 OR list_contains(l_roads, r_roads[1])) THEN NULL
      WHEN len(r_roads) = 1 THEN 'road_lot_conflict'
      WHEN len(r_roads) > 1 AND len(common) = 1 THEN NULL
      WHEN len(r_roads) > 1 THEN 'multiple_standard_outputs'
      WHEN len(l_lots) = 1 AND len(l_roads) = 1 AND road_evidence IS NULL THEN NULL
      WHEN len(l_lots) = 1 AND len(l_roads) = 1 THEN 'road_lot_conflict'
      WHEN len(l_lots) = 1 THEN 'multiple_road_addresses'
      WHEN len(l_lots) > 1 THEN 'multiple_standard_outputs'
      WHEN nr_matched IS NOT NULL THEN if(nr_sub_missing, 'sub_number_omitted', 'sub_number_mismatch')
      WHEN nl_matched IS NOT NULL THEN if(nl_sub_missing, 'sub_number_omitted', 'sub_number_mismatch')
      WHEN lot_name_only IS NOT NULL OR road_name_only IS NOT NULL THEN 'number_not_found'
      WHEN parsed_any THEN 'scope_not_found'
      ELSE 'missing_address_number'
    END AS reason_code,
    array_to_string(list_concat(r_roads, list_filter(l_roads, lambda x: NOT list_contains(r_roads, x))), ' / ') AS candidates,
    uniq(list_concat(r_roads, l_roads, IFNULL(nr_samples, []), IFNULL(nl_samples, []))) AS candidate_roads,
    l_lots AS candidate_lots
  FROM decided d
),
adjusted AS (
  -- 확정을 두 경우에 다시 검토로 내린다. ① 한 원문에 주소가 여러 개 ② 지역 표기가 기준자료와 정확히 일치하지 않음
  -- 단 ②에서 시군구만 빠졌고 같은 이름 + 번호가 전국에 하나뿐이면 확정한다(unique_without_sigungu).
  SELECT * REPLACE (
    if(demote IS NOT NULL, '검토 필요', status) AS status,
    if(demote IS NOT NULL, NULL, standard_road_address) AS standard_road_address,
    if(demote IS NOT NULL, NULL, standard_lot_address) AS standard_lot_address,
    CASE
      WHEN demote = 'multiple_addresses'
        THEN concat('원문에 주소가 여러 개 있어 확정하지 않음 · ', reason, ' · 후보 ', IFNULL(standard_road_address, standard_lot_address))
      WHEN demote = 'region_partial'
        THEN concat('번호는 일치하나 지역 표기가 기준자료와 정확히 일치하지 않아 확정하지 않음(',
                    CASE WHEN match_score = 0 THEN '원문에 지역 표기 없음'
                         WHEN match_score BETWEEN 8 AND 15 THEN '시군구 중 시 이름만 일치'
                         WHEN match_score < 8 THEN '시군구 표기 없음·불일치'
                         ELSE '원문의 읍·면이 기준자료와 다름' END,
                    ') · ', reason, ' · 후보 ', IFNULL(standard_road_address, standard_lot_address))
      WHEN unique_without_sigungu
        THEN concat(reason, ' · 원문에 시군구 없음: 같은 도로명·번호(지번)가 전국에 하나뿐이고 적힌 시도·읍면동과 맞아 확정')
      ELSE reason
    END AS reason,
    IFNULL(demote, reason_code) AS reason_code)
  FROM (
    SELECT *,
      CASE
        WHEN status NOT IN ('확정', '지번만 확정') THEN NULL
        WHEN multi_address THEN 'multiple_addresses'
        WHEN NOT IFNULL(if(len(r_roads) > 0, r_exact, l_exact), FALSE) AND NOT unique_without_sigungu THEN 'region_partial'
      END AS demote
    FROM (
      SELECT *,
        if(len(r_roads) > 0, r_score, l_score) AS match_score,
        -- 시군구 없이 쓴 주소의 확정(엄격): 시군구·시 이름은 없고(점수 1~7) 시도나 읍면동 전체 이름이 적혀 있으며
        -- (region_written), 적힌 지역이 하나도 어긋나지 않고(다른 시도·시군구는 2단계에서 이미 뺐고 읍·면은 eup_ok),
        -- 같은 이름 + 번호가 전국에 하나뿐(unique_nationwide).
        NOT IFNULL(if(len(r_roads) > 0, r_exact, l_exact), FALSE)
          AND IFNULL(if(len(r_roads) > 0, r_score, l_score), 0) BETWEEN 1 AND 7
          AND IFNULL(if(len(r_roads) > 0, r_unique, l_unique), FALSE) AS unique_without_sigungu
      FROM final
    )
  )
),
codes AS (
  SELECT r.standard_road_address,
    list(DISTINCT r.road_name_code) FILTER (WHERE r.road_name_code IS NOT NULL) AS road_codes,
    list(DISTINCT r.legal_dong_code) FILTER (WHERE r.legal_dong_code IS NOT NULL) AS legal_codes,
    list_distinct(flatten(list(r.administrative_area_codes))) AS admin_codes
  FROM ref_road r
  JOIN (SELECT DISTINCT standard_road_address FROM adjusted WHERE standard_road_address IS NOT NULL) w
    ON w.standard_road_address = r.standard_road_address
  GROUP BY r.standard_road_address
)
SELECT
  f.address_id, f.source_row, f.road_address AS input_road_address, f.lot_address AS input_lot_address,
  f.status, f.standard_road_address, f.standard_lot_address,
  if(len(c.road_codes) = 1, c.road_codes[1], NULL) AS road_name_code,
  if(len(c.legal_codes) = 1, c.legal_codes[1], NULL) AS legal_dong_code,
  if(len(c.admin_codes) = 1, c.admin_codes[1], NULL) AS administrative_area_code,
  f.reason, NULLIF(f.candidates, '') AS candidates,
  f.reason_code, f.candidate_roads, f.candidate_lots
FROM adjusted f LEFT JOIN codes c USING (standard_road_address)
ORDER BY f.source_row, f.address_id
