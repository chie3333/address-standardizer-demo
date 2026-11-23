// 기존 후보의 비교·추천 UI. 기본 처리와 원래 후보 순위는 변경하지 않는다.
import {object, list, scalar, reviewCandidates} from "./result.mjs";

const pending = new Map();
const active = (analysis) => ["queued", "running"].includes(analysis?.status);
const keyOf = (jobId, addressId) => JSON.stringify([jobId, addressId]);

function node(tag, className, ...children) {
  const element = document.createElement(tag);
  element.className = className;
  element.append(...children);
  return element;
}

export function canExplain(row) {
  return reviewCandidates(row).some((candidate) => scalar(object(candidate).address));
}

export function recommendationView(analysis) {
  const recommendation = object(analysis.recommendation);
  const candidates = list(analysis.candidates).map(object);
  const indices = new Set(list(recommendation.candidate_indices));
  const selected = recommendation.decision === "insufficient" ? []
    : candidates.filter((item) => indices.has(item.candidate_index));
  return {selected, others: candidates.filter((item) => !selected.includes(item)),
    title: recommendation.decision === "recommend" ? "우선 검토할 후보 1개"
      : recommendation.decision === "tie" ? `공동 검토 후보 ${selected.length}개` : "추천 보류",
    narrowed: selected.length > 0 && selected.length < analysis.total_candidates,
    reason: scalar(recommendation.reason), recommendation};
}

async function follow({jobId, addressId, api, onUpdate}, initial) {
  let analysis = initial;
  while (active(analysis)) {
    await new Promise((resolve) => setTimeout(resolve, 1200));
    const response = await api(`/api/jobs/${encodeURIComponent(jobId)}/analysis?address_id=${encodeURIComponent(addressId)}`);
    analysis = response.analysis;
    onUpdate(analysis);
  }
}

function watch(options, initial) {
  const key = keyOf(options.jobId, options.addressId);
  if (pending.has(key)) return;
  const task = follow(options, initial).catch((error) => {
    options.onUpdate({...initial, connection_error: error.message});
  }).finally(() => pending.delete(key));
  pending.set(key, task);
}

