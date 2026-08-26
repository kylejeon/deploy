/* AutoDeploy 설치 콘솔 — dev-spec-web-console §F8.
 *
 * 프로토타입의 화면 구성을 그대로 두고 목데이터만 실제 API 로 바꾼 것이다.
 * 단계 키(preflight/bootstrap/configure/verify/create/apply/rollback/clean)는
 * 서버의 ansible_log.STEP_MARKERS 와 같은 값이라 화면에서 새로 지어내지 않는다.
 */
"use strict";

/* ══ 상수 (~/hub-provisioning 실측 기준) ══════════════════ */

const PROFILES = {
  onprem: "전부 사이트 안에서 동작 (폐쇄망)",
  "hybrid-with-ai": "사이트에 앱+AI, 중앙과 연동",
  "hybrid-without-ai": "사이트에 앱만, 중앙과 연동",
};
const FORKS = 5; /* ansible 기본 forks — 6대 이상은 5대씩 나눠 돈다 */

/* 서버가 로그에서 뽑는 단계 키와 1:1. 없는 단계를 그리면 영원히 안 채워진다. */
const PHASES = {
  install: [["preflight", "preflight 게이트"], ["bootstrap", "host → k0s (bootstrap.yml)"], ["configure", "플랫폼 · 앱 (cluster.yml)"]],
  configure: [["preflight", "preflight 게이트"], ["configure", "플랫폼 · 앱 (cluster.yml)"]],
  verify: [["verify", "verify role"]],
  patch: [["create", "번들 생성 (patch-create.yml)"], ["apply", "번들 적용 (patch-apply.yml)"]],
  rollback: [["rollback", "직전 패치 복귀 (patch-rollback.yml)"]],
  clean: [["clean", "초기화 (clean.yml)"]],
};
const KIND_LABEL = { install: "설치", configure: "configure", patch: "patch", rollback: "rollback", verify: "verify", clean: "초기화" };

const MODES = {
  reset: { flag: "", level: "reset", keep: "false", label: "초기화 (다시 설치할 서버)",
    d: "설치가 꼬였거나 프로파일을 바꿔 다시 설치할 때",
    wipes: "클러스터 · 앱 · 데이터 전부", keeps: "bootstrap 산출물(도구·다운로드) → 재설치가 빠름" },
  "reset-keep": { flag: "--keep-data", level: "reset", keep: "true", label: "초기화 + 데이터 보존",
    d: "앱만 갈아엎고 /data 는 남길 때",
    wipes: "클러스터 · 앱", keeps: "/data · bootstrap 산출물" },
  uninstall: { flag: "--uninstall", level: "uninstall", keep: "false", label: "완전 삭제 (서버 반납)",
    d: "서버를 반납하거나 hub를 철수할 때",
    wipes: "reset 이 지우는 것 + bootstrap 산출물 전부", keeps: "OS 기본 구성만" },
};

const PILL = {
  running: ["run", "진행 중"], awaiting: ["cancel", "승인 대기"], succeeded: ["ok", "성공"],
  failed: ["fail", "실패"], queued: ["wait", "대기"], cancelled: ["cancel", "취소됨"],
};
const ACTIVE = ["queued", "running", "awaiting"];
const DASH_POLL_MS = 4000;

/* ══ 유틸 ══════════════════════════════════════════════ */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* SQLite 의 시각은 UTC 문자열이다. Z 를 붙이지 않으면 브라우저가 로컬시각으로
 * 읽어 9시간이 어긋난다. */
function toDate(sqlTime) {
  if (!sqlTime) return null;
  const d = new Date(String(sqlTime).replace(" ", "T") + "Z");
  return isNaN(d) ? null : d;
}
const two = (n) => String(n).padStart(2, "0");
function shortTime(sqlTime) {
  const d = toDate(sqlTime);
  return d ? `${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}` : "–";
}
function clockTime(sqlTime) {
  const d = toDate(sqlTime) || new Date();
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
}
function fmtSec(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return `${h}시간 ${m}분`;
  return m ? `${m}분 ${two(s)}초` : `${s}초`;
}
function duration(job) {
  const start = toDate(job.started_at) || toDate(job.created_at);
  const end = toDate(job.finished_at);
  if (!start) return "–";
  return fmtSec(((end || new Date()) - start) / 1000);
}
const pill = (st) => {
  const [cls, label] = PILL[st] || ["wait", st];
  return `<span class="pill pill--${cls}"><span class="pill__dot"></span>${esc(label)}</span>`;
};
function kindTag(job) {
  if (job.kind === "install") return "";
  const cls = job.kind === "clean" ? " kind--clean" : (job.kind === "patch" || job.kind === "rollback") ? " kind--patch" : "";
  const mode = job.clean_mode && MODES[job.clean_mode];
  const extra = mode ? ` ${mode.level}${mode.flag ? " " + mode.flag : ""}` : "";
  return `<span class="kind${cls}">${esc(KIND_LABEL[job.kind] || job.kind)}${esc(extra)}</span>`;
}
const hostsOf = (job) => (job.hosts || []).map((h) => h.host);
function hostLabel(job) {
  const hs = hostsOf(job);
  if (!hs.length) return job.kind === "patch" ? "컨트롤러" : "–";
  return hs.length === 1 ? hs[0] : `${hs[0]} 외 ${hs.length - 1}대`;
}
const phasesOf = (job) => PHASES[job.kind] || PHASES.install;

function toast(message, kind) {
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " toast--" + kind : "");
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 6500);
}

/* ══ API ══════════════════════════════════════════════ */

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function api(path, { method = "GET", body, text = false } = {}) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (method !== "GET") opts.headers["X-CSRF-Token"] = state.csrf;

  let response;
  try {
    response = await fetch(path, opts);
  } catch (e) {
    throw new ApiError("서버에 연결할 수 없습니다", 0);
  }
  if (response.status === 401) { location.href = "/login"; throw new ApiError("로그인이 필요합니다", 401); }
  if (text) {
    const body = await response.text();
    if (!response.ok) throw new ApiError(body || `요청 실패 (${response.status})`, response.status);
    return body;
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(data.error || `요청 실패 (${response.status})`, response.status);
  return data;
}

