// What every module of the page shares: constants, the page's state, DOM and API helpers,
// the status menu, paging, and unsaved changes. app.js is the page's entry point and loads
// the rest: detail.js (the job panel), selection.js (ticking rows), scoring.js, and
// searches.js (the Searches tab). Each module imports only modules loaded before it.
import { fmtDate, isSaved, label, SITE_NAMES } from "./format.js";

// What other modules call back into: the job list's refresh and rows (app.js), and the job
// panel (detail.js). app.js fills these in as it loads, so no module has to import a module
// that imports it.
export const page = { refresh: null, renderRows: null, openDetail: null, renderDetail: null };

// Sent in a header only, never in a URL, where it would stay in the browser's history.
export const TOKEN = document.querySelector('meta[name="jobflow-token"]').content;
export const AUTO_REFRESH_MS = 3 * 60 * 1000;
// versions: each edited job's tracking version when you started editing it, sent with
// Save so changes made elsewhere in the meantime are caught instead of overwritten.
// statuses: My jobs' chosen statuses; null until recalled, or set to the server's defaults.
export const state = { view: "jobs", statuses: null, jobs: [], summary: null,
  pending: new Map(), versions: new Map(), detail: null, loadedAt: null, scoring: null,
  saving: false,
  // Rows ticked for a bulk change (Inbox triage; Filtered, Not interested, and Archived back
  // to the Inbox); lastPicked anchors shift-click ranges.
  selected: new Set(), lastPicked: null,
  // Country and site filters: null for all, else the ticked ones (none ticked: no jobs); and
  // the countries and sites among this tab's jobs, with counts.
  countries: null, sites: null, countryCounts: null,
  // Paging splits the rows the filters returned (the filters themselves cover every job);
  // pageKey is the query they came from, so a new query starts again at page 1.
  page: 0, pageSize: 50, pageKey: null };
// The status groups (which statuses My jobs shows, which a job can move to) come from the
// server, with the summary (state.summary: untriaged, triage_choices, pursuing, ...; db.py).
const PAGE_SIZES = [5, 10, 20, 50, 100];
// The sort menu, which the remembered filters include.
export const sortInput = document.getElementById("sort");
// The tabs that each show one status; My jobs shows the statuses chosen in its menu.
export const TAB_STATUS = { inbox: "new", filtered: "filtered", not_interested: "not_interested",
  archived: "archived" };
// Tabs whose jobs you can send back to the Inbox by ticking several at once.
export const RETURN_TABS = ["filtered", "not_interested", "archived"];
// The one-click statuses in each tab's Status column; My jobs has a menu instead.
export const QUICK_STATUSES = { inbox: ["interested", "not_interested"],
  filtered: ["interested", "not_interested"], not_interested: ["interested"],
  archived: ["interested"] };

