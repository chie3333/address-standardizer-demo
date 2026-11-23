export const STATUS = {
  resolved: ["표준화 완료", "green"], held: ["보류", "amber"],
  address_only: ["기본 주소만 확인", "amber"], lot_only: ["지번만 확인", "amber"],
  no_match: ["일치 없음", "red"], ambiguous: ["해석 모호", "amber"],
  no_label_candidates: ["라벨 후보 없음", "slate"], excluded: ["제외", "slate"],
  parsed: ["파싱 완료", "blue"], parse_unresolved: ["파싱 미해결", "amber"],
  failed: ["실행 실패", "red"], skipped: ["미실행", "slate"],
  completed: ["실행 완료", "blue"], preprocess_passed: ["전처리 통과", "blue"],
  found: ["계층 조회 일치", "blue"], matched: ["상세 검증 일치", "blue"],
  multiple_matches: ["복수 일치", "amber"], conflict: ["충돌", "red"],
  unique_path: ["지역 경로 1개", "blue"], multiple_paths: ["복수 지역 경로", "amber"],
  no_region_tokens: ["지역 토큰 없음", "slate"], partial_path: ["지역 일부 확인", "amber"],
};
export const REVIEW = {
  verified: ["기본 도로명주소 확인", "blue"], no_candidate: ["후보 없음", "slate"],
  single_candidate: ["후보 1개", "amber"], preferred_candidate: ["단독 1순위", "amber"],
  parallel_candidates: ["해석별 공동 1순위", "amber"], tied_candidates: ["공동 1순위", "amber"],
};
export const REASON = {
  blank_input: "주소가 비어 있습니다.", numeric_only: "숫자만 입력된 행입니다.",
  po_box: "사서함을 포함한 행입니다.", duplicate_input: "앞선 행과 중복된 입력입니다.",
  duplicate_inherited: "앞선 같은 원문 행(대표 행)의 결과를 물려받았습니다.",
  multiple_addresses: "주소가 여러 개로 해석되어 확인이 필요합니다.",
  sub_number_mismatch: "명시된 부번이 기준 후보와 다릅니다.",
  sub_number_omitted: "부번이 생략되어 확인이 필요합니다.",
  number_not_found: "조회한 기준 범위에서 번호를 찾지 못했습니다.",
  multiple_road_addresses: "한 지번에 도로명주소가 여러 개 연결됩니다.",
  missing_codes: "세 코드 중 일부가 확정되지 않았습니다.",
  demo_no_fixture: "이 주소에 준비된 시연 결과가 없습니다.",
  demo_parser_error: "파싱 실행 실패를 보여주는 시연 사례입니다.",
};
export const CODES = [
  ["legal_dong_code", "법정동코드"], ["administrative_area_code", "행정구역분류코드"],
  ["road_name_code", "도로명코드"],
];
export const MODULES = {
  input_preparation: "입력 준비", preprocessing: "전처리", input_filter: "입력 선별",
  parser: "주소 파싱", parser_retry: "파싱 재시도", region_validation: "지역 경로 검증",
  region_boundary_validation: "지역명 경계 검증", region_merged_name_validation: "붙여 쓴 지역명 검증",
  hierarchy_search: "계층 조회", standardization: "표준화", address_validation: "상세 검증",
  resolution: "결과 판정", address_review: "검토 후보 조회", demo_lookup: "시연 사례 조회",
};
export const GROUPS = {
  resolved: ["resolved"],
  review: ["held", "address_only", "lot_only", "ambiguous", "multiple_matches", "multiple_paths", "parse_unresolved"],
  issue: ["no_match", "failed", "excluded", "conflict", "no_label_candidates", "no_region_tokens"],
};
// 검토 필요 주소를 나누는 단계. 파이프라인 검토 결과(review.tier)를 그대로 쓴다.
export const TIERS = [
  ["near_confirmed", "유력 · 거의 확정", "green", "단독 1순위 · 근거 분명 · 코드만 빠진 확정 주소 포함"],
  ["check_needed", "유력 · 확인 필요", "amber", "단독 1순위 · 비슷한 곳 있음"],
  ["general", "일반 보류", "slate", "동률 · 후보 없음 · 정보 부족"],
  ["historical", "과거 기준자료 일치", "blue", "올해 자료엔 없고 과거 연도에만 있음"],
];
// 사람이 승인해 확정할 수 있는 단계. 일괄 승인은 거의 확정 단계에서만 쓴다.
// 일반 보류를 1순위 후보 수로 다시 나눈다. 후보가 많을수록 원문 단서가 부족하다는 뜻이다.
export const CANDIDATE_BUCKETS = [
  ["none", "후보 없음", (count) => count === 0],
  ["one", "1순위 1개", (count) => count === 1],
  ["two", "1순위 2개", (count) => count === 2],
  ["few", "1순위 3~5개", (count) => count >= 3 && count <= 5],
  ["many", "1순위 6개 이상", (count) => count >= 6],
];
export const firstRankCount = (row) =>
  reviewCandidates(row).map(object).filter((item) => scalar(item.rank) === "1").length;