/* ══ 상태 / 라우팅 ══════════════════════════════════════ */

const state = {
  view: "dash", csrf: "", me: null,
  servers: [], mtime: 0, jobs: [], job: null,
  jobId: null, hostFilter: "", autoscroll: true,
  lines: [], lastLineId: 0, lastLineAt: null,
  sse: null, pollTimer: null, tickTimer: null,
};

function go(view, id) {
  closeStream();
  clearInterval(state.pollTimer); state.pollTimer = null;
  state.view = view;
  if (id !== undefined) { state.jobId = id; state.hostFilter = ""; state.autoscroll = true; }
  for (const v of ["dash", "job", "srv", "new", "clean"]) $("#view-" + v).hidden = v !== view;
  for (const b of $("#nav").children) {
    b.dataset.go === view ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current");
  }
  const hash = view === "job" ? `#job/${state.jobId}` : `#${view}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
  window.scrollTo({ top: 0 });
  ({ dash: showDash, job: showJob, srv: showServers, new: showNew, clean: showClean })[view]();
}

function routeFromHash() {
  const m = /^#job\/(\d+)$/.exec(location.hash);
  if (m) return go("job", Number(m[1]));
  const view = (location.hash || "#dash").slice(1);
  go(["dash", "srv", "new", "clean"].includes(view) ? view : "dash");
}

/* ══ 대시보드 ══════════════════════════════════════════ */

async function showDash() {
  await refreshDash();
  state.pollTimer = setInterval(refreshDash, DASH_POLL_MS);
  loadPreflight();
}

async function refreshDash() {
  try {
    const data = await api("/api/jobs?limit=50");
    state.jobs = data.jobs;
  } catch (e) {
    if (e.status !== 401) toast(e.message, "danger");
    return;
  }
  if (state.view !== "dash") return;
  renderDash();
}

function renderDash() {
  const jobs = state.jobs;
  const active = jobs.filter((j) => ACTIVE.includes(j.status));
  const badge = $("#navBadge");
  badge.hidden = active.length === 0;
  badge.textContent = active.length;

  $("#liveSlot").innerHTML = active.length
    ? active.map(liveCard).join("")
    : `<div class="card"><div class="card__body dim" style="text-align:center; padding:26px">진행 중인 작업이 없습니다.</div></div>`;

  const done = jobs.filter((j) => !ACTIVE.includes(j.status));
  const tiles = [
    ["진행 중", active.length, "run"],
    ["성공", done.filter((j) => j.status === "succeeded").length, "ok"],
    ["실패", done.filter((j) => j.status === "failed").length, "fail"],
    ["등록 서버", state.servers.length, ""],
  ];
  $("#tiles").innerHTML = tiles.map(([n, v, c]) =>
    `<div class="tile ${c ? "tile--" + c : ""}"><span class="lbl">${esc(n)}</span><span class="tile__v">${v}</span></div>`).join("");

  $("#recentCount").textContent = `${jobs.length}건`;
  $("#rows").innerHTML = jobs.length ? jobs.map(jobRow).join("")
    : `<tr><td colspan="8" class="dim" style="text-align:center; padding:24px">아직 실행한 작업이 없습니다.</td></tr>`;
  $$("#rows tr[data-open]").forEach((tr) => tr.addEventListener("click", () => go("job", Number(tr.dataset.open))));
}

function liveCard(job) {
  const isClean = job.kind === "clean";
  const phases = phasesOf(job);
  const idx = Math.max(0, phases.findIndex(([k]) => k === job.current_step));
  const pct = job.status === "queued" ? 0 : Math.round(((idx + 0.5) / phases.length) * 100);
  const queued = job.status === "queued"
    ? `<p class="note">앞선 작업 ${job.queue_position || 1}건이 끝나면 시작됩니다.</p>` : "";
  return `<div class="card live${isClean ? " live--clean" : ""}">
    <div class="live__top">
      <div>
        <div class="live__hosp">${esc(hostLabel(job))}</div>
        <div class="live__meta">${kindTag(job)}${job.env ? `<span class="env">${esc(job.env)}</span>` : ""}
          <span class="sep">·</span><span class="mono">#${job.id}</span>
          <span class="sep">·</span>${esc(job.started_by || "")}
          <span class="sep">·</span>${esc(duration(job))} 경과</div>
      </div>
      <div class="row" style="align-items:flex-start">${pill(job.status)}
        <button class="btn btn--sm" data-open-job="${job.id}">상세</button></div>
    </div>
    <div class="live__prog">
      <div class="bar${isClean ? " bar--clean" : ""}"><i style="width:${pct}%"></i></div>
      <div class="live__now"><b>${esc(stepLabel(job, job.current_step))}</b>
        <span class="dim">${idx + 1} / ${phases.length}</span></div>
      ${queued}
    </div></div>`;
}

function stepLabel(job, key) {
  if (!key) return job.status === "queued" ? "대기 중" : "시작 중";
  const found = phasesOf(job).find(([k]) => k === key);
  return found ? found[1] : key;
}

function jobRow(job) {
  return `<tr data-open="${job.id}">
    <td><span class="jobid">#${job.id}</span> ${kindTag(job)}</td>
    <td><span class="tag">${esc(hostLabel(job))}</span></td>
    <td>${job.env ? `<span class="env">${esc(job.env)}</span> ` : ""}<span class="mono dim">${esc(job.ref || "")}</span></td>
    <td>${pill(job.status)}</td>
    <td class="dim">${esc(stepLabel(job, job.current_step))}</td>
    <td class="mono">${esc(duration(job))}</td>
    <td class="mono dim">${esc(shortTime(job.created_at))}</td>
    <td style="text-align:right"><span class="dim">›</span></td></tr>`;
}

async function loadPreflight() {
  const slot = $("#pfSlot");
  slot.innerHTML = `<div class="pf"><div class="pf__i" style="grid-column:1/-1"><span class="dim">컨트롤러 자격 점검 중…</span></div></div>`;
  try {
    const data = await api("/api/preflight");
    const glyph = { ok: "✓", warn: "!", err: "✕" };
    const items = data.checks.map((c) => {
      const level = c.level === "ok" ? "ok" : c.level === "warn" ? "warn" : "err";
      const [head, ...rest] = c.message.split(":");
      return `<div class="pf__i"><span class="pf__g pf__g--${level}" aria-hidden="true">${glyph[level]}</span>
        <span class="pf__t"><b>${esc(head)}</b><br><span class="dim">${esc(rest.join(":").trim() || c.message)}</span></span></div>`;
    }).join("");
    slot.innerHTML = `<div class="pf">${items}
      <button class="btn btn--xs" id="pfAgain">다시 점검</button></div>`;
    $("#pfAgain").addEventListener("click", loadPreflight);
  } catch (e) {
    slot.innerHTML = `<div class="alert"><span class="alert__g" aria-hidden="true">✕</span>
      <p class="note" style="color:var(--ink-2)">자격 점검을 실행할 수 없습니다 — ${esc(e.message)}</p></div>`;
  }
}

/* ══ 작업 상세 ══════════════════════════════════════════ */

async function showJob() {
  const view = $("#view-job");
  view.innerHTML = `<p class="dim">불러오는 중…</p>`;
  try {
    state.job = await api(`/api/jobs/${state.jobId}`);
  } catch (e) {
    view.innerHTML = `<div class="alert"><span class="alert__g">✕</span>
      <p class="note" style="color:var(--ink-2)">${esc(e.message)}</p></div>`;
    return;
  }
  state.lines = []; state.lastLineId = 0; state.lastLineAt = null;
  renderJob();
  openStream();
  clearInterval(state.tickTimer);
  state.tickTimer = setInterval(tickSince, 1000);
}

function renderJob() {
  const job = state.job;
  const phases = phasesOf(job);
  const idx = phases.findIndex(([k]) => k === job.current_step);
  const finished = !ACTIVE.includes(job.status);

  const steps = phases.map(([key, label], i) => {
    let s = "wait";
    if (job.status === "cancelled" && i === idx) s = "cancel";
    else if (job.status === "failed" && i === idx) s = "fail";
    else if (i < idx || (finished && job.status === "succeeded")) s = "done";
    else if (i === idx) s = job.status === "awaiting" ? "done" : "run";
    const mark = s === "done" ? "✓" : s === "fail" ? "✕" : s === "cancel" ? "–" : String(i + 1);
    const spin = s === "run" ? `<span class="spin" aria-hidden="true"></span>` : "";
    return `<div class="step" data-state="${s}"><span class="step__n">${mark}</span>
      <span class="step__name">${esc(label)}</span><span class="step__t">${spin}</span></div>`;
  }).join("");

  const hosts = (job.hosts || []).map((h) => {
    const cls = { succeeded: "ok", failed: "fail", running: "run", cancelled: "wait", queued: "wait" }[h.status] || "wait";
    const mark = { succeeded: "✓", failed: "✕", running: "•", cancelled: "–", queued: "·" }[h.status] || "·";
    const recap = h.recap ? `<span class="mono dim" style="font-size:11.5px">ok=${h.recap.ok} chg=${h.recap.changed} failed=${h.recap.failed} unreach=${h.recap.unreachable}</span>` : "";
    return `<div class="hrow"><span class="hdot hdot--${cls}">${mark}</span>
      <span class="mono">${esc(h.host)}</span>${recap}</div>`;
  }).join("");

  const failedHosts = (job.hosts || []).filter((h) => h.status === "failed").map((h) => h.host);
  const actions = [];
  if (ACTIVE.includes(job.status) && job.status !== "awaiting") actions.push(`<button class="btn btn--sm btn--ghost-danger" id="jCancel">작업 취소</button>`);
  if (job.status === "awaiting") {
    actions.push(`<button class="btn btn--sm btn--primary" id="jApprove">적용 승인</button>`);
    actions.push(`<button class="btn btn--sm btn--ghost-danger" id="jReject">적용 거부</button>`);
  }
  if (failedHosts.length) actions.push(`<button class="btn btn--sm btn--primary" id="jRetryFailed">실패한 ${failedHosts.length}대만 재시도</button>`);
  if (finished && hostsOf(job).length) actions.push(`<button class="btn btn--sm" id="jRetryAll">전체 재시도</button>`);
  actions.push(`<a class="btn btn--sm" href="/api/jobs/${job.id}/log" download>로그 내려받기</a>`);
  /* 링크는 서버가 chat.getPermalink 로 받아둔 값만 쓴다 — 워크스페이스 도메인을
     모르는 채로 URL 을 조립하면 엉뚱한 곳으로 보낸다. */
  if (job.slack_permalink) {
    actions.push(`<a class="btn btn--sm" href="${esc(job.slack_permalink)}" target="_blank" rel="noopener">Slack 스레드 열기</a>`);
  }

  const awaiting = job.status === "awaiting"
    ? `<div class="alert alert--warn"><span class="alert__g">!</span><p class="note" style="color:var(--ink-2)">
        번들이 만들어졌습니다. <b>적용 승인</b>을 눌러야 타겟에 반영됩니다. 거부해도 번들은 컨트롤러에 남습니다.</p></div>` : "";
  const error = job.error_message && job.status === "failed"
    ? `<div class="alert"><span class="alert__g">✕</span><p class="note" style="color:var(--ink-2)">${esc(job.error_message)}</p></div>` : "";

  const chips = ["", ...hostsOf(job)].map((h) =>
    `<button type="button" class="hchip" data-host="${esc(h)}" aria-pressed="${state.hostFilter === h}">${h ? esc(h) : "전체"}</button>`).join("");

  $("#view-job").innerHTML = `
    <div class="topline"><button class="btn btn--sm" id="jBack">← 목록</button>
      <h1>#${job.id} ${esc(hostLabel(job))}</h1>${kindTag(job)}
      ${job.env ? `<span class="env">${esc(job.env)}</span>` : ""}${pill(job.status)}</div>
    <div class="stack gap-16">
      ${awaiting}${error}
      <div class="card"><div class="card__body">
        <dl class="kv">
          <dt>시작</dt><dd>${esc(shortTime(job.created_at))}</dd>
          <dt>소요</dt><dd id="jDur">${esc(duration(job))}</dd>
          <dt>실행자</dt><dd>${esc(job.started_by || "–")}</dd>
          <dt>ref</dt><dd>${esc(job.ref || "–")}${job.ref_type ? ` (${esc(job.ref_type)})` : ""}</dd>
          <dt>종료 코드</dt><dd>${job.exit_code === null || job.exit_code === undefined ? "–" : job.exit_code}</dd>
          ${job.cancel_by ? `<dt>취소</dt><dd>${esc(job.cancel_by)}</dd>` : ""}
        </dl>
        <div class="row" style="margin-top:14px">${actions.join("")}</div>
      </div></div>
      <div class="split">
        <div class="stack gap-12">
          <div class="card"><div class="card__head"><h2>진행 단계</h2></div><div class="steps">${steps}</div>
            ${hosts ? `<div class="hosts">${hosts}</div>` : ""}</div>
        </div>
        <div class="stack gap-12">
          <div class="console">
            <div class="console__bar">
              <span class="lbl">실행 로그</span>
              <div class="hostbar push">${chips}</div>
              <button class="toggle" id="jAuto" aria-pressed="${state.autoscroll}">자동 스크롤</button>
              <span class="since" id="jSince"></span>
            </div>
            <div class="console__body" id="log"></div>
          </div>
          <div class="console console--errs" id="errPanel" hidden>
            <div class="console__errbar">
              <span id="errCount"></span>
              <span class="push console__errhint">클릭하면 로그의 해당 위치로 이동합니다</span>
            </div>
            <div class="console__errbody" id="errBody"></div>
          </div>
        </div>
      </div>
    </div>`;

  $("#jBack").addEventListener("click", () => go("dash"));
  $("#jAuto").addEventListener("click", (e) => {
    state.autoscroll = !state.autoscroll;
    e.currentTarget.setAttribute("aria-pressed", state.autoscroll);
    if (state.autoscroll) scrollLog(true);
  });
  $$("#view-job .hchip").forEach((b) => b.addEventListener("click", () => {
    state.hostFilter = b.dataset.host;
    $$("#view-job .hchip").forEach((x) => x.setAttribute("aria-pressed", x.dataset.host === state.hostFilter));
    paintLog();
  }));
  $("#jCancel")?.addEventListener("click", () => confirmCancel(job.id));
  $("#jApprove")?.addEventListener("click", () => act(`/api/jobs/${job.id}/approve`, "적용을 승인했습니다"));
  $("#jReject")?.addEventListener("click", () => act(`/api/jobs/${job.id}/reject`, "적용을 거부했습니다 (번들은 유지)"));
  $("#jRetryFailed")?.addEventListener("click", () => retry(job, failedHosts));
  $("#jRetryAll")?.addEventListener("click", () => retry(job, hostsOf(job)));
  paintLog();
}

/* 로그 — 호스트 필터는 해당 호스트 줄 + 공통 줄(host=null)을 남긴다 (AC-6) */
function visibleLines() {
  if (!state.hostFilter) return state.lines;
  return state.lines.filter((l) => !l.host || l.host === state.hostFilter);
}

/* ── 오류 요약 ────────────────────────────────────────
 * 실행 로그 아래에 실패 줄만 모아 보여준다. 설치 로그는 수천 줄이라
 * 실패 지점을 찾으려고 스크롤을 되짚게 되는데 그걸 없애는 것이 목적이다.
 *
 * 대상은 파서가 err 로 태깅한 줄뿐이다. `FAILED - RETRYING` 은 정상 폴링이라
 * out 으로 분류되므로 여기 섞이지 않는다 — k0s 기동 대기만 해도 수십 줄이 나온다.
 *
 * ansible 은 `fatal: [host]: FAILED! =>` 다음 줄부터 들여쓴 YAML 본문에 진짜
 * 이유를 담는다. 그 본문도 err 로 태깅돼 오므로, 들여쓰기를 보고 머리줄에
 * 이어붙여 한 덩어리로 묶는다. 머리줄만 보여주면 "FAILED!" 밖에 안 보인다.
 */
const ERR_BODY_MAX = 8;

function errorBlocks() {
  const blocks = [];
  let prevWasErr = false;
  for (const line of visibleLines()) {
    if (line.kind !== "err") { prevWasErr = false; continue; }
    if (prevWasErr && /^\s/.test(line.line)) blocks[blocks.length - 1].body.push(line.line);
    else blocks.push({ id: line.id, at: line.created_at, step: line.step, head: line.line, body: [] });
    prevWasErr = true;
  }
  return blocks;
}

function renderErrors() {
  const panel = $("#errPanel");
  if (!panel) return;
  const blocks = errorBlocks();
  panel.hidden = blocks.length === 0;
  if (!blocks.length) return;

  const labels = new Map(phasesOf(state.job || {}));
  $("#errCount").textContent = `오류 ${blocks.length}건`;
  $("#errBody").innerHTML = blocks.map((b) => {
    const shown = b.body.slice(0, ERR_BODY_MAX);
    const more = b.body.length - shown.length;
    const body = shown.map((t) => `<div class="errblk__line">${esc(t)}</div>`).join("")
      + (more > 0 ? `<div class="errblk__meta">… ${more}줄 더 (전체는 위 로그에서)</div>` : "");
    const step = b.step ? ` · ${esc(labels.get(b.step) || b.step)}` : "";
    return `<button class="errblk" type="button" data-jump="${b.id}">
      <div class="errblk__meta">${esc(clockTime(b.at))}${step}</div>
      <div class="errblk__line">${esc(b.head)}</div>${body}</button>`;
  }).join("");
}

/* 클릭하면 본문 로그의 그 줄로 이동. 자동 스크롤이 켜져 있으면 새 줄이 올 때마다
 * 다시 맨 아래로 끌려가 이동이 무의미해지므로 함께 끈다. */
function jumpToLine(id) {
  const target = $(`#log .ln[data-id="${id}"]`);
  if (!target) return;
  if (state.autoscroll) {
    state.autoscroll = false;
    $("#jAuto")?.setAttribute("aria-pressed", "false");
  }
  target.scrollIntoView({ block: "center" });
  $$("#log .ln--hit").forEach((el) => el.classList.remove("ln--hit"));
  target.classList.add("ln--hit");
}

