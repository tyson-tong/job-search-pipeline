"use strict";
// What every script of the page shares: constants, the page's state, DOM and API helpers,
// and unsaved changes. Loaded first; then detail.js (the job panel), app.js (the job list
// and the page's wiring), and searches.js (the Searches tab).
// Sent in a header only, never in a URL, where it would stay in the browser's history.
const TOKEN = document.querySelector('meta[name="jobflow-token"]').content;
const LABELS = { new: "New", interested: "Interested", not_interested: "Not interested", applied: "Applied",
  interviewing: "Interviewing", offered: "Offered", accepted: "Accepted", declined: "Declined",
  rejected: "Rejected", withdrawn: "Withdrawn", filtered: "Filtered", archived: "Archived" };
const SITE_NAMES = { linkedin: "LinkedIn", indeed: "Indeed", seek: "Seek" };
const AUTO_REFRESH_MS = 3 * 60 * 1000;
// versions: each edited job's tracking version when you started editing it, sent with
// Save so changes made elsewhere in the meantime are caught instead of overwritten.
// statuses: My jobs' chosen statuses; null until recalled, or set to the server's defaults.
const state = { view: "jobs", statuses: null, jobs: [], summary: null,
  pending: new Map(), versions: new Map(), detail: null, loadedAt: null, scoring: null,
  // Rows ticked for a bulk change (Inbox triage; Filtered and Not interested back to the Inbox); lastPicked anchors shift-click ranges.
  selected: new Set(), lastPicked: null,
  // Country and site filters: null for all, else the ticked ones (none ticked: no jobs); and
  // the countries and sites among this tab's jobs, with counts.
  countries: null, sites: null, countryCounts: null,
  // Paging splits the rows the filters returned (the filters themselves cover every job);
  // pageKey is the query they came from, so a new query starts again at page 1.
  page: 0, pageSize: 50, pageKey: null };
// A job still in the Inbox can only be triaged; the other statuses come after Interested.
const TRIAGE_STATUSES = ["new", "interested", "not_interested"];
// My jobs holds only jobs you picked as Interested and what followed; these never show there.
const NOT_MY_JOBS = ["new", "not_interested", "filtered", "archived"];
const PAGE_SIZES = [5, 10, 20, 50, 100];
// The tabs that each show one status; My jobs shows the statuses chosen in its menu.
const TAB_STATUS = { inbox: "new", filtered: "filtered", not_interested: "not_interested" };
// Tabs whose jobs you can send back to the Inbox by ticking several at once.
const RETURN_TABS = ["filtered", "not_interested"];
// The one-click statuses in each tab's Status column; My jobs has a menu instead.
const QUICK_STATUSES = { inbox: ["interested", "not_interested"], filtered: ["interested", "not_interested"],
  not_interested: ["interested"] };

