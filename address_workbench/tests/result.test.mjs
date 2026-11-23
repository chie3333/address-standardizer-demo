import { test } from "node:test";
import assert from "node:assert/strict";
import { addressDiff, countRows, csvCell, makeCsv, reviewCandidates, rowReasons, standardAddress } from "../web/result.mjs";

test("address diff preserves shared text between multiple edits", () => {
  const original = "가온시  서림구 은하로 152-2";
  const standardized = "가온특별시 서림구 은하로 152-3";
  const diff = addressDiff(original, standardized);
  const changed = (parts) => parts.filter((part) => part.changed).map((part) => part.text);
  assert.deepEqual(changed(diff.original), [" ", "2"]);
  assert.deepEqual(changed(diff.standardized), ["특별", "3"]);
  assert.equal(diff.original.map((part) => part.text).join(""), original);
  assert.equal(diff.standardized.map((part) => part.text).join(""), standardized);
});

test("address diff leaves identical strings plain and keeps Hangul syllables together", () => {
  const same = "가온시 은하로 150";
  assert.equal(addressDiff(same, same).standardized.some((part) => part.changed), false);
  const diff = addressDiff("서림", "서".normalize("NFD") + "림");
  assert.deepEqual(diff.original[0], {text: "서", changed: true});
  assert.deepEqual(diff.standardized[0], {text: "서".normalize("NFD"), changed: true});
});

test("review statuses, reasons and unknown review shape remain independent", () => {
  const row = {
    status: "future_status", review: {status: "new", candidates: {vendor_payload: [1]}},
    output: {screening_result: "numeric_only"}, traces: [{reason_code: "numeric_only"}],
  };
  assert.deepEqual(reviewCandidates(row), []);
  assert.deepEqual(rowReasons(row), ["numeric_only"]);
  assert.equal(countRows([row]).get("future_status"), 1);
});

test("held output and review candidates never become an automatic standard address", () => {
  const row = {
    standardization: {status: "held", address: {standard_road_address: "held"}},
    review: {candidates: [{address: "candidate"}]},
  };
  assert.deepEqual(standardAddress(row), {address: {}, source: ""});
  row.review.standard_address = {standard_road_address: "partial", road_name_code: "00001"};
  assert.equal(standardAddress(row).source, "review.standard_address");
  assert.equal(standardAddress(row).address.road_name_code, "00001");
});

test("CSV keeps output source, quoting, ordering and identifier text", () => {
  const record = {address_id: "0001", raw_address: '=HYPERLINK("example")'};
  const row = {record, input_index: 0, status: "future_status", traces: []};
  const csv = makeCsv({source: "demo", dataset: {run_id: "r", reference_version: "2025", results: [row]}});
  assert.ok(csv.startsWith('\uFEFF"source","run_id"'));
  assert.ok(csv.includes('"demo","r","2025","0","0001"'));
  assert.ok(csv.includes('"future_status"'));
  assert.equal(csvCell(record.raw_address), "\"'=HYPERLINK(\"\"example\"\")\"");
  assert.equal(csvCell("  +5"), "\"'  +5\"");
});

test("review tiers, approval and grouping keep reviewed confirmations apart from resolved ones", async () => {
  const {rowGroup, rowTier, canApprove, isApproved, topCandidate} = await import("../web/result.mjs");
  const standard = {standard_road_address: "서울특별시 종로구 세종대로 209", road_name_code: "111102005001"};
  const near = {
    status: "no_match", record: {address_id: "a"},
    review: {tier: "near_confirmed", candidates: [{rank: 1, address: standard.standard_road_address, standard_address: standard}]},
  };
  assert.equal(rowGroup(near), "review");  // 번호는 없지만 후보가 있으면 검토 필요
  assert.equal(rowTier(near), "near_confirmed");
  assert.equal(topCandidate(near).address, standard.standard_road_address);
  assert.equal(canApprove(near), true);
  assert.equal(rowGroup({status: "no_match", review: {candidates: []}}), "issue");
  assert.equal(rowTier({status: "held", review: {}}), "general");
  const tied = {status: "held", review: {tier: "general", candidates: [{rank: 1, address: "x"}, {rank: 1, address: "y"}]}};
  assert.equal(topCandidate(tied), null);
  assert.equal(canApprove(tied), false);

  const approved = {...near, approval: {status: "reviewed_confirmed", standard_address: standard}};
  assert.equal(isApproved(approved), true);
  assert.equal(rowGroup(approved), "approved");
  assert.equal(canApprove(approved), false);
  assert.deepEqual(standardAddress(approved), {address: standard, source: "approval"});
  const csv = makeCsv({source: "pipeline", dataset: {run_id: "r", reference_version: "2025",
    results: [{...approved, input_index: 0, traces: []}]}});
  assert.ok(csv.includes('"review_tier","approval"'));
  assert.ok(csv.includes('"near_confirmed","검토 후 확정"'));
});

