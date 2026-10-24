/* QDA tool frontend — thin client over /api/v1. No business logic lives here. */
"use strict";

// ---------------------------------------------------------------------------
// state, helpers
// ---------------------------------------------------------------------------
const S = {
  revision: null, settings: null, codes: null, codesFlat: [], codeById: {}, lastUsed: null, showRetired: false,
  view: null, params: {}, interview: null, passId: null, selectedSegment: null, pending: null, draftComment: null,
  polling: true, expanded: {}, retrievalSel: new Set(), analysisTab: "matrix",
};
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const el = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
const VALENCE_LABEL = {"-1": "−", "0": "0", "1": "+"};
// Agency value list; empty = unset. Mirrors app/services/core.py.
const AGENCIES = ["GOV-CENTRAL", "GOV-PROV", "REGULATOR", "GRID", "SOE", "MARKET", "ASSOC", "RESEARCH", "MULTI"];
const AGENCY_MEANING = {
  "GOV-CENTRAL": "central government / NDRC / NEA / BMWK / legislator",
  "GOV-PROV": "province, federal state, municipality",
  "REGULATOR": "BNetzA and similar",
  "GRID": "grid companies, TSO, DSO",
  "SOE": "state-owned enterprises other than grid",
  "MARKET": "private market actors incl. aggregators, banks, customers",
  "ASSOC": "associations",
  "RESEARCH": "academia",
  "MULTI": "explicitly several actor types jointly (co-steering)",
};
const AGENCY_TOOLTIP = "Agency: which actor type brought about the situation or could change it (Huang & Malkin 2026)";
const NQ_TITLE = "Quotation block: never quote verbatim";
const QB_LABEL = {full: "yes", partial: "partly", none: "no"};
// paragraph locator "R ¶12 · 00:22:09" / "I · 00:03:37"
function paraLabel(p) { return `<b>${p.speaker === "R" ? "R" : "I"}</b>${p.para_no ? ` ¶${p.para_no}` : ""}${p.timestamp ? ` <span class="ts">· ${esc(p.timestamp)}</span>` : ""}`; }
function locator(s) { return `${esc(s.interview_id)}, ¶${s.para_no}${s.timestamp ? ` <span class="muted small ts">· ${esc(s.timestamp)}</span>` : ""}`; }
function nqChip(s) { return s.quotation_block === "full" ? ` <span class="chip nq" title="${NQ_TITLE}">NQ</span>` : s.quotation_block === "partial" ? ` <span class="chip nq" title="${NQ_TITLE} (partly)">NQ (part)</span>` : ""; }

function toast(msg, kind = "") {
  const t = el(`<div class="toast ${kind}">${esc(msg)}</div>`);
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), kind === "error" ? 6000 : 3000);
}

async function api(method, path, body, opts = {}) {
  const init = {method, headers: {}};
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  let res;
  try { res = await fetch("/api/v1" + path, init); }
  catch (e) { $("#sync").className = "sync error"; toast("Server not reachable: " + e.message, "error"); throw e; }
  const rev = res.headers.get("X-Revision");
  if (rev) S.revision = parseInt(rev, 10);
  let data = null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("json")) data = await res.json(); else data = await res.text();
  if (!res.ok) {
    const msg = (data && data.detail) ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : res.statusText;
    if (!opts.silent) toast(msg, "error");
    const err = new Error(msg); err.status = res.status; err.data = data; throw err;
  }
  return data;
}

// code points <-> UTF-16
function cpToUtf16(text, cp) { let i = 0, n = 0; while (n < cp && i < text.length) { i += text.codePointAt(i) > 0xffff ? 2 : 1; n++; } return i; }
function utf16ToCp(text, idx) { let i = 0, n = 0; while (i < idx && i < text.length) { i += text.codePointAt(i) > 0xffff ? 2 : 1; n++; } return n; }
function cpSlice(cps, a, b) { return cps.slice(a, b).join(""); }

function hexToRgba(hex, a) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || "");
  if (!m) return `rgba(120,120,120,${a})`;
  return `rgba(${parseInt(m[1], 16)},${parseInt(m[2], 16)},${parseInt(m[3], 16)},${a})`;
}
function codeLabel(c) { return c ? c.name : "?"; }
function fmtDate(s) { return s ? s.replace("T", " ").slice(0, 16) : "—"; }

// ---------------------------------------------------------------------------
// modal / prompts
// ---------------------------------------------------------------------------
function modal({title, html = "", fields = [], okText = "OK", cancelText = "Cancel", danger = false, onOpen}) {
  return new Promise((resolve) => {
    const root = $("#modal-root");
    const f = fields.map((x) => {
      if (x.type === "select") return `<label class="field"><span>${esc(x.label)}</span><select name="${x.name}">${x.options.map((o) => `<option value="${esc(o.value)}" ${o.value == x.value ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select></label>`;
      if (x.type === "textarea") return `<label class="field"><span>${esc(x.label)}</span><textarea name="${x.name}">${esc(x.value || "")}</textarea></label>`;
      if (x.type === "checkbox") return `<label class="inline-label"><input type="checkbox" name="${x.name}" ${x.value ? "checked" : ""}> ${esc(x.label)}</label>`;
      if (x.type === "color") return `<label class="field"><span>${esc(x.label)}</span><input type="color" name="${x.name}" value="${esc(x.value || "#888888")}"></label>`;
      return `<label class="field"><span>${esc(x.label)}</span><input type="text" name="${x.name}" value="${esc(x.value || "")}" ${x.placeholder ? `placeholder="${esc(x.placeholder)}"` : ""}></label>`;
    }).join("");
    const box = el(`<div class="backdrop"><div class="modal"><h2>${esc(title)}</h2>${html}<form>${f}
      <div class="actions"><button type="button" class="btn cancel">${esc(cancelText)}</button><button type="submit" class="btn ${danger ? "danger" : "primary"}">${esc(okText)}</button></div></form></div></div>`);
    root.appendChild(box);
    const done = (v) => { box.remove(); resolve(v); };
    box.querySelector(".cancel").onclick = () => done(null);
    box.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); done(null); } });
    box.querySelector("form").onsubmit = (e) => {
      e.preventDefault();
      const out = {};
      fields.forEach((x) => { const i = box.querySelector(`[name="${x.name}"]`); out[x.name] = x.type === "checkbox" ? i.checked : i.value; });
      done(out);
    };
    if (onOpen) onOpen(box);
    const first = box.querySelector("input,textarea,select,button.primary,button.danger");
    if (first) first.focus();
  });
}
const confirmDialog = (title, html, okText = "Confirm", danger = true) => modal({title, html, okText, danger}).then((v) => v !== null);
async function askReason(title, extraFields = []) {
  const v = await modal({title, fields: [...extraFields, {name: "reason", label: "Reason (one line, written to the codebook changelog)", placeholder: "e.g. definition sharpened after pilot"}]});
  if (v && !v.reason.trim()) { toast("A reason is required.", "error"); return null; }
  return v;
}

// ---------------------------------------------------------------------------
// global data
// ---------------------------------------------------------------------------
async function loadCodes() {
  const d = await api("GET", "/codes?include_retired=true");
  S.codes = d.tree; S.codesFlat = d.flat; S.lastUsed = d.last_used_code_id;
  S.codeById = {}; d.flat.forEach((c) => (S.codeById[c.id] = c));
  S.shortcutMap = {};
  if (S.settings) Object.entries(S.settings.shortcuts).forEach(([k, key]) => { const c = d.flat.find((x) => x.code_key === key); if (c) S.shortcutMap[k] = c.id; });
}
async function loadSettings() {
  S.settings = await api("GET", "/settings");
  $("#pilot-badge").classList.toggle("hidden", S.settings.settings.pilot_phase !== "true");
}
async function refreshUndo() {
  const u = await api("GET", "/undo/state");
  $("#btn-undo").disabled = !u.can_undo; $("#btn-undo").title = u.undo_description ? "Undo: " + u.undo_description : "Nothing to undo";
  $("#btn-redo").disabled = !u.can_redo; $("#btn-redo").title = u.redo_description ? "Redo: " + u.redo_description : "Nothing to redo";
}
async function doUndo() { const r = await api("POST", "/undo"); if (r.ok) toast("Undone: " + r.undone); await afterWrite(); }
async function doRedo() { const r = await api("POST", "/redo"); if (r.ok) toast("Redone: " + r.redone); await afterWrite(); }
async function afterWrite() { await Promise.all([loadCodes(), refreshUndo()]); await render(true); }

// live refresh
async function poll() {
  if (!S.polling) return;
  try {
    const r = await fetch("/api/v1/revision");
    const d = await r.json();
    $("#sync").className = "sync";
    if (S.revision !== null && d.revision !== S.revision) {
      S.revision = d.revision;
      await Promise.all([loadCodes(), loadSettings(), refreshUndo()]);
      await render(true);
    } else S.revision = d.revision;
  } catch (e) { $("#sync").className = "sync error"; }
}
setInterval(poll, 2000);

