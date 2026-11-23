"""4단계 후보 도구: 확정되지 않은 행에만, 사람이 검토할 후보를 단다. 어느 도구도 확정하지 않는다.

순서와 대상
  H 건물명       확정 안 된 행 전부. 원문 건물명으로 원문 시군구 안 건물을 찾는다(sql/4_building_scope + 4_buildings).
  I 폐지 주소    아직 미일치인 행. 기준일 전에 폐지된 주소인지, 또는 원문 지번이 지금도 있는 건물의 연결 지번에서
                 빠졌는지(그 건물을 후보로) 본다(sql/4_abolished).
  J 번호 오타    도로명·리는 있는데 번호가 없고 후보도 없는 행. 자리 바뀜·한 자리 다름·빠짐·더함(sql/4_number_typos).
  K 붙여쓰기     후보가 없고 '로·길 + 숫자 + 3자리 + 동'인 행. 건물번호와 동을 나눠 다시 매칭한다.
결과 반영 규칙(증거 문장, 순위, 빠른 승인 단계)은 BigQuery 엔진에서 복사한 함수(address_pipeline/rules.py)를 쓴다.
"""
from __future__ import annotations

import json
from typing import Mapping

from . import rules as bq

from .sources import Sources, read_sql, statements
from .stage123_match import match_batch
from .state import Run


def building_matches(sources: Sources, phrases: Mapping[str, tuple[str, str, str]], texts: Mapping[str, str]) -> list[dict]:
    """건물명 조각 {주소 ID: (원문 표기, 정규화 이름, 핵심 이름)}으로 건물 후보를 찾는다. 5a단계도 쓴다."""
    # 지역 확인은 건물명 조각을 뺀 원문으로 한다(남해군 … 진주시산림조합의 '진주시'로 진주시를 찾지 않게).
    reqs = [json.dumps({"id": i, "text": bq.compact(texts[i]).replace(bq.compact(original), " ", 1),
                        "phrase": name, "core": core}, ensure_ascii=False)
            for i, (original, name, core) in phrases.items()]
    scope, query = statements(read_sql("4_buildings.sql"))
    return sources.query([scope, *statements(read_sql("4_building_scope.sql")), query], array={"reqs": ("STRING", reqs)})


def buildings(r: Run) -> dict:
    phrases = bq.building_requests(r.results, r.texts)
    if not phrases:
        return {"requests": 0}
    matches = building_matches(r.sources, phrases, r.texts)
    counts = bq.apply_buildings(r.results, r.comparisons, matches, phrases, r.texts)
    bq.refresh_summary(r.dataset)
    return {"requests": len(phrases), **counts}


def _requests(ids: list[str], texts: Mapping[str, str]) -> list[str]:
    return [json.dumps({"id": i, "text": texts[i]}, ensure_ascii=False) for i in ids]


def abolished(r: Run) -> dict:
    targets = bq.abolished_targets(r.results, r.texts)
    if not targets:
        return {"requests": 0}
    hits = r.sources.query(statements(read_sql("4_abolished.sql")), array={"reqs": ("STRING", _requests(targets, r.texts))})
    counts = bq.apply_abolished(r.results, hits, comparisons=r.comparisons, texts=r.texts)
    bq.refresh_summary(r.dataset)
    return {"requests": len(targets), **counts}


def number_typos(r: Run) -> dict:
    targets = bq.typo_targets(r.results, r.texts)
    if not targets:
        return {"requests": 0}
    hits = r.sources.query(statements(read_sql("4_number_typos.sql")), array={"reqs": ("STRING", _requests(targets, r.texts))})
    counts = bq.apply_typos(r.results, r.comparisons, hits, r.texts)
    bq.refresh_summary(r.dataset)
    return {"requests": len(targets), **counts}


def glued_numbers(r: Run) -> dict:
    targets = bq.glued_targets(r.results, r.texts)
    if not targets:
        return {"requests": 0}
    batch = f"{r.batch}-split"
    source = {row["address_id"]: row["source_row"] for row in r.rows}
    r.sources.append_rows("input_raw", [{"batch": batch, "address_id": i, "road_address": variant, "lot_address": "",
                                         "source_row": source[i]} for i, (variant, *_rest) in targets.items()])
    counts = bq.apply_glued(r.results, r.comparisons, targets, match_batch(r.sources, batch))
    bq.refresh_summary(r.dataset)
    return {"requests": len(targets), **counts}
