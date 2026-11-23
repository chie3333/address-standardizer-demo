import {
  STATUS, REVIEW, REASON, CODES, MODULES, GROUPS, SAMPLE, OUTPUT_COLUMNS, object, list, scalar,
  statusLabel, formatValue, reviewCandidates, rowReasons, standardAddress, countRows, makeCsv,
  TIERS, APPROVED, CANDIDATE_BUCKETS, rowGroup, rowTier, topCandidate, isApproved, canApprove, candidateBucket, missingCodes, roadLotRelation, RELATIONS, relationKind,
} from "./result.mjs";
import {analysisPanel} from "./analysis.mjs";

const $ = (id) => document.getElementById(id);
const icons = {
  dashboard: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  pin: "M20 10c0 5-8 11-8 11S4 15 4 10a8 8 0 1 1 16 0Z M12 7a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z",
  upload: "M12 16V3m-5 5 5-5 5 5M4 16v4h16v-4",
  download: "M12 3v13m-5-5 5 5 5-5M4 16v4h16v-4",
  database: "M4 6c0-4 16-4 16 0s-16 4-16 0Zm0 0v12c0 4 16 4 16 0V6M4 12c0 4 16 4 16 0",
  search: "M16 10a6 6 0 1 0-12 0 6 6 0 0 0 12 0Zm-1.7 4.3L21 21",
  chevron: "m9 6 6 6-6 6", check: "m5 12 4 4L19 6",
  alert: "M12 8v5m0 3h.01M11 3 2 19h20L13 3z",
  info: "M12 11v6m0-10h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z",
  refresh: "M20 9a8 8 0 0 0-14-3L4 9m0-5v5h5m-5 6a8 8 0 0 0 14 3l2-3m0 5v-5h-5",
  play: "m8 4 12 8-12 8z", layers: "m12 3 9 5-9 5-9-5zM3 12l9 5 9-5M3 16l9 5 9-5",
};

function icon(name) {
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(namespace, "path");
  path.setAttribute("d", icons[name]);
  svg.append(path);
  return svg;
}

function el(tag, className = "", ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.append(...children.filter((child) => child !== null && child !== undefined));
  return node;
}

const labelOf = (labels, value) => Object.hasOwn(labels, value) ? labels[value] : undefined;
const muted = (message) => el("p", "muted", message);
const code = (value) => el("code", "", scalar(value));
const badge = (value, labels = STATUS) => {
  const [label, color] = labelOf(labels, value) ?? [scalar(value) || "상태 미제공", "slate"];
  const node = el("span", `badge ${color}`, el("i", ""), label);
  node.title = scalar(value);
  return node;
};
const json = (value) => el("pre", "json", JSON.stringify(value, null, 2));
// 처리 상태 배지. 번호가 기준자료에 없었지만(no_match) 검토 후보가 생긴 행은 검토 필요에 있으므로
// '일치 없음' 대신 '번호 불일치 · 후보 있음'으로 보여준다.
// 검토 필요 행에서 도로명주소와 지번이 1:N · N:1 · N:M으로 얽혀 있으면 그 관계를 표시한다.
const relationChip = (row) => {
  const relation = rowGroup(row) === "review" ? roadLotRelation(row) : null;
  if (!relation) return null;
  const tone = RELATIONS.find(([kind]) => kind === relation.kind)[2];
  const chip = el("span", `relation-chip ${tone}`, el("b", "", relation.kind), relation.label);
  chip.title = `도로명주소 : 지번 = ${relation.kind} · ${relation.note}`;
  return chip;
};
const REVIEW_NO_MATCH = {no_match: ["번호 불일치 · 후보 있음", "amber"]};
const rowBadge = (row) => isApproved(row) ? badge("reviewed_confirmed", APPROVED)
  : rowGroup(row) === "review" && row.status === "no_match" ? badge("no_match", REVIEW_NO_MATCH) : badge(row.status);

function disclosure(title, ...contents) {
  return el("details", "disclosure", el("summary", "", title), ...contents);
}

function fields(items) {
  const dl = el("dl", "fields");
  for (const [label, value] of items) dl.append(el("div", "", el("dt", "", label), el("dd", "", value)));
  return dl;
}

for (const span of document.querySelectorAll("[data-icon]")) span.append(icon(span.dataset.icon));

const views = {dashboard: "결과 대시보드", single: "단일 주소 입력", batch: "파일 일괄 처리"};
const state = {config: null, payload: null, job: null, selected: null, filter: "", filterType: "status", tier: null, bucket: null, relation: null, tag: "", page: 0, tab: "compare", pending: false, engine: null};

// 목록 배지(제외 사유 · 코드 없음 · 도로명·지번 관계)로 거르는 표시 필터. 상태 필터와 함께(AND) 적용한다.
const EXCLUDE_REASONS = {
  duplicate_input: "중복 입력", blank_input: "빈 주소", numeric_only: "숫자만 입력", po_box: "사서함 포함",
};
const excludeTags = (row) => {
  if (row.status !== "excluded") return [];
  const known = rowReasons(row).filter((reason) => EXCLUDE_REASONS[reason]);
  return known.length ? known.map((reason) => [`exclude:${reason}`, `제외 · ${EXCLUDE_REASONS[reason]}`])
    : [["exclude:other", "제외 · 기타 사유"]];
};
const rowTags = (row) => [
  ...(row.inherited_from ? [["dup:inherited", "중복 · 대표 행 결과 상속"]] : []),
  ...excludeTags(row),
  ...missingCodes(row).map((label) => [`code:${label}`, `${label} 없음`]),
  ...(relationKind(row) ? [[`rel:${relationKind(row)}`, `${relationKind(row)} · ${RELATIONS.find(([kind]) => kind === relationKind(row))[1]}`]] : []),
];
const tagOrder = (key) => ["dup", "exclude", "code", "rel"].indexOf(key.split(":")[0]);
const hasTag = (row, tag) => rowTags(row).some(([key]) => key === tag);
const matchesStatus = (row) => !state.filter
  || (state.filterType === "group" ? rowGroup(row) === state.filter : row.status === state.filter);
const PAGE_SIZE = 25;
let inspection = 0;
let fileMeta = null;