// ---------------------------------------------------------------------------
// routing
// ---------------------------------------------------------------------------
function parseHash() {
  const h = location.hash.replace(/^#\/?/, "") || "dashboard";
  const [pathPart, query] = h.split("?");
  const parts = pathPart.split("/");
  const params = {};
  if (query) query.split("&").forEach((kv) => { const [k, v] = kv.split("="); params[decodeURIComponent(k)] = decodeURIComponent(v || ""); });
  return {view: parts[0], arg: parts.slice(1).join("/"), params};
}
window.addEventListener("hashchange", () => render(false));

async function render(keep = false) {
  const {view, arg, params} = parseHash();
  S.view = view; S.arg = arg; S.params = params;
  $$("#nav a[data-view]").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  const main = $("#main");
  const scroll = keep ? {main: main.scrollTop, tr: $(".transcript")?.parentElement?.scrollTop, ct: $(".codetree")?.scrollTop} : null;
  try {
    const fn = VIEWS[view] || VIEWS.dashboard;
    await fn(main, arg, params, keep);
  } catch (e) { console.error(e); main.innerHTML = `<div class="page"><div class="banner danger">${esc(e.message)}</div></div>`; }
  if (scroll) { main.scrollTop = scroll.main; const t = $(".transcript")?.parentElement; if (t && scroll.tr) t.scrollTop = scroll.tr; const c = $(".codetree"); if (c && scroll.ct) c.scrollTop = scroll.ct; }
}

const VIEWS = {};

// ---------------------------------------------------------------------------
// dashboard
// ---------------------------------------------------------------------------
VIEWS.dashboard = async (main) => {
  const d = await api("GET", "/dashboard");
  main.innerHTML = `<div class="page">
    <h1>Dashboard</h1>
    ${d.weekly_reminder ? `<div class="banner warn"><b>Weekly reminder:</b> export a full dump (Export → all types) and copy the <code>backups/</code> folder to an external drive. ${d.last_full_export ? "Last full export: " + esc(d.last_full_export) : "No full export yet."}</div>` : ""}
    ${d.backup.error ? `<div class="banner danger">${esc(d.backup.error)}</div>` : ""}
    <div class="grid3">
      <div class="panel"><div class="muted">Interviews</div><div class="stat">${d.progress.interviews}</div><div class="small">${d.progress.done} done · ${d.progress.in_progress} in progress</div></div>
      <div class="panel"><div class="muted">Coded segments (primary pass)</div><div class="stat">${d.progress.segments}</div><div class="small">${d.progress.respondent_turns} respondent turns in total</div></div>
      <div class="panel"><div class="muted">Codebook</div><div class="stat">${d.codebook.codes}</div><div class="small">${d.codebook.incomplete.length ? `<span class="warn">${d.codebook.incomplete.length} code(s) missing definition or anchor example</span>` : '<span class="ok">all codes complete</span>'}</div></div>
    </div>
    <div class="panel"><div class="row"><b>Backups</b><span class="muted">last: ${esc(d.backup.last_backup || "none")} · ${d.backup.count} files in <code>${esc(d.backup.folder)}</code>${d.backup.secondary ? " · secondary: <code>" + esc(d.backup.secondary) + "</code>" : ""}</span><span class="spacer"></span><button class="btn" id="bk">Backup now</button></div></div>
    <div class="panel"><h2 style="margin-top:0">Interviews</h2>
      <table class="tbl"><thead><tr><th>ID</th><th>Country</th><th>Role</th><th>Label</th><th>Pilot</th><th>Status</th><th class="num">Turns</th><th class="num">Segments</th><th>Last edited</th><th></th></tr></thead>
      <tbody>${d.interviews.map((i) => `<tr><td><a href="#/coding/${esc(i.id)}">${esc(i.id)}</a></td><td>${i.country}</td><td>${esc(i.role_category)}</td><td>${esc(i.role_label)}</td><td>${i.pilot ? "yes" : ""}</td><td>${i.status}</td><td class="num">${i.respondent_turns}</td><td class="num">${i.segment_count}</td><td>${esc(fmtDate(i.last_edited))}</td><td><a href="#/coding/${esc(i.id)}">open</a></td></tr>`).join("") || `<tr><td colspan="10" class="muted">No interviews yet — <a href="#/import">import a transcript</a>.</td></tr>`}</tbody></table></div>
    ${d.codebook.incomplete.length ? `<div class="panel"><h2 style="margin-top:0">Codebook completeness</h2><ul>${d.codebook.incomplete.map((c) => `<li><a href="#/codebook?code=${c.id}">${esc(c.name)}</a> <span class="muted">missing ${c.missing.join(", ")}</span></li>`).join("")}</ul></div>` : ""}
  </div>`;
  $("#bk", main).onclick = async () => { const r = await api("POST", "/backup"); toast("Backup written: " + r.filename, "ok"); render(true); };
};

// ---------------------------------------------------------------------------
// import
// ---------------------------------------------------------------------------
VIEWS.import = async (main) => {
  main.innerHTML = `<div class="page"><h1>Import transcript</h1>
    <div class="panel">
      <p>Only <b>.md</b> files with YAML frontmatter, UTF-8 (BOM/CRLF fine), paragraphs separated by blank lines and starting with <code>[hh:mm:ss] I:</code> / <code>[hh:mm:ss] R:</code>. The timestamp is kept per paragraph. Tags: <code>[CONFIDENTIAL]</code>, <code>[EN-ORIGINAL]</code>, <code>[NO-QUOTE]</code>; all other square brackets are plain text. See the README for the exact format.</p>
      <div class="dropzone" id="dz">Drop a .md file here or <label style="color:var(--accent);cursor:pointer">choose a file<input type="file" id="file" accept=".md" class="hidden"></label></div>
      <div class="row" style="margin-top:.6rem">
        <label class="inline-label"><input type="radio" name="mode" value="new" checked> New interview (duplicates refused)</label>
        <label class="inline-label"><input type="radio" name="mode" value="reimport"> Re-import keeping existing codings</label>
        <label class="inline-label"><input type="checkbox" id="anon"> Apply anonymization list on import</label>
      </div>
    </div>
    <div id="preview"></div></div>`;
  const dz = $("#dz", main), fi = $("#file", main);
  let file = null;
  const preview = async () => {
    if (!file) return;
    const fd = new FormData(); fd.append("file", file); fd.append("dry_run", "true");
    fd.append("mode", $('input[name=mode]:checked', main).value); fd.append("anonymize", $("#anon", main).checked);
    let rep;
    try { rep = await api("POST", "/interviews/import", fd, {silent: true}); }
    catch (e) { $("#preview", main).innerHTML = `<div class="banner danger"><b>Import rejected:</b> ${esc(e.message)}</div>`; return; }
    const m = rep.meta;
    $("#preview", main).innerHTML = `<div class="panel"><h2 style="margin-top:0">Preview: ${esc(rep.id)} <span class="muted small">(${rep.mode === "reimport" ? "re-import" : "new"})</span></h2>
      ${rep.warnings.map((w) => `<div class="banner warn">${esc(w)}</div>`).join("")}
      <table class="tbl" style="max-width:700px"><tr><th>Country</th><td>${m.country}</td><th>Role category</th><td>${esc(m.role_category)}</td></tr>
      <tr><th>Role label</th><td colspan="3">${esc(m.role_label)}</td></tr>
      <tr><th>Date</th><td>${m.date}</td><th>Duration</th><td>${m.duration_min ?? "—"} min</td></tr>
      <tr><th>Language</th><td>${m.language_original || "—"}</td><th>Mode</th><td>${m.interview_mode || "—"}</td></tr>
      <tr><th>Pilot</th><td>${m.pilot ? "yes" : "no"}</td><th>Notes</th><td>${esc(m.notes || "")}</td></tr>
      <tr><th>Paragraphs</th><td>${rep.paragraphs} (${rep.respondent_turns} respondent turns)</td><th>Confidential ranges</th><td>${rep.confidential_ranges}</td></tr>
      <tr><th>Paragraphs with timestamp</th><td>${rep.timestamps ?? rep.stripped_timestamps}${rep.paragraphs && (rep.timestamps ?? rep.stripped_timestamps) < rep.paragraphs ? ` <span class="warn">(${rep.paragraphs - (rep.timestamps ?? rep.stripped_timestamps)} without)</span>` : ""}</td><th>EN-original / quotation-blocked ranges</th><td>${rep.en_original_ranges} / ${rep.no_quote_ranges ?? 0}</td></tr>
      <tr><th>Anonymization replacements</th><td colspan="3">${rep.anonymization_replacements}${rep.mapped_terms_remaining ? ` <span class="warn">(${rep.mapped_terms_remaining} mapped term(s) remain)</span>` : ""}</td></tr>
      ${rep.mode === "reimport" ? `<tr><th>Existing codings</th><td>${rep.existing_segments}</td><th>Re-attachable</th><td>${rep.reattached} ${rep.reattach_failed.length ? `<span class="danger">(${rep.reattach_failed.length} would be lost)</span>` : ""}</td></tr>` : ""}
      </table>
      ${rep.reattach_failed.length ? `<h3 class="danger">Codings that cannot be re-attached (would be dropped)</h3><ul>${rep.reattach_failed.map((f) => `<li>¶${f.para_no} [${f.codes.join(", ")}]: ${esc(f.text)}</li>`).join("")}</ul>` : ""}
      <div class="row" style="margin-top:.6rem"><button class="btn primary" id="commit">Import ${esc(rep.id)}</button><span class="muted small">A backup is created after every import.</span></div></div>`;
    $("#commit", main).onclick = async () => {
      if (rep.mode === "reimport" && !(await confirmDialog("Re-import " + rep.id + "?", `<p>The stored text of <b>${esc(rep.id)}</b> will be replaced. ${rep.reattached} coding(s) will be re-attached${rep.reattach_failed.length ? `, <b class="danger">${rep.reattach_failed.length} will be dropped</b>` : ""}. The undo history is cleared. A backup is created first.</p>`, "Re-import"))) return;
      const fd2 = new FormData(); fd2.append("file", file); fd2.append("mode", rep.mode === "reimport" ? "reimport" : "new"); fd2.append("anonymize", $("#anon", main).checked);
      const r = await api("POST", "/interviews/import", fd2);
      toast(`Imported ${r.id} (${r.paragraphs} paragraphs)`, "ok");
      location.hash = "#/coding/" + r.id;
    };
  };
  fi.onchange = () => { file = fi.files[0]; preview(); };
  dz.ondragover = (e) => { e.preventDefault(); dz.classList.add("over"); };
  dz.ondragleave = () => dz.classList.remove("over");
  dz.ondrop = (e) => { e.preventDefault(); dz.classList.remove("over"); file = e.dataTransfer.files[0]; preview(); };
  $$('input[name=mode], #anon', main).forEach((i) => (i.onchange = preview));
};

// ---------------------------------------------------------------------------
// coding workspace
// ---------------------------------------------------------------------------
VIEWS.coding = async (main, arg, params, keep) => {
  if (!arg) {
    const ivs = await api("GET", "/interviews");
    main.innerHTML = `<div class="page"><h1>Coding</h1><div class="panel">${ivs.length ? "<p>Choose an interview:</p><ul>" + ivs.map((i) => `<li><a href="#/coding/${esc(i.id)}">${esc(i.id)}</a> — ${esc(i.role_label)} <span class="muted">(${i.status}, ${i.segment_count} segments)</span></li>`).join("") + "</ul>" : '<p>No interviews yet. <a href="#/import">Import a transcript</a> first.</p>'}</div></div>`;
    return;
  }
  const passId = params.pass ? parseInt(params.pass, 10) : null;
  const data = await api("GET", `/interviews/${encodeURIComponent(arg)}${passId ? "?pass_id=" + passId : ""}`);
  S.interview = data; S.passId = data.pass_id;
  if (!keep) { S.selectedSegment = null; S.pending = null; S.draftComment = null; }
  if (S.selectedSegment && !data.paragraphs.some((p) => p.segments.some((s) => s.id === S.selectedSegment))) S.selectedSegment = null;
  main.innerHTML = `<div class="coding"><div class="tcol"><div class="iv-header"></div><div class="transcript"></div></div><div class="codetree"></div><div class="segdetail"></div></div>`;
  renderHeader(); renderTranscript(); renderCodeTree(); renderSegDetail();
  if (params.p && !keep) { const p = $(`.para[data-pid="${params.p}"]`); if (p) { p.scrollIntoView({block: "center"}); p.style.background = "#fef9c3"; setTimeout(() => (p.style.background = ""), 2500); } }
  if (params.seg && !keep) { selectSegment(parseInt(params.seg, 10), true); }
};

function currentPass() { return S.interview.passes.find((p) => p.id === S.passId); }
function isRecode() { const p = currentPass(); return p && p.name !== "primary"; }

function renderHeader() {
  const iv = S.interview.interview, h = $(".iv-header");
  const coded = new Set(); S.interview.paragraphs.forEach((p) => { if (p.segments.length) coded.add(p.id); });
  const rturns = S.interview.paragraphs.filter((p) => p.speaker === "R").length;
  const pass = currentPass();
  h.innerHTML = `<span class="title">${esc(iv.id)}</span><span class="muted">${esc(iv.role_label)} · ${iv.country} · ${esc(iv.role_category)}${iv.pilot ? " · pilot" : ""}</span>
    <span class="muted">¶ coded ${coded.size}/${rturns} · ${S.interview.segment_count} segments</span>
    ${isRecode() ? `<span class="badge pilot" style="background:#7c3aed;color:#fff;padding:.1rem .4rem;border-radius:3px">RE-CODING PASS ${esc(pass.name)}${pass.locked ? " (locked)" : ""}</span>` : ""}
    <span class="spacer"></span>
    ${S.interview.passes.length > 1 ? `<select id="passsel" title="Coding pass">${S.interview.passes.map((p) => `<option value="${p.id}" ${p.id === S.passId ? "selected" : ""}>${esc(p.name)}${p.locked ? " 🔒" : ""}</option>`).join("")}</select>` : ""}
    <select id="status" title="Interview status"><option value="not-started" ${iv.status === "not-started" ? "selected" : ""}>not started</option><option value="in-progress" ${iv.status === "in-progress" ? "selected" : ""}>in progress</option><option value="done" ${iv.status === "done" ? "selected" : ""}>done</option></select>
    ${iv.status !== "done" ? '<button class="btn small" id="markdone">Mark done</button>' : ""}
    <button class="btn small" id="ivdetails" title="Edit role label, actor group and notes">Details</button>
    <button class="btn small" id="ivmemo" title="Interview memo">Memo</button>
    <button class="btn small" id="help" title="Coding rules and shortcuts">?</button>`;
  $("#status", h).onchange = async (e) => { await api("PATCH", `/interviews/${encodeURIComponent(iv.id)}`, {status: e.target.value}); await afterWrite(); };
  const md = $("#markdone", h); if (md) md.onclick = async () => { await api("PATCH", `/interviews/${encodeURIComponent(iv.id)}`, {status: "done"}); toast("Marked as done", "ok"); await afterWrite(); };
  const ps = $("#passsel", h); if (ps) ps.onchange = (e) => { location.hash = `#/coding/${iv.id}?pass=${e.target.value}`; };
  $("#ivmemo", h).onclick = () => memoDialog("interview", iv.id, "Interview memo " + iv.id);
  $("#ivdetails", h).onclick = () => interviewDetailsDialog(iv);
  $("#help", h).onclick = showHelp;
}

function showHelp() {
  const sc = S.settings.shortcuts;
  modal({title: "Coding rules & shortcuts", cancelText: "Close", okText: "OK", html: `<div class="help">
    <h3>Coding guide</h3>
    <p>Your study's coding guide and the definitions, coding rules and anchor examples in the <b>Codebook</b> view are binding for system boundary, code delimitations, valence, agency, segmentation, quote candidates and comment style. In case of doubt they prevail over this help text; only respondent turns are codable and coding follows content, not the interviewer's question.</p>
    <h3>Rules the tool enforces</h3><ul>
    <li><b>Valence per code:</b> every code on a segment carries −1 / 0 / +1; BAR is always −1, INT has no valence of its own.</li>
    <li><b>Agency per code</b> (Huang &amp; Malkin 2026): which actor type brought about the situation or could change it; empty = no attribution in the text. Values: ${AGENCIES.map((a) => `<code>${a}</code> = ${esc(AGENCY_MEANING[a])}`).join("; ")}. Co-steering by several actor types = <code>MULTI</code> (list the types in the comment).</li>
    <li><b>Quotation block:</b> text with a red dotted underline and an <b>NQ</b> marker comes from a <code>[NO-QUOTE]…[/NO-QUOTE]</code> passage. It is coded like everything else, but such segments can never be quote candidates (the checkbox is disabled) and are excluded from the quote bank; exports carry "Quotation block: yes / partly / no".</li>
    <li><b>Confidential text</b> (<code>[CONFIDENTIAL]…[/CONFIDENTIAL]</code> or marked in the tool) cannot be coded and never appears in search, exports or matrices.</li>
    <li>Segments may carry several codes (typically ≤ 3); overlapping and nested segments are allowed. Removing the last code deletes the segment.</li>
    <li>Codebook changes (rename, definition, rule, anchor, re-parent) need a one-line reason and are written to the changelog; codes are retired, never deleted.</li></ul>
    <h3>Transcript tags</h3><ul>
    <li><code>[CONFIDENTIAL]…[/CONFIDENTIAL]</code> — excluded from coding and every output.</li>
    <li><code>[EN-ORIGINAL]…[/EN-ORIGINAL]</code> — spoken in English in the original interview (blue dotted underline, <b>EN</b> marker); segments report "Original English: yes / partly / no".</li>
    <li><code>[NO-QUOTE]…[/NO-QUOTE]</code> — quotation block (red dotted underline, <b>NQ</b> marker): codable, never quotable.</li></ul>
    <p class="small muted">Every paragraph starts with <code>[hh:mm:ss] R:</code> or <code>[hh:mm:ss] I:</code>; the timestamp is kept and shown next to ¶n (locator <i>ID, ¶n · hh:mm:ss</i>). All other square brackets (placeholders such as <code>[the TSO]</code>, editorial notes) are ordinary, codable text.</p>
    <h3>Shortcuts</h3><p>Select text, then press a key (or click a code):</p>
    <p>${Object.entries(sc).map(([k, key]) => { const c = S.codesFlat.find((x) => x.code_key === key); return `<span class="kbd">${k}</span> ${esc(c ? c.name : key)}`; }).join(" &nbsp;· ")}</p>
    <p><span class="kbd">L</span> assign last-used code · <span class="kbd">/</span> code picker · <span class="kbd">n</span>/<span class="kbd">p</span> next/previous coding · <span class="kbd">Ctrl+Z</span>/<span class="kbd">Ctrl+Y</span> undo/redo · <span class="kbd">Esc</span> clear selection</p>
    <p>With a segment open and no text selected, a shortcut/click adds the code to that segment.</p>
    <p class="muted small">References: Bergek et al. (2008); Bergek et al. (2015); Hekkert et al. (2007); Hekkert &amp; Negro (2009); Suurs &amp; Hekkert (2009); Huang &amp; Malkin (2026); Mayring (2014); Eisenhardt (1989).</p></div>`});
}

function renderTranscript() {
  const t = $(".transcript");
  const scroller = t.parentElement, keepTop = scroller ? scroller.scrollTop : 0;  // emptying the list resets the scroll position; restored below
  t.innerHTML = "";
  for (const p of S.interview.paragraphs) {
    const div = el(`<div class="para ${p.speaker}" data-pid="${p.id}"><div class="label">${paraLabel(p)}</div><div class="gutter"></div><div class="text"></div></div>`);
    const text = $(".text", div);
    if (p.speaker === "I") { text.textContent = p.text; t.appendChild(div); continue; }
    const cps = Array.from(p.text);
    const bounds = new Set([0, cps.length]);
    p.segments.forEach((s) => { bounds.add(s.start_char); bounds.add(s.end_char); });
    p.confidential.forEach((c) => { bounds.add(c.start_char); bounds.add(c.end_char); });
    const ens = p.en_original || [];
    ens.forEach((e) => { bounds.add(e.start_char); bounds.add(e.end_char); });
    const nqs = p.no_quote || [];
    nqs.forEach((e) => { bounds.add(e.start_char); bounds.add(e.end_char); });
    const bs = Array.from(bounds).sort((a, b) => a - b);
    for (let i = 0; i < bs.length - 1; i++) {
      const a = bs[i], b = bs[i + 1];
      const covering = p.segments.filter((s) => s.start_char <= a && s.end_char >= b);
      const conf = p.confidential.some((c) => c.start_char <= a && c.end_char >= b);
      const span = document.createElement("span");
      span.textContent = cpSlice(cps, a, b);
      const enRange = ens.find((e) => e.start_char <= a && e.end_char >= b);
      if (enRange) { span.classList.add("en"); if (enRange.start_char === a) span.classList.add("en-start"); span.title = "Spoken in English in the original interview"; }
      const nqRange = nqs.find((e) => e.start_char <= a && e.end_char >= b);
      if (nqRange) { span.classList.add("nq"); if (nqRange.start_char === a) span.classList.add("nq-start"); span.title = (span.title ? span.title + " · " : "") + NQ_TITLE; }
      if (conf) span.classList.add("conf");
      else if (covering.length) {
        span.classList.add("seg"); if (covering.some((s) => s.id === S.selectedSegment)) span.classList.add("sel");
        span.dataset.segs = covering.map((s) => s.id).join(" ");
        const first = covering[0].codes[0];
        span.style.background = hexToRgba(first ? first.color : "#888", Math.min(0.18 + 0.12 * covering.length, 0.55));
        if (covering.length > 1) span.style.textDecoration = "underline";
        span.title = covering.map((s) => s.codes.map(codeLabelWithValence).join(", ")).join(" | ") + (nqRange ? " · " + NQ_TITLE : "");
        span.onclick = (e) => { if (window.getSelection().toString()) return; e.stopPropagation(); cycleSelect(covering.map((s) => s.id)); };
      }
      text.appendChild(span);
    }
    t.appendChild(div);
    // margin stripes
    const gutter = $(".gutter", div);
    const sorted = [...p.segments].sort((x, y) => x.start_char - y.start_char || y.end_char - x.end_char);
    const lanes = [];
    for (const s of sorted) {
      let lane = lanes.findIndex((end) => end <= s.start_char);
      if (lane === -1) { lane = lanes.length; lanes.push(0); }
      lanes[lane] = s.end_char;
      const spans = $$(`span[data-segs~="${s.id}"]`, text);
      if (!spans.length) continue;
      const top = spans[0].offsetTop, bottom = spans[spans.length - 1].offsetTop + spans[spans.length - 1].offsetHeight;
      const st = el(`<div class="stripe ${s.id === S.selectedSegment ? "sel" : ""}" style="left:${lane * 6}px;top:${top}px;height:${Math.max(6, bottom - top)}px;background:${s.codes[0] ? s.codes[0].color : "#888"}" title="${esc(s.codes.map(codeLabelWithValence).join(", "))}"></div>`);
      st.onclick = (e) => { e.stopPropagation(); selectSegment(s.id); };
      gutter.appendChild(st);
    }
  }
  if (scroller) scroller.scrollTop = keepTop;
  t.onmouseup = onTranscriptMouseUp;
  t.parentElement.onclick = (e) => { if (e.target.closest(".sel-toolbar")) return; if (!window.getSelection().toString()) clearPending(); };
}
function codeLabelWithValence(c) { return `${c.name} (${VALENCE_LABEL[String(c.valence)]}${c.agency ? ", " + c.agency : ""})`; }

function cycleSelect(ids) {
  const i = ids.indexOf(S.selectedSegment);
  selectSegment(ids[(i + 1) % ids.length]);
}
function selectSegment(id, scroll = false) {
  S.selectedSegment = id; S.draftComment = null; S.pending = null;
  renderTranscript(); renderSegDetail();
  if (scroll) { const sp = $(`span[data-segs~="${id}"]`); if (sp) sp.scrollIntoView({block: "center"}); }
}
function clearPending() { if (S.pending) { S.pending = null; $$(".text.pending").forEach((x) => x.classList.remove("pending")); $$(".sel-toolbar").forEach((x) => x.remove()); } }

function onTranscriptMouseUp(e) {
  setTimeout(() => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.toString().trim()) return;
    const range = sel.getRangeAt(0);
    const textEl = range.commonAncestorContainer.nodeType === 1 ? range.commonAncestorContainer.closest(".para.R .text") : range.commonAncestorContainer.parentElement.closest(".para.R .text");
    if (!textEl) { if (e && e.target.closest(".para.I")) toast("Interviewer turns are not codable."); return; }
    const para = textEl.closest(".para"), pid = parseInt(para.dataset.pid, 10);
    const p = S.interview.paragraphs.find((x) => x.id === pid);
    const r1 = document.createRange(); r1.setStart(textEl, 0); r1.setEnd(range.startContainer, range.startOffset);
    const r2 = document.createRange(); r2.setStart(textEl, 0); r2.setEnd(range.endContainer, range.endOffset);
    let a = utf16ToCp(p.text, r1.toString().length), b = utf16ToCp(p.text, r2.toString().length);
    if (b > Array.from(p.text).length) b = Array.from(p.text).length;
    // trim whitespace at both ends
    const cps = Array.from(p.text);
    while (a < b && /\s/.test(cps[a])) a++;
    while (b > a && /\s/.test(cps[b - 1])) b--;
    if (a >= b) return;
    if (p.confidential.some((c) => a < c.end_char && b > c.start_char)) { toast("Selection overlaps confidential text and cannot be coded.", "error"); return; }
    $$(".text.pending").forEach((x) => x.classList.remove("pending"));
    $$(".sel-toolbar").forEach((x) => x.remove());
    S.pending = {paragraph_id: pid, start_char: a, end_char: b, text: cpSlice(cps, a, b)};
    S.selectedSegment = null; renderSegDetail();
    textEl.classList.add("pending");
    const rect = range.getBoundingClientRect(), host = textEl.closest(".tcol"), hr = host.getBoundingClientRect();
    const tb = el(`<div class="sel-toolbar"><span>${a}–${b}</span><button class="btn tiny" data-act="picker">Code… <span class="kbd">/</span></button>${S.lastUsed && S.codeById[S.lastUsed] ? `<button class="btn tiny" data-act="last">${esc(S.codeById[S.lastUsed].name)} <span class="kbd">L</span></button>` : ""}<button class="btn tiny" data-act="conf">Mark confidential</button></div>`);
    tb.style.left = Math.max(4, rect.left - hr.left + host.scrollLeft) + "px";
    tb.style.top = (rect.bottom - hr.top + host.scrollTop + 4) + "px";
    tb.onclick = (ev) => { const act = ev.target.closest("button")?.dataset.act; if (act === "picker") openPicker(); else if (act === "last") assignCode(S.lastUsed); else if (act === "conf") markConfidential(pid, a, b); };
    host.appendChild(tb);
  }, 0);
}

