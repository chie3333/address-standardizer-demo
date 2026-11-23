import {test} from "node:test";
import assert from "node:assert/strict";
import {recommendationView} from "../web/analysis.mjs";

const candidates = [{candidate_index: 0, address: "기존 A", rank: 1}, {candidate_index: 1, address: "기존 B", rank: 2},
  {candidate_index: 2, address: "기존 C", rank: 2}];

test("AI recommendation highlights a subset without changing original order or ranks", () => {
  const analysis = {total_candidates: 3, candidates: structuredClone(candidates),
    recommendation: {decision: "recommend", candidate_indices: [1], reason: "건물명 근거가 더 강함"}};
  const before = structuredClone(analysis);
  const view = recommendationView(analysis);
  assert.deepEqual(view.selected.map((item) => item.address), ["기존 B"]);
  assert.deepEqual(view.others.map((item) => item.address), ["기존 A", "기존 C"]);
  assert.equal(view.narrowed, true);
  assert.deepEqual(analysis, before);
});

test("tied candidates are kept together and an all-candidate tie is not called narrowed", () => {
  const analysis = {total_candidates: 3, candidates,
    recommendation: {decision: "tie", candidate_indices: [0, 1, 2], reason: "구분할 근거 없음"}};
  assert.equal(recommendationView(analysis).narrowed, false);
  analysis.recommendation.candidate_indices = [0, 2];
  assert.equal(recommendationView(analysis).selected.length, 2);
  assert.equal(recommendationView(analysis).narrowed, true);
});

test("insufficient evidence and older explanation-only results do not highlight candidates", () => {
  const view = recommendationView({total_candidates: 3, candidates,
    recommendation: {decision: "insufficient", candidate_indices: [], reason: "근거 부족"}});
  assert.equal(view.title, "추천 보류");
  assert.deepEqual(view.selected, []);
  assert.equal(view.others.length, 3);
  assert.deepEqual(recommendationView({candidates}).selected, []);
});