test("general holds are bucketed by the number of first-rank candidates", async () => {
  const {candidateBucket, firstRankCount} = await import("../web/result.mjs");
  const rows = (ranks) => ({review: {candidates: ranks.map((rank, i) => ({rank, address: `a${i}`}))}});
  assert.equal(candidateBucket(rows([])), "none");
  assert.equal(candidateBucket(rows([1, 1, 2])), "two");
  assert.equal(firstRankCount(rows([1, 1, 2])), 2);
  assert.equal(candidateBucket(rows([1, 1, 1, 1, 1])), "few");
  assert.equal(candidateBucket(rows([1, 1, 1, 1, 1, 1, 1])), "many");
});

test("past-year matches form their own tier and are not approvable", async () => {
  const {rowTier, canApprove, TIERS} = await import("../web/result.mjs");
  const row = {status: "no_match", review: {tier: "historical", candidates: [{rank: 1, address: "x",
    reference_years: [2024, 2023], standard_address: {standard_road_address: "x"}}]}};
  assert.ok(TIERS.some(([key]) => key === "historical"));
  assert.equal(rowTier(row), "historical");
  assert.equal(canApprove(row), false);
});

test("rows with a single first-rank candidate get their own bucket", async () => {
  const {candidateBucket, rowTier} = await import("../web/result.mjs");
  assert.equal(candidateBucket({review: {candidates: [{rank: 1, address: "a"}]}}), "one");
  assert.equal(rowTier({status: "address_only", review: {tier: "near_confirmed"}}), "near_confirmed");
});

test("confirmed addresses with empty reference codes are marked", async () => {
  const {missingCodes} = await import("../web/result.mjs");
  const row = {status: "address_only", review: {standard_address: {standard_road_address: "녹촌로 197",
    road_name_code: "1", legal_dong_code: "2", administrative_area_code: ""}}};
  assert.deepEqual(missingCodes(row), ["행정구역분류코드"]);
  assert.deepEqual(missingCodes({status: "held", review: {}}), []);
  const csv = makeCsv({source: "pipeline", dataset: {run_id: "r", reference_version: "2025",
    results: [{...row, record: {address_id: "a"}, input_index: 0, traces: []}]}});
  assert.ok(csv.includes('"missing_codes"') && csv.includes('"행정구역분류코드"'));
});

test("road and lot relations among first-rank candidates are labelled", async () => {
  const {roadLotRelation} = await import("../web/result.mjs");
  const road = (address, lots) => ({rank: 1, kind: "road", address, linked_lot_addresses: lots});
  const oneLotManyRoads = {review: {candidates: [road("통일로 246-20", ["무악동 46-82"]), road("통일로12길 23", ["무악동 46-82"])]}};
  assert.equal(roadLotRelation(oneLotManyRoads).kind, "1:N");
  assert.equal(roadLotRelation({review: {candidates: [road("재재기로139번길 88", ["차산리 387", "차산리 산 127-3"])]}}).kind, "N:1");
  assert.equal(roadLotRelation({review: {candidates: [road("a", ["x", "y"]), road("b", ["y"])]}}).kind, "N:M");
  assert.equal(roadLotRelation({review: {candidates: [road("a", ["x"])]}}), null);
});

test("relation kind is only given to review rows", async () => {
  const {relationKind, RELATIONS} = await import("../web/result.mjs");
  const road = (address, lots) => ({rank: 1, kind: "road", address, linked_lot_addresses: lots});
  const candidates = [road("통일로 246-20", ["무악동 46-82"]), road("통일로12길 23", ["무악동 46-82"])];
  assert.equal(relationKind({status: "held", review: {candidates}}), "1:N");
  assert.equal(relationKind({status: "resolved", review: {candidates}}), null);
  assert.deepEqual(RELATIONS.map(([kind]) => kind), ["1:N", "N:1", "N:M"]);
});

test("road-lot relation prefers the server value when the engine provides one", async () => {
  const {roadLotRelation, relationKind} = await import("../web/result.mjs");
  const road = (address, lots) => ({kind: "road", rank: 1, address, linked_lot_addresses: lots});
  // 부번이 다른 대안 후보들: 화면 계산으로는 N:M이지만 서버가 관계 없음(null)으로 주면 따른다.
  const alternatives = [road("a 59-10", ["x"]), road("a 59-13", ["y"])];
  assert.equal(roadLotRelation({output: {road_lot_relation: null}, review: {candidates: alternatives}}), null);
  assert.equal(roadLotRelation({review: {candidates: alternatives}}).kind, "N:M");
  const given = {kind: "1:N", roads: 3, lots: 1, label: "한 지번에 도로명주소 3개"};
  const relation = roadLotRelation({output: {road_lot_relation: given}, review: {candidates: []}});
  assert.equal(relation.kind, "1:N");
  assert.match(relation.note, /한 필지에 건물이 여러 개/);
  assert.equal(relationKind({status: "lot_only", output: {road_lot_relation: given}, review: {candidates: []}}), "1:N");
});