function paintLog() {
  const el = $("#log");
  if (!el) return;
  const lines = visibleLines();
  el.innerHTML = lines.length ? lines.map(lineHtml).join("")
    : `<div class="ln"><time></time><span class="tx" style="color:var(--console-dim)">로그가 아직 없습니다.</span></div>`;
  renderErrors();
  scrollLog();
}

function lineHtml(line) {
  const kind = ["task", "ok", "chg", "err", "recap", "warn", "skip"].includes(line.kind) ? line.kind : "out";
  return `<div class="ln ln--${kind}" data-id="${line.id}"><time>${esc(clockTime(line.created_at))}</time><span class="tx">${esc(line.line)}</span></div>`;
}

function appendLines(rows) {
  if (!rows.length) return;
  state.lines.push(...rows);
  state.lastLineId = rows[rows.length - 1].id;
  state.lastLineAt = Date.now();
  renderErrors();
  const el = $("#log");
  if (!el) return;
  const html = rows.filter((l) => !state.hostFilter || !l.host || l.host === state.hostFilter).map(lineHtml).join("");
  if (!html) return;
  if (state.lines.length === rows.length) el.innerHTML = "";
  el.insertAdjacentHTML("beforeend", html);
  scrollLog();
}

function scrollLog(force) {
  const el = $("#log");
  if (el && (state.autoscroll || force)) el.scrollTop = el.scrollHeight;
}

