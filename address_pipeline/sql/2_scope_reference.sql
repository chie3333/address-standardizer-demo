-- 2단계 앞부분: 이번 입력에 필요한 기준자료만 RAW에서 골라 임시 표로 만든다. 전국 기준표를 미리 만들어 두지 않는다.
-- 1단계가 만든 input_cands(원문에서 뽑은 '이름 + 번호' 후보)의 이름과 같은 도로명·리·동·읍면·행정동의 RAW 행을 고르고,
-- 그 행들이 가리키는 도로명·지번 지역의 행도 모두 가져온다. 그래야 건물 하나·지번 하나의 정보(연결된 지번,
-- 일련번호, 같은 본번의 다른 부번)가 전국 기준표로 만들 때와 똑같이 모인다.
-- 임시 표 이름은 전국 기준표와 같다(ref_road, ref_lot, ref_lot_keys). 뒤의 매칭 SQL은 그대로 이 표를 읽는다.
-- 대표 이름(읍면동·리 등)은 any_value 대신 min으로 골라, 한 건물이 여러 리에 걸쳐도 실행마다 같은 값이 나온다.

-- 원문마다 적힌 지역: 시군구 단어가 적힌 시군구, 또는 시도가 적힌 시도의 모든 시군구.
-- 시도 표기가 하나도 없는 원문(지역 표기 없이 쓴 주소)은 전국을 본다(region_free).
-- 2단계 매칭(2_match)은 시군구·시 이름·시도 중 하나가 원문과 맞지 않는 행을 버리므로, 여기서 미리 빼도 결과가 같다.
CREATE OR REPLACE TEMP TABLE scope_texts AS
SELECT DISTINCT compact,
  NOT regexp_matches(compact, '(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|충청|전북|전남|전라|경북|경남|경상|제주)([^시]|$)|특별시|광역시|특별자치') AS region_free
FROM input_cands;

CREATE OR REPLACE TEMP TABLE scope_text_regions AS
WITH sido_map AS (
  SELECT * FROM (VALUES
    ('서울특별시', '서울', '서울시'), ('부산광역시', '부산', '부산시'), ('대구광역시', '대구', '대구시'),
    ('인천광역시', '인천', '인천시'), ('광주광역시', '광주', '광주광역시'), ('대전광역시', '대전', '대전시'), ('울산광역시', '울산', '울산시'),
    ('세종특별자치시', '세종', '세종시'), ('경기도', '경기', '경기도'), ('강원특별자치도', '강원', '강원도'),
    ('충청북도', '충북', '충청북도'), ('충청남도', '충남', '충청남도'), ('전북특별자치도', '전북', '전라북도'),
    ('전라남도', '전남', '전라남도'), ('경상북도', '경북', '경상북도'), ('경상남도', '경남', '경상남도'), ('제주특별자치도', '제주', '제주도')
  ) v(sido, short, alt)
)
SELECT DISTINCT t.compact, f.sigungu_code
FROM scope_texts t JOIN sigungu_forms f ON strpos(t.compact, f.form) > 0
WHERE NOT t.region_free
UNION
SELECT DISTINCT t.compact, g.sigungu_code
FROM scope_texts t
JOIN sido_map m ON strpos(t.compact, m.sido) > 0 OR strpos(t.compact, m.alt) > 0
                OR regexp_matches(t.compact, m.short || '([^시]|$)')
JOIN sigungu_regions g ON g.sido = m.sido
WHERE NOT t.region_free;

-- 이름 + 지역: 이름이 나온 원문에 적힌 지역의 시군구 코드. 지역 표기가 없는 원문의 이름은 지역 없이(전국) 본다.
CREATE OR REPLACE TEMP TABLE scope_names AS
SELECT DISTINCT c.kind, c.name, r.sigungu_code
FROM input_cands c JOIN scope_text_regions r ON r.compact = c.compact;

CREATE OR REPLACE TEMP TABLE scope_free_names AS
SELECT DISTINCT c.kind, c.name FROM input_cands c JOIN scope_texts t ON t.compact = c.compact WHERE t.region_free;

-- 1차: 원문 이름과 같은 RAW 행 중 그 원문에 적힌 지역의 행.
CREATE OR REPLACE TEMP TABLE scope_first AS
SELECT DISTINCT r.road_key, r.lot_area_key, r.sigungu_code FROM raw_clean_all r
WHERE (r.road_key, r.sigungu_code) IN (SELECT name, sigungu_code FROM scope_names WHERE kind = 'road')
   OR (r.lot_area_key, r.sigungu_code) IN (SELECT name, sigungu_code FROM scope_names WHERE kind = 'lot')
   OR (r.emd_key, r.sigungu_code) IN (SELECT name, sigungu_code FROM scope_names WHERE kind = 'lot')
   OR (r.admin_dong_key, r.sigungu_code) IN (SELECT name, sigungu_code FROM scope_names WHERE kind = 'lot')
   OR r.road_key IN (SELECT name FROM scope_free_names WHERE kind = 'road')
   OR r.lot_area_key IN (SELECT name FROM scope_free_names WHERE kind = 'lot')
   OR r.emd_key IN (SELECT name FROM scope_free_names WHERE kind = 'lot')
   OR r.admin_dong_key IN (SELECT name FROM scope_free_names WHERE kind = 'lot');

