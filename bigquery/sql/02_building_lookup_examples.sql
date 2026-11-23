-- 건물명 사전 조회 예시: 로컬 파이프라인이 후보를 못 찾은 세 사례.
--   1) 부분 일치   통영 동달리 '청구아파트'      → 통영청구하이츠아파트 (대곡길 12, 시군구 안 1곳)
--   2) 접미어 차이 통영 정량동 '종우원에이 아파트' → 종우원에이맨션 (정동3길 65)
--   3) 지역 없음   '대송에이스빌'               → 목포시 고하대로 707, 707-1 (전국 2곳, 같은 단지)
-- 핵심이름(name_core)이 짧으면(예: '청구') 포함 일치가 넓어지므로, 포함 일치는 지역 범위 안에서만 쓴다.
WITH q AS (
  SELECT * FROM UNNEST([
    STRUCT('통영 동달리 청구아파트' AS label, '청구' AS core, '경상남도' AS sido, '통영시' AS sigungu, '용남면' AS emd, '동달리' AS ri),
    STRUCT('통영 정량동 종우원에이 아파트', '종우원에이', '경상남도', '통영시', '정량동', CAST(NULL AS STRING)),
    STRUCT('지역 없음 대송에이스빌', '대송에이스', CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING), CAST(NULL AS STRING))
  ])
)
SELECT q.label,
  CASE WHEN b.name_core = q.core THEN '핵심이름 일치' ELSE '포함 일치' END AS how,
  b.original_names, b.standard_road_address, b.road_name_code, b.legal_dong_code,
  b.same_name_in_sigungu, b.same_name_nationwide
FROM q JOIN `your-gcp-project.address.building_names` b
  ON NOT b.is_generic
 AND (q.sido IS NULL OR (b.sido = q.sido AND b.sigungu = q.sigungu AND b.emd = q.emd AND (q.ri IS NULL OR b.ri = q.ri)))
 AND (b.name_core = q.core OR STRPOS(b.name_core, q.core) > 0)
ORDER BY q.label, how;