/* "마지막 출력 이후 N초" — TASK 단위 갱신이라 긴 TASK 중에는 정적인 것이 정상.
 * 60초부터 강조하고 120초부터 안내를 덧붙인다 (§F4). */
function tickSince() {
  const el = $("#jSince");
  if (!el) { clearInterval(state.tickTimer); state.tickTimer = null; return; }
  // 소요는 렌더 때 한 번 박히면 그대로라 새로고침해야 움직였다. 같은 타이머에 태운다.
  const dur = $("#jDur");
  if (dur && state.job) dur.textContent = duration(state.job);
  if (!state.job || !ACTIVE.includes(state.job.status)) { el.textContent = ""; return; }
  if (!state.lastLineAt) { el.textContent = "출력 대기 중"; return; }
  const sec = Math.floor((Date.now() - state.lastLineAt) / 1000);
  el.textContent = sec >= 120 ? `마지막 출력 ${fmtSec(sec)} 전 · 긴 작업 진행 중일 수 있습니다`
    : `마지막 출력 ${fmtSec(sec)} 전`;
  el.classList.toggle("since--warn", sec >= 60);
}

/* ══ SSE ══════════════════════════════════════════════ */

function openStream() {
  closeStream();
  const es = new EventSource(`/api/jobs/${state.jobId}/stream?after=${state.lastLineId}`);
  state.sse = es;
  let batch = [];
  let flush = null;

  es.onmessage = (event) => {
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    if (data.type === "line") {
      if (data.id <= state.lastLineId) return;
      batch.push(data);
      if (!flush) flush = setTimeout(() => { flush = null; appendLines(batch); batch = []; }, 80);
      return;
    }
    if (data.type === "status" || data.type === "end") {
      if (flush) { clearTimeout(flush); flush = null; appendLines(batch); batch = []; }
      refreshJobMeta(data.type === "end");
    }
  };
  es.onerror = () => {
    /* 브라우저가 알아서 재연결한다. 재연결 시 after 가 갱신되도록 다시 연다. */
    if (state.sse !== es) return;
    es.close();
    state.sse = null;
    if (state.view === "job") setTimeout(() => { if (state.view === "job") openStream(); }, 2000);
  };
}