async function assignCode(codeId, valence = 0) {
  const c = S.codeById[codeId];
  if (!c || !c.active) return;
  if (S.pending) {
    const pend = S.pending;
    const seg = await api("POST", "/segments", {paragraph_id: pend.paragraph_id, start_char: pend.start_char, end_char: pend.end_char, codes: [{code_id: codeId, valence}], coding_pass_id: S.passId});
    window.getSelection().removeAllRanges(); clearPending();
    S.selectedSegment = seg.id; await afterWrite();
    toast(`Coded as ${c.name}`, "ok");
  } else if (S.selectedSegment) {
    await api("POST", `/segments/${S.selectedSegment}/codes`, {code_id: codeId, valence});
    await afterWrite();
  } else toast("Select respondent text first (or open a segment to add a code).");
}

async function markConfidential(pid, a, b, segmentId = null) {
  const overl = S.interview.paragraphs.find((p) => p.id === pid).segments.filter((s) => s.start_char < b && s.end_char > a);
  const ok = await confirmDialog("Mark text as confidential?", `<p>The selected text will be excluded from coding, all exports and all matrices.${overl.length ? ` <b class="danger">${overl.length} coded segment(s) overlapping this range will be deleted</b> (undoable).` : ""}</p>`, "Mark confidential");
  if (!ok) return;
  await api("POST", "/confidential", {paragraph_id: pid, start_char: a, end_char: b, delete_overlapping: true});
  window.getSelection().removeAllRanges(); clearPending(); S.selectedSegment = null;
  await afterWrite();
}

function openPicker() {
  const items = S.codesFlat.filter((c) => c.active);
  let filtered = items, idx = 0;
  const html = `<input type="search" id="pk" placeholder="Type to filter codes…" autocomplete="off"><div class="picker-list" id="pl"></div>`;
  modal({title: S.pending ? "Code selection" : S.selectedSegment ? "Add code to segment" : "Pick a code", html, okText: "Assign", onOpen: (box) => {
    const inp = $("#pk", box), list = $("#pl", box);
    const draw = () => { list.innerHTML = filtered.map((c, i) => `<div class="${i === idx ? "sel" : ""}" data-id="${c.id}"><span class="sw" style="background:${c.color};width:10px;height:10px;border-radius:2px;display:inline-block"></span>${"&nbsp;&nbsp;".repeat(c.depth)}${esc(c.name)} <span class="muted small">${esc(c.code_key)}</span></div>`).join("") || '<div class="muted">no match</div>'; };
    inp.oninput = () => { const q = inp.value.toLowerCase(); filtered = items.filter((c) => c.name.toLowerCase().includes(q) || c.code_key.toLowerCase().includes(q) || c.path.toLowerCase().includes(q)); idx = 0; draw(); };
    inp.onkeydown = (e) => { if (e.key === "ArrowDown") { idx = Math.min(idx + 1, filtered.length - 1); draw(); e.preventDefault(); } if (e.key === "ArrowUp") { idx = Math.max(idx - 1, 0); draw(); e.preventDefault(); } };
    list.onclick = (e) => { const d = e.target.closest("[data-id]"); if (d) { box.querySelector(".cancel").click(); assignCode(parseInt(d.dataset.id, 10)); } };
    box.querySelector("form").onsubmit = (e) => { e.preventDefault(); const c = filtered[idx]; box.querySelector(".cancel").click(); if (c) assignCode(c.id); };
    draw(); inp.focus();
  }});
}