// ---- Helpers -------------------------------------------------------------------------
export function el(tag, attrs = {}, ...children) {
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
export function fill(node, children, replace = true) {
  const nodes = [children].flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false)
    .map((c) => c instanceof Node ? c : document.createTextNode(String(c)));
  if (replace) node.replaceChildren(...nodes); else node.append(...nodes);
  return node;
}
export function toast(message, isError = false) {
  const node = el("div", { className: "toast" + (isError ? " error" : ""), role: "status" }, message);
  document.body.append(node);
  setTimeout(() => node.remove(), isError ? 7000 : 3000);
}
export async function api(path, options = {}) {
  const response = await fetch(path, { ...options,
    headers: { "X-Jobflow-Token": TOKEN, ...(options.headers || {}) } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw Object.assign(new Error(data.error || `Request failed (${response.status})`),
      { status: response.status, data });
  }
  return data;
}
export function remember() {
  try {
    localStorage.setItem("jobflow.filters", JSON.stringify({ statuses: [...state.statuses],
      sort: sortInput.value, sites: state.sites && [...state.sites], view: state.view,
      pageSize: state.pageSize }));
  } catch (e) { /* optional */ }
}
// The filters remembered from your last visit; call once state.summary is loaded.
export function recall() {
  try {
    const saved = JSON.parse(localStorage.getItem("jobflow.filters") || "null");
    if (!saved) return;
    // My jobs holds only jobs you picked as Interested and what followed.
    const statuses = Array.isArray(saved.statuses)
      ? saved.statuses.filter((s) => !state.summary.untriaged.includes(s)) : [];
    if (Array.isArray(saved.statuses)) state.statuses = new Set(statuses);
    if (saved.sort) sortInput.value = saved.sort;
    if (Array.isArray(saved.sites)) state.sites = new Set(saved.sites.filter((s) => s in SITE_NAMES));
    else if (saved.site in SITE_NAMES) state.sites = new Set([saved.site]);  // saved when one site was picked
    if (saved.view) state.view = saved.view;
    if (PAGE_SIZES.includes(saved.pageSize)) state.pageSize = saved.pageSize;
  } catch (e) { /* optional */ }
}

// The status menu of a job's row, and of the job panel.
export function statusSelect(job) {
  const value = current(job, "status");
  // A new, not interested, filtered, or archived job can only be triaged (or go back to
  // the Inbox); any other doesn't go back to New.
  const { untriaged, triage_choices: triage, editable } = state.summary;
  const choices = untriaged.includes(job.status) ? triage : editable.filter((s) => s !== "new");
  const options = choices.map((s) => el("option", { value: s, selected: s === value }, label(s)));
  // Statuses only jobflow sets: shown, but not a choice.
  if (!choices.includes(value)) {
    options.unshift(el("option", { value, selected: true, disabled: true }, label(value)));
  }
  return el("select", { "aria-label": "Status",
    onchange: (event) => setPending(job, "status", event.target.value) }, options);
}
// Where each page starts in state.jobs: every pageSize rows, except that similar jobs, and
// the same ad in other countries, stay together on one page (so a page can run longer).
export function pageStarts() {
  const starts = [0];
  let count = 0;
  state.jobs.forEach((job, i) => {
    const prev = state.jobs[i - 1];
    const continues = prev && ((job.group != null && job.group === prev.group)
      || (job.area != null && job.area === prev.area));
    if (count >= state.pageSize && !continues) { starts.push(i); count = 0; }
    count++;
  });
  return starts;
}

// ---- Pending (unsaved) changes ---------------------------------------------------------
export function current(job, field) {
  const pending = state.pending.get(job.id);
  return pending && field in pending ? pending[field] : job[field];
}
export function setPending(job, field, value) {
  stagePending(job, field, value);
  renderSaveBar();
  page.renderRows();
  // Rebuilding the drawer while typing a note would lose the cursor.
  if (field !== "note" && state.detail && state.detail.id === job.id) page.renderDetail();
}
export function stagePending(job, field, value) {
  const pending = { ...(state.pending.get(job.id) || {}) };
  if (isSaved(field, value, job[field])) delete pending[field];
  else pending[field] = value;
  if (Object.keys(pending).length) {
    state.pending.set(job.id, pending);
    if (!state.versions.has(job.id) && job.version) state.versions.set(job.id, job.version);
  } else {
    state.pending.delete(job.id);
    state.versions.delete(job.id);
  }
}
export function renderSaveBar() {
  const n = state.pending.size;
  const time = state.loadedAt
    && state.loadedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const loaded = time ? `Updated ${time}` : "";
  document.getElementById("unsaved").textContent =
    n ? `${n} unsaved change${n > 1 ? "s" : ""}` : loaded;
  document.getElementById("save").disabled = !n;
  document.getElementById("discard").disabled = !n;
  // The detail panel covers the header, so it has its own Save button.
  const drawerSave = document.getElementById("d-save");
  if (drawerSave) drawerSave.disabled = !n;
  const drawerUnsaved = document.getElementById("d-unsaved");
  if (drawerUnsaved) drawerUnsaved.textContent = n ? `${n} unsaved` : "";
}
export async function save(force = false) {
  // Ctrl+S while a save is under way would send the same changes again, and their now
  // outdated versions would come back as a conflict.
  if (state.saving) return;
  state.saving = true;
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
    await page.refresh();
    if (state.detail) await page.openDetail(state.detail.id);
  } catch (error) {
    if (error.status === 409 && error.data && error.data.conflicts) {
      state.saving = false;   // resolveConflicts may save again
      return await resolveConflicts(error.data.conflicts);
    }
    toast(error.message, true);
  } finally {
    state.saving = false;
    renderSaveBar();
  }
}
// Nothing was saved: some jobs changed elsewhere after this page loaded them.
async function resolveConflicts(conflicts) {
  const lines = conflicts.map((c) => `• #${c.id} ${c.title}${c.company ? ` (${c.company})` : ""}: `
    + `now ${label(c.status)}${c.follow_up_on ? `, follow up ${fmtDate(c.follow_up_on)}` : ""}`
    + (c.changed_via ? `, last changed via ${c.changed_via}` : ""));
  const one = conflicts.length === 1;
  const overwrite = confirm(`${one ? "This job has" : "These jobs have"} changed since you `
    + `loaded ${one ? "it" : "them"}, so nothing was saved:\n\n${lines.join("\n")}\n\n`
    + "OK: save your changes anyway (replacing the status and follow-up; notes are added).\n"
    + "Cancel: keep your drafts and load the latest data to review; Save again to save "
    + "them over it.");
  if (overwrite) return save(true);
  // You've now seen these changes: the next Save goes over them, not into this conflict again.
  conflicts.forEach((c) => { if (state.versions.has(c.id)) state.versions.set(c.id, c.version); });
  toast("Nothing saved. Showing the latest data; your drafts are kept.");
  await page.refresh();
  if (state.detail) await page.openDetail(state.detail.id);
}
window.addEventListener("beforeunload", (event) => {
  if (state.pending.size) { event.preventDefault(); event.returnValue = ""; }
});
export async function copyCommand(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied: " + text); }
  catch (error) { toast("Copy failed; select the command and copy it instead.", true); }
}