function closeStream() {
  if (state.sse) { state.sse.close(); state.sse = null; }
  clearInterval(state.tickTimer); state.tickTimer = null;
}

async function refreshJobMeta(ended) {
  if (state.view !== "job") return;
  try {
    state.job = await api(`/api/jobs/${state.jobId}`);
  } catch { return; }
  const lines = state.lines, lastId = state.lastLineId, lastAt = state.lastLineAt;
  renderJob();
  state.lines = lines; state.lastLineId = lastId; state.lastLineAt = lastAt;
  paintLog();
  if (ended) { closeStream(); state.tickTimer = setInterval(tickSince, 1000); }
}

/* ══ 작업 조작 ══════════════════════════════════════════ */

async function act(path, okMessage) {
  try {
    await api(path, { method: "POST" });
    toast(okMessage);
    await refreshJobMeta(false);
    openStream();
  } catch (e) {
    toast(e.message, "danger");
  }
}

function confirmCancel(id) {
  modal(`<h2>작업을 취소할까요?</h2>
    <p class="note">실행 중인 ansible 프로세스를 종료합니다. 여러 대를 대상으로 하는 작업이면
    <b>전부</b> 중단됩니다 — 프로세스가 하나라 서버별로 나눠 멈출 수 없습니다.
    이미 적용된 변경은 되돌아가지 않습니다.</p>`,
    { label: "취소 실행", danger: true },
    () => act(`/api/jobs/${id}/cancel`, "취소를 요청했습니다"));
}

async function retry(job, hosts) {
  if (!hosts.length) return;
  const payload = { kind: job.kind, hosts, env: job.env, ref: job.ref, ref_type: job.ref_type, clean_mode: job.clean_mode };
  if (job.kind === "clean") payload.confirm = hosts[0];
  modal(`<h2>${esc(hosts.length)}대를 다시 실행할까요?</h2>
    <p class="note">대상: <b class="mono">${esc(hosts.join(", "))}</b><br>
    Ansible 은 멱등해서 이미 끝난 부분은 건너뜁니다.</p>`,
    { label: "재시도" },
    async () => {
      try {
        const created = await api("/api/jobs", { method: "POST", body: payload });
        toast(`작업 #${created.id} 을(를) 만들었습니다`);
        go("job", created.id);
      } catch (e) { toast(e.message, "danger"); }
    });
}