// --- code tree -------------------------------------------------------------
function renderCodeTree() {
  const ct = $(".codetree"); if (!ct) return;
  const q = (ct.dataset.q || "").toLowerCase();
  ct.innerHTML = `<div class="search"><input type="search" placeholder="Filter codes… (type-ahead)" value="${esc(ct.dataset.q || "")}"></div><div class="tree"></div>
    <div class="hint">${S.pending ? "<b>Text selected</b> — press a shortcut or click a code." : S.selectedSegment ? "<b>Segment open</b> — click a code to add it." : "Select respondent text, then press a key or click a code."}<br>Frequencies: total (DE/CN). <label class="inline-label"><input type="checkbox" id="showret" ${S.showRetired ? "checked" : ""}> show retired</label></div>`;
  const tree = $(".tree", ct);
  const shortcutFor = {}; Object.entries(S.settings.shortcuts).forEach(([k, key]) => (shortcutFor[key] = k));
  const matches = (n) => !q || n.name.toLowerCase().includes(q) || n.code_key.toLowerCase().includes(q);
  const anyDesc = (n) => n.children.some((k) => matches(k) || anyDesc(k));
  const walk = (nodes, depth) => {
    for (const c of nodes) {
      if (!c.active && !S.showRetired) continue;
      if (!matches(c) && !anyDesc(c)) continue;
      const hasKids = c.children.some((k) => k.active || S.showRetired);
      const expanded = S.expanded[c.id] !== false && (S.expanded[c.id] || depth === 0 || !!q);
      const f = c.frequency, fw = c.frequency_with_children;
      const row = el(`<div class="cnode ${c.active ? "" : "retired"} ${c.id === S.lastUsed ? "last-used" : ""}" data-id="${c.id}" draggable="${c.active}" style="padding-left:${depth * 14 + 4}px" title="${esc(c.definition)}">
        <span class="tog">${hasKids ? (expanded ? "▾" : "▸") : ""}</span><span class="sw" style="background:${c.color}"></span><span class="nm">${esc(c.name)}</span>
        ${shortcutFor[c.code_key] ? `<span class="key">${shortcutFor[c.code_key]}</span>` : ""}<span class="freq" title="segments: total (DE/CN); with sub-codes: ${fw.total}">${f.total}<span class="muted"> (${f.DE}/${f.CN})</span></span></div>`);
      row.onclick = (e) => { if (e.target.classList.contains("tog") && hasKids) { S.expanded[c.id] = !expanded; renderCodeTree(); return; } assignCode(c.id); };
      row.ondblclick = () => { location.hash = `#/codebook?code=${c.id}`; };
      row.oncontextmenu = (e) => { e.preventDefault(); location.hash = `#/codebook?code=${c.id}`; };
      // drag & drop
      row.ondragstart = (e) => { e.dataTransfer.setData("text/plain", String(c.id)); e.dataTransfer.effectAllowed = "move"; };
      row.ondragover = (e) => { e.preventDefault(); const r = row.getBoundingClientRect(), y = e.clientY - r.top; row.classList.remove("drop-into", "drop-before", "drop-after"); row.classList.add(y < r.height / 4 ? "drop-before" : y > r.height * 3 / 4 ? "drop-after" : "drop-into"); };
      row.ondragleave = () => row.classList.remove("drop-into", "drop-before", "drop-after");
      row.ondrop = async (e) => {
        e.preventDefault();
        const mode = row.classList.contains("drop-before") ? "before" : row.classList.contains("drop-after") ? "after" : "into";
        row.classList.remove("drop-into", "drop-before", "drop-after");
        const srcId = parseInt(e.dataTransfer.getData("text/plain"), 10);
        if (!srcId || srcId === c.id) return;
        const src = S.codeById[srcId];
        if (mode === "into") {
          const v = await askReason(`Move "${src.name}" under "${c.name}"?`); if (!v) return;
          await api("PATCH", `/codes/${srcId}`, {parent_id: c.id, reparent: true, reason: v.reason});
        } else {
          const siblings = S.codesFlat.filter((x) => x.parent_id === c.parent_id && x.id !== srcId).sort((a, b) => a.sort_order - b.sort_order || a.id - b.id);
          let pos = siblings.findIndex((x) => x.id === c.id); if (mode === "after") pos++;
          const body = {position: pos};
          if (src.parent_id !== c.parent_id) { const v = await askReason(`Move "${src.name}" next to "${c.name}"?`); if (!v) return; body.parent_id = c.parent_id; body.reparent = true; body.reason = v.reason; }
          await api("PATCH", `/codes/${srcId}`, body);
        }
        await afterWrite();
      };
      tree.appendChild(row);
      if (hasKids && expanded) walkInto(c.children, depth + 1);
    }
  };
  const walkInto = (nodes, depth) => walk(nodes, depth);
  walk(S.codes, 0);
  $("input", ct).oninput = (e) => { ct.dataset.q = e.target.value; renderCodeTree(); $("input", ct).focus(); const i = $("input", ct); i.setSelectionRange(i.value.length, i.value.length); };
  $("#showret", ct).onchange = (e) => { S.showRetired = e.target.checked; renderCodeTree(); };
}

// --- segment detail -----------------------------------------------------------
function findSegment(id) { for (const p of S.interview.paragraphs) for (const s of p.segments) if (s.id === id) return {s, p}; return null; }