export function candidateBucket(row) {
  const count = firstRankCount(row);
  return CANDIDATE_BUCKETS.find(([, , test]) => test(count))[0];
}
export const APPROVABLE_TIERS = ["near_confirmed", "check_needed"];
export const APPROVED = {reviewed_confirmed: ["검토 후 확정", "blue"]};
export const SAMPLE = ["가온시 서림구 은하로 152-2", "가온시 서림구 은하로 150", "한별시 새빛구 달빛길 24"];
export const object = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
export const list = (value) => Array.isArray(value) ? value : [];
export const scalar = (value) => ["string", "number", "boolean"].includes(typeof value) ? String(value) : "";
export const statusLabel = (value) => (Object.hasOwn(STATUS, value) ? STATUS[value][0] : scalar(value));
export const formatValue = (value) => value != null && typeof value === "object" ? JSON.stringify(value) : scalar(value);
export const reviewCandidates = (row) => list(object(row.review).candidates);

// 1순위 후보가 하나뿐이면 그 후보를 돌려준다.
export function topCandidate(row) {
  const first = reviewCandidates(row).map(object).filter((item) => scalar(item.rank) === "1");
  return first.length === 1 ? first[0] : null;
}

export const isApproved = (row) => object(row.approval).status === "reviewed_confirmed";

// 표준화 완료·검토 후 확정·검토 필요·문제 중 어디에 속하는지. 번호가 기준자료에 없어도(no_match)
// 검토 후보가 있으면 검토 필요로 본다.
export function rowGroup(row) {
  if (isApproved(row)) return "approved";
  if (GROUPS.resolved.includes(row.status)) return "resolved";
  if (GROUPS.review.includes(row.status)) return "review";
  if (row.status === "no_match" && reviewCandidates(row).length) return "review";
  return "issue";
}

export const rowTier = (row) => {
  const tier = scalar(object(row.review).tier);
  return TIERS.some(([key]) => key === tier) ? tier : "general";
};

export function canApprove(row) {
  const top = topCandidate(row);
  return !isApproved(row) && rowGroup(row) === "review" && APPROVABLE_TIERS.includes(rowTier(row))
    && Boolean(top && scalar(object(top.standard_address).standard_road_address));
}

export function rowReasons(row) {
  const review = object(row.review);
  const output = object(row.output);
  return [...new Set([
    review.base_reason, output.reason_code,
    output.screening_result !== "passed" ? output.screening_result : null,
    object(row.standardization).reason_code, object(row.region_validation).reason_code,
    ...list(row.traces).map((trace) => object(trace).reason_code),
  ].filter((value) => typeof value === "string" && value.trim()))];
}

export function standardAddress(row) {
  const approved = object(object(row.approval).standard_address);
  if (isApproved(row) && scalar(approved.standard_road_address)) return {address: approved, source: "approval"};
  const standardization = object(row.standardization);
  const address = object(standardization.address);
  if (standardization.status === "resolved" && scalar(address.standard_road_address)) {
    return {address, source: "standardization.address"};
  }
  const reviewAddress = object(object(row.review).standard_address);
  if (scalar(reviewAddress.standard_road_address)) {
    return {address: reviewAddress, source: "review.standard_address"};
  }
  return {address: {}, source: ""};
}

// 도로명주소는 확정됐지만 기준자료에 비어 있는 코드(예: 행정구역분류코드)의 이름 목록.
export function missingCodes(row) {
  const {address} = standardAddress(row);
  if (!scalar(address.standard_road_address)) return [];
  return CODES.filter(([key]) => !scalar(address[key])).map(([, label]) => label);
}

// 도로명주소와 지번이 하나로 정해지지 않는 관계. 검토 필요 화면에서 단계와 상관없이 모아 볼 수 있다.
export const RELATIONS = [
  ["1:N", "한 지번 · 도로명주소 여러 개", "rel-1n"],
  ["N:1", "한 도로명주소 · 지번 여러 개", "rel-n1"],
  ["N:M", "도로명주소 · 지번 모두 여러 개", "rel-nm"],
];
export const relationKind = (row) => rowGroup(row) === "review" ? roadLotRelation(row)?.kind ?? null : null;