/* ══ 서버 ══════════════════════════════════════════════ */

async function loadServers() {
  const data = await api("/api/servers");
  state.servers = data.servers;
  state.mtime = data.mtime_ns;
  return data;
}

async function showServers() {
  try { await loadServers(); }
  catch (e) {
    $("#srvRows").innerHTML = `<tr><td colspan="7"><div class="alert"><span class="alert__g">✕</span>
      <p class="note" style="color:var(--ink-2)">${esc(e.message)}</p></div></td></tr>`;
    return;
  }
  renderServers();
}

function renderServers() {
  const rows = state.servers.map((s) => {
    const key = s.key_installed_at
      ? `<span class="tag" style="color:var(--ok)">등록됨 ${esc(shortTime(s.key_installed_at))}</span>`
      : `<button class="btn btn--xs btn--primary" data-key="${esc(s.host)}">키 등록</button>`;
    return `<tr>
      <td><span class="mono" style="font-weight:600">${esc(s.host)}</span></td>
      <td class="mono dim">${esc(s.ansible_user)}@${esc(s.ansible_host)}</td>
      <td class="mono dim">${esc(s.site_name)}</td>
      <td><span class="tag" title="${esc(PROFILES[s.profile] || "")}">${esc(s.profile)}</span></td>
      <td>${key}</td>
      <td class="dim">${esc(s.memo || "–")}</td>
      <td style="text-align:right"><div class="row" style="justify-content:flex-end; flex-wrap:nowrap">
        <button class="btn btn--xs" data-edit="${esc(s.host)}">편집</button>
        <button class="btn btn--xs btn--ghost-danger" data-del="${esc(s.host)}">삭제</button></div></td></tr>`;
  }).join("");

  $("#srvRows").innerHTML = rows || `<tr><td colspan="7" class="dim" style="text-align:center; padding:24px">
    등록된 서버가 없습니다. 오른쪽 위 <b>＋ 서버 추가</b>로 시작하세요.</td></tr>`;

  $("#yamlPrev").innerHTML = state.servers.length ? yamlPreview() : `<span class="c"># 아직 서버가 없습니다</span>`;
  $$("#srvRows [data-edit]").forEach((b) => b.addEventListener("click", () => serverModal(b.dataset.edit)));
  $$("#srvRows [data-del]").forEach((b) => b.addEventListener("click", () => confirmDelete(b.dataset.del)));
  $$("#srvRows [data-key]").forEach((b) => b.addEventListener("click", () => sshKeyModal(b.dataset.key)));
}

function yamlPreview() {
  const body = state.servers.map((s) =>
    `    <span class="k">${esc(s.host)}</span>:
      <span class="k">ansible_host</span>: <span class="v">${esc(s.ansible_host)}</span>
      <span class="k">ansible_user</span>: <span class="v">${esc(s.ansible_user)}</span>
      <span class="k">site_name</span>: <span class="v">${esc(s.site_name)}</span>
      <span class="k">profile</span>: <span class="v">${esc(s.profile)}</span>`).join("\n");
  return `<span class="k">sites</span>:\n  <span class="k">hosts</span>:\n${body}`;
}

function serverModal(host) {
  const s = host ? state.servers.find((x) => x.host === host) : null;
  const opts = Object.keys(PROFILES).map((p) =>
    `<option value="${p}"${s && s.profile === p ? " selected" : ""}>${p}</option>`).join("");
  modal(`<h2>${s ? "서버 편집" : "서버 추가"}</h2>
    <div class="stack gap-12">
      <div class="field"><label for="m-host">이름 (inventory_hostname)</label>
        <input class="input mono" id="m-host" value="${esc(s ? s.host : "")}"${s ? " disabled" : ""} placeholder="yonseiwa"></div>
      <div class="two">
        <div class="field"><label for="m-ip">주소</label>
          <input class="input mono" id="m-ip" value="${esc(s ? s.ansible_host : "")}" placeholder="192.168.100.209"></div>
        <div class="field"><label for="m-user">계정</label>
          <input class="input mono" id="m-user" value="${esc(s ? s.ansible_user : "connecteve")}"></div>
      </div>
      <div class="two">
        <div class="field"><label for="m-site">site_name</label>
          <input class="input mono" id="m-site" value="${esc(s ? s.site_name : "")}"></div>
        <div class="field"><label for="m-prof">프로파일</label>
          <select class="input mono" id="m-prof">${opts}</select></div>
      </div>
      <div class="field"><label for="m-memo">메모 <span class="dim">— 병원명 등 (sites.yml 에는 안 들어감)</span></label>
        <input class="input" id="m-memo" value="${esc(s ? s.memo || "" : "")}"></div>
    </div>
    ${s ? "" : `<p class="note">추가한 뒤 <b>SSH 키 등록</b>을 해야 설치를 시작할 수 있습니다.</p>`}`,
    { label: s ? "저장" : "추가" },
    async () => {
      const payload = {
        host: s ? s.host : $("#m-host").value.trim(),
        ansible_host: $("#m-ip").value.trim(),
        ansible_user: $("#m-user").value.trim(),
        site_name: $("#m-site").value.trim(),
        profile: $("#m-prof").value,
        memo: $("#m-memo").value.trim(),
        mtime_ns: state.mtime,
      };
      try {
        if (s) await api(`/api/servers/${encodeURIComponent(s.host)}`, { method: "PUT", body: payload });
        else await api("/api/servers", { method: "POST", body: payload });
        toast(s ? "저장했습니다" : `${payload.host} 을(를) 추가했습니다`);
        await showServers();
      } catch (e) { toast(e.message, "danger"); throw e; }
    });
}