function renderSegDetail() {
  const d = $(".segdetail"); if (!d) return;
  if (S.pending) { d.innerHTML = `<h3>Selection</h3><blockquote>${esc(S.pending.text)}</blockquote><p class="muted small">Offsets ${S.pending.start_char}–${S.pending.end_char} (code points). Press a shortcut, click a code, or <span class="kbd">/</span> to pick.</p>`; return; }
  const found = S.selectedSegment ? findSegment(S.selectedSegment) : null;
  if (!found) { d.innerHTML = `<h3 class="muted">No segment selected</h3><p class="muted small">Click a coloured margin stripe or highlighted text to open a segment. Use <span class="kbd">n</span>/<span class="kbd">p</span> to step through codings.</p>${isRecode() ? '<p class="small">Re-coding pass: the primary codings are hidden; code the interview afresh, then finish the pass in the Reliability view.</p>' : ""}`; return; }
  const {s, p} = found;
  const locked = currentPass().locked;
  const funcs = S.codesFlat.filter((c) => c.type === "function" && c.active);
  const blocked = s.quotation_block && s.quotation_block !== "none";
  d.innerHTML = `<div class="row"><h3 style="margin:0">${esc(p.interview_id)}, ¶${p.para_no}${p.timestamp ? ` <span class="muted ts">· ${esc(p.timestamp)}</span>` : ""}</h3><span class="muted small" title="segment_ref">${esc(s.segment_ref || `${p.interview_id}-S${s.id}`)}</span><span class="spacer"></span><button class="btn tiny" id="segmemo">Memo</button></div>
    <blockquote>${esc(s.text_snapshot)}</blockquote>
    ${s.en_original && s.en_original !== "none" ? `<p class="small"><span class="chip" style="background:#0e7490">EN</span> ${s.en_original === "full" ? "Spoken in English in the original interview (not translated)." : "Partly spoken in English in the original interview."}</p>` : ""}
    ${blocked ? `<p class="small"><span class="chip nq">NQ</span> ${s.quotation_block === "full" ? "Quotation block: this passage must never be quoted verbatim." : "Partly quotation-blocked: the segment overlaps a [NO-QUOTE] passage and must not be quoted verbatim."} Coding is allowed; the quote-candidate flag is not.</p>` : ""}
    <div class="codes">${s.codes.map((c) => `<div class="codechip" data-code="${c.code_id}"><span class="sw" style="background:${c.color}"></span><span class="nm">${esc(c.name)}</span>
      <span class="valence" title="Valence: + supports · 0 neutral · − weakens/blocks">${[1, 0, -1].map((v) => `<button data-v="${v}" class="${c.valence === v ? "on " + (v === 1 ? "plus" : v === -1 ? "minus" : "") : ""}" ${locked ? "disabled" : ""}>${VALENCE_LABEL[String(v)]}</button>`).join("")}</span>
      <select class="agency" title="${esc(AGENCY_TOOLTIP)}${c.agency ? " — " + esc(c.agency) + ": " + esc(AGENCY_MEANING[c.agency] || "") : ""}" ${locked ? "disabled" : ""}><option value="">agency: unset</option>${AGENCIES.map((a) => `<option value="${a}" title="${esc(AGENCY_MEANING[a])}" ${c.agency === a ? "selected" : ""}>${a}</option>`).join("")}</select>
      <button class="btn tiny danger rm" title="Remove this code" ${locked ? "disabled" : ""}>×</button></div>`).join("")}</div>
    <div class="row" style="margin:.4rem 0"><button class="btn small" id="addcode" ${locked ? "disabled" : ""}>+ Add code <span class="kbd">/</span></button><label class="inline-label" title="${blocked ? esc(NQ_TITLE) + ": segments overlapping a [NO-QUOTE] passage cannot be quote candidates." : "Mark as a quote candidate for the quote bank"}"><input type="checkbox" id="quote" ${s.quote_candidate ? "checked" : ""} ${locked || blocked ? "disabled" : ""}> quote candidate${blocked ? ' <span class="muted small">(blocked: NQ passage)</span>' : ""}</label></div>
    <label class="field"><span>Comment (autosaved)</span><textarea id="comment" ${locked ? "disabled" : ""}>${esc(S.draftComment ?? s.comment)}</textarea></label>
    <details><summary>Edit boundaries</summary><div class="row" style="margin:.3rem 0"><input type="number" id="bstart" value="${s.start_char}" style="width:80px"> – <input type="number" id="bend" value="${s.end_char}" style="width:80px"><button class="btn small" id="bapply" ${locked ? "disabled" : ""}>Apply</button></div><p class="muted small">Code-point offsets within ¶${p.para_no} (0–${Array.from(p.text).length}). Or select text and click "Use selection".</p><button class="btn small" id="buse" ${locked ? "disabled" : ""}>Use current text selection</button></details>
    <h3>Function interactions</h3>
    ${s.interactions.map((it) => `<div class="interaction"><b>${esc(codeLabel(S.codeById[it.from_code_id]))}</b> → <b>${esc(codeLabel(S.codeById[it.to_code_id]))}</b> <span class="chip" style="background:${it.mechanism === "blocking" ? "#b91c1c" : "#15803d"}">${it.mechanism}</span>${it.description ? "<br>" + esc(it.description) : ""}${it.condition ? `<br><i>${esc(it.condition)}</i>` : ""}<br><button class="btn tiny" data-edit="${it.id}">edit</button> <button class="btn tiny danger" data-del="${it.id}">delete</button></div>`).join("") || '<p class="muted small">none</p>'}
    <button class="btn small" id="addint">+ Add interaction</button>
    <hr style="border:none;border-top:1px solid var(--border);margin:.8rem 0">
    <div class="row"><button class="btn small danger" id="delseg" ${locked ? "disabled" : ""}>Delete segment</button><button class="btn small" id="confseg" ${locked ? "disabled" : ""}>Mark confidential</button></div>
    <p class="muted small">created ${esc(fmtDate(s.created_at))}</p>`;
  $$(".codechip", d).forEach((chip) => {
    const codeId = parseInt(chip.dataset.code, 10);
    $$("button[data-v]", chip).forEach((b) => (b.onclick = async () => { await api("PATCH", `/segments/${s.id}/codes/${codeId}`, {valence: parseInt(b.dataset.v, 10)}); await afterWrite(); }));
    $(".agency", chip).onchange = async (e) => { await api("PATCH", `/segments/${s.id}/codes/${codeId}`, {agency: e.target.value}); await afterWrite(); };
    $(".rm", chip).onclick = async () => {
      if (s.codes.length === 1) { if (await confirmDialog("Delete segment?", "<p>This is the only code of the segment; removing it deletes the segment (undoable).</p>", "Delete")) { await api("DELETE", `/segments/${s.id}`); S.selectedSegment = null; await afterWrite(); } return; }
      await api("DELETE", `/segments/${s.id}/codes/${codeId}`); await afterWrite();
    };
  });
  $("#addcode", d).onclick = openPicker;
  $("#segmemo", d).onclick = () => memoDialog("segment", String(s.id), `Memo for segment ${s.id} (${p.interview_id}, ¶${p.para_no}${p.timestamp ? " · " + p.timestamp : ""})`);
  $("#quote", d).onchange = async (e) => { try { await api("PATCH", `/segments/${s.id}`, {quote_candidate: e.target.checked}); } catch (err) { e.target.checked = !e.target.checked; return; } await afterWrite(); };
  const ta = $("#comment", d); let timer = null;
  // autosave without redrawing the panel, so the comment box keeps focus and nothing typed during the save is lost
  const save = async () => {
    const v = ta.value;
    if (v === s.comment) { if (S.draftComment === v) S.draftComment = null; return; }
    await api("PATCH", `/segments/${s.id}`, {comment: v});
    s.comment = v;
    S.draftComment = ta.isConnected && ta.value !== v ? ta.value : null;
    await refreshUndo();
  };
  ta.oninput = () => { S.draftComment = ta.value; clearTimeout(timer); timer = setTimeout(save, 1200); };
  ta.onblur = () => { clearTimeout(timer); save(); };
  ta.onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); clearTimeout(timer); save(); } e.stopPropagation(); };
  $("#bapply", d).onclick = async () => { await api("PATCH", `/segments/${s.id}`, {start_char: parseInt($("#bstart", d).value, 10), end_char: parseInt($("#bend", d).value, 10)}); await afterWrite(); };
  $("#buse", d).onclick = async () => { if (!S.pending) { toast("Select text in the same paragraph first."); return; } if (S.pending.paragraph_id !== p.id) { toast("Selection must be in the same paragraph.", "error"); return; } const pend = S.pending; clearPending(); await api("PATCH", `/segments/${s.id}`, {start_char: pend.start_char, end_char: pend.end_char}); await afterWrite(); };
  $("#delseg", d).onclick = async () => { if (await confirmDialog("Delete segment?", `<p>Delete this segment with ${s.codes.length} code(s)? This can be undone.</p>`, "Delete")) { await api("DELETE", `/segments/${s.id}`); S.selectedSegment = null; await afterWrite(); } };
  $("#confseg", d).onclick = () => markConfidential(p.id, s.start_char, s.end_char);
  $("#addint", d).onclick = () => interactionDialog({segment_id: s.id, interview_id: p.interview_id, country: p.country}, funcs);
  $$("[data-edit]", d).forEach((b) => (b.onclick = () => interactionDialog(s.interactions.find((x) => x.id === parseInt(b.dataset.edit, 10)), funcs)));
  $$("[data-del]", d).forEach((b) => (b.onclick = async () => { if (await confirmDialog("Delete interaction?", "", "Delete")) { await api("DELETE", `/interactions/${b.dataset.del}`); await afterWrite(); } }));
}

// interview details: role label, actor group, notes (PATCH /interviews/{id}; one undo entry, only changed fields)
async function interviewDetailsDialog(iv) {
  const allowed = S.settings.role_categories || [];
  const groups = allowed.includes(iv.role_category) ? allowed : [iv.role_category, ...allowed];
  const v = await modal({title: "Interview details " + iv.id, okText: "Save", fields: [
    {name: "role_label", label: "Role label", value: iv.role_label},
    {name: "role_category", label: "Actor group", type: "select", value: iv.role_category,
     options: groups.map((g) => ({value: g, label: allowed.includes(g) ? g : g + " (old value, not in the current list)"}))},
    {name: "notes", label: "Notes", type: "textarea", value: iv.notes || ""}]});
  if (!v) return;
  const body = {};
  if (v.role_label.trim() !== iv.role_label) body.role_label = v.role_label;
  if (v.role_category !== iv.role_category) body.role_category = v.role_category;
  if (v.notes !== (iv.notes || "")) body.notes = v.notes;
  if (!Object.keys(body).length) { toast("Nothing changed."); return; }
  await api("PATCH", `/interviews/${encodeURIComponent(iv.id)}`, body);
  toast("Interview details saved", "ok");
  await afterWrite();
}

async function interactionDialog(it, funcs) {
  const opts = funcs.map((c) => ({value: c.id, label: c.name}));
  const v = await modal({title: it.id ? "Edit interaction" : "New function interaction", okText: "Save", fields: [
    {name: "from_code_id", label: "From function", type: "select", options: opts, value: it.from_code_id || opts[0].value},
    {name: "to_code_id", label: "To function", type: "select", options: opts, value: it.to_code_id || opts[1].value},
    {name: "mechanism", label: "Mechanism", type: "select", options: [{value: "inducing", label: "inducing (virtuous)"}, {value: "blocking", label: "blocking (vicious)"}], value: it.mechanism || "blocking"},
    {name: "country", label: "Country", type: "select", options: [{value: "DE", label: "DE"}, {value: "CN", label: "CN"}], value: it.country || "DE"},
    {name: "description", label: "Description", type: "textarea", value: it.description || ""},
    {name: "condition", label: "Condition (optional, \"if X then Y\")", value: it.condition || ""}]});
  if (!v) return;
  const body = {from_code_id: parseInt(v.from_code_id, 10), to_code_id: parseInt(v.to_code_id, 10), mechanism: v.mechanism, country: v.country, description: v.description, condition: v.condition};
  if (it.id) await api("PATCH", `/interactions/${it.id}`, body);
  else await api("POST", "/interactions", {...body, segment_id: it.segment_id || null, interview_id: it.interview_id || null});
  await afterWrite();
}

async function memoDialog(scope, refId, title) {
  const memos = await api("GET", `/memos?scope=${scope}&ref_id=${encodeURIComponent(refId)}`);
  const m = memos[0];
  let versions = m ? await api("GET", `/memos/${m.id}/versions`) : [];
  const v = await modal({title, okText: "Save", fields: [{name: "text", label: "Memo text (every save keeps a version)", type: "textarea", value: m ? m.text : ""}],
    html: versions.length > 1 ? `<details><summary>${versions.length} versions</summary>${versions.slice(1).map((x) => `<p class="small"><b>${esc(x.saved_at)}</b><br>${esc(x.text)}</p>`).join("")}</details>` : ""});
  if (!v) return;
  if (m) await api("PUT", `/memos/${m.id}`, {text: v.text}); else await api("POST", "/memos", {scope, ref_id: refId, text: v.text});
  toast("Memo saved", "ok"); await refreshUndo();
}

// --- keyboard ------------------------------------------------------------------
document.addEventListener("keydown", async (e) => {
  const tag = (e.target.tagName || "").toLowerCase();
  const inInput = tag === "input" || tag === "textarea" || tag === "select" || e.target.isContentEditable;
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === "z" && !inInput) { e.preventDefault(); doUndo(); return; }
  if ((e.ctrlKey || e.metaKey) && (e.key.toLowerCase() === "y" || (e.shiftKey && e.key.toLowerCase() === "z")) && !inInput) { e.preventDefault(); doRedo(); return; }
  if (S.view !== "coding" || !S.interview || inInput || e.ctrlKey || e.metaKey || e.altKey) return;
  if ($("#modal-root").children.length) return;
  const k = e.key;
  if (k === "Escape") { window.getSelection().removeAllRanges(); clearPending(); S.selectedSegment = null; renderTranscript(); renderSegDetail(); renderCodeTree(); return; }
  if (k === "/") { e.preventDefault(); openPicker(); return; }
  if (k === "l" || k === "L") { if (S.lastUsed) assignCode(S.lastUsed); else toast("No code used yet."); return; }
  if (k === "n" || k === "p") { stepSegment(k === "n" ? 1 : -1); return; }
  if (S.shortcutMap[k] !== undefined) { e.preventDefault(); assignCode(S.shortcutMap[k]); }
});
function stepSegment(dir) {
  const all = []; S.interview.paragraphs.forEach((p) => p.segments.forEach((s) => all.push(s.id)));
  if (!all.length) return;
  const i = all.indexOf(S.selectedSegment);
  const next = i === -1 ? (dir > 0 ? all[0] : all[all.length - 1]) : all[(i + dir + all.length) % all.length];
  selectSegment(next, true);
}
$("#btn-undo").onclick = doUndo; $("#btn-redo").onclick = doRedo;

// ---------------------------------------------------------------------------
// codebook management
// ---------------------------------------------------------------------------
VIEWS.codebook = async (main, arg, params) => {
  const pilot = S.settings.settings.pilot_phase === "true";
  const sel = params.code ? S.codeById[parseInt(params.code, 10)] : null;
  const flat = S.codesFlat.filter((c) => c.active || S.showRetired);
  main.innerHTML = `<div class="page"><div class="row"><h1 style="margin:0">Codebook</h1><span class="spacer"></span>
      <label class="inline-label" title="While on, every codebook change is logged with phase = pilot"><input type="checkbox" id="pilot" ${pilot ? "checked" : ""}> pilot phase</label>
      <label class="inline-label"><input type="checkbox" id="showret" ${S.showRetired ? "checked" : ""}> show retired</label>
      <button class="btn" id="newcode">+ New code</button><button class="btn" id="help">Coding rules</button></div>
    <div class="grid2" style="grid-template-columns: 1fr 1.2fr; margin-top:.8rem">
      <div class="panel"><table class="tbl"><thead><tr><th>Code</th><th>Type</th><th class="num">Total</th><th class="num">DE</th><th class="num">CN</th><th>Complete</th></tr></thead><tbody>
        ${flat.map((c) => `<tr class="${sel && sel.id === c.id ? "sel" : ""}" style="${sel && sel.id === c.id ? "background:var(--accent-bg)" : ""}"><td style="padding-left:${c.depth * 14 + 6}px"><a href="#/codebook?code=${c.id}"><span class="chip" style="background:${c.color}">&nbsp;</span>${esc(c.name)}</a> <span class="muted small">${esc(c.code_key)}</span>${c.active ? "" : ' <span class="muted small">(retired)</span>'}</td><td class="small">${c.type}</td><td class="num">${c.frequency.total}</td><td class="num">${c.frequency.DE}</td><td class="num">${c.frequency.CN}</td><td>${c.definition && c.anchor_example ? '<span class="ok">✓</span>' : `<span class="warn" title="missing ${!c.definition ? "definition" : ""} ${!c.anchor_example ? "anchor example" : ""}">!</span>`}</td></tr>`).join("")}
      </tbody></table></div>
      <div class="panel" id="detail">${sel ? "" : '<p class="muted">Select a code to edit it. Every rename, definition change or move asks for a one-line reason and is written to the changelog.</p>'}</div>
    </div>
    <div class="panel"><h2 style="margin-top:0">Changelog</h2><div id="changelog"></div></div></div>`;
  $("#pilot", main).onchange = async (e) => { await api("PUT", "/settings/pilot_phase", {value: e.target.checked}); await loadSettings(); toast(e.target.checked ? "Pilot phase ON — changes are logged as pilot" : "Pilot phase OFF"); };
  $("#showret", main).onchange = (e) => { S.showRetired = e.target.checked; render(true); };
  $("#help", main).onclick = showHelp;
  $("#newcode", main).onclick = () => newCodeDialog(sel && sel.active ? sel.id : null);
  if (sel) renderCodeDetail($("#detail", main), sel);
  const log = await api("GET", "/changelog" + (sel ? "?code_id=" + sel.id : ""));
  $("#changelog", main).innerHTML = log.length ? `<div class="scroll-x" style="max-height:320px;overflow:auto"><table class="tbl"><thead><tr><th>Date</th><th>Phase</th><th>Code</th><th>Action</th><th>Field</th><th>Old</th><th>New</th><th>Reason</th></tr></thead><tbody>${log.map((r) => `<tr><td class="nowrap">${esc(fmtDate(r.timestamp))}</td><td>${r.phase}</td><td>${esc(r.code_name || "")}</td><td>${r.action}</td><td>${esc(r.field || "")}</td><td class="small">${esc((r.old_value || "").slice(0, 80))}</td><td class="small">${esc((r.new_value || "").slice(0, 80))}</td><td>${esc(r.reason)}</td></tr>`).join("")}</tbody></table></div>` : '<p class="muted">No changes logged yet.</p>';
};

async function newCodeDialog(parentId) {
  const parents = [{value: "", label: "(top level)"}].concat(S.codesFlat.filter((c) => c.active && c.depth < 2).map((c) => ({value: c.id, label: "  ".repeat(c.depth) + c.name})));
  const v = await modal({title: "New (inductive) code", okText: "Create", fields: [
    {name: "name", label: "Name"}, {name: "parent_id", label: "Parent", type: "select", options: parents, value: parentId || ""},
    {name: "definition", label: "Definition (required)", type: "textarea"}, {name: "coding_rule", label: "Coding rule (required)", type: "textarea"},
    {name: "anchor_example", label: "Anchor example (can be filled later; flagged in codebook export if missing)", type: "textarea"},
    {name: "color", label: "Colour", type: "color", value: "#ffbb78"}, {name: "reason", label: "Reason / origin (changelog)", placeholder: "e.g. emerged in DE-00"}]});
  if (!v) return;
  const c = await api("POST", "/codes", {name: v.name, parent_id: v.parent_id ? parseInt(v.parent_id, 10) : null, definition: v.definition, coding_rule: v.coding_rule, anchor_example: v.anchor_example, color: v.color, reason: v.reason});
  toast("Code created: " + c.name, "ok"); await loadCodes(); await refreshUndo();
  if (S.view === "codebook") location.hash = "#/codebook?code=" + c.id; else render(true);
}

function renderCodeDetail(box, c) {
  const parents = [{value: "", label: "(top level)"}].concat(S.codesFlat.filter((x) => x.active && x.id !== c.id && x.depth < 2).map((x) => ({value: x.id, label: "  ".repeat(x.depth) + x.name})));
  box.innerHTML = `<div class="row"><h2 style="margin:0"><span class="chip" style="background:${c.color}">&nbsp;</span>${esc(c.name)}</h2><span class="muted">${esc(c.code_key)} · ${c.type}${c.active ? "" : " · retired " + esc(fmtDate(c.retired_at))}</span></div>
    <p class="muted small">${esc(c.path)} · ${c.frequency.total} segments (DE ${c.frequency.DE} / CN ${c.frequency.CN})</p>
    <form id="cf"><label class="field"><span>Name</span><input type="text" name="name" value="${esc(c.name)}"></label>
      <label class="field"><span>Parent</span><select name="parent_id">${parents.map((o) => `<option value="${o.value}" ${String(o.value) === String(c.parent_id || "") ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select></label>
      <label class="field"><span>Definition</span><textarea name="definition">${esc(c.definition)}</textarea></label>
      <label class="field"><span>Coding rule</span><textarea name="coding_rule">${esc(c.coding_rule)}</textarea></label>
      <label class="field"><span>Anchor example</span><textarea name="anchor_example">${esc(c.anchor_example)}</textarea></label>
      <div class="row"><label class="inline-label">Colour <input type="color" name="color" value="${esc(c.color)}"></label><span class="spacer"></span>${c.active ? '<button type="submit" class="btn primary">Save changes…</button>' : ""}</div></form>
    <div class="row" style="margin-top:.8rem">
      <button class="btn" id="memo">Memo</button><button class="btn" id="segs">Segments / split support</button>
      ${c.active ? `<button class="btn" id="merge">Merge into…</button><button class="btn danger" id="retire">Retire…</button>` : ""}</div>
    <div id="seglist" style="margin-top:.8rem"></div>`;
  $("#cf", box).onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target); const body = {};
    for (const k of ["name", "definition", "coding_rule", "anchor_example", "color"]) if (f.get(k) !== c[k]) body[k] = f.get(k);
    const np = f.get("parent_id") ? parseInt(f.get("parent_id"), 10) : null;
    if (np !== (c.parent_id || null)) { body.parent_id = np; body.reparent = true; }
    const logged = ["name", "definition", "coding_rule", "anchor_example", "parent_id"].some((k) => k in body);
    if (!Object.keys(body).length) { toast("Nothing changed."); return; }
    if (logged) { const v = await askReason("Save codebook change"); if (!v) return; body.reason = v.reason; }
    await api("PATCH", `/codes/${c.id}`, body); toast("Saved", "ok"); await afterWrite();
  };
  $("#memo", box).onclick = () => memoDialog("code", String(c.id), "Memo: " + c.name);
  $("#segs", box).onclick = async () => {
    const segs = await api("GET", `/codes/${c.id}/segments`);
    const subs = S.codesFlat.filter((x) => x.active && x.parent_id === c.id);
    $("#seglist", box).innerHTML = `<h3>${segs.length} segment(s) coded with ${esc(c.name)}</h3>${subs.length ? `<p class="small muted">Split support: choose a sub-code per segment to move the coding from <b>${esc(c.name)}</b> to the sub-code (valence/agency kept).</p>` : `<p class="small muted">Create sub-codes first to use split support.</p>`}
      <div class="seglist">${segs.map((s) => { const sc = s.codes.find((x) => x.code_id === c.id); return `<div class="seg-row"><div><a href="#/coding/${esc(s.interview_id)}?seg=${s.id}">${esc(s.interview_id)}, ¶${s.para_no}</a> <span class="muted small">${esc(s.role_label)} · ${VALENCE_LABEL[String(sc.valence)]}${sc.agency ? " · " + sc.agency : ""}</span>${subs.length ? ` <select data-seg="${s.id}"><option value="">move to sub-code…</option>${subs.map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("")}</select>` : ""}</div><div class="quote">${esc(s.text_snapshot)}</div></div>`; }).join("") || '<p class="muted">none</p>'}</div>`;
    $$("select[data-seg]", box).forEach((sl) => (sl.onchange = async () => {
      if (!sl.value) return;
      const s = segs.find((x) => x.id === parseInt(sl.dataset.seg, 10)); const sc = s.codes.find((x) => x.code_id === c.id);
      await api("POST", "/segments/batch", {operations: [{op: "add_code", segment_id: s.id, code_id: parseInt(sl.value, 10), valence: sc.valence, agency: sc.agency || ""}, ...(s.codes.length > 1 ? [{op: "remove_code", segment_id: s.id, code_id: c.id}] : [])]});
      if (s.codes.length === 1) toast("Sub-code added; the parent code stays because it was the only code (remove it in the coding view if needed).");
      await afterWrite(); $("#segs", box).click();
    }));
  };
  const mg = $("#merge", box); if (mg) mg.onclick = async () => {
    const targets = S.codesFlat.filter((x) => x.active && x.id !== c.id).map((x) => ({value: x.id, label: x.path}));
    const v = await modal({title: `Merge "${c.name}" into…`, okText: "Merge", danger: true, html: `<p>All ${c.frequency.total} coding(s) of <b>${esc(c.name)}</b> move to the target, memos are concatenated, the source is retired. Undoable.</p>`, fields: [{name: "target", label: "Target code", type: "select", options: targets}, {name: "reason", label: "Reason (changelog)"}]});
    if (!v) return; if (!v.reason.trim()) { toast("A reason is required.", "error"); return; }
    const r = await api("POST", "/codes/merge", {source_id: c.id, target_id: parseInt(v.target, 10), reason: v.reason});
    toast(`Merged: ${r.moved} coding(s) moved`, "ok"); location.hash = "#/codebook?code=" + v.target; await loadCodes(); await refreshUndo();
  };
  const rt = $("#retire", box); if (rt) rt.onclick = async () => {
    const segs = await api("GET", `/codes/${c.id}/segments`);
    const v = await modal({title: `Retire "${c.name}"?`, okText: "Retire", danger: true, html: `<p>Codes are never deleted. <b>${segs.length} coding(s)</b> are affected. Undoable.</p>`, fields: [
      {name: "reassign", label: "Affected codings", type: "select", options: [{value: "keep", label: "keep on the retired code"}, ...(c.parent_id ? [{value: "parent", label: "reassign to parent: " + codeLabel(S.codeById[c.parent_id])}] : [])]},
      {name: "reason", label: "Reason (changelog)"}]});
    if (!v) return; if (!v.reason.trim()) { toast("A reason is required.", "error"); return; }
    await api("POST", `/codes/${c.id}/retire`, {reason: v.reason, reassign: v.reassign}); toast("Code retired", "ok"); location.hash = "#/codebook"; await loadCodes(); await refreshUndo();
  };
}