function view() {
  const name = location.hash.replace(/^#\/?/, "");
  return Object.hasOwn(views, name) ? name : "dashboard";
}

function route() {
  const selected = view();
  for (const name of Object.keys(views)) $(name).hidden = name !== selected;
  for (const link of document.querySelectorAll("[data-view]")) {
    if (link.dataset.view === selected) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  $("breadcrumb").textContent = views[selected];
  if ($("app-error").textContent) showError("");
}
window.addEventListener("hashchange", () => {
  route();
  if (location.hash.startsWith("#/")) window.scrollTo(0, 0);
});
route();

async function api(path, init) {
  const response = await fetch(path, init);
  const body = await response.json();
  if (!response.ok) throw Object.assign(new Error(body.error || "요청을 처리하지 못했습니다."), {status: response.status});
  return body;
}

function showError(message) {
  $("app-error").textContent = message;
  $("app-error").hidden = !message;
}

function setPending(pending) {
  state.pending = pending;
  $("loading").hidden = !pending || Boolean(state.job);
  $("load-sample").disabled = pending;
  $("single-form").querySelector("[type=submit]").disabled = pending;
  $("run-file").disabled = pending || !fileMeta;
  for (const select of document.querySelectorAll("[data-engine-choice]")) select.disabled = pending;
}

const ENGINE_KEY = "address-workbench-engine";
const engines = () => state.config?.engines ?? [];
const currentEngine = () => engines().find((item) => item.key === state.engine) ?? engines()[0];

// 고른 엔진 이름을 사이드바·상단 표시에 반영한다.
function renderEngine() {
  const demo = state.config.source === "demo";
  const engine = currentEngine();
  const label = demo ? "시연 모드" : engine?.label ?? "연결된 파이프라인";
  $("sidebar-mode").textContent = demo ? "시연 모드" : `${label} 연결`;
  $("sidebar-reference").textContent = `기준자료 ${engine?.reference_version ?? state.config.reference_version}${demo ? " · 시연" : ""}`;
  $("source-chip").textContent = demo ? "시연 모드" : engines().length > 1 ? "다음 실행에 사용" : label;
  $("source-chip").classList.toggle("demo", demo);
}

const POLL_MS = 1000;
const JOB_KEY = "address-workbench-job";

function remember(jobId) {
  try { jobId ? sessionStorage.setItem(JOB_KEY, jobId) : sessionStorage.removeItem(JOB_KEY); } catch {}
}
function remembered() {
  try { return sessionStorage.getItem(JOB_KEY); } catch { return null; }
}

// 진행 중인 실행의 행을 받아 화면용 결과로 묶는다. 완료되면 서버가 검증한 전체 결과로 바꾼다.
function partialPayload(job, rows) {
  const results = [...rows].sort((a, b) => a.input_index - b.input_index);
  const summary = {};
  for (const row of results) summary[row.status] = (summary[row.status] ?? 0) + 1;
  return {
    source: job.source,
    dataset: {run_id: job.run_id, reference_version: job.reference_version, results, summary,
      batch_results: [], review_summary: {}},
    candidate_comparisons: {},
  };
}

function renderProgress() {
  const job = state.job;
  if (job) $("loading").hidden = true;  // 진행률 패널이 대신 상태를 보여준다.
  $("progress").hidden = !job || job.status === "done";
  if (!job || job.status === "done") return;
  const ratio = job.total ? Math.min(job.processed / job.total, 1) : 0;
  const failed = job.status === "failed";
  const queued = job.status === "queued";
  $("progress").classList.toggle("failed", failed);
  $("progress-title").textContent = failed ? "처리가 중단되었습니다" : queued ? "처리 대기 중" : "처리 중";
  $("progress-meta").textContent =
    `${job.processed.toLocaleString()} / ${job.total.toLocaleString()}건 · ${Math.round(ratio * 100)}% · 배치 ${job.batches_done}개 완료 · ${job.elapsed_s}초`;
  $("progress-bar").style.width = `${ratio * 100}%`;
  $("progress-note").textContent = failed
    ? `오류(${job.error || "알 수 없음"})로 멈췄습니다. 끝난 배치의 결과는 아래에서 보고 내려받을 수 있습니다.`
    : queued ? "실행 중인 작업이 끝나면 자동으로 시작합니다."
      : "배치가 끝날 때마다 결과가 아래 목록에 추가됩니다. 이 탭을 새로고침해도 이어서 볼 수 있습니다.";
}

async function followJob(jobId) {
  let rows = [];
  let first = true;
  remember(jobId);
  for (;;) {
    const snapshot = await api(`/api/jobs/${jobId}?since=${rows.length}`);
    const sources = engines().length ? engines().map((item) => item.source) : [state.config.source];
    if (!sources.includes(snapshot.source)) throw new Error("실행 결과의 데이터 출처가 현재 모드와 다릅니다.");
    rows = rows.concat(snapshot.rows);
    state.job = snapshot;
    const changed = first || snapshot.rows.length || snapshot.payload || snapshot.status !== "running";
    if (snapshot.payload) {
      // 진행 중에 받은 행은 중간 결과다. 끝나면 서버가 최종 행(출력 컬럼 등 포함)으로 바꿔 두므로 처음부터 다시 받는다.
      if (rows.length > snapshot.rows.length) rows = (await api(`/api/jobs/${jobId}?since=0`)).rows;
      const results = [...rows].sort((a, b) => a.input_index - b.input_index);
      state.payload = {...snapshot.payload, dataset: {...snapshot.payload.dataset, results}};
    } else if (changed) state.payload = partialPayload(snapshot, rows);
    if (first) {
      state.selected = null;
      state.tab = "compare";
      state.filter = "";
      state.filterType = "status";
      state.page = 0;
      $("search").value = "";
      if (view() !== "dashboard") location.hash = "/dashboard";
      first = false;
    }
    if (!state.selected || !state.payload.dataset.results.some((row) => row.record.address_id === state.selected)) {
      state.selected = state.payload.dataset.results[0]?.record.address_id ?? null;
    }
    renderProgress();
    if (changed) renderDashboard();
    if (snapshot.status !== "queued" && snapshot.status !== "running") return snapshot;
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
}

async function runRequest(path, init = {method: "POST"}) {
  if (state.pending) return;
  showError("");
  setPending(true);
  const originView = view();
  const formStatus = originView === "batch" ? $("upload-status") : originView === "single" ? $("single-demo") : null;
  const oldText = formStatus?.textContent;
  const wasHidden = formStatus?.hidden;
  if (formStatus) {
    formStatus.hidden = false;
    formStatus.textContent = "처리 중입니다.";
    formStatus.setAttribute("role", "status");
  }
  try {
    const engine = currentEngine() ? `&engine=${encodeURIComponent(currentEngine().key)}` : "";
    const started = await api(`${path}${path.includes("?") ? "&" : "?"}async=1${engine}`, init);
    const finished = await followJob(started.job_id);
    if (finished.status === "failed") showError(`처리가 중단되었습니다. ${finished.processed.toLocaleString()}건까지의 결과는 남아 있습니다.`);
  } catch (error) {
    showError(error.message);
  } finally {
    if (formStatus?.textContent === "처리 중입니다.") {
      formStatus.textContent = oldText;
      formStatus.hidden = wasHidden;
    }
    setPending(false);
    renderDashboard();
  }
}

// 새로고침 뒤에도 이 탭이 기억한 자기 작업을 이어서 보여준다.
async function resumeJob() {
  const jobId = remembered();
  if (!jobId) return false;
  setPending(true);
  try {
    const finished = await followJob(jobId);
    if (finished.status === "failed") showError(`처리가 중단되었습니다. ${finished.processed.toLocaleString()}건까지의 결과는 남아 있습니다.`);
  } catch (error) {
    if (error.status !== 404) throw error;
    remember(null);
    return false;
  } finally {
    setPending(false);
    renderDashboard();
  }
  return true;
}

async function start() {
  try {
    state.config = await api("/api/config");
    const demo = state.config.source === "demo";
    let saved = null;
    try { saved = localStorage.getItem(ENGINE_KEY); } catch {}
    state.engine = engines().some((item) => item.key === saved) ? saved : state.config.default_engine;
    // 위쪽 선택 상자와 입력 화면의 선택 상자는 같은 값을 쓴다.
    const selects = [...document.querySelectorAll("[data-engine-choice]")];
    for (const select of selects) {
      select.replaceChildren(...engines().map((item) => new Option(item.label, item.key, false, item.key === state.engine)));
      select.onchange = () => {
        state.engine = select.value;
        for (const other of selects) other.value = state.engine;
        try { localStorage.setItem(ENGINE_KEY, state.engine); } catch {}
        renderEngine();
      };
    }
    $("engine-field").hidden = engines().length < 2;
    for (const label of document.querySelectorAll(".engine-choice")) label.hidden = engines().length < 2;
    renderEngine();
    for (const id of ["load-sample", "demo-banner", "single-demo", "fill-example", "download-sample"]) $(id).hidden = !demo;
    const resumed = await resumeJob();
    if (!resumed && demo && view() === "dashboard") await runRequest("/api/sample");
  } catch (error) {
    showError(error.message);
  } finally {
    renderDashboard();
  }
}

$("load-sample").onclick = () => runRequest("/api/sample");
$("fill-example").onclick = () => { $("address-input").value = SAMPLE[0]; $("address-input").focus(); };
$("single-form").onsubmit = (event) => {
  event.preventDefault();
  runRequest("/api/single", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({raw_address: $("address-input").value})});
};
$("download-sample").onclick = (event) => {
  event.preventDefault();
  download("demo-addresses.csv", "\uFEFFid,주소\r\n" + SAMPLE.map((address, index) => `sample-${index + 1},${address}`).join("\r\n"), "text/csv");
};
$("upload").onchange = () => inspectFile();
$("sheet").onchange = () => inspectFile($("sheet").value);
$("batch-form").onsubmit = (event) => {
  event.preventDefault();
  if (!fileMeta || $("address-col").value === "") return;
  const params = new URLSearchParams({format: fileMeta.format, address_col: $("address-col").value});
  if (fileMeta.sheet) params.set("sheet", fileMeta.sheet);
  if ($("id-col").value !== "") params.set("id_col", $("id-col").value);
  runRequest(`/api/table/run?${params}`, {method: "POST", body: fileMeta.file});
};

async function inspectFile(sheet = null) {
  const request = ++inspection;
  const file = $("upload").files[0];
  fileMeta = null;
  $("run-file").disabled = true;
  $("mapping").hidden = !sheet;
  $("upload-status").textContent = "";
  if (!file) return;
  const format = file.name.split(".").pop().toLowerCase();
  const params = new URLSearchParams({format});
  if (sheet) params.set("sheet", sheet);
  try {
    $("upload-status").textContent = "파일의 열을 읽는 중입니다.";
    const data = await api(`/api/table/inspect?${params}`, {method: "POST", body: file});
    if (request !== inspection) return;
    fileMeta = {file, format, sheet: data.sheet};
    $("sheet-field").hidden = !data.sheets.length;
    $("sheet").replaceChildren(...data.sheets.map((name) => new Option(name, name)));
    if (data.sheet) $("sheet").value = data.sheet;
    for (const [id, hint] of [["address-col", "주소 열 선택"], ["id-col", "자동 ID 만들기"]]) {
      $(id).replaceChildren(new Option(hint, ""), ...data.headers.map((header, i) =>
        new Option(header || `(이름 없는 열 ${i + 1})`, String(i))));
    }
    const addressIndex = data.headers.findIndex((header) => ["주소", "address", "raw_address", "원본주소", "원본 주소"].includes(header.trim().toLowerCase()));
    if (addressIndex >= 0) $("address-col").value = String(addressIndex);
    $("upload-status").textContent = `${file.name} · ${data.row_count.toLocaleString()}행`;
    $("mapping").hidden = false;
    $("run-file").disabled = state.pending;
  } catch (error) {
    if (request !== inspection) return;
    $("upload-status").textContent = error.message;
  }
}

function download(name, content, type) {
  const url = URL.createObjectURL(new Blob([content], {type}));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

for (const button of document.querySelectorAll("[data-export]")) {
  button.onclick = () => {
    const payload = state.payload;
    const ext = button.dataset.export;
    const prefix = `${payload.source}-address-results-${payload.dataset.run_id.slice(0, 8)}`;
    if (ext === "csv") download(`${prefix}.csv`, makeCsv(payload), "text/csv;charset=utf-8");
    else download(`${prefix}.json`, JSON.stringify(payload, null, 2), "application/json");
    $("download").open = false;
  };
}

function setFilter(value, type = "status", focus = false) {
  state.filter = value;
  state.filterType = type;
  state.tier = null;
  state.bucket = null;
  state.relation = null;
  state.page = 0;
  renderDashboard();
  if (focus) $("list-title").scrollIntoView({block: "nearest"});
}
$("search").oninput = () => { state.page = 0; renderList(); };
$("status-filter").onchange = () => setFilter($("status-filter").value);
$("tag-filter").onchange = () => {
  state.tag = $("tag-filter").value; state.tier = null; state.bucket = null; state.relation = null; state.page = 0;
  renderDashboard();
};

function renderDashboard() {
  const {payload} = state;
  $("dashboard-empty").hidden = Boolean(payload) || state.pending;
  $("dashboard-content").hidden = !payload;
  $("download").hidden = !payload;
  if (!payload) return;
  const {dataset} = payload;
  const producer = engines().length > 1 ? engines().find((item) => item.source === state.payload.source)?.label : null;
  $("run-label").textContent = `${producer ? `${producer} · ` : ""}기준자료 ${dataset.reference_version} · 실행 ${dataset.run_id.slice(0, 8)}`;
  const counts = countRows(dataset.results);
  const groups = new Map();
  for (const row of dataset.results) groups.set(rowGroup(row), (groups.get(rowGroup(row)) ?? 0) + 1);
  const groupCount = (group) => groups.get(group) ?? 0;
  const cards = [
    ["전체 입력", state.job && state.job.status !== "done" ? state.job.total : dataset.results.length, "layers", "blue", ""],
    ["표준화 완료", groupCount("resolved"), "check", "green", "resolved"],
    ["검토 후 확정", groupCount("approved"), "check", "blue", "approved"],
    ["검토 필요", groupCount("review"), "search", "amber", "review"],
    ["불일치 · 제외 · 실패", groupCount("issue"), "alert", "red", "issue"],
  ];
  const duplicates = dataset.results.filter((row) => row.inherited_from).length;
  $("metrics").replaceChildren(...cards.map(([label, count, glyph, color, group]) => {
    const button = el("button", `panel metric ${color}`, el("span", "metric-icon", icon(glyph)),
      el("span", "metric-text", el("span", "", label), el("strong", "", count.toLocaleString()), el("small", "", "건"),
        !group && duplicates ? el("span", "metric-note", `중복 ${duplicates.toLocaleString()}건 포함`) : null));
    button.type = "button";
    button.setAttribute("aria-pressed", String(group === state.filter && (!group || state.filterType === "group")));
    button.onclick = () => setFilter(group, "group", true);
    return button;
  }));
  const filter = $("status-filter");
  filter.replaceChildren(new Option("모든 상태", ""), ...[...counts].map(([status, count]) =>
    new Option(`${statusLabel(status)} · ${count.toLocaleString()}`, status)));
  filter.value = state.filterType === "status" ? state.filter : "";
  // 표시 필터 항목은 지금 상태 필터에 걸린 행 기준으로 센다.
  const tags = new Map();
  for (const row of dataset.results) {
    if (!matchesStatus(row)) continue;
    for (const [key, label] of rowTags(row)) tags.set(key, [label, (tags.get(key)?.[1] ?? 0) + 1]);
  }
  if (state.tag && !tags.has(state.tag)) state.tag = "";
  const tagFilter = $("tag-filter");
  tagFilter.hidden = !tags.size;
  tagFilter.replaceChildren(new Option("모든 표시", ""), ...[...tags].sort(([a], [b]) => tagOrder(a) - tagOrder(b) || a.localeCompare(b))
    .map(([key, [label, count]]) => new Option(`${label} · ${count.toLocaleString()}`, key)));
  tagFilter.value = state.tag;
  const active = $("active-filter");
  const groupCard = cards.find((card) => card[4] === state.filter);
  active.hidden = !(state.filterType === "group" && state.filter && groupCard);
  if (!active.hidden) {
    const clear = el("button", "filter-chip", `${groupCard[0]} ×`);
    clear.type = "button";
    clear.onclick = () => setFilter("");
    active.replaceChildren(clear);
  }
  renderTiers();
  renderList();
  renderAggregates(dataset);
}

// 검토 필요를 고르면 주소 목록 전에 거의 확정·확인 필요·일반 보류 단계를 먼저 고르게 한다.
function renderTiers() {
  const picker = $("tier-picker");
  picker.hidden = !(state.filterType === "group" && state.filter === "review");
  if (picker.hidden) return;
  const reviewRows = state.payload.dataset.results.filter((row) => rowGroup(row) === "review");
  const buttons = TIERS.map(([key, label, color, hint]) => {
    const count = reviewRows.filter((row) => rowTier(row) === key).length;
    const button = el("button", `panel tier-card ${color}`,
      el("span", "tier-label", el("i", ""), label),
      el("strong", "", count.toLocaleString(), el("small", "", " 건")),
      el("span", "tier-hint", hint));
    button.type = "button";
    button.setAttribute("aria-pressed", String(state.tier === key));
    button.onclick = () => {
      state.tier = state.tier === key ? null : key; state.bucket = null; state.relation = null; state.page = 0;
      renderTiers(); renderList();
    };
    return button;
  });
  // 도로명주소·지번 관계(1:N · N:1 · N:M)는 단계와 상관없이 따로 모아 본다.
  const relationButtons = RELATIONS.map(([kind, label, tone]) => {
    const count = reviewRows.filter((row) => relationKind(row) === kind).length;
    const button = el("button", `relation-filter ${tone}`, el("b", "", kind), label,
      el("span", "bucket-count", count.toLocaleString()));
    button.type = "button";
    button.disabled = !count;
    button.setAttribute("aria-pressed", String(state.relation === kind));
    button.onclick = () => {
      state.relation = state.relation === kind ? null : kind; state.tier = null; state.bucket = null; state.page = 0;
      renderTiers(); renderList();
    };
    return button;
  });
  const children = [el("div", "tier-heading", el("strong", "", "검토 단계 선택"),
    muted("단계를 고르면 해당 주소 목록이 나옵니다.")), el("div", "tier-grid", ...buttons),
    el("div", "bucket-row relation-row", el("span", "bucket-title", "도로명·지번 관계로 모아 보기"), ...relationButtons)];
  if (state.tier === "general") {
    // 일반 보류는 1순위 후보 수로 한 번 더 나눠 볼 수 있다.
    const general = reviewRows.filter((row) => rowTier(row) === "general");
    const chip = (key, label, count) => {
      const button = el("button", "bucket-chip", label, el("span", "bucket-count", count.toLocaleString()));
      button.type = "button";
      button.setAttribute("aria-pressed", String(state.bucket === key));
      button.onclick = () => { state.bucket = key; state.page = 0; renderTiers(); renderList(); };
      return button;
    };
    children.push(el("div", "bucket-row", el("span", "bucket-title", "후보 수"),
      chip(null, "전체", general.length),
      ...CANDIDATE_BUCKETS.map(([key, label]) => chip(key, label, general.filter((row) => candidateBucket(row) === key).length))));
  }
  if (state.tier === "near_confirmed") {
    const targets = reviewRows.filter((row) => rowTier(row) === "near_confirmed" && canApprove(row));
    const bulk = el("button", "button primary", icon("check"), `1순위로 일괄 승인 · ${targets.length.toLocaleString()}건`);
    bulk.type = "button";
    bulk.disabled = !targets.length || !state.job?.job_id;
    bulk.onclick = () => {
      const sure = confirm(`거의 확정 단계 ${targets.length}건을 각 1순위 후보로 승인합니다. 원문 그대로 맞은 확정과 구분해 '검토 후 확정'으로 표시합니다.`);
      if (sure) approve(targets.map((row) => row.record.address_id));
    };
    children.push(el("div", "tier-actions", bulk, muted("승인한 주소는 검토 후 확정 카드로 옮겨지고, 상세 화면에서 승인을 취소할 수 있습니다.")));
  }
  picker.replaceChildren(...children);
}

// 단독 1순위 후보를 검토 후 확정으로 표시한다. 서버가 승인 가능 여부를 다시 확인한다.
async function approve(addressIds, undo = false) {
  if (!state.job?.job_id) return showError("처리가 끝난 실행에서만 승인할 수 있습니다.");
  try {
    const {rows} = await api(`/api/jobs/${state.job.job_id}/approve`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({address_ids: addressIds, undo}),
    });
    const updated = new Map(rows.map((row) => [row.record.address_id, row]));
    state.payload.dataset.results = state.payload.dataset.results.map((row) => updated.get(row.record.address_id) ?? row);
    renderDashboard();
  } catch (error) {
    showError(error.message);
  }
}

function renderList() {
  const search = $("search").value.trim().toLowerCase();
  const filtered = state.payload.dataset.results.filter((row) => {
    if (!matchesStatus(row)) return false;
    if (state.tag && !hasTag(row, state.tag)) return false;
    if (state.filterType === "group" && state.filter === "review" && !state.tag) {
      if (state.relation ? relationKind(row) !== state.relation : rowTier(row) !== state.tier) return false;
    }
    if (state.filterType === "group" && state.filter === "review" && state.tier === "general" && state.bucket
      && candidateBucket(row) !== state.bucket) return false;
    return !search || [row.record.raw_address, row.record.address_id].some((part) => scalar(part).toLowerCase().includes(search));
  });
  if (!filtered.some((row) => row.record.address_id === state.selected)) {
    state.selected = filtered[0]?.record.address_id ?? null;
    state.tab = "compare";
  }
  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  state.page = Math.min(state.page, pages - 1);
  const rows = filtered.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE);
  $("list-count").textContent = `${filtered.length.toLocaleString()}건`;
  $("row-list").replaceChildren(...rows.map((row) => {
    const selected = row.record.address_id === state.selected;
    const top = rowGroup(row) === "review" ? topCandidate(row) : null;
    const button = el("button", "address-row",
      el("span", "row-top", el("span", "row-address", row.record.raw_address || "(빈 주소)"), icon("chevron")),
      top ? el("span", "row-candidate", `→ 1순위 ${scalar(top.address)}`) : null,
      el("span", "row-bottom", el("span", "row-badges", rowBadge(row),
        row.inherited_from ? el("span", "dup-chip", el("b", "", "중복"), `${row.inherited_from} 결과`) : null,
        missingCodes(row).length ? el("span", "code-missing", `${missingCodes(row).join(", ")} 없음`) : null,
        relationChip(row),
        state.relation ? el("span", "row-tier", TIERS.find(([key]) => key === rowTier(row))[1]) : null),
        el("span", "row-id", row.record.address_id)));
    button.type = "button";
    button.setAttribute("aria-pressed", String(selected));
    button.onclick = () => {
      state.selected = row.record.address_id;
      state.tab = "compare";
      for (const item of $("row-list").children) item.setAttribute("aria-pressed", String(item === button));
      renderDetail(row);
    };
    return button;
  }));
  if (!rows.length) $("row-list").append(muted(state.filterType === "group" && state.filter === "review" && !state.tier && !state.relation && !state.tag
    ? "위에서 검토 단계나 관계를 고르세요." : "조건에 맞는 주소가 없습니다."));
  const footer = $("pagination");
  footer.replaceChildren(el("span", "", filtered.length
    ? `${state.page * PAGE_SIZE + 1}–${Math.min((state.page + 1) * PAGE_SIZE, filtered.length)} / ${filtered.length}건`
    : "0건"));
  if (pages > 1) {
    const prev = el("button", "", "이전");
    const next = el("button", "", "다음");
    prev.disabled = state.page === 0;
    next.disabled = state.page >= pages - 1;
    const turn = (offset) => {
      state.page += offset;
      state.selected = filtered[state.page * PAGE_SIZE]?.record.address_id;
      state.tab = "compare";
      renderList();
    };
    prev.onclick = () => turn(-1);
    next.onclick = () => turn(1);
    footer.append(el("span", "pager", prev, next));
  }
  renderDetail(filtered.find((row) => row.record.address_id === state.selected));
}

function renderDetail(row) {
  const detail = $("detail");
  if (!row) {
    const choose = state.filterType === "group" && state.filter === "review" && !state.tier && !state.relation;
    detail.replaceChildren(el("div", "empty", el("h2", "", choose ? "검토 단계나 관계를 고르세요" : "표시할 주소가 없습니다"),
      muted(choose ? "위에서 단계나 도로명·지번 관계를 고르면 해당 주소 목록과 후보가 나옵니다." : "검색어나 상태 필터를 확인하세요.")));
    return;
  }
  const rawReview = object(row.review);
  const reasons = rowReasons(row);
  const demo = state.payload.source === "demo";
  const {address, source: addressSource} = standardAddress(row);
  const standardRoadAddress = scalar(address.standard_road_address);
  const rawAddress = scalar(row.record.raw_address);
  // 표준화 주소와 같은 후보에서 같은 RAW 행의 지번주소와 행정동을 가져온다.
  const standardCandidate = object(reviewCandidates(row).map(object).find((item) =>
    scalar(item.address) === standardRoadAddress || list(item.linked_road_addresses).map(scalar).includes(standardRoadAddress)));
  const standardLot = {
    lot: standardCandidate.kind === "lot" ? scalar(standardCandidate.address) : list(standardCandidate.linked_lot_addresses).map(scalar).join(", "),
    admin: list(standardCandidate.admin_dongs).map(scalar).filter(Boolean).join(", "),
  };
  const addressLabel = !standardRoadAddress ? "원문 주소"
    : addressSource === "approval" ? "검토 후 확정 주소"
    : addressSource === "review.standard_address" ? "확인된 기본 도로명주소" : "표준화 주소";
  const head = el("div", "detail-head",
    el("div", "detail-meta", el("span", "", `입력 ${row.input_index + 1}행`), code(row.record.address_id), demo ? el("span", "demo-tag", "시연") : null),
    el("div", "detail-title",
      el("div", "detail-addresses", el("span", "detail-address-label", addressLabel),
        el("h2", "", standardRoadAddress || rawAddress || "(빈 주소)"),
        standardRoadAddress && (standardLot.lot || standardLot.admin) ? el("p", "detail-raw-address detail-lot",
          el("span", "detail-address-label", "지번 주소"),
          el("span", "detail-raw-value", standardLot.lot || "단일 지번 없음"),
          standardLot.admin ? el("span", "admin-dong", `행정동 ${standardLot.admin}`) : null) : null,
        standardRoadAddress ? el("p", "detail-raw-address",
          el("span", "detail-address-label", "원문 주소"),
          el("span", "detail-raw-value", rawAddress || "(빈 주소)")) : null),
      rowBadge(row)),
    row.record.normalized_address && row.record.normalized_address !== row.record.raw_address
      ? el("p", "muted", `정규화 주소 · ${row.record.normalized_address}`) : null);
  if (reasons.length) {
    const primary = reasons[0];
    const mainReason = labelOf(REASON, primary);
    head.append(el("div", "reason", icon("info"), el("div", "",
      el("strong", "", mainReason ?? "파이프라인 사유"), el("span", "reason-codes", ...reasons.map(code)))));
  }
  const relation = rowGroup(row) === "review" ? roadLotRelation(row) : null;
  if (relation) {
    head.append(el("div", `relation-note ${RELATIONS.find(([kind]) => kind === relation.kind)[2]}`, icon("layers"),
      el("span", "", el("strong", "", `도로명주소 : 지번 = ${relation.kind}`), ` · ${relation.label} · ${relation.note}`)));
  }
  // 부번이 다르거나 빠졌을 때 같은 본번 아래 기준자료에 있는 부번을 모두 보여준다.
  for (const item of list(object(row.review).sub_numbers).map(object)) {
    const main = scalar(item.number);
    const subs = list(item.sub_numbers).map(Number);
    const kindLabel = item.kind === "road" ? "건물번호" : "지번";
    const input = Number(item.input_sub) ? `${main}-${item.input_sub}` : main;
    head.append(el("div", "sub-number-note", icon("layers"), el("div", "",
      el("strong", "", `기준자료의 ${kindLabel} ${main} 부번 ${subs.length.toLocaleString()}개`),
      el("span", "muted", ` · 원문 ${input}`),
      el("div", "sub-number-list", ...subs.map((sub) => el("span", "sub-number", sub ? `${main}-${sub}` : `${main} (부번 없음)`))),
      item.kind === "lot" ? el("p", "muted", "기준자료(RAW)는 건물이 있는 지번만 담고 있어, 건물 없는 필지는 목록에 없을 수 있습니다.") : null)));
  }
  const missing = missingCodes(row);
  if (missing.length) {
    head.append(el("div", "code-note", icon("info"), el("span", "",
      `주소는 확정됐지만 기준자료(RAW)에 ${missing.join(", ")}가 비어 있어 해당 코드는 빈 값으로 둡니다.`)));
  }
  if (isApproved(row)) {
    const undo = el("button", "button", "승인 취소");
    undo.type = "button";
    undo.onclick = () => approve([row.record.address_id], true);
    head.append(el("div", "approval-note", icon("check"),
      el("span", "", `1순위 후보를 승인해 확정했습니다 · ${scalar(object(row.approval).approved_at).replace("T", " ").slice(0, 16)}`), undo));
  } else if (canApprove(row)) {
    const top = topCandidate(row);
    const ok = el("button", "button primary", icon("check"), "1순위로 승인");
    ok.type = "button";
    ok.disabled = !state.job?.job_id;
    ok.onclick = () => approve([row.record.address_id]);
    head.append(el("div", "approval-note pending", el("span", "", `${labelOf(Object.fromEntries(TIERS.map(([k, l]) => [k, l])), rowTier(row))} · 1순위 ${scalar(top.address)}`), ok));
  }
  if (row.inherited_from) {
    head.append(el("div", "code-note", icon("layers"), el("span", "",
      "원문이 앞 행과 같아 다시 처리하지 않고 대표 행 ", code(row.inherited_from), "의 결과를 그대로 물려받았습니다.")));
  } else if (scalar(object(row.output).representative_address_id)) {
    head.append(el("p", "muted", "중복 대표 ID · ", code(row.output.representative_address_id)));
  }
  const analysisJobId = state.job?.job_id;
  head.append(analysisPanel({row, jobId: analysisJobId, ready: state.job?.status === "done", api,
    onUpdate: (analysis) => {
      if (state.job?.job_id !== analysisJobId) return;
      const current = state.payload.dataset.results.find((item) => item.record.address_id === row.record.address_id);
      if (!current) return;
      current.additional_analysis = analysis;
      if (state.selected === current.record.address_id) renderDetail(current);
    }}));
  const tabs = [
    ["compare", "후보 비교", Array.isArray(rawReview.candidates) ? rawReview.candidates.length : null],
    ["verified", "검증된 구성요소"],
    ["trace", "처리 이력", list(row.traces).length],
    ["codes", "주소와 코드"],
  ];
  const tabBar = el("div", "tabs");
  for (const [key, title, count] of tabs) {
    const button = el("button", "tab", title, count == null ? null : el("span", "tab-count", String(count)));
    button.type = "button";
    button.id = `tab-${key}`;
    button.setAttribute("aria-pressed", String(state.tab === key));
    button.onclick = () => { state.tab = key; renderDetail(row); $(`tab-${key}`).focus(); };
    tabBar.append(button);
  }
  const body = el("div", "detail-body");
  if (state.tab === "compare") renderComparison(body, row, rawReview);
  if (state.tab === "verified") renderVerified(body, row);
  if (state.tab === "trace") renderTraces(body, row);
  if (state.tab === "codes") renderCodes(body, row);
  detail.replaceChildren(head, tabBar, body);
}

function renderComparison(body, row, review) {
  const candidates = reviewCandidates(row);
  const source = state.payload.candidate_comparisons;
  const comparisons = object(object(source)[row.record.address_id]);
  const appendRegion = () => {
    // 표준화가 끝난 주소는 후보 카드의 검증 근거로 충분하므로 지역 블록을 생략한다.
    if (row.region_validation == null || row.status === "resolved") return;
    const region = el("section", "region-summary");
    renderRegion(region, row, {compare: true});
    body.append(region);
  };
  body.append(el("div", "section-title", el("h3", "", "후보 주소 비교"),
    review.status ? badge(review.status, REVIEW) : null));
  if (review.review_type) body.append(el("p", "subtext", scalar(review.review_type)));
  if (review.base_status && review.base_status !== row.status) {
    body.append(el("div", "status-line", "상세 조회 상태", badge(review.base_status)));
  }
  if (!candidates.length) {
    const message = review.status === "no_candidate" || Array.isArray(review.candidates)
      ? "반환된 후보 주소가 없습니다." : "후보 목록이 제공되지 않았습니다.";
    body.append(el("div", "empty compact", icon("search"), el("p", "", message)));
    appendRegion();
  } else {
    body.append(el("div", "original", el("span", "", "원본 주소"), el("strong", "", row.record.raw_address || "(빈 주소)")));
    // 후보가 많으면 한 줄 요약(순위·종류·주소·근거)만 보이고, 누르면 검토 근거 전체가 펼쳐진다.
    const grid = el("div", "candidate-list");
    for (const [i, value] of candidates.entries()) {
      const item = object(value);
      const rank = scalar(item.rank);
      const kind = labelOf({road: "도로명", lot: "지번"}, item.kind) ?? scalar(item.kind);
      const years = list(item.reference_years).map(scalar).filter(Boolean);
      const rawIds = list(item.raw_records).map((ref) =>
        Array.isArray(ref) && ref.length === 2 ? `${scalar(ref[0])} · ${scalar(ref[1])}` : formatValue(ref));
      const swap = substitutionOf(item);
      // 도로명 후보는 표준화 도로명주소를 크게, 같은 RAW 행의 지번주소를 작게 보여준다.
      const roads = list(item.linked_road_addresses).map(scalar).filter(Boolean);
      const lots = list(item.linked_lot_addresses).map(scalar).filter(Boolean);
      const primary = item.kind === "lot" && roads.length ? roads[0] : scalar(item.address);
      const secondary = item.kind === "lot" ? (roads.length ? scalar(item.address) : "") : lots.join(", ");
      const adminDong = list(item.admin_dongs).map(scalar).filter(Boolean).join(", ");
      const evidence = (Array.isArray(item.evidence) ? item.evidence : item.evidence == null ? [] : [item.evidence])
        .filter((line) => line !== swap?.line);
      const card = el("article", swap ? "candidate substituted" : "candidate",
        el("div", "candidate-top", el("span", "rank", `후보 ${i + 1}`),
          el("span", "", [rank && `${rank}순위`, kind].filter(Boolean).join(" · "))),
        swap ? el("div", "swap-banner",
          el("strong", "swap-address", primary || "후보 주소 미제공"),
          secondary || adminDong ? el("span", "swap-lot", [secondary, adminDong && `행정동 ${adminDong}`].filter(Boolean).join(" · ")) : null,
          el("div", "swap-note", el("small", "", swap.kind === "llm" ? "LLM이 오타로 고쳐 찾은 후보" : "유사 지명으로 바꿔 찾은 후보"),
            el("span", "swap", el("span", "from", swap.from), el("span", "arrow", "→"), el("strong", "", swap.to))))
          : el("h4", "", primary || (typeof value === "string" ? value : "후보 주소 미제공")),
        !swap && (secondary || adminDong) ? el("p", "candidate-lot", [secondary, adminDong && `행정동 ${adminDong}`].filter(Boolean).join(" · ")) : null,
        fields([["기준연도", years.join(", ") || "미제공"], ["RAW ID", rawIds.length
          ? el("span", "raw-list", ...rawIds.map(code)) : "미제공"]]));
      if (!swap && list(item.linked_road_addresses).length > 1) {
        card.append(el("div", "linked-roads", el("strong", "", "연결 도로명주소"),
          ...item.linked_road_addresses.map((address) => el("div", "", formatValue(address)))));
      }
      const steps = verificationSteps(row, item, swap);
      if (steps.length) card.append(verificationList(steps));
      card.append(el("div", "evidence", el("strong", "", "반환된 근거"),
        evidence.length ? el("ul", "", ...evidence.map((line) => el("li", "", formatValue(line))))
          : muted("근거 문구 미제공")));
      card.querySelector(".candidate-top")?.remove();
      if (!swap) card.querySelector("h4")?.remove();
      const reason = evidence.length ? formatValue(evidence[0]) + (evidence.length > 1 ? ` 외 ${evidence.length - 1}건` : "")
        : swap ? (swap.kind === "llm" ? "LLM 오타 교정 후보" : "유사 지명 후보") : "근거 문구 미제공";
      // 지번 후보는 연결 도로명주소가 같을 수 있어 지번(예: 자혜리 19-4)을 요약에 함께 둔다.
      const lotShort = item.kind === "lot" && secondary ? secondary.split(" ").slice(-2).join(" ") : "";
      const why = [lotShort, reason].filter(Boolean).join(" · ");
      const summary = el("summary", "candidate-summary",
        el("span", "rank", rank ? `${rank}순위` : `후보 ${i + 1}`),
        el("span", "candidate-kind", kind || ""),
        el("span", "candidate-main", el("strong", "", primary || "후보 주소 미제공"),
          el("span", "candidate-why", why)),
        icon("chevron"));
      const item_ = el("details", "candidate-item", summary, card);
      item_.open = candidates.length === 1;
      grid.append(item_);
    }
    body.append(grid);
    appendRegion();
    const withComparison = candidates.flatMap((_, index) =>
      list(comparisons[index]).filter((item) => scalar(object(item).component)).map((item) => [index, item]));
    if (withComparison.length) {
      body.append(el("h3", "comparison-title", "구성요소별 일치 · 불일치"));
      const table = el("table", "data-table", el("thead", "", el("tr", "",
        ...["후보 / 항목", "원본", "후보 값", "판정 / 근거"].map((label) => el("th", "", label)))));
      const tbody = el("tbody");
      const outcomes = {match: ["일치", "green"], mismatch: ["불일치", "red"], unknown: ["미확인", "slate"]};
      for (const [index, item] of withComparison) {
        tbody.append(el("tr", "", el("th", "", `후보 ${index + 1} · ${item.component}`),
          el("td", "", scalar(item.input_value) || "미제공"), el("td", "", scalar(item.candidate_value) || "미제공"),
          el("td", "", badge(item.outcome, outcomes), scalar(item.evidence) ? el("div", "table-note", scalar(item.evidence)) : null)));
      }
      table.append(tbody);
      body.append(el("div", "table-scroll", table));
      if (new Set(withComparison.map(([index]) => index)).size < candidates.length) {
        body.append(muted("일부 후보는 구조화된 구성요소 비교를 제공하지 않았습니다."));
      }
    }
  }
  if (row.review != null) body.append(disclosure("검토 원본 데이터", json(row.review)));
}

function regionInfo(row) {
  const region = object(row.region_validation);
  const partial = region.partial_match != null && !list(region.paths).length ? object(region.partial_match) : null;
  const tokenList = list(object(row.output).tokens);
  return {
    region, partial,
    paths: partial ? list(partial.paths) : list(region.paths),
    unmatched: partial ? list(partial.unmatched_tokens).map((index) => object(tokenList[index]).raw).filter(Boolean) : [],
  };
}

function renderRegion(body, row, {compare = false} = {}) {
  const {region, partial, paths, unmatched} = regionInfo(row);
  body.append(el("h3", compare ? "comparison-title" : "", "지역 검증 경로"));
  if (region.status) body.append(el("div", "status-line", badge(partial ? "partial_path" : region.status),
    region.reference_year == null ? null : `기준연도 ${scalar(region.reference_year)}`,
    region.used_correction === true ? "수정값 적용" : null));
  if (partial) body.append(muted(`확인된 상위 지역까지 표시합니다. ${unmatched.map(scalar).join(", ") || "일부 지명"}은 기준자료에서 찾지 못했습니다.`));
  if (!paths.length) body.append(muted("반환된 지역 경로가 없습니다."));
  for (const path of paths) {
    const nodes = list(path).map((node) => {
      const item = object(node);
      return el("span", "token", el("small", "", scalar(item.label ?? item.node_type)), scalar(item.name) || formatValue(node));
    });
    nodes.push(...unmatched.map((raw) => el("span", "token missing", el("small", "", "미확인"), scalar(raw))));
    if (nodes.length) body.append(el("div", "token-path", ...nodes));
  }
  if (!partial) return;
  const tokenList = list(object(row.output).tokens);
  const alternatives = list(partial.alternatives).map(object).filter((alt) => list(alt.paths).length);
  if (alternatives.length) {
    body.append(el("h3", "comparison-title", "다른 해석"));
    for (const alt of alternatives) {
      const missing = list(alt.unmatched_tokens).map((index) => scalar(object(tokenList[index]).raw)).filter(Boolean);
      for (const path of list(alt.paths)) {
        body.append(el("div", "token-path alternative",
          ...list(path).map((node) => el("span", "token", el("small", "", scalar(object(node).label)), scalar(object(node).name))),
          ...missing.map((raw) => el("span", "token missing", el("small", "", "미확인"), raw))));
      }
    }
  }
  const candidates = reviewCandidates(row).map(object);
  const hits = (suggestion) => {
    const matched = candidates.filter((item) => substitutionOf(item)?.to === scalar(suggestion.name));
    const exact = matched.filter((item) => list(item.evidence).includes("기준연도 기본 도로명주소 확인")).length;
    return {exact, other: matched.length - exact};
  };
  const suggestions = list(partial.suggestions).map(object).map((suggestion) => ({suggestion, ...hits(suggestion)}));
  suggestions.sort((a, b) => (b.exact - a.exact) || (b.other - a.other));
  const suggestionRow = ({suggestion, exact, other}) => {
    const fullPath = list(list(suggestion.paths)[0]).map((node) => scalar(object(node).name)).join(" ");
    const score = typeof suggestion.jamo_distance === "number" ? `자모 ${suggestion.jamo_distance}개 차이`
      : typeof suggestion.similarity === "number" ? `유사도 ${Math.round(Math.min(suggestion.similarity, 1) * 100)}%` : "";
    const [tone, text] = exact ? ["green", `번지 일치 ${exact}건`] : other ? ["amber", `부번 다른 주소 ${other}건`] : ["slate", "번지 일치 없음"];
    return el("div", `suggestion-row${exact || other ? "" : " quiet"}`,
      el("span", "swap", el("span", "from", scalar(suggestion.raw)), el("span", "arrow", "→"), el("strong", "", scalar(suggestion.name))),
      el("span", "suggestion-meta", [scalar(suggestion.label), score].filter(Boolean).join(" · ")),
      fullPath ? el("span", "suggestion-path", fullPath) : null,
      el("span", `badge ${tone}`, el("i", ""), text));
  };
  body.append(el("h3", "comparison-title", "유사 지명 후보"));
  if (!suggestions.length) body.append(muted("확인된 지역 안에서 비슷한 지명을 찾지 못했습니다."));
  const withHits = suggestions.filter((item) => item.exact || item.other);
  const withoutHits = suggestions.filter((item) => !item.exact && !item.other);
  body.append(...withHits.map(suggestionRow));
  if (withoutHits.length) {
    if (withHits.length) body.append(disclosure(`번지 일치 없는 유사 지명 ${withoutHits.length}개`, ...withoutHits.map(suggestionRow)));
    else body.append(...withoutHits.map(suggestionRow));
  }
  if (suggestions.length) body.append(muted(compare
    ? "유사 지명으로 다시 찾은 주소는 위 후보 카드에 표시합니다. 자동으로 확정하지 않습니다."
    : "후보 비교 탭에서 유사 지명으로 다시 찾은 주소를 확인할 수 있습니다. 자동으로 확정하지 않습니다."));
}

// 후보 하나가 원문의 어떤 조각에서 어떤 근거로 나왔는지 단계별로 정리한다.
function verificationSteps(row, item, swap) {
  const output = object(row.output);
  const region = object(row.region_validation);
  const usedCorrection = region.used_correction === true;
  let tokens = list(output.tokens).map((token) => {
    const raw = scalar(object(token).raw);
    const corrected = scalar(object(token).corrected);
    return usedCorrection && corrected ? {raw: corrected, correctedFrom: raw} : {raw};
  });
  let paths = list(region.paths);
  let dbLabels = list(region.db_labels);
  if (swap?.kind === "similar") {
    const suggestion = list(object(region.partial_match).suggestions).map(object)
      .find((entry) => scalar(entry.name) === swap.to);
    if (!suggestion) return [];
    paths = list(suggestion.paths);
    dbLabels = list(suggestion.db_labels);
    tokens = tokens.map((token, index) => index === suggestion.token_index
      ? {raw: scalar(suggestion.name), swappedFrom: token.raw} : token);
  }
  const roads = list(item.linked_road_addresses).map(scalar);
  const lots = list(item.linked_lot_addresses).map(scalar);
  const road = item.kind === "road" ? scalar(item.address) : roads[0] || "";
  const lot = item.kind === "lot" ? scalar(item.address) : lots[0] || "";
  const text = `${road} ${lot}`;
  const path = (paths.map(list).find((nodes) => nodes.every((node) => text.includes(scalar(object(node).name))))
    ?? list(paths[0])).map(object);
  const steps = [];
  const used = new Set();
  tokens.forEach((token, index) => {
    const labels = list(dbLabels[index]).map(scalar);
    const options = path.map((node, at) => ({node, at})).filter(({node, at}) => !used.has(at) && labels.includes(scalar(node.label)));
    const pick = options.find(({node}) => scalar(node.name) === token.raw)
      ?? options.find(({node}) => scalar(node.name).startsWith(token.raw)) ?? options[0];
    if (!pick) return;
    used.add(pick.at);
    const name = scalar(pick.node.name);
    const label = scalar(pick.node.label);
    const why = token.correctedFrom ? "오타 수정 (LLM이 읽은 명칭으로 검증)"
      : token.swappedFrom
      ? (name === token.raw ? "유사 지명으로 대체" : `유사 지명 ${token.raw}(으)로 대체 · 행정동 → 법정동`)
      : name === token.raw ? "기준자료 명칭 일치"
      : label === "시도" || name.startsWith(token.raw) ? "약칭 → 정식 명칭"
      : label === "읍면동" ? "행정동 → 법정동" : "별칭 일치";
    steps.push({order: pick.at, from: token.swappedFrom ?? token.correctedFrom ?? token.raw, to: name, label, why,
      tone: token.swappedFrom || token.correctedFrom ? "swapped" : ""});
  });
  path.forEach((node, at) => {
    if (used.has(at)) return;
    const below = steps.filter((step) => step.order > at).map((step) => step.to);
    steps.push({order: at, from: "", to: scalar(node.name), label: scalar(node.label),
      why: below.length ? `원문에 없음 · ${below.join(" ")}의 상위 지역으로 보완` : "원문에 없음 · 경로에서 보완", tone: "infer"});
  });
  steps.sort((a, b) => a.order - b.order);
  const records = list(item.raw_records).map((ref) => Array.isArray(ref) ? ref.join("·") : scalar(ref)).join(", ");
  const otherSub = list(item.evidence).includes("같은 본번의 다른 부번");
  // 건물명으로 찾았거나 좁힌 후보는 번호와 건물명을 따로 보여준다.
  const evidenceText = list(item.evidence).map(scalar).join(" · ");
  const building = scalar(item.building_name);
  const numberMissing = evidenceText.includes("입력 번호는 기준자료에 없음");
  for (const segment of list(output.unresolved_segments).map(scalar).filter((value) => /[0-9]/.test(value))) {
    const number = segment.match(/[0-9]+(?:-[0-9]+)?/)?.[0] ?? segment;
    const from = building ? number : segment;
    if (lot && lot.endsWith(` ${number}`)) {
      steps.push({from, to: lot, label: "지번", why: `기준자료 지번 일치${records ? ` · RAW ${records}` : ""}`});
    } else if (road && road.endsWith(` ${number}`)) {
      steps.push({from, to: road, label: "건물번호", why: `기준자료 건물번호 일치${records ? ` · RAW ${records}` : ""}`});
    } else {
      steps.push({from, to: item.kind === "lot" ? lot || road : road || lot, label: item.kind === "lot" ? "지번" : "번호",
        why: numberMissing ? "입력 번호는 기준자료에 없음 · 아래 건물명으로 찾음"
          : otherSub ? "본번은 같고 부번이 다름" : "번호가 정확히 일치하지 않음", tone: "warn"});
    }
  }
  if (building) {
    const fuzzy = /건물명 유사 일치.*?\((\d+)점\)/.exec(evidenceText);
    const sameName = /시군구 안 같은 이름 (\d+)곳/.exec(evidenceText);
    steps.push({from: scalar(item.building_input) || building, to: building, label: "건물명",
      why: [fuzzy ? `건물명 유사 일치 (${fuzzy[1]}점)` : "기준자료 건물명 일치",
        sameName ? `시군구 안 같은 이름 ${sameName[1]}곳` : "", records ? `RAW ${records}` : ""].filter(Boolean).join(" · "),
      tone: fuzzy ? "swapped" : ""});
  }
  // 행정동 이름으로 찾았지만 기준자료 행의 행정동이 다른 참고 후보는 해당 단계를 경고로 표시한다.
  const adminNote = list(item.evidence).map(scalar).map((line) => /^참고: 행정동 불일치 · 입력 (.+?) \/ 기준자료 (.+)$/.exec(line)).find(Boolean);
  if (adminNote) {
    const [, given, actual] = adminNote;
    for (const step of steps) {
      if (step.from === given || step.to === given || (step.tone === "swapped" && swap?.to === given)) {
        Object.assign(step, {tone: "warn", why: `행정동 ${given} → 법정동 ${step.to} · 기준자료 행의 행정동은 ${actual}`});
      } else if (step.label === "지번" || step.label === "건물번호") {
        Object.assign(step, {tone: "warn", why: `번호는 같은 법정동에 있으나 행정동이 ${actual} (참고 후보)`});
      }
    }
  }
  if (road && lot && steps.length) {
    steps.push({from: "", to: road, label: "도로명주소", why: "같은 RAW 행의 도로명주소", tone: "result"});
  }
  return steps;
}

function verificationList(steps) {
  return el("div", "verify",
    el("strong", "", "검증 근거"),
    el("ol", "verify-steps", ...steps.map((step) => el("li", step.tone ? `verify-step ${step.tone}` : "verify-step",
      el("span", "verify-from", step.from || "—"),
      el("span", "arrow", "→"),
      el("span", "verify-to", el("small", "", step.label), step.to),
      el("span", "verify-why", step.why)))));
}

const SUBSTITUTION = /^(유사 지명 후보|LLM 오타 수정):\s*(.+?)\s*→\s*(.+)$/;
function substitutionOf(item) {
  const evidence = Array.isArray(item.evidence) ? item.evidence : [];
  for (const line of evidence) {
    const match = typeof line === "string" ? SUBSTITUTION.exec(line) : null;
    if (match) return {kind: match[1] === "LLM 오타 수정" ? "llm" : "similar", from: match[2], to: match[3], line};
  }
  return null;
}

function renderVerified(body, row) {
  const region = object(row.region_validation);
  const output = object(row.output);
  const labels = list(output.confirmed_token_labels);
  const {partial} = regionInfo(row);
  const dbLabelsFor = (index) => {
    if (!partial) return list(region.db_labels?.[index]);
    const position = list(partial.matched_tokens).indexOf(index);
    return position < 0 ? [] : list(partial.db_labels?.[position]);
  };
  renderRegion(body, row);
  if (labels.length) body.append(el("h3", "comparison-title", "계층 조회에서 확인된 라벨"),
    fields(labels.map((value) => {
      const item = object(value);
      return [`${scalar(item.token)} · ${scalar(item.column)}`, scalar(item.value)];
    })));
  if (list(output.tokens).length) {
    const tokens = el("div", "token-path");
    for (const [index, item] of output.tokens.entries()) {
      const token = object(item);
      const dbLabels = dbLabelsFor(index).map(scalar).join(", ");
      tokens.append(el("span", "token", el("small", "", `파서 · ${list(token.labels).join(", ") || "라벨 없음"}`),
        `${scalar(token.raw)}${token.corrected ? ` → ${scalar(token.corrected)}` : ""}`,
        el("small", "", `DB 라벨 · ${dbLabels || "미제공 또는 미일치"}`)));
    }
    body.append(el("h3", "comparison-title", "파서가 추출한 토큰"), tokens);
  }
  if (list(output.unresolved_segments).length) {
    body.append(el("h3", "comparison-title", "파싱에서 남은 원문"),
      el("div", "token-path", ...output.unresolved_segments.map((item) => el("span", "token", formatValue(item)))));
  }
  if (row.region_validation != null) body.append(disclosure("지역 검증 원본 데이터", json(row.region_validation)));
  if (row.output != null) body.append(disclosure("단계 출력 원본 데이터", json(row.output)));
}

function renderTraces(body, row) {
  body.append(el("h3", "", "단계별 처리 이력"));
  if (!row.traces.length) body.append(muted("기록된 실행 단계가 없습니다."));
  body.append(traceList(row.traces, state.payload.source === "demo"));
}

function traceList(traces, demo) {
  const container = el("ol", "timeline");
  for (const value of traces) {
    const trace = object(value);
    const details = object(trace.details);
    const elapsed = !demo && typeof trace.elapsed_ms === "number"
      ? `${trace.elapsed_ms.toLocaleString(undefined, {maximumFractionDigits: 2})} ms${details.shared_elapsed === true ? " · 공유 요청" : ""}`
      : "";
    container.append(el("li", "", el("div", "trace-title",
      el("strong", "", labelOf(MODULES, trace.module_name) ?? (scalar(trace.module_name) || "단계")),
      badge(trace.status)), el("div", "trace-meta",
      [trace.module_name, trace.module_version ? `v${trace.module_version}` : "", elapsed].filter(Boolean).join(" · ")),
    trace.reason_code ? el("p", "trace-reason", code(trace.reason_code)) : null,
    Object.keys(details).length ? disclosure("단계 근거", json(details)) : null));
  }
  return container;
}

function renderCodes(body, row) {
  const {address, source} = standardAddress(row);
  const review = object(row.review);
  const standardization = object(row.standardization);
  body.append(el("div", "section-title", el("h3", "", "주소와 코드"),
    standardization.status ? badge(standardization.status) : null));
  body.append(fields([
    ["기본 도로명주소", scalar(address.standard_road_address) || "미제공"],
    ["주소 출처", source ? code(source) : "미제공"],
    ["확인된 지번", scalar(review.confirmed_lot) || "미제공"],
    ["표준화 RAW ID", list(standardization.raw_record_ids).length
      ? el("span", "raw-list", ...standardization.raw_record_ids.map(code)) : "미제공"],
    ...CODES.map(([key, label]) => [label, scalar(address[key]) ? code(address[key]) : "미제공"]),
  ]));
  // 결과 파일(CSV)에 그대로 나가는 한 줄 도로명주소와 개별 컬럼.
  const output = object(row.output_fields);
  body.append(el("div", "section-title", el("h3", "", "출력 컬럼"), muted("결과 다운로드(CSV)에 이 값이 그대로 나갑니다.")));
  if (Object.keys(output).length) {
    body.append(el("div", "output-line", el("span", "detail-address-label", OUTPUT_COLUMNS[0]),
      el("strong", "", scalar(output[OUTPUT_COLUMNS[0]]) || "(도로명주소 미확정)")));
    body.append(fields(OUTPUT_COLUMNS.slice(1).map((column) => [column, scalar(output[column]) || "—"])));
  } else {
    body.append(muted("이 결과에는 출력 컬럼이 없습니다. 파이프라인 연결 모드에서 실행하면 채워집니다."));
  }
  if (row.standardization != null) body.append(disclosure("표준화 원본 데이터", json(row.standardization)));
}

function renderAggregates(dataset) {
  const aggregate = $("aggregates");
  const summary = el("div", "aggregate-grid");
  const statuses = el("section", "", el("h3", "", "주소 상태 집계"), muted("DatasetResult.summary · 건수"));
  const counts = Object.entries(dataset.summary);
  statuses.append(counts.length ? fields(counts.map(([name, count]) => [badge(name), scalar(count)])) : muted("집계가 없습니다."));
  const reviews = el("section", "", el("h3", "", "검토 유형 집계"));
  for (const [type, statuses] of Object.entries(object(dataset.review_summary))) {
    reviews.append(el("h4", "", type), fields(Object.entries(object(statuses)).map(([name, count]) =>
      [badge(name, REVIEW), scalar(count)])));
  }
  if (!Object.keys(object(dataset.review_summary)).length) reviews.append(muted("검토 유형 집계가 제공되지 않았습니다."));
  summary.append(statuses, reviews);
  const batches = el("section", "batches", el("h3", "", "배치 조회 이력"));
  for (const batch of dataset.batch_results) {
    const traces = list(object(batch).search_traces);
    if (traces.length) batches.append(disclosure(`${scalar(batch.run_id)} · ${traces.length}개 단계`,
      traceList(traces, state.payload.source === "demo")));
  }
  if (batches.childElementCount === 1) batches.append(muted("반환된 배치 조회 이력이 없습니다."));
  aggregate.replaceChildren(summary, batches);
}

start();