// 1순위 후보들의 도로명주소와 지번 관계. 한 지번에 건물이 여럿(1:N), 한 건물이 여러 필지(N:1), 둘 다(N:M)이면
// 도로명주소·지번을 하나로 정하지 못한 이유가 될 수 있어 따로 보여준다.
const RELATION_NOTES = {
  "1:N": "한 필지에 건물이 여러 개라 지번만으로는 도로명주소를 하나로 정할 수 없습니다.",
  "N:1": "한 건물이 여러 필지에 걸쳐 있어 대표 지번이 하나로 정해지지 않습니다. 도로명주소는 하나입니다.",
  "N:M": "후보 도로명주소와 지번이 서로 여러 개로 얽혀 있어 하나로 정할 수 없습니다.",
};

export function roadLotRelation(row) {
  // 서버(BigQuery 엔진)가 실제 연결된 도로명주소·지번으로 관계를 계산해 주면 그 값을 쓴다(null이면 관계 없음).
  const output = object(row.output);
  if (Object.hasOwn(output, "road_lot_relation")) {
    const given = output.road_lot_relation;
    return given && RELATION_NOTES[given.kind] ? {...given, note: RELATION_NOTES[given.kind]} : null;
  }
  const first = reviewCandidates(row).map(object).filter((item) => scalar(item.rank) === "1");
  const roads = new Set();
  const lots = new Set();
  for (const item of first) {
    if (item.kind === "lot") {
      lots.add(scalar(item.address));
      list(item.linked_road_addresses).map(scalar).filter(Boolean).forEach((road) => roads.add(road));
    } else {
      roads.add(scalar(item.address));
      list(item.linked_lot_addresses).map(scalar).filter(Boolean).forEach((lot) => lots.add(lot));
    }
  }
  roads.delete(""); lots.delete("");
  if (roads.size > 1 && lots.size === 1) {
    return {kind: "1:N", roads: roads.size, lots: 1, label: `한 지번에 도로명주소 ${roads.size}개`,
      note: "한 필지에 건물이 여러 개라 지번만으로는 도로명주소를 하나로 정할 수 없습니다."};
  }
  if (roads.size === 1 && lots.size > 1) {
    return {kind: "N:1", roads: 1, lots: lots.size, label: `한 도로명주소에 지번 ${lots.size}개`,
      note: "한 건물이 여러 필지에 걸쳐 있어 대표 지번이 하나로 정해지지 않습니다. 도로명주소는 하나입니다."};
  }
  if (roads.size > 1 && lots.size > 1) {
    return {kind: "N:M", roads: roads.size, lots: lots.size, label: `도로명주소 ${roads.size}개 · 지번 ${lots.size}개`,
      note: "후보 도로명주소와 지번이 서로 여러 개로 얽혀 있어 하나로 정할 수 없습니다."};
  }
  return null;
}

const addressSegmenter = new Intl.Segmenter("ko", {granularity: "grapheme"});

function matchingLengths(before, after, backwards = false) {
  const lengths = new Uint32Array(after.length + 1);
  for (let i = 0; i < before.length; i++) {
    const character = before[backwards ? before.length - 1 - i : i];
    let diagonal = 0;
    for (let j = 1; j <= after.length; j++) {
      const previous = lengths[j];
      lengths[j] = character === after[backwards ? after.length - j : j - 1]
        ? diagonal + 1 : Math.max(previous, lengths[j - 1]);
      diagonal = previous;
    }
  }
  return lengths;
}

function alignAddress(before, after, addOriginal, addStandardized) {
  if (before.length <= 1) {
    const match = before.length ? after.indexOf(before[0]) : -1;
    addOriginal(before.join(""), match < 0);
    if (match < 0) addStandardized(after.join(""), true);
    else {
      addStandardized(after.slice(0, match).join(""), true);
      addStandardized(after[match], false);
      addStandardized(after.slice(match + 1).join(""), true);
    }
    return;
  }
  if (!after.length) {
    addOriginal(before.join(""), true);
    return;
  }

  // Find the common subsequence in two halves without storing the whole comparison matrix.
  const middle = Math.floor(before.length / 2);
  const left = before.slice(0, middle);
  const right = before.slice(middle);
  const fromStart = matchingLengths(left, after);
  const fromEnd = matchingLengths(right, after, true);
  let split = 0;
  let best = -1;
  for (let i = 0; i <= after.length; i++) {
    const length = fromStart[i] + fromEnd[after.length - i];
    if (length > best) {
      best = length;
      split = i;
    }
  }
  alignAddress(left, after.slice(0, split), addOriginal, addStandardized);
  alignAddress(right, after.slice(split), addOriginal, addStandardized);
}