function confirmDelete(host) {
  modal(`<h2>${esc(host)} 을(를) 목록에서 지울까요?</h2>
    <p class="note">인벤토리에서만 제거합니다. <b>서버 자체는 그대로</b>이며 설치된 것도 지워지지 않습니다.
    서버 내용을 지우려면 먼저 <b>서버 초기화</b>를 실행하세요.</p>`,
    { label: "삭제", danger: true },
    async () => {
      try {
        await api(`/api/servers/${encodeURIComponent(host)}`, { method: "DELETE", body: { mtime_ns: state.mtime } });
        toast(`${host} 을(를) 삭제했습니다`);
        await showServers();
      } catch (e) { toast(e.message, "danger"); throw e; }
    });
}

function sshKeyModal(host) {
  const s = state.servers.find((x) => x.host === host);
  modal(`<h2>SSH 키 등록 — ${esc(host)}</h2>
    <p class="note">맥미니의 공개키를 <span class="mono">${esc(s.ansible_user)}@${esc(s.ansible_host)}</span> 의
    <span class="mono">~/.ssh/authorized_keys</span> 에 추가합니다. 이후 hubctl 이 비밀번호 없이 접속합니다.</p>
    <div class="field"><label for="m-pw">타겟 서버 비밀번호</label>
      <input class="input mono" id="m-pw" type="password" autocomplete="off"></div>
    <p class="note">비밀번호는 이 등록에만 쓰이고 저장되지 않습니다. 같은 키가 이미 있으면 줄이 늘지 않습니다.</p>`,
    { label: "등록" },
    async () => {
      const password = $("#m-pw").value;
      if (!password) { toast("비밀번호를 입력하세요", "warn"); throw new Error("empty"); }
      try {
        await api(`/api/servers/${encodeURIComponent(host)}/ssh-key`, { method: "POST", body: { password } });
        toast(`${host} 키 등록 완료`);
        await showServers();
      } catch (e) { toast(e.message, "danger"); throw e; }
    });
}

/* ══ 새 설치 ════════════════════════════════════════════ */

async function showNew() {
  try { await loadServers(); } catch (e) { toast(e.message, "danger"); }
  const list = $("#nSrvList");
  list.innerHTML = state.servers.length ? state.servers.map((s) => {
    const blocked = !s.key_installed_at;
    return `<label class="srv" title="${blocked ? "SSH 키 등록이 필요합니다" : ""}">
      <input type="checkbox" name="nsrv" value="${esc(s.host)}"${blocked ? " disabled" : ""}>
      <span><span class="mono" style="font-weight:600">${esc(s.host)}</span>
        <span class="dim" style="font-size:12.5px"> ${esc(s.memo || s.site_name)}</span><br>
        <span class="mono dim" style="font-size:12px">${esc(s.ansible_host)} · ${esc(s.profile)}</span></span>
      <span>${blocked ? `<span class="tag" style="color:var(--err)">키 미등록</span>` : `<span class="tag">준비됨</span>`}</span>
    </label>`;
  }).join("") : `<p class="note">등록된 서버가 없습니다. <b>서버</b> 화면에서 먼저 추가하세요.</p>`;

  $$('input[name="nsrv"]').forEach((c) => c.addEventListener("change", drawPlan));
  $("#nAll").onchange = (e) => {
    $$('input[name="nsrv"]:not(:disabled)').forEach((c) => { c.checked = e.target.checked; });
    drawPlan();
  };
  $("#f-env").onchange = drawPlan;
  $("#f-ref").oninput = drawPlan;
  $("#f-reftag").onchange = drawPlan;
  drawPlan();
}

const pickedHosts = () => $$('input[name="nsrv"]:checked').map((c) => c.value);

function drawPlan() {
  const hosts = pickedHosts();
  $("#nCount").textContent = `${hosts.length}대 선택`;
  $("#plan").innerHTML = PHASES.install.map(([, label], i) =>
    `<div class="plan__i"><b>${i + 1}</b><span>${esc(label)}</span></div>`).join("");

  const env = $("#f-env").value;
  const ref = $("#f-ref").value.trim();
  const tail = ref ? ` -- -e hub_deploy_ref=${ref}${$("#f-reftag").checked ? " -e hub_deploy_ref_type=tag" : ""}` : "";
  const limit = hosts.length ? hosts.join(",") : "<대상 없음>";
  $("#cmdPrev").innerHTML = `./bin/hubctl install -e <b>${esc(env)}</b> -l <b>${esc(limit)}</b>${esc(tail)}`;

  const warn = [];
  if (hosts.length > FORKS) warn.push(`선택한 ${hosts.length}대는 ansible 기본 forks(${FORKS})보다 많아 ${FORKS}대씩 나눠 진행됩니다.`);
  if (hosts.length > 1) warn.push("환경과 ref 는 실행당 하나라 선택한 전부에 공통 적용됩니다.");
  $("#newWarn").innerHTML = `<div class="alert alert--warn"><span class="alert__g" aria-hidden="true">!</span>
    <p class="note" style="color:var(--ink-2)">30분~1시간 걸립니다. 진행상황은 Slack 채널에도 게시됩니다.
    ${warn.map((w) => `<br>${esc(w)}`).join("")}</p></div>`;
  $("#newSubmit").disabled = hosts.length === 0;
}

$("#newForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const hosts = pickedHosts();
  if (!hosts.length) return;
  const env = $("#f-env").value;
  const ref = $("#f-ref").value.trim();
  modal(`<h2>${hosts.length}대에 설치를 시작할까요?</h2>
    <p class="note">대상: <b class="mono">${esc(hosts.join(", "))}</b><br>
    환경: <b class="mono">${esc(env)}</b>${ref ? ` · ref: <b class="mono">${esc(ref)}</b>` : ""}</p>`,
    { label: "설치 시작" },
    async () => {
      try {
        const created = await api("/api/jobs", {
          method: "POST",
          body: { kind: "install", hosts, env, ref: ref || null, ref_type: ref && $("#f-reftag").checked ? "tag" : null },
        });
        toast(`작업 #${created.id} 을(를) 시작했습니다`);
        go("job", created.id);
      } catch (e) { toast(e.message, "danger"); throw e; }
    });
});

/* ══ 서버 초기화 ════════════════════════════════════════ */