// ---------------------------------------------------------------------------
// retrieval & search
// ---------------------------------------------------------------------------
VIEWS.retrieval = async (main, arg, params) => {
  const roles = S.settings.role_categories;
  const codeOpts = S.codesFlat.filter((c) => c.active).map((c) => `<option value="${c.id}" ${params.code == c.id ? "selected" : ""}>${"— ".repeat(c.depth)}${esc(c.name)}</option>`).join("");
  main.innerHTML = `<div class="page"><h1>Retrieval & search</h1>
    <div class="panel"><form id="rf" class="row">
      <select name="code_id"><option value="">any code</option>${codeOpts}</select>
      <label class="inline-label"><input type="checkbox" name="include_sub" checked> incl. sub-codes</label>
      <select name="country"><option value="">DE + CN</option><option>DE</option><option>CN</option></select>
      <select name="role_category"><option value="">any role</option>${roles.map((r) => `<option>${esc(r)}</option>`).join("")}</select>
      <select name="pilot"><option value="">pilot + main</option><option value="true">pilot only</option><option value="false">main only</option></select>
      <select name="valence"><option value="">any valence</option><option value="1">+1</option><option value="0">0</option><option value="-1">−1</option></select>
      <select name="agency" title="${esc(AGENCY_TOOLTIP)}"><option value="">any agency</option>${AGENCIES.map((a) => `<option value="${a}" title="${esc(AGENCY_MEANING[a])}">${a}</option>`).join("")}<option value="unset">unset</option></select>
      <label class="inline-label"><input type="checkbox" name="quote_candidate"> quote candidates only</label>
      <input type="text" name="q" placeholder="text in segment/comment" style="width:180px">
      <button class="btn primary">Retrieve</button></form>
      <div id="results"></div></div>
    <div class="panel"><h2 style="margin-top:0">Full-text search across transcripts</h2><form id="sf" class="row"><input type="search" name="q" placeholder="search text (min. 2 characters)" style="width:300px"><select name="speaker"><option value="">I + R</option><option value="R">respondent only</option><option value="I">interviewer only</option></select><select name="country"><option value="">DE + CN</option><option>DE</option><option>CN</option></select><button class="btn">Search</button></form><div id="hits"></div></div></div>`;
  const run = async () => {
    const f = new FormData($("#rf", main)); const qs = [];
    for (const [k, v] of f.entries()) { if (v === "" || v === null) continue; qs.push(`${k}=${encodeURIComponent(v === "on" ? "true" : v)}`); }
    const segs = await api("GET", "/retrieve?" + qs.join("&"));
    S.retrievalSel = new Set();
    const box = $("#results", main);
    box.innerHTML = `<div class="row" style="margin:.6rem 0"><b>${segs.length} segment(s)</b><span class="spacer"></span><label class="inline-label"><input type="checkbox" id="selall"> select all</label><button class="btn small" id="exq">Export selected as quote bank</button><button class="btn small" id="exall">Export all as quote bank</button></div>
      <div class="scroll-x"><table class="tbl"><thead><tr><th></th><th>Locator</th><th>Timestamp</th><th>Role label</th><th>Codes (valence, agency)</th><th>Quote</th><th>Comment</th><th title="★ quote candidate · NQ quotation block">★ / NQ</th></tr></thead><tbody>
      ${segs.map((s) => `<tr><td><input type="checkbox" data-id="${s.id}"></td><td class="nowrap"><a href="#/coding/${esc(s.interview_id)}?seg=${s.id}" title="${esc(s.segment_ref || "")}">${esc(s.interview_id)}, ¶${s.para_no}</a><br><span class="muted small">${s.country}${s.pilot ? " · pilot" : ""}</span>${s.en_original === "full" ? ' <span class="chip" style="background:#0e7490" title="Spoken in English in the original interview">EN</span>' : s.en_original === "partial" ? ' <span class="chip" style="background:#0e7490" title="Partly spoken in English in the original interview">EN (part)</span>' : ""}</td><td class="nowrap ts">${esc(s.timestamp || "")}</td><td class="small">${esc(s.role_label)}</td><td>${s.codes.map((c) => `<span class="chip" style="background:${c.color}" title="${c.agency ? esc(c.agency) + ": " + esc(AGENCY_MEANING[c.agency] || "") : ""}">${esc(c.name)} ${VALENCE_LABEL[String(c.valence)]}${c.agency ? " · " + c.agency : ""}</span>`).join(" ")}</td><td class="quote">${esc(s.text_snapshot)}</td><td class="small">${esc(s.comment)}</td><td class="nowrap">${s.quote_candidate ? "★" : ""}${nqChip(s)}</td></tr>`).join("")}</tbody></table></div>`;
    $$("input[data-id]", box).forEach((cb) => (cb.onchange = () => { cb.checked ? S.retrievalSel.add(parseInt(cb.dataset.id, 10)) : S.retrievalSel.delete(parseInt(cb.dataset.id, 10)); }));
    $("#selall", box).onchange = (e) => $$("input[data-id]", box).forEach((cb) => { cb.checked = e.target.checked; cb.onchange(); });
    $("#exq", box).onclick = async () => { if (!S.retrievalSel.size) { toast("Select segments first."); return; } const r = await api("POST", "/export", {types: ["quote_bank"], quote_segment_ids: Array.from(S.retrievalSel)}); toast("Quote bank written to " + r.path, "ok"); if (r.warnings.length) r.warnings.forEach((w) => toast(w, "error")); };
    $("#exall", box).onclick = async () => { const r = await api("POST", "/export", {types: ["quote_bank"], quote_segment_ids: segs.map((s) => s.id)}); toast("Quote bank written to " + r.path, "ok"); if (r.warnings.length) r.warnings.forEach((w) => toast(w, "error")); };
  };
  $("#rf", main).onsubmit = (e) => { e.preventDefault(); run(); };
  if (params.code) run();
  $("#sf", main).onsubmit = async (e) => {
    e.preventDefault(); const f = new FormData(e.target);
    const hits = await api("GET", `/search?q=${encodeURIComponent(f.get("q"))}&speaker=${f.get("speaker")}&country=${f.get("country")}`);
    $("#hits", main).innerHTML = `<p><b>${hits.length} hit(s)</b>${hits.length >= 500 ? " (limited to 500)" : ""}</p><div class="scroll-x"><table class="tbl"><thead><tr><th>Locator</th><th>Timestamp</th><th>Speaker</th><th>Interview</th><th>Snippet</th></tr></thead><tbody>${hits.map((h) => `<tr><td class="nowrap"><a href="#/coding/${esc(h.interview_id)}?p=${h.paragraph_id}">${esc(h.interview_id)}${h.para_no ? ", ¶" + h.para_no : ""}</a></td><td class="nowrap ts">${esc(h.timestamp || "")}</td><td>${h.speaker === "R" ? "R" : "I (interviewer)"}</td><td class="small">${h.country} · ${esc(h.role_label)}</td><td class="small">${esc(h.snippet)}</td></tr>`).join("") || '<tr><td colspan="5" class="muted">no hits</td></tr>'}</tbody></table></div>`;
  };
};