// ---- Helpers -------------------------------------------------------------------------
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "className") node.className = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  return fill(node, children, false);
}
// Append children (nested arrays allowed), skipping null/undefined/false; text is never parsed as HTML.
function fill(node, children, replace = true) {
  const nodes = [children].flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false)
    .map((c) => c instanceof Node ? c : document.createTextNode(String(c)));
  if (replace) node.replaceChildren(...nodes); else node.append(...nodes);
  return node;
}
const safeUrl = (url) => /^https?:\/\//i.test(url || "") ? url : null;
const label = (status) => LABELS[status] || status;
const fmtDate = (value) => value ? String(value).slice(0, 10) : "";
function fmtSalary(min, max, currency) {
  if (min == null) return "";
  const k = (n) => `${Math.round(n / 1000)}k`;
  return (min === max ? k(min) : `${k(min)}–${k(max)}`) + (currency ? ` ${currency}` : "") + " /yr";
}
function toast(message, isError = false) {
  const node = el("div", { className: "toast" + (isError ? " error" : ""), role: "status" }, message);
  document.body.append(node);
  setTimeout(() => node.remove(), isError ? 7000 : 3000);
}
async function api(path, options = {}) {
  const response = await fetch(path, { ...options,
    headers: { "X-Jobflow-Token": TOKEN, ...(options.headers || {}) } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw Object.assign(new Error(data.error || `Request failed (${response.status})`),
      { status: response.status, data });
  }
  return data;
}
function remember() {
  try { localStorage.setItem("jobflow.filters", JSON.stringify({ statuses: [...state.statuses],
    sort: sortInput.value, sites: state.sites && [...state.sites], view: state.view, pageSize: state.pageSize })); } catch (e) { /* optional */ }
}
function recall() {
  try {
    const saved = JSON.parse(localStorage.getItem("jobflow.filters") || "null");
    if (!saved) return;
    const statuses = Array.isArray(saved.statuses) ? saved.statuses.filter((s) => !NOT_MY_JOBS.includes(s)) : [];
    if (Array.isArray(saved.statuses)) state.statuses = new Set(statuses);
    if (saved.sort) sortInput.value = saved.sort;
    if (Array.isArray(saved.sites)) state.sites = new Set(saved.sites.filter((s) => s in SITE_NAMES));
    else if (saved.site in SITE_NAMES) state.sites = new Set([saved.site]);  // saved when one site was picked
    if (saved.view) state.view = saved.view;
    if (PAGE_SIZES.includes(saved.pageSize)) state.pageSize = saved.pageSize;
  } catch (e) { /* optional */ }
}

// ---- Pending (unsaved) changes ---------------------------------------------------------
function current(job, field) {
  const pending = state.pending.get(job.id);
  return pending && field in pending ? pending[field] : job[field];
}
function setPending(job, field, value) {
  stagePending(job, field, value);
  renderSaveBar();
  renderRows();
  // Rebuilding the drawer while typing a note would lose the cursor.
  if (field !== "note" && state.detail && state.detail.id === job.id) renderDetail();
}
function stagePending(job, field, value) {
  const pending = { ...(state.pending.get(job.id) || {}) };
  const original = field === "note" ? "" : (job[field] ?? (field === "follow_up_on" ? "" : null));
  if ((value ?? "") === (fmtDate(original) || original || "")) delete pending[field];
  else pending[field] = value;
  if (Object.keys(pending).length) {
    state.pending.set(job.id, pending);
    if (!state.versions.has(job.id) && job.version) state.versions.set(job.id, job.version);
  } else {
    state.pending.delete(job.id);
    state.versions.delete(job.id);
  }
}
function renderSaveBar() {
  const n = state.pending.size;
  const loaded = state.loadedAt ? `Updated ${state.loadedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "";
  document.getElementById("unsaved").textContent = n ? `${n} unsaved change${n > 1 ? "s" : ""}` : loaded;
  document.getElementById("save").disabled = !n;
  document.getElementById("discard").disabled = !n;
  // The detail panel covers the header, so it has its own Save button.
  const drawerSave = document.getElementById("d-save");
  if (drawerSave) drawerSave.disabled = !n;
  const drawerUnsaved = document.getElementById("d-unsaved");
  if (drawerUnsaved) drawerUnsaved.textContent = n ? `${n} unsaved` : "";
}
async function save(force = false) {
  const changes = [...state.pending].map(([id, change]) => {
    const version = state.versions.get(id);
    return version ? { id, ...change, expected_version: version } : { id, ...change };
  });
  document.getElementById("save").disabled = true;
  try {
    const result = await api("/api/save", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ changes, force }) });
    state.pending.clear();
    state.versions.clear();
    renderSaveBar();
    toast(`Saved ${result.saved.length} job${result.saved.length > 1 ? "s" : ""}`);
    await refresh();
    if (state.detail) await openDetail(state.detail.id);
  } catch (error) {
    if (error.status === 409 && error.data && error.data.conflicts) return resolveConflicts(error.data.conflicts);
    toast(error.message, true);
  } finally {
    renderSaveBar();
  }
}
// Nothing was saved: some jobs changed elsewhere after this page loaded them.
async function resolveConflicts(conflicts) {
  const lines = conflicts.map((c) => `• #${c.id} ${c.title}${c.company ? ` (${c.company})` : ""}: `
    + `now ${label(c.status)}${c.follow_up_on ? `, follow up ${fmtDate(c.follow_up_on)}` : ""}`
    + (c.changed_via ? `, last changed via ${c.changed_via}` : ""));
  const overwrite = confirm(`${conflicts.length === 1 ? "This job has" : "These jobs have"} changed since you `
    + `loaded ${conflicts.length === 1 ? "it" : "them"}, so nothing was saved:\n\n${lines.join("\n")}\n\n`
    + "OK: save your changes anyway (replacing the status and follow-up; notes are added).\n"
    + "Cancel: keep your drafts and load the latest data to review.");
  if (overwrite) return save(true);
  toast("Nothing saved. Showing the latest data; your drafts are kept.");
  await refresh();
  if (state.detail) await openDetail(state.detail.id);
}
window.addEventListener("beforeunload", (event) => {
  if (state.pending.size) { event.preventDefault(); event.returnValue = ""; }
});
async function copyCommand(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied: " + text); }
  catch (error) { toast("Copy failed; select the command and copy it instead.", true); }
}