async function showClean() {
  try { await loadServers(); } catch (e) { toast(e.message, "danger"); }
  $("#cSrvList").innerHTML = state.servers.length ? state.servers.map((s) =>
    `<label class="srv srv--danger"><input type="radio" name="csrv" value="${esc(s.host)}">
      <span><span class="mono" style="font-weight:600">${esc(s.host)}</span>
        <span class="dim" style="font-size:12.5px"> ${esc(s.memo || s.site_name)}</span><br>
        <span class="mono dim" style="font-size:12px">${esc(s.ansible_host)}</span></span>
      <span></span></label>`).join("")
    : `<p class="note">등록된 서버가 없습니다.</p>`;

  $("#modeList").innerHTML = Object.entries(MODES).map(([key, m], i) =>
    `<label class="type type--danger"><input type="radio" name="cmode" value="${key}"${i === 0 ? " checked" : ""}>
      <span><span class="type__n">${esc(m.label)}</span><span class="type__d">${esc(m.d)}</span>
        <dl class="wipe"><dt>지움</dt><dd>${esc(m.wipes)}</dd><dt>남김</dt><dd>${esc(m.keeps)}</dd></dl></span></label>`).join("");

  $$('input[name="csrv"], input[name="cmode"]').forEach((r) => r.addEventListener("change", drawCleanPlan));
  drawCleanPlan();
}

function drawCleanPlan() {
  const host = $('input[name="csrv"]:checked')?.value;
  const mode = MODES[$('input[name="cmode"]:checked')?.value || "reset"];
  $("#cleanPlan").innerHTML = [
    ["대상 확인 (confirm 대조)", true],
    ["클러스터 · 앱 제거", true],
    ["/data 삭제", mode.keep === "false"],
    ["bootstrap 산출물 제거", mode.level === "uninstall"],
  ].map(([label, on], i) =>
    `<div class="plan__i${on ? "" : " plan__i--off"}"><b>${i + 1}</b><span>${esc(label)}</span></div>`).join("");

  $("#cleanCmd").innerHTML = `ansible-playbook clean.yml -i inventory/sites.yml -l <b>${esc(host || "<대상 없음>")}</b>` +
    ` -e confirm=<b>${esc(host || "…")}</b> -e level=<b>${esc(mode.level)}</b> -e keep_data=<b>${esc(mode.keep)}</b>`;
  $("#cleanSubmit").disabled = !host;
}

$("#cleanForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const host = $('input[name="csrv"]:checked')?.value;
  if (!host) return;
  const modeKey = $('input[name="cmode"]:checked').value;
  const mode = MODES[modeKey];
  modal(`<h2 style="color:var(--err)">되돌릴 수 없습니다</h2>
    <p class="note"><b class="mono">${esc(host)}</b> 에서 <b>${esc(mode.wipes)}</b> 을(를) 지웁니다.<br>
    남는 것: ${esc(mode.keeps)}</p>
    <div class="field"><label for="m-confirm">확인을 위해 서버 이름을 그대로 입력하세요</label>
      <input class="input mono" id="m-confirm" placeholder="${esc(host)}" autocomplete="off"></div>`,
    { label: "초기화 실행", danger: true },
    async () => {
      const typed = $("#m-confirm").value.trim();
      if (typed !== host) { toast("서버 이름이 일치하지 않습니다", "warn"); throw new Error("mismatch"); }
      try {
        const created = await api("/api/jobs", {
          method: "POST",
          body: { kind: "clean", hosts: [host], clean_mode: modeKey, confirm: typed },
        });
        toast(`초기화 작업 #${created.id} 을(를) 시작했습니다`);
        go("job", created.id);
      } catch (e) { toast(e.message, "danger"); throw e; }
    });
});

/* ══ 모달 ══════════════════════════════════════════════ */

function modal(html, { label = "확인", danger = false } = {}, onConfirm) {
  const scrim = document.createElement("div");
  scrim.className = "scrim";
  scrim.innerHTML = `<div class="modal" role="dialog" aria-modal="true">${html}
    <div class="row" style="justify-content:flex-end">
      <button class="btn" data-close>닫기</button>
      <button class="btn ${danger ? "btn--danger" : "btn--primary"}" data-ok>${esc(label)}</button>
    </div></div>`;
  $("#modalSlot").append(scrim);

  const close = () => { scrim.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  scrim.addEventListener("click", (e) => { if (e.target === scrim) close(); });
  $("[data-close]", scrim).addEventListener("click", close);

  const ok = $("[data-ok]", scrim);
  ok.addEventListener("click", async () => {
    ok.disabled = true;
    try { await onConfirm(); close(); }
    catch { ok.disabled = false; }
  });
  scrim.querySelector("input")?.focus();
}

/* ══ 시작 ══════════════════════════════════════════════ */

$("#nav").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-go]");
  if (btn) go(btn.dataset.go);
});
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-open-job]");
  if (btn) go("job", Number(btn.dataset.openJob));
  // 작업 화면은 통째로 다시 그려지므로 개별 바인딩 대신 위임으로 받는다.
  const jump = e.target.closest("[data-jump]");
  if (jump) jumpToLine(Number(jump.dataset.jump));
});
// 인자 없이 부르면 serverModal 이 "서버 추가" 모드로 뜬다. 이 버튼은
// console.html 에 정적으로 있어 다시 그려지지 않으므로 여기서 한 번만 묶는다
// (행마다 붙는 편집·삭제 버튼은 렌더 때마다 다시 묶어야 해서 renderServers 안에 있다).
$("#addSrv").addEventListener("click", () => serverModal());

$("#logout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch { /* 어차피 화면을 뜬다 */ }
  location.href = "/login";
});
window.addEventListener("hashchange", routeFromHash);

(async function start() {
  try {
    state.me = await api("/api/me");
  } catch { return; }
  state.csrf = state.me.csrf_token;
  $("#whoName").textContent = state.me.username;
  $("#whoAv").textContent = (state.me.username[0] || "·").toUpperCase();
  $("#whoSub").textContent = state.me.last_login_at ? `최근 ${shortTime(state.me.last_login_at)}` : "";
  try { await loadServers(); } catch { /* 화면에서 다시 알린다 */ }
  routeFromHash();
})();