export function addressDiff(original, standardized) {
  if (original === standardized) {
    return {original: [{text: original, changed: false}], standardized: [{text: standardized, changed: false}]};
  }
  const characters = (value) => Array.from(addressSegmenter.segment(value), ({segment}) => segment);
  const before = characters(original);
  const after = characters(standardized);
  const originalParts = [];
  const standardizedParts = [];
  const append = (parts, text, changed) => {
    if (!text) return;
    const last = parts.at(-1);
    if (last?.changed === changed) last.text += text;
    else parts.push({text, changed});
  };

  let start = 0;
  let beforeEnd = before.length;
  let afterEnd = after.length;
  while (start < beforeEnd && start < afterEnd && before[start] === after[start]) start++;
  while (beforeEnd > start && afterEnd > start && before[beforeEnd - 1] === after[afterEnd - 1]) {
    beforeEnd--;
    afterEnd--;
  }
  const prefix = before.slice(0, start).join("");
  append(originalParts, prefix, false);
  append(standardizedParts, prefix, false);

  alignAddress(before.slice(start, beforeEnd), after.slice(start, afterEnd),
    (text, changed) => append(originalParts, text, changed),
    (text, changed) => append(standardizedParts, text, changed));
  const suffix = before.slice(beforeEnd).join("");
  append(originalParts, suffix, false);
  append(standardizedParts, suffix, false);
  return {original: originalParts, standardized: standardizedParts};
}

export function countRows(rows) {
  const counts = new Map();
  for (const row of rows) counts.set(row.status, (counts.get(row.status) ?? 0) + 1);
  return counts;
}

export function csvCell(value) {
  let text = scalar(value);
  if (/^[\s\uFEFF]*[=+\-@＝＋－＠]|^[\t\r\n]/u.test(text)) text = "'" + text;
  return '"' + text.replaceAll('"', '""') + '"';
}

// 한 줄 도로명주소 + 제출 양식 개별 컬럼. 값은 서버가 확정 근거 RAW 행에서 채운 row.output_fields에서 읽는다.
export const OUTPUT_COLUMNS = [
  "도로명주소",  // 시도 시군구 읍면동 도로명 건물번호 건물명 동 층 호를 이은 한 줄 주소(도로명주소 확정 행만)
  "법정동시도명", "법정동시군구명", "법정동읍면동명", "법정동리명",
  "행정구역분류코드", "법정동코드", "도로명코드",
  "대지구분코드", "지번본번", "지번부번", "지번목록",  // 한 건물이 여러 필지면 지번을 모두 ", "로 잇는다
  "건물명", "건물동명", "건물층명", "건물호명", "층구분코드",
  "도로명", "지하구분코드", "건물본번", "건물부번",
];

export function makeCsv(payload) {
  const {dataset, source} = payload;
  const header = ["source", "run_id", "reference_version", "input_index", "address_id", "raw_address",
    ...OUTPUT_COLUMNS, "normalized_address", "status", "reason_codes", "review_type", "review_status", "review_tier", "approval", "candidate_count",
    "standard_address_source", "standardization_status", "standard_road_address", "missing_codes", "road_lot_relation",
    "confirmed_lot", "standardization_raw_ids", "inherited_from"];
  const rows = dataset.results.map((row) => {
    const review = object(row.review);
    const {address, source: addressSource} = standardAddress(row);
    const standardization = object(row.standardization);
    return [source, dataset.run_id, dataset.reference_version, row.input_index, row.record.address_id,
      row.record.raw_address, ...OUTPUT_COLUMNS.map((column) => object(row.output_fields)[column] ?? ""),
      row.record.normalized_address, row.status, rowReasons(row).join(" | "),
      review.review_type, review.status, review.tier, isApproved(row) ? "검토 후 확정" : "",
      Array.isArray(review.candidates) ? review.candidates.length : null,
      addressSource, standardization.status, address.standard_road_address, missingCodes(row).join(" | "),
      rowGroup(row) === "review" ? roadLotRelation(row)?.label ?? "" : "",
      review.confirmed_lot, list(standardization.raw_record_ids).join(" | "), row.inherited_from ?? ""];
  });
  return "\uFEFF" + [header, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n") + "\r\n";
}