export function analysisPanel({row, jobId, ready, api, onUpdate}) {
  const analysis = object(row.additional_analysis);
  const complete = analysis.status === "done" && Boolean(object(analysis.recommendation).decision);
  const addressId = row.record.address_id;
  const options = {jobId, addressId, api, onUpdate};
  const section = node("section", "additional-analysis");
  section.setAttribute("aria-label", "기존 후보 추가 분석");
  const button = node("button", "button", analysis.status === "failed" ? "다시 시도" : "추가 분석");
  button.type = "button";
  const busy = active(analysis);
  button.disabled = !jobId || !ready || !canExplain(row) || busy;
  if (busy) button.textContent = analysis.status === "queued" ? "분석 대기 중…" : "분석 중…";
  if (complete) button.textContent = "분석 결과 보기";
  const description = !canExplain(row) ? "비교·추천할 기존 후보가 없습니다."
    : !ready ? "기본 처리가 끝나면 기존 후보 중 유력한 후보를 추천합니다."
      : "기존 후보를 비교해 더 유력한 후보와 이유를 추천합니다.";
  const heading = node("div", "analysis-heading", node("div", "", node("h3", "", "후보 비교·추천"),
    node("p", "muted", description)), button);
  section.append(heading);
  const result = node("div", "analysis-result");
  section.append(result);
  button.onclick = async () => {
    if (complete) {
      result.hidden = !result.hidden;
      button.textContent = result.hidden ? "분석 결과 보기" : "분석 결과 접기";
      return;
    }
    button.disabled = true;
    button.textContent = "분석 요청 중…";
    try {
      const response = await api(`/api/jobs/${encodeURIComponent(jobId)}/analysis`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({address_id: addressId, retry: analysis.status === "failed"}),
      });
      watch(options, response.analysis);
      onUpdate(response.analysis);
    } catch (error) {
      onUpdate({status: "failed", message: error.message});
    }
  };
  if (busy) {
    const status = node("p", "muted", analysis.connection_error || analysis.message || "기존 근거를 분석하고 있습니다.");
    status.setAttribute("role", "status");
    result.append(status);
    if (analysis.connection_error) {
      button.disabled = false;
      button.textContent = "진행 상태 다시 확인";
    } else {
      // 렌더가 끝난 뒤 시작해, 실패 시 다시 그리기가 재귀 호출로 이어지지 않게 한다.
      queueMicrotask(() => watch(options, analysis));
    }
  }
  if (analysis.status === "failed") {
    const error = node("p", "analysis-error", analysis.message || "추가 분석을 완료하지 못했습니다.");
    error.setAttribute("role", "alert");
    result.append(error);
  }
  if (analysis.status === "done") {
    if (complete) button.textContent = "분석 결과 접기";
    result.append(node("p", "muted", `${analysis.mode === "demo" ? "시연 기록 비교" : "AI가 기존 후보를 비교한 참고 의견"} · ${analysis.elapsed_s ?? 0}초`));
    const view = recommendationView(analysis);
    if (complete) {
      const summary = node("div", "analysis-recommendation", node("strong", "analysis-recommendation-title", view.title),
        node("p", "", view.reason));
      if (view.narrowed) summary.append(node("p", "analysis-shortlist", `기존 ${analysis.total_candidates}개 → 우선 검토 ${view.selected.length}개`));
      const references = list(view.recommendation.evidence_refs);
      if (references.length) {
        const cited = node("details", "analysis-sources", node("summary", "", "후보를 비교한 근거 보기"));
        for (const ref of references) cited.append(node("p", "", `후보 ${Number(ref.candidate_index) + 1} · ${scalar(ref.address)}`),
          node("ul", "", ...list(ref.evidence).map((text) => node("li", "", scalar(text)))));
        summary.append(cited);
      }
      result.append(summary);
    } else {
      result.append(node("p", "muted", "이전 분석은 후보 설명만 포함합니다. 추가 분석을 누르면 후보 비교·추천을 받을 수 있습니다."));
    }
    if (analysis.total_candidates > analysis.explained_candidates) {
      result.append(node("p", "muted", `기존 후보 ${analysis.total_candidates}개 중 앞의 ${analysis.explained_candidates}개를 비교했습니다. 전체 후보를 비교하지 못해 추천은 보류합니다.`));
    }
    const candidateCard = (value, recommended) => {
      const item = object(value);
      const card = node("article", `analysis-candidate${recommended ? " analysis-preferred" : ""}`,
        node("h4", "", `후보 ${Number(item.candidate_index) + 1} · ${scalar(item.address)}`));
      const labels = [recommended ? (view.recommendation.decision === "tie" ? "공동 검토" : "AI 추천") : "다른 기존 후보",
        scalar(item.rank) ? `원래 ${scalar(item.rank)}순위` : ""].filter(Boolean);
      card.append(node("p", "analysis-candidate-label", labels.join(" · ")));
      const lines = [
        ["제안", recommended ? `우선 검토할 기존 후보: ${scalar(item.address)}` : scalar(item.proposal)], ["근거", scalar(item.evidence)],
        ["확인할 점", list(item.check_points).map(scalar).join("\n")],
        ["조회 내역", list(analysis.lookups).map(scalar).join(" → ")],
      ];
      const fields = node("dl", "analysis-fields");
      for (const [title, text] of lines) fields.append(node("div", "", node("dt", "", title), node("dd", "", text)));
      card.append(fields);
      const evidence = node("details", "analysis-sources", node("summary", "", "기존 검증 근거 보기"));
      evidence.append(node("ul", "", ...list(item.source_evidence).map((text) => node("li", "", scalar(text)))));
      card.append(evidence);
      return card;
    };
    for (const item of view.selected) result.append(candidateCard(item, true));
    if (view.others.length) {
      const others = node("details", "analysis-alternatives", node("summary", "", `다른 기존 후보 ${view.others.length}개 비교 보기`));
      others.open = !view.selected.length;
      for (const item of view.others) others.append(candidateCard(item, false));
      result.append(others);
    }
    result.append(node("p", "muted", "AI 추천은 검토를 돕는 의견입니다. 원래 판정·후보·순위·승인은 유지되며, 조회 내역은 기존 처리 기록입니다."));
  }
  return section;
}