// ---------------------------------------------------------------------------
// analysis
// ---------------------------------------------------------------------------
VIEWS.analysis = async (main, arg) => {
  const tab = arg || S.analysisTab || "matrix"; S.analysisTab = tab;
  const tabs = [["matrix", "Code × interview"], ["crosstab", "Crosstab"], ["cooccurrence", "Co-occurrence"], ["interactions", "Interactions"], ["agency", "Agency"], ["barriers", "Barriers"], ["learning", "Mutual learning"], ["crosscase", "Cross-case"]];
  main.innerHTML = `<div class="page"><div class="row"><h1 style="margin:0">Analysis</h1><span class="spacer"></span><button class="btn" id="exm">Export all matrices (CSV/XLSX)</button></div>
    <div class="tabs" style="margin-top:.6rem">${tabs.map(([k, l]) => `<a href="#/analysis/${k}" class="${k === tab ? "active" : ""}">${l}</a>`).join("")}</div><div id="body"></div></div>`;
  $("#exm", main).onclick = async () => { const r = await api("POST", "/export", {types: ["matrices", "interactions"]}); toast("Written to " + r.path, "ok"); };
  const body = $("#body", main);
  const segTable = (segs) => `<div class="scroll-x"><table class="tbl"><thead><tr><th>Locator</th><th>Role label</th><th>Codes</th><th>Quote</th></tr></thead><tbody>${segs.map((s) => `<tr><td class="nowrap"><a href="#/coding/${esc(s.interview_id)}?seg=${s.id}">${locator(s)}</a>${nqChip(s)}</td><td class="small">${esc(s.role_label)}</td><td>${s.codes.map((c) => `<span class="chip" style="background:${c.color}">${esc(c.name)} ${VALENCE_LABEL[String(c.valence)]}${c.agency ? " · " + c.agency : ""}</span>`).join(" ")}</td><td>${esc(s.text_snapshot)}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">none</td></tr>'}</tbody></table></div>`;
  if (tab === "matrix") {
    const draw = async () => {
      const f = new FormData($("#mf", body)); const qs = Array.from(f.entries()).filter(([, v]) => v).map(([k, v]) => `${k}=${encodeURIComponent(v === "on" ? "true" : v)}`).join("&");
      const m = await api("GET", "/analysis/matrix?" + qs);
      $("#mt", body).innerHTML = `<div class="scroll-x"><table class="tbl matrix"><thead><tr><th>Code</th>${m.interviews.map((i) => `<th class="num" title="${esc(i.role_label)}">${esc(i.id)}</th>`).join("")}<th class="num">Total</th></tr></thead><tbody>${m.codes.map((c) => { const cells = m.cells[c.id] || {}; return `<tr><td style="padding-left:${c.depth * 12 + 6}px"><span class="chip" style="background:${c.color}">&nbsp;</span>${esc(c.name)}</td>${m.interviews.map((i) => `<td class="num ${cells[i.id] ? "nz" : ""}">${cells[i.id] ? `<a href="#/retrieval?code=${c.id}">${cells[i.id]}</a>` : ""}</td>`).join("")}<td class="num"><b>${m.totals[c.id] || 0}</b></td></tr>`; }).join("")}</tbody></table></div>`;
    };
    body.innerHTML = `<form id="mf" class="row" style="margin-bottom:.6rem"><select name="country"><option value="">DE + CN</option><option>DE</option><option>CN</option></select><select name="role_category"><option value="">any role</option>${S.settings.role_categories.map((r) => `<option>${esc(r)}</option>`).join("")}</select><select name="pilot"><option value="">pilot + main</option><option value="true">pilot only</option><option value="false">main only</option></select><label class="inline-label"><input type="checkbox" name="with_children"> aggregate sub-codes into parents</label></form><div id="mt"></div>`;
    $$("#mf select, #mf input", body).forEach((i) => (i.onchange = draw)); await draw();
  } else if (tab === "crosstab") {
    const draw = async () => {
      const attr = $("#attr", body).value;
      const ct = await api("GET", "/analysis/crosstab?attribute=" + attr);
      $("#ct", body).innerHTML = `<div class="scroll-x"><table class="tbl matrix"><thead><tr><th rowspan="2">Code</th>${ct.values.map((v) => `<th colspan="5" style="text-align:center">${esc(v)}</th>`).join("")}</tr><tr>${ct.values.map(() => `<th class="num">seg</th><th class="num">int</th><th class="num">+</th><th class="num">0</th><th class="num">−</th>`).join("")}</tr></thead><tbody>${ct.rows.map((r) => `<tr><td style="padding-left:${r.depth * 12 + 6}px">${esc(r.name)}</td>${ct.values.map((v) => { const c = r.cells[v]; return `<td class="num ${c.segments ? "nz clickable" : ""}" data-ids="${c.segment_ids.join(",")}" data-label="${esc(r.name)} × ${esc(v)}">${c.segments || ""}</td><td class="num">${c.interviews || ""}</td><td class="num">${c.valence["1"] || ""}</td><td class="num">${c.valence["0"] || ""}</td><td class="num">${c.valence["-1"] || ""}</td>`; }).join("")}</tr>`).join("")}</tbody></table></div><div id="ctsegs"></div>`;
      $$("td.clickable", body).forEach((td) => (td.onclick = async () => { const ids = td.dataset.ids.split(",").map(Number); const all = await api("GET", "/retrieve"); const segs = all.filter((s) => ids.includes(s.id)); $("#ctsegs", body).innerHTML = `<h3>${esc(td.dataset.label)}: ${segs.length} segment(s)</h3>` + segTable(segs); }));
    };
    body.innerHTML = `<div class="row" style="margin-bottom:.6rem">Attribute: <select id="attr"><option value="country">country</option><option value="role_category">role category</option></select><span class="muted small">seg = segments · int = distinct interviews · +/0/− = by valence. Click a cell for the segments.</span></div><div id="ct"></div>`;
    $("#attr", body).onchange = draw; await draw();
  } else if (tab === "cooccurrence") {
    const co = await api("GET", "/analysis/cooccurrence");
    const ids = co.codes.map((c) => c.id);
    body.innerHTML = `<p class="muted small">Number of segment pairs whose text overlaps (same segment or overlapping segments in the same paragraph) and that carry the two codes. Diagonal = segments with the code.</p><div class="scroll-x"><table class="tbl matrix"><thead><tr><th></th>${co.codes.map((c) => `<th class="num" title="${esc(c.name)}" style="max-width:70px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.name)}</th>`).join("")}</tr></thead><tbody>${co.codes.map((a) => `<tr><td class="nowrap">${esc(a.name)}</td>${ids.map((b) => { const v = a.id === b ? co.diagonal[a.id] || 0 : co.cells[`${Math.min(a.id, b)}:${Math.max(a.id, b)}`] || 0; return `<td class="num ${v ? "nz" : ""}">${v || ""}</td>`; }).join("")}</tr>`).join("") || '<tr><td class="muted">No codings yet.</td></tr>'}</tbody></table></div>`;
  } else if (tab === "interactions") {
    const v = await api("GET", "/analysis/interactions");
    const fk = v.functions;
    const mat = (cc) => `<h3>${cc}: function × function (inducing / blocking)</h3><div class="scroll-x"><table class="tbl matrix"><thead><tr><th>from \\ to</th>${fk.map((f) => `<th class="num">${esc(f.name)}</th>`).join("")}</tr></thead><tbody>${fk.map((a) => `<tr><td class="nowrap">${esc(a.name)}</td>${fk.map((b) => { const c = v.matrix[cc][a.code_key][b.code_key]; const nz = c.inducing || c.blocking; return `<td class="num ${nz ? "nz" : ""}">${nz ? `<span class="ok">${c.inducing}</span> / <span class="danger">${c.blocking}</span>` : ""}</td>`; }).join("")}</tr>`).join("")}</tbody></table></div>`;
    body.innerHTML = `<div class="row"><button class="btn small" id="newint">+ New standalone interaction</button></div>
      <div class="scroll-x" style="margin-top:.5rem"><table class="tbl"><thead><tr><th>Country</th><th>Interview</th><th>From</th><th>To</th><th>Mechanism</th><th>Description</th><th>Condition</th><th>Segment</th><th></th></tr></thead><tbody>${v.interactions.map((it) => `<tr><td>${it.country}</td><td>${esc(it.interview_id || "")}</td><td>${esc(it.from_name)}</td><td>${esc(it.to_name)}</td><td><span class="chip" style="background:${it.mechanism === "blocking" ? "#b91c1c" : "#15803d"}">${it.mechanism}</span></td><td>${esc(it.description)}</td><td>${esc(it.condition)}</td><td class="small">${it.segment_id ? `<a href="#/coding/${esc(it.interview_id)}?seg=${it.segment_id}">¶${it.para_no}</a> ${esc((it.segment_text || "").slice(0, 80))}` : ""}</td><td class="nowrap"><button class="btn tiny" data-edit="${it.id}">edit</button> <button class="btn tiny danger" data-del="${it.id}">del</button></td></tr>`).join("") || '<tr><td colspan="9" class="muted">No interactions yet. Add them from a segment (Coding view) or here.</td></tr>'}</tbody></table></div>
      ${mat("DE")}${mat("CN")}<h3>Mermaid (for a figure)</h3><div class="row"><button class="btn small" id="copym">Copy</button><span class="muted small">Paste into a Mermaid renderer (e.g. VS Code preview, mermaid.live — note: that is an external site).</span></div><pre class="code" id="mm">${esc(v.mermaid)}</pre>`;
    const funcs = S.codesFlat.filter((c) => c.type === "function" && c.active);
    $("#newint", body).onclick = () => interactionDialog({}, funcs);
    $$("[data-edit]", body).forEach((b) => (b.onclick = () => interactionDialog(v.interactions.find((x) => x.id === parseInt(b.dataset.edit, 10)), funcs)));
    $$("[data-del]", body).forEach((b) => (b.onclick = async () => { if (await confirmDialog("Delete interaction?", "", "Delete")) { await api("DELETE", `/interactions/${b.dataset.del}`); await afterWrite(); } }));
    $("#copym", body).onclick = () => { navigator.clipboard.writeText(v.mermaid).then(() => toast("Copied", "ok")); };
  } else if (tab === "agency") {
    const ag = await api("GET", "/analysis/agency");
    body.innerHTML = `<p class="muted small">Who fulfils each function (Huang & Malkin, 2026): number of codings per agency value, per function and country.</p><div class="scroll-x"><table class="tbl matrix"><thead><tr><th>Function</th><th>Country</th>${ag.agencies.map((a) => `<th class="num">${a}</th>`).join("")}</tr></thead><tbody>${ag.rows.map((r) => ["DE", "CN"].map((cc, i) => `<tr>${i === 0 ? `<td rowspan="2">${esc(r.name)}</td>` : ""}<td>${cc}</td>${ag.agencies.map((a) => `<td class="num ${r.cells[cc][a] ? "nz" : ""}">${r.cells[cc][a] || ""}</td>`).join("")}</tr>`).join("")).join("")}</tbody></table></div>`;
  } else if (tab === "barriers") {
    const b = await api("GET", "/analysis/barriers");
    body.innerHTML = `<p class="muted small">Segments coded <b>Barrier / Blocking Mechanism</b>, grouped by the co-coded function (same or overlapping segment) and country.</p>` + ["DE", "CN"].map((cc) => `<h2>${cc}</h2>` + (Object.keys(b.groups[cc]).length ? Object.entries(b.groups[cc]).map(([f, segs]) => `<h3>${esc(f)} <span class="muted">(${segs.length})</span></h3>${segTable(segs)}`).join("") : '<p class="muted">none</p>')).join("");
  } else if (tab === "learning") {
    const l = await api("GET", "/analysis/learning");
    body.innerHTML = `<p class="muted small">Segments coded <b>Mutual Learning / Transferability</b> by country.</p><div class="grid2"><div><h2>DE (${l.DE.length})</h2>${segTable(l.DE)}</div><div><h2>CN (${l.CN.length})</h2>${segTable(l.CN)}</div></div>`;
  } else if (tab === "crosscase") {
    body.innerHTML = `<div class="row" style="margin-bottom:.6rem">Code: <select id="cc"><option value="">choose…</option>${S.codesFlat.filter((c) => c.active).map((c) => `<option value="${c.id}">${"— ".repeat(c.depth)}${esc(c.name)}</option>`).join("")}</select><label class="inline-label"><input type="checkbox" id="sub" checked> incl. sub-codes</label></div><div id="ccb"></div>`;
    const draw = async () => { const id = $("#cc", body).value; if (!id) return; const r = await api("GET", `/analysis/crosscase?code_id=${id}&include_sub=${$("#sub", body).checked}`);
      const sm = (cc) => `<p class="small muted">${r.summary[cc].segments} segments in ${r.summary[cc].interviews} interview(s) · valence + ${r.summary[cc].valence["1"]} / 0 ${r.summary[cc].valence["0"]} / − ${r.summary[cc].valence["-1"]}</p>`;
      $("#ccb", body).innerHTML = `<div class="grid2"><div><h2>DE</h2>${sm("DE")}${segTable(r.DE)}</div><div><h2>CN</h2>${sm("CN")}${segTable(r.CN)}</div></div>`; };
    $("#cc", body).onchange = draw; $("#sub", body).onchange = draw;
  }
};

// ---------------------------------------------------------------------------
// reliability
// ---------------------------------------------------------------------------
VIEWS.reliability = async (main, arg, params) => {
  const ivs = await api("GET", "/interviews");
  const id = params.iv || (ivs[0] && ivs[0].id);
  main.innerHTML = `<div class="page"><h1>Intra-coder reliability</h1>
    <div class="panel"><div class="row">Interview: <select id="iv">${ivs.map((i) => `<option value="${esc(i.id)}" ${i.id === id ? "selected" : ""}>${esc(i.id)} — ${esc(i.role_label)}</option>`).join("")}</select><span class="spacer"></span><button class="btn primary" id="start">Start re-coding pass</button></div>
    <p class="small muted">A re-coding pass opens the interview with the existing codings hidden and writes into a separate pass. Finish the pass to lock it and compare it with the primary pass (which stays the analytic dataset). Recommended after some weeks (Mayring, 2014).</p><div id="passes"></div></div><div id="report"></div></div>`;
  $("#iv", main).onchange = (e) => { location.hash = "#/reliability?iv=" + e.target.value; };
  $("#start", main).onclick = async () => { const p = await api("POST", `/interviews/${encodeURIComponent(id)}/passes`); toast("Re-coding pass started: " + p.name, "ok"); location.hash = `#/coding/${id}?pass=${p.id}`; };
  if (!id) return;
  const data = await api("GET", `/interviews/${encodeURIComponent(id)}`);
  $("#passes", main).innerHTML = `<table class="tbl" style="max-width:640px"><thead><tr><th>Pass</th><th>Started</th><th>Finished</th><th></th></tr></thead><tbody>${data.passes.map((p) => `<tr><td>${esc(p.name)}${p.locked ? " 🔒" : ""}</td><td>${esc(fmtDate(p.started_at))}</td><td>${esc(fmtDate(p.finished_at))}</td><td><a href="#/coding/${esc(id)}?pass=${p.id}">open</a>${p.name !== "primary" && !p.locked ? ` · <button class="btn tiny" data-finish="${p.id}">finish & lock</button>` : ""}${p.name !== "primary" ? ` · <a href="#/reliability?iv=${esc(id)}&pass=${p.id}">report</a>` : ""}</td></tr>`).join("")}</tbody></table>`;
  $$("[data-finish]", main).forEach((b) => (b.onclick = async () => { if (await confirmDialog("Finish re-coding pass?", "<p>The pass is locked afterwards; no further codings can be added to it.</p>", "Finish")) { await api("POST", `/passes/${b.dataset.finish}/finish`); render(true); } }));
  let rep;
  try { rep = await api("GET", `/reliability/${encodeURIComponent(id)}${params.pass ? "?pass_id=" + params.pass : ""}`, undefined, {silent: true}); }
  catch (e) { $("#report", main).innerHTML = `<div class="panel muted">${esc(e.message)}</div>`; return; }
  $("#report", main).innerHTML = `<div class="panel"><h2 style="margin-top:0">Report: ${esc(rep.pass1.name)} vs. ${esc(rep.pass2.name)}${rep.pass2.locked ? "" : ' <span class="warn small">(pass not finished yet)</span>'}</h2>
    <div class="grid3"><div><div class="muted">Respondent paragraphs</div><div class="stat">${rep.paragraphs}</div></div><div><div class="muted">Identical code sets</div><div class="stat">${rep.percent_agreement ?? "—"} %</div><div class="small">${rep.agreeing_paragraphs} of ${rep.paragraphs}</div></div><div><div class="muted">Pooled Cohen's κ (present/absent per code × paragraph)</div><div class="stat">${rep.pooled_kappa ?? "—"}</div><div class="small">${rep.segments_pass1} vs. ${rep.segments_pass2} segments</div></div></div>
    <h3>Per code</h3><table class="tbl"><thead><tr><th>Code</th><th class="num">both</th><th class="num">only pass 1</th><th class="num">only pass 2</th><th class="num">neither</th><th class="num">% agreement</th><th class="num">κ</th></tr></thead><tbody>${rep.per_code.map((r) => `<tr><td>${esc(r.name)}</td><td class="num">${r.both}</td><td class="num">${r.only_pass1}</td><td class="num">${r.only_pass2}</td><td class="num">${r.neither}</td><td class="num">${r.percent_agreement}</td><td class="num">${r.kappa ?? "—"}</td></tr>`).join("")}</tbody></table>
    <h3>Disagreements (${rep.disagreements.length})</h3>${rep.disagreements.map((d) => `<div class="panel"><div class="row"><b>¶${d.para_no}</b><a href="#/coding/${esc(id)}?p=${d.paragraph_id}" class="small">open</a></div><div class="grid2"><div><div class="muted small">pass 1 (primary)</div>${d.pass1.map((n) => `<span class="chip" style="background:${d.only_pass1.includes(n) ? "#b45309" : "#6b7280"}">${esc(n)}</span>`).join(" ") || "<span class='muted'>—</span>"}</div><div><div class="muted small">pass 2</div>${d.pass2.map((n) => `<span class="chip" style="background:${d.only_pass2.includes(n) ? "#b45309" : "#6b7280"}">${esc(n)}</span>`).join(" ") || "<span class='muted'>—</span>"}</div></div><p class="small" style="white-space:pre-wrap">${esc(d.text)}</p><label class="field"><span>Resolution note</span><textarea data-par="${d.paragraph_id}">${esc(d.note)}</textarea></label></div>`).join("") || '<p class="muted">none</p>'}</div>`;
  $$("textarea[data-par]", main).forEach((ta) => (ta.onblur = async () => { await api("PUT", `/reliability/${rep.pass2.id}/notes/${ta.dataset.par}`, {note: ta.value}); toast("Note saved", "ok"); }));
};

// ---------------------------------------------------------------------------
// export
// ---------------------------------------------------------------------------
VIEWS.export = async (main) => {
  const types = await api("GET", "/export/types");
  const labels = {codebook: "Codebook (md/xlsx)", interview_overview: "Interview overview (md/xlsx)", segments: "Segments by function (md) + segments.csv", quote_bank: "Quote bank (md/csv, all quote candidates)", interactions: "Interactions (csv/md/xlsx/mmd)", matrices: "Agency matrix, crosstabs, co-occurrence, code matrix", changelog: "Codebook changelog (md)", reliability: "Reliability report (md)", memos: "Memos with versions (md)", full_dump: "Full dump (json + csv folder)"};
  const prev = await api("GET", "/exports");
  const mapping = await api("GET", "/anonymization");
  main.innerHTML = `<div class="page"><h1>Export</h1><div class="grid2">
    <div class="panel"><p>Files are written to <code>${esc(S.settings.exports_dir)}\\&lt;timestamp&gt;\\</code>. Confidential text is never exported. Exported text is checked against the anonymization list (${mapping.length} term(s)).</p>
      <div class="col">${types.map((t) => `<label class="inline-label"><input type="checkbox" name="t" value="${t}" checked> ${esc(labels[t] || t)}</label>`).join("")}</div>
      <div class="row" style="margin-top:.8rem"><button class="btn primary" id="run">Export selected</button><button class="btn" id="all">Export everything (supervisor USB)</button></div><div id="result"></div></div>
    <div class="panel"><h2 style="margin-top:0">Previous exports</h2>${prev.map((p) => `<details><summary>${esc(p.name)} ${p.has_warnings ? '<span class="warn">⚠ warnings</span>' : ""} <span class="muted small">(${p.files.length} files)</span></summary><p class="small mono">${esc(p.path)}</p><ul class="small">${p.files.map((f) => `<li>${esc(f)}</li>`).join("")}</ul></details>`).join("") || '<p class="muted">none yet</p>'}</div></div></div>`;
  const run = async (sel) => {
    $("#result", main).innerHTML = '<p class="muted">Exporting…</p>';
    const r = await api("POST", "/export", {types: sel});
    $("#result", main).innerHTML = `<div class="banner info"><b>Done:</b> ${r.files.length} file(s) written to <code>${esc(r.path)}</code></div>${r.warnings.length ? `<div class="banner warn"><b>Anonymization warning:</b> mapped terms still occur in exported text.<ul>${r.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}<ul class="small">${r.files.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>`;
  };
  $("#run", main).onclick = () => run($$("input[name=t]:checked", main).map((i) => i.value));
  $("#all", main).onclick = () => run(null);
};

// ---------------------------------------------------------------------------
// readable interview reports
// ---------------------------------------------------------------------------
VIEWS.readable = async (main) => {
  const ivs = (await api("GET", "/interviews")).slice().sort((a, b) => (a.country === b.country ? a.id.localeCompare(b.id) : (a.country === "DE" ? -1 : 1)));
  main.innerHTML = `<div class="page"><h1>Readable interview reports</h1><div class="panel">
    <p>One easy-to-read page per interview: the full transcript, the coded passages highlighted in the category colour, and next to each answer one box per coded segment with its segment ID, categories, valence, agency and interactions. Main coding pass only. The interview memo follows the transcript. Comments and confidential text are not included.</p>
    <p class="muted small">Open a report in the browser and use <b>Print → Save as PDF</b> to get a PDF. "Save selected as files" writes the pages plus an overview (<code>index.html</code>) to <code>${esc(S.settings.exports_dir)}\\&lt;timestamp&gt;\\readable_reports\\</code>.</p>
    <div class="row" style="margin:.7rem 0"><a class="btn" href="/api/v1/readable" target="_blank" rel="noopener">Open overview</a><span class="spacer"></span>
      <button class="btn small" id="selall">Select all</button><button class="btn small" id="selnone">Select none</button>
      <button class="btn primary" id="write">Save selected as files</button></div>
    <div id="result"></div>
    <table class="tbl"><thead><tr><th></th><th>ID</th><th>Role label</th><th>Actor group</th><th class="right">Segments</th><th></th></tr></thead><tbody>
    ${ivs.map((i) => `<tr><td><input type="checkbox" name="iv" value="${esc(i.id)}" checked></td><td class="nowrap">${esc(i.id)}</td><td>${esc(i.role_label)}</td><td>${esc(i.role_category)}</td><td class="right">${i.segment_count ?? ""}</td><td class="nowrap"><a href="/api/v1/readable/${encodeURIComponent(i.id)}" target="_blank" rel="noopener">Open report</a></td></tr>`).join("") || '<tr><td colspan="6" class="muted">No interviews imported yet.</td></tr>'}
    </tbody></table></div></div>`;
  $("#selall", main).onclick = () => $$("input[name=iv]", main).forEach((c) => (c.checked = true));
  $("#selnone", main).onclick = () => $$("input[name=iv]", main).forEach((c) => (c.checked = false));
  $("#write", main).onclick = async () => {
    const sel = $$("input[name=iv]:checked", main).map((c) => c.value);
    if (!sel.length) { toast("Select at least one interview."); return; }
    $("#result", main).innerHTML = '<p class="muted">Writing reports…</p>';
    const r = await api("POST", "/readable/export", {interview_ids: sel});
    $("#result", main).innerHTML = `<div class="banner info"><b>Done:</b> ${r.files.length} file(s) written to <code>${esc(r.path)}</code>. Open <code>index.html</code> there to start.</div>${r.warnings.length ? `<div class="banner warn"><b>Anonymization warning:</b> mapped terms still occur in the reports.<ul>${r.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}`;
  };
};

// ---------------------------------------------------------------------------
// settings
// ---------------------------------------------------------------------------
VIEWS.settings = async (main) => {
  await loadSettings();
  const st = S.settings, s = st.settings;
  const backups = await api("GET", "/backups");
  const mapping = await api("GET", "/anonymization");
  main.innerHTML = `<div class="page"><h1>Settings</h1><div class="grid2">
    <div class="panel"><h2 style="margin-top:0">Project & backups</h2>
      <p class="small">Project file: <code>${esc(st.project_file)}</code><br>Backups: <code>${esc(st.backups_dir)}</code><br>Exports: <code>${esc(st.exports_dir)}</code><br>Version ${esc(st.version)}</p>
      <label class="field"><span>Secondary backup folder (a local folder that your own OneDrive/iCloud/Dropbox client syncs; the app itself never touches the network)</span><input type="text" id="sec" value="${esc(s.secondary_backup_dir || "")}" placeholder="e.g. D:\\OneDrive\\qda-backups"></label>
      <label class="field"><span>Automatic backup interval (minutes, if changed)</span><input type="number" id="int" value="${esc(s.backup_interval_min || 15)}" min="1" style="width:100px"></label>
      <div class="row"><button class="btn primary" id="save">Save</button><button class="btn" id="bk">Backup now</button></div>
      <p class="small muted">Backups also run on start, on shutdown, after every import and before every restore/migration. Rotation: all of the last 7 days, then one per day for 60 days, then one per week; never fewer than the most recent 20.</p>
      <h3>Restore from backup</h3><p class="small muted">A safety backup of the current file is created first. The undo history is cleared.</p>
      <div style="max-height:260px;overflow:auto"><table class="tbl"><thead><tr><th>File</th><th class="num">Size</th><th>Created</th><th></th></tr></thead><tbody>${backups.map((b) => `<tr><td class="mono small">${esc(b.filename)}</td><td class="num">${Math.round(b.size / 1024)} KB</td><td>${esc(fmtDate(b.created_at))}</td><td><button class="btn tiny danger" data-restore="${esc(b.filename)}">restore</button></td></tr>`).join("")}</tbody></table></div></div>
    <div class="panel"><h2 style="margin-top:0">Coding</h2>
      <label class="inline-label"><input type="checkbox" id="pilot" ${s.pilot_phase === "true" ? "checked" : ""}> Pilot phase (codebook changes logged with phase = pilot)</label>
      <h3>Keyboard shortcuts (coding view)</h3><table class="tbl" style="max-width:420px"><tbody>${Object.entries(st.shortcuts).map(([k, key]) => { const c = S.codesFlat.find((x) => x.code_key === key); return `<tr><td><span class="kbd">${k}</span></td><td>${esc(c ? c.name : key)}</td></tr>`; }).join("")}<tr><td><span class="kbd">L</span></td><td>assign last-used code</td></tr><tr><td><span class="kbd">/</span></td><td>open code picker</td></tr><tr><td><span class="kbd">n</span> / <span class="kbd">p</span></td><td>next / previous coding</td></tr><tr><td><span class="kbd">Ctrl+Z</span> / <span class="kbd">Ctrl+Y</span></td><td>undo / redo</td></tr><tr><td><span class="kbd">Esc</span></td><td>clear selection</td></tr></tbody></table>
      <h3>Anonymization list</h3><p class="small muted">Term → replacement. Applied on import (optional) or on demand with a review. Every export warns if a mapped term still occurs. The list stays inside the project file.</p>
      <table class="tbl" style="max-width:520px"><tbody>${mapping.map((m) => `<tr><td>${esc(m.term)}</td><td>→ ${esc(m.replacement)}</td><td><button class="btn tiny danger" data-delmap="${m.id}">×</button></td></tr>`).join("")}</tbody></table>
      <form id="mapf" class="row" style="margin-top:.4rem"><input type="text" name="term" placeholder="real name / term" style="width:180px" required><input type="text" name="replacement" placeholder="replacement, e.g. [Company A]" style="width:200px" required><button class="btn">Add</button></form>
      <div class="row" style="margin-top:.5rem"><button class="btn" id="scan">Scan transcripts for mapped terms…</button></div><div id="scanres"></div></div></div>
    </div>`;
  $("#save", main).onclick = async () => { await api("PUT", "/settings/secondary_backup_dir", {value: $("#sec", main).value}); await api("PUT", "/settings/backup_interval_min", {value: $("#int", main).value}); toast("Saved", "ok"); render(true); };
  $("#bk", main).onclick = async () => { const r = await api("POST", "/backup"); toast("Backup written: " + r.filename, "ok"); render(true); };
  $("#pilot", main).onchange = async (e) => { await api("PUT", "/settings/pilot_phase", {value: e.target.checked}); await loadSettings(); };
  $$("[data-restore]", main).forEach((b) => (b.onclick = async () => { if (await confirmDialog("Restore " + b.dataset.restore + "?", "<p>The current project file is replaced by this backup (a safety copy is made first). Everything coded after that backup is lost from the working file.</p>", "Restore")) { const r = await api("POST", "/restore", {filename: b.dataset.restore}); toast(`Restored from ${r.restored_from}; safety copy ${r.safety_backup}`, "ok"); await afterWrite(); } }));
  $$("[data-delmap]", main).forEach((b) => (b.onclick = async () => { await api("DELETE", `/anonymization/${b.dataset.delmap}`); render(true); }));
  $("#mapf", main).onsubmit = async (e) => { e.preventDefault(); const f = new FormData(e.target); await api("POST", "/anonymization", {term: f.get("term"), replacement: f.get("replacement")}); render(true); };
  $("#scan", main).onclick = async () => {
    const hits = await api("POST", "/anonymization/scan");
    const box = $("#scanres", main);
    if (!hits.length) { box.innerHTML = '<p class="ok">No mapped terms found in the transcripts.</p>'; return; }
    box.innerHTML = `<p><b>${hits.length} hit(s)</b> — review and replace:</p><div style="max-height:300px;overflow:auto"><table class="tbl"><thead><tr><th><input type="checkbox" id="hall" checked></th><th>Where</th><th>Term</th><th>Context</th></tr></thead><tbody>${hits.map((h, i) => `<tr><td><input type="checkbox" data-i="${i}" checked></td><td class="nowrap">${esc(h.interview_id)} ${h.para_no ? "¶" + h.para_no : "(I)"}</td><td>${esc(h.term)} → ${esc(h.replacement)}</td><td class="small">${esc(h.context)}</td></tr>`).join("")}</tbody></table></div><div class="row" style="margin-top:.5rem"><button class="btn danger" id="apply">Replace selected in the stored transcripts</button></div>`;
    $("#hall", box).onchange = (e) => $$("input[data-i]", box).forEach((c) => (c.checked = e.target.checked));
    $("#apply", box).onclick = async () => {
      const sel = $$("input[data-i]:checked", box).map((c) => hits[parseInt(c.dataset.i, 10)]);
      if (!sel.length) return;
      if (!(await confirmDialog("Replace " + sel.length + " occurrence(s)?", "<p>The stored transcript text is changed in place (segment offsets are shifted, text snapshots updated). A backup is created first and the undo history is cleared.</p>", "Replace"))) return;
      const r = await api("POST", "/anonymization/apply", {hits: sel}); toast(`Replaced ${r.replaced} occurrence(s) in ${r.paragraphs} paragraph(s)`, "ok"); render(true);
    };
  };
};

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------
(async function boot() {
  try { await loadSettings(); await loadCodes(); await refreshUndo(); }
  catch (e) { $("#main").innerHTML = `<div class="page"><div class="banner danger">Could not load the project: ${esc(e.message)}</div></div>`; return; }
  if (!location.hash) location.hash = "#/dashboard";
  await render(false);
})();
