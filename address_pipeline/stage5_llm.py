"""5단계 LLM: 4단계까지 후보가 하나도 없는 행에만 쓴다. LLM 결과는 반드시 규칙으로 다시 확인하고, 확정하지 않는다.

5a LLM 원문 구분 (llm_spans)  원문의 주소 부분·건물명·동층호를 가리키게 한다(원문 글자 그대로인지 확인).
    주소 부분 → 1~3단계로 다시 매칭. 정확히 맞으면 'LLM 원문 구분 후보'(검토 필요).
    건물명    → 4단계 H 건물명 검색. 규칙이 건물명 조각을 못 고른 행(번호 없이 건물명만 적은 원문 등)에 쓴다.
5b LLM 교정 (M)  5a 뒤에도 후보가 없는 행. 표기·명칭 오타를 고친 문장을 다시 매칭한다(address_pipeline.openai_correct).
"""
from __future__ import annotations

from . import rules as bq

from .llm_spans import identify
from .stage123_match import match_batch
from .stage4_candidates import building_matches
from .state import Run

SPAN_REVIEW = "LLM 원문 구분 후보"
SPAN_REASONS = {"missing_address_number", "scope_not_found", "number_not_found"}


def _generate(r: Run):
    if r.generate is None:
        from .openai_correct import make_generate

        call = make_generate()

        def cached(prompt: str, request: str) -> str:
            key = (prompt, request)
            if key not in r.llm_cache:
                r.llm_cache[key] = call(prompt, request)
            return r.llm_cache[key]

        r.generate = cached
    return r.generate


def span_targets(r: Run) -> list[str]:
    """후보가 없고, 원문 표기가 문제일 수 있는 행(번호 없음·지역/도로명 불일치·번호 없음). 건물 없는 땅으로 보이는 행은 뺀다."""
    out = []
    for row in r.results:
        address_id = row["record"]["address_id"]
        output = row.get("output") or {}
        if (address_id not in r.texts or row["status"] not in {"no_match", "held"}
                or (row.get("review") or {}).get("candidates")
                or output.get("reason_code") not in SPAN_REASONS
                or str(output.get("reason") or "").startswith("리·동은 있으나")):
            continue
        out.append(address_id)
    return out[:r.llm_max]


def spans(r: Run) -> dict:
    targets = span_targets(r)
    if not targets:
        return {"requests": 0}
    found = identify({i: r.texts[i] for i in targets}, _generate(r))
    by_id = {row["record"]["address_id"]: row for row in r.results}
    counts = {"requests": len(targets), "address_part": 0, "building": 0, "detail": 0, "rejected": 0, "errors": 0}
    for span in found:
        by_id[span["address_id"]]["output"]["llm_spans"] = {key: span[key] for key in
                                                            ("address_part", "building", "detail", "rejected")}
        counts["rejected"] += len(span["rejected"])
        counts["errors"] += bool(span["error"])
        for field in ("address_part", "building", "detail"):
            counts[field] += bool(span[field])

    # 주소 부분: 원문과 다를 때만(주소가 아닌 글자를 뺐을 때) 다시 매칭한다.
    parts = [s for s in found if s["address_part"] and bq.compact(s["address_part"]) != bq.compact(s["text"])]
    if parts:
        batch = f"{r.batch}-span"
        source = {row["address_id"]: row["source_row"] for row in r.rows}
        r.sources.append_rows("input_raw", [{"batch": batch, "address_id": s["address_id"], "road_address": s["address_part"],
                                             "lot_address": "", "source_row": source[s["address_id"]]} for s in parts])
        fixes = [{"address_id": s["address_id"], "original_address": s["text"], "corrected_address": s["address_part"],
                  "changes": "주소가 아닌 구간을 뺌", "inferred": None} for s in parts]
        adopted = bq.apply_ai(r.results, r.comparisons, fixes, match_batch(r.sources, batch), r.texts,
                              provider="openai_span", review_type=SPAN_REVIEW, reason_code="llm_span_address",
                              reason="LLM이 고른 주소 부분이 기준자료와 정확히 일치 · 사람 승인 필요",
                              label="LLM 원문 구분", note_key="llm_span_match")
        counts["address_verified"] = adopted.get("verified", 0) + adopted.get("verified_region_changed", 0)

    # 건물명: 아직 후보가 없는 행만 H 건물명 검색으로 보낸다.
    phrases = {}
    for s in found:
        row = by_id[s["address_id"]]
        if not s["building"] or (row.get("review") or {}).get("candidates"):
            continue
        name = bq.normalize_name(s["building"])
        core = bq.building_core(name)
        if len(core) >= 2 and name not in bq._NOT_BUILDING and core not in bq._NOT_BUILDING:
            phrases[s["address_id"]] = (s["building"], name, core)
    if phrases:
        before = {i for i in phrases if (by_id[i].get("review") or {}).get("candidates")}
        added = bq.apply_buildings(r.results, r.comparisons, building_matches(r.sources, phrases, r.texts), phrases, r.texts)
        for i in phrases:
            review = by_id[i].get("review") or {}
            if i not in before and review.get("review_type") == bq.BUILDING_REVIEW:
                for candidate in review.get("candidates") or []:
                    candidate["evidence"].insert(0, f"건물명 구간 '{phrases[i][0]}'은 LLM이 원문에서 고름(원문 글자 그대로)")
        counts["building_candidates"] = added.get("candidates_added", 0)
    bq.refresh_summary(r.dataset)
    return counts


def correction(r: Run) -> dict:
    targets = [i for i in bq.ai_targets(r.results) if i in r.texts][:r.llm_max]
    if not targets:
        return {"requests": 0}
    from .openai_correct import correct

    fixes = correct({i: r.texts[i] for i in targets}, _generate(r))
    batch = f"{r.batch}-ai"
    source = {row["address_id"]: row["source_row"] for row in r.rows}
    r.sources.append_rows("input_raw", [{"batch": batch, "address_id": f["address_id"], "road_address": f["corrected_address"],
                                         "lot_address": "", "source_row": source[f["address_id"]]}
                                        for f in fixes if f["corrected_address"]])
    counts = bq.apply_ai(r.results, r.comparisons, fixes, match_batch(r.sources, batch), r.texts, provider="openai")
    bq.refresh_summary(r.dataset)
    return {"requests": len(targets), **counts}