-- 1차 행이 가리키는 도로명·지번 지역(같은 시군구)의 행을 모두 가져온다. 건물 하나·지번 하나의 정보가 빠짐없이 모인다.
CREATE OR REPLACE TEMP TABLE raw_2025_clean AS
SELECT r.* FROM raw_clean_all r
WHERE (r.road_key, r.sigungu_code) IN (SELECT road_key, sigungu_code FROM scope_first)
   OR (r.lot_area_key, r.sigungu_code) IN (SELECT lot_area_key, sigungu_code FROM scope_first);

-- 도로명주소 건물 단위: 도로명(공백 제거) + 건물본번 + 건물부번
CREATE OR REPLACE TEMP TABLE ref_road AS
SELECT road_key, main_no, sub_no, sigungu_code, legal_dong_code,
  min(sido) AS sido, min(sigungu) AS sigungu, min(emd) AS emd, min(ri) AS ri,
  min(road_name_code) AS road_name_code, min(standard_road_address) AS standard_road_address,
  coalesce(list(DISTINCT administrative_area_code ORDER BY administrative_area_code) FILTER (WHERE administrative_area_code IS NOT NULL), []) AS administrative_area_codes,
  coalesce(list(DISTINCT standard_lot_address ORDER BY standard_lot_address) FILTER (WHERE standard_lot_address IS NOT NULL), []) AS lot_addresses,
  coalesce(list(DISTINCT admin_dong ORDER BY admin_dong) FILTER (WHERE admin_dong IS NOT NULL), []) AS admin_dongs,
  list(serial ORDER BY serial) AS serials
FROM raw_2025_clean
WHERE main_no > 0 AND road_key IS NOT NULL
GROUP BY road_key, main_no, sub_no, sigungu_code, legal_dong_code;

-- 지번 단위: 리(없으면 읍면동) + 산 여부 + 지번본번 + 지번부번
CREATE OR REPLACE TEMP TABLE ref_lot AS
SELECT lot_area_key, lot_mountain, lot_main, lot_sub, sigungu_code, legal_dong_code,
  min(sido) AS sido, min(sigungu) AS sigungu, min(emd) AS emd, min(ri) AS ri,
  min(standard_lot_address) AS standard_lot_address,
  coalesce(list(DISTINCT standard_road_address ORDER BY standard_road_address) FILTER (WHERE standard_road_address IS NOT NULL), []) AS road_addresses,
  coalesce(list(DISTINCT road_name_code ORDER BY road_name_code) FILTER (WHERE road_name_code IS NOT NULL), []) AS road_name_codes,
  coalesce(list(DISTINCT administrative_area_code ORDER BY administrative_area_code) FILTER (WHERE administrative_area_code IS NOT NULL), []) AS administrative_area_codes,
  list(serial ORDER BY serial) AS serials
FROM raw_2025_clean
WHERE lot_main > 0
GROUP BY lot_area_key, lot_mountain, lot_main, lot_sub, sigungu_code, legal_dong_code;

-- 지번 조회 키 세 가지: 법정리·법정동, 리를 빼고 쓴 읍·면, 행정동(법정동과 다를 때)
CREATE OR REPLACE TEMP TABLE ref_lot_keys AS
WITH keyed AS (
  SELECT r.*, k.lot_key, k.key_type
  FROM raw_2025_clean r,
    unnest([
      {'lot_key': r.lot_area_key, 'key_type': if(r.ri IS NULL, '법정동', '법정리')},
      {'lot_key': if(r.ri IS NOT NULL, r.emd_key, NULL), 'key_type': '읍면'},
      {'lot_key': if(r.admin_dong IS NOT NULL AND r.admin_dong != r.emd, r.admin_dong_key, NULL), 'key_type': '행정동'}
    ]) AS u(k)
  WHERE r.lot_main > 0 AND k.lot_key IS NOT NULL
)
SELECT lot_key, key_type, lot_mountain, lot_main, lot_sub, sigungu_code, legal_dong_code,
  min(sido) AS sido, min(sigungu) AS sigungu, min(emd) AS emd, min(ri) AS ri,
  min(standard_lot_address) AS standard_lot_address,
  coalesce(list(DISTINCT standard_road_address ORDER BY standard_road_address) FILTER (WHERE standard_road_address IS NOT NULL), []) AS road_addresses
FROM keyed
GROUP BY lot_key, key_type, lot_mountain, lot_main, lot_sub, sigungu_code, legal_dong_code;
