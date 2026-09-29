"use strict";
// Sent in a header only, never in a URL, where it would stay in the browser's history.
const TOKEN = document.querySelector('meta[name="jobflow-token"]').content;
const LABELS = { new: "New", interested: "Interested", not_interested: "Not interested", applied: "Applied",
  interviewing: "Interviewing", offered: "Offered", accepted: "Accepted", declined: "Declined",
  rejected: "Rejected", withdrawn: "Withdrawn", filtered: "Filtered", archived: "Archived" };
const SITE_NAMES = { linkedin: "LinkedIn", indeed: "Indeed", seek: "Seek" };
const AUTO_REFRESH_MS = 3 * 60 * 1000;
// versions: each edited job's tracking version when you started editing it, sent with
// Save so changes made elsewhere in the meantime are caught instead of overwritten.
const state = { view: "jobs", statuses: new Set(["interested", "applied", "interviewing", "offered", "accepted"]), jobs: [], summary: null,
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

// ---- Loading -------------------------------------------------------------------------
const qInput = document.getElementById("q"),
  salaryInput = document.getElementById("salary"), sortInput = document.getElementById("sort");
// Column filters beyond the search, site, and minimum salary: [input, query parameter].
const COLUMN_FILTERS = [["f-score", "score_min"], ["f-posted", "posted"]].map(([id, param]) => [document.getElementById(id), param]);
function statusParams() {
  const params = new URLSearchParams();
  const statuses = TAB_STATUS[state.view] ? [TAB_STATUS[state.view]] : [...state.statuses];
  statuses.forEach((s) => params.append("status", s));
  return params;
}
// Jobs in the Inbox, Filtered, and Not interested aren't scored or followed up, so those
// sorts (and, for Filtered, Recently added) don't apply there.
const HIDDEN_SORTS = { inbox: ["score", "follow_up"], filtered: ["score", "added", "follow_up"],
  not_interested: ["score", "follow_up"] };
function fitSort() {
  const hidden = HIDDEN_SORTS[state.view] || [];
  [...sortInput.options].forEach((option) => {
    option.hidden = option.disabled = hidden.includes(option.value); });
  if (sortInput.selectedOptions[0]?.disabled) { sortInput.value = "posted"; remember(); }
}
function noStatuses() { return state.view === "jobs" && !state.statuses.size; }
// The filter with nothing ticked, if any: then the tab shows no jobs. It isn't fetched, as the
// server reads an empty list as all (or, for statuses, the default ones).
const WHERE_CHOSEN = { statuses: "Status menu", countries: "Country filter", sites: "Sites filter" };
function nothingChosen() {
  return noStatuses() ? "statuses" : state.countries && !state.countries.size ? "countries"
    : state.sites && !state.sites.size ? "sites" : null;
}
function filterParams() {
  const params = statusParams();
  (state.countries || []).forEach((c) => params.append("country", c));
  (state.sites || []).forEach((s) => params.append("site", s));
  if (qInput.value.trim()) params.set("q", qInput.value.trim());
  if (salaryInput.value) params.set("salary_min_usd", salaryInput.value);
  // Only My jobs has scored jobs; elsewhere a minimum match set there would hide every job.
  COLUMN_FILTERS.forEach(([input, param]) => {
    if (input.value && !(param === "score_min" && state.view !== "jobs")) params.set(param, input.value); });
  params.set("sort", sortInput.value);
  return params;
}
async function refresh() {
  try {
    if (state.view === "searches") {
      const [summary, searches] = await Promise.all([api("/api/summary"), api("/api/searches")]);
      state.summary = summary; state.searches = searches; state.loadedAt = new Date();
      renderAll();
      return;
    }
    fitSort();
    const before = state.countryCounts;
    const [summary, jobs, countries] = await Promise.all([api("/api/summary"),
      nothingChosen() ? [] : api("/api/jobs?" + filterParams()),
      noStatuses() ? { counts: {}, names: before ? before.names : {}, unknown: before && before.unknown, sites: {} }
        : api("/api/countries?" + statusParams())]);
    state.summary = summary;
    state.jobs = jobs;
    state.countryCounts = countries;
    const key = state.view + "?" + filterParams();
    if (key !== state.pageKey) { state.page = 0; state.pageKey = key; }
    state.lastPicked = null;  // rows may have moved
    state.loadedAt = new Date();
    renderAll();
  } catch (error) {
    toast(error.message, true);
  }
}

// ---- Rendering -------------------------------------------------------------------------
function renderAll() {
  const searching = state.view === "searches";
  document.getElementById("filters").hidden = searching;
  document.getElementById("jobs-table").hidden = searching;
  if (searching) document.getElementById("bulk").hidden = true;
  document.getElementById("searches-view").hidden = !searching;
  renderPipeline(); renderTabs(); renderSaveBar(); renderScoring();
  if (searching) renderSearches(); else { renderStatusMenu(); renderRows(); }
}
// Only an alert, when follow-ups are overdue; the status counts are in My jobs' menu.
function renderPipeline() {
  const box = document.getElementById("pipeline");
  const { overdue } = state.summary;
  fill(box, [
    overdue ? el("button", { className: "chip alert", title: "Jobs with a follow-up date in the past",
      onclick: () => { state.view = "jobs"; sortInput.value = "follow_up"; remember(); refresh(); } },
      "Follow-ups overdue ", el("b", {}, overdue)) : null]);
}
function renderTabs() {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("on", tab.dataset.view === state.view));
  document.getElementById("inbox-count").textContent = state.summary.counts.new || 0;
  // Like the Inbox's, the total with the chosen statuses, before the other filters.
  document.getElementById("jobs-count").textContent = tabTotal("jobs");
  document.getElementById("status-filter").hidden = state.view !== "jobs";
}
// All jobs in a tab (for My jobs, with the chosen statuses), before the other filters.
function tabTotal(view) {
  const statuses = TAB_STATUS[view] ? [TAB_STATUS[view]] : [...state.statuses];
  return statuses.reduce((sum, s) => sum + (state.summary.counts[s] || 0), 0);
}
function renderStatusMenu() {
  const menu = document.getElementById("status-menu");
  const all = state.summary.statuses.filter((s) => !NOT_MY_JOBS.includes(s));
  fill(menu, [
    ...all.map((s) => el("label", {},
      el("input", { type: "checkbox", checked: state.statuses.has(s), onchange: (event) => {
        event.target.checked ? state.statuses.add(s) : state.statuses.delete(s);
        remember(); refresh(); } }),
      label(s), el("span", { className: "meta" }, ` (${state.summary.counts[s] || 0})`))),
    el("div", { className: "row" },
      el("button", { className: "btn small", onclick: () => {
        state.statuses = new Set(state.summary.default_statuses); remember(); refresh(); } }, "Default"),
      el("button", { className: "btn small", onclick: () => {
        state.statuses = new Set(all); remember(); refresh(); } }, "All"),
      el("button", { className: "btn small", onclick: () => {
        state.statuses = new Set(); remember(); refresh(); } }, "None"))]);
  const chosen = [...state.statuses].map(label);
  document.getElementById("status-summary").textContent =
    "Status: " + (!chosen.length ? "none" : chosen.length > 3 ? `${chosen.length} selected` : chosen.join(", ")) + " ▾";
}
function statusSelect(job) {
  const value = current(job, "status");
  // A new, not interested, or filtered job can only be triaged (or go back to the Inbox);
  // any other doesn't go back to New.
  const choices = ["new", "not_interested", "filtered"].includes(job.status) ? TRIAGE_STATUSES
    : state.summary.editable.filter((s) => s !== "new");
  const options = choices.map((s) => el("option", { value: s, selected: s === value }, label(s)));
  if (value === "filtered") options.unshift(el("option", { value: "filtered", selected: true, disabled: true }, "Filtered"));
  return el("select", { "aria-label": "Status", onchange: (event) => setPending(job, "status", event.target.value) }, options);
}
function duplicateFlag(job) {
  const started = job.duplicate_in_progress;
  const applying = ["interested", "applied"].includes(current(job, "status"));
  if (started && applying) {
    return el("span", { className: "flag bad", title: "A similar job is already in progress" },
      `Same role as #${started.id} (${label(started.status).toLowerCase()})?`);
  }
  // Highly similar: titles at least 60% and ads at least 80% alike; similar: the rest.
  // Similar jobs in this list sit next to this one; any others are counted by where they are.
  const counts = new Map();
  job.duplicates.forEach((d) => {
    const key = `${d.level}|${similarPlace(d) || ""}`;
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  return ["highly similar", "similar"].flatMap((level) => [...counts]
    .filter(([key]) => key.startsWith(`${level}|`))
    .map(([key, n]) => {
      const place = key.slice(level.length + 1);
      return el("span", { className: "flag warn", title: place
        ? `Similar title and ad text; not in this list (${place}). Open for details`
        : "Similar title and ad text; shown next to this job. Open for details" },
        `${n} ${level}` + (place ? ` (${place})` : ""));
    }));
}
// Where a similar job is when it isn't in the list shown: its tab, or "hidden by your
// filters" when it belongs to this tab; null when it's in the list.
function similarPlace(similar) {
  if (state.jobs.some((job) => job.id === similar.id)) return null;
  const tab = Object.keys(TAB_STATUS).find((view) => TAB_STATUS[view] === similar.status)
    || (similar.status === "archived" ? null : "jobs");
  if (tab === state.view) return "hidden by your filters";
  return { inbox: "Inbox", filtered: "Filtered", not_interested: "Not interested", jobs: "My jobs" }[tab]
    || label(similar.status);
}
function renderSortHeads() {
  document.querySelectorAll(".sort-head").forEach((head) => {
    const sort = sortInput.value, desc = head.dataset.sort;
    head.classList.toggle("on", sort === desc || sort === `${desc}_asc`);
    head.textContent = head.textContent.replace(/ [▲▼]$/, "")
      + (sort === desc ? " ▼" : sort === `${desc}_asc` ? " ▲" : "");
  });
}
// Where each page starts in state.jobs: every pageSize rows, except that similar jobs, and
// the same ad in other countries, stay together on one page (so a page can run longer).
function pageStarts() {
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
function renderPager(starts) {
  const pages = starts.length;
  document.getElementById("pager").hidden = !state.jobs.length;
  document.getElementById("page-size").value = String(state.pageSize);
  document.getElementById("page-label").textContent = `Page ${state.page + 1} of ${pages}`;
  document.getElementById("page-prev").disabled = state.page === 0;
  document.getElementById("page-next").disabled = state.page >= pages - 1;
}
// All: every job in the tab (for My jobs, with the chosen statuses), ignoring the other
// filters. Filtered: just the rows on this page, in the same order.
function renderExports(first, end) {
  const all = statusParams();
  all.set("sort", sortInput.value);
  const view = filterParams();
  state.jobs.slice(first, end).forEach((job) => view.append("id", job.id));
  const exportAll = document.getElementById("export-all"), exportView = document.getElementById("export-view");
  exportAll.dataset.path = "/api/export.csv?" + all; exportView.dataset.path = "/api/export.csv?" + view;
  // With no statuses chosen, the server would export the default ones instead of none.
  exportAll.hidden = noStatuses() || !tabTotal(state.view);
  exportView.hidden = !state.jobs.length;
}
function goToPage(page) {
  state.page = page; state.lastPicked = null; renderRows();
}
function renderRows() {
  renderSortHeads();
  renderCountryFilter();
  renderSiteFilter();
  const body = document.getElementById("rows");
  // The summary and the rows load separately, so a job saved in between can make the
  // rows outnumber the total; then there is nothing hidden to report.
  const n = state.jobs.length, total = tabTotal(state.view);
  document.getElementById("shown").textContent = n >= state.summary.max_rows
    ? `The first ${n} jobs: narrow the filters to see the rest`
    : n < total ? `${n} of ${total} jobs` : `${n} job${n === 1 ? "" : "s"}`;
  const inbox = state.view === "inbox", filtered = state.view === "filtered";
  const selectable = inbox || RETURN_TABS.includes(state.view);
  const loaded = new Set(state.jobs.map((job) => job.id));
  state.selected.forEach((id) => { if (!selectable || !loaded.has(id)) state.selected.delete(id); });
  // After a save or refresh removes rows, stay on the last page that still exists.
  const starts = pageStarts();
  state.page = Math.min(state.page, starts.length - 1);
  const first = starts[state.page], end = starts[state.page + 1] ?? n;
  renderPager(starts);
  renderExports(first, end);
  renderBulk();
  // Only My jobs holds jobs that are followed up or scored, so the other tabs have
  // no Follow up or Match column.
  document.querySelectorAll(".fu-col").forEach((cell) => { cell.hidden = selectable; });
  document.querySelectorAll(".match-col").forEach((cell) => { cell.hidden = selectable; });
  if (!n) {
    const none = nothingChosen();
    fill(body, [el("tr", {}, el("td", { colspan: selectable ? 7 : 8, className: "empty" },
      none ? `No ${none} chosen: pick some in the ${WHERE_CHOSEN[none]}.`
        : inbox && !total ? "Inbox is empty: no new jobs to review."
        : filtered && !total ? "No filtered jobs."
        : state.view === "not_interested" && !total ? "No jobs marked not interested."
        : "No jobs match these filters."))]);
    return;
  }
  fill(body, [...state.jobs.slice(first, end).map((job, offset) => {
    const index = first + offset;
    const score = job.score == null ? null : Math.round(job.score);
    const scoreClass = score == null ? "" : score >= 70 ? " hi" : score >= 45 ? " mid" : " lo";
    const statusCell = QUICK_STATUSES[state.view]
      ? el("div", { className: "quick" }, QUICK_STATUSES[state.view].map((s) => {
          const on = current(job, "status") === s;
          // Clicking the chosen one again keeps the job where it is.
          return el("button", { className: "btn small" + (on ? " on" : ""), "aria-pressed": String(on),
            onclick: () => setPending(job, "status", on ? job.status : s) }, label(s));
        }))
      : statusSelect(job);
    const grouped = job.group != null, abroad = job.area != null;
    const groupStart = grouped && (index === 0 || state.jobs[index - 1].group !== job.group);
    const areaStart = abroad && (index === 0 || state.jobs[index - 1].area !== job.area);
    return el("tr", { className: [state.pending.has(job.id) ? "dirty" : "", grouped ? "grouped" : "",
      groupStart ? "group-start" : "", abroad ? "abroad" : "", areaStart ? "area-start" : ""].filter(Boolean).join(" "),
      title: grouped ? "Similar jobs, shown together"
        : abroad ? "The same ad in another country, shown together" : null },
      selectable ? el("td", { className: "sel", onclick: (event) => pick(job.id, index, event) },
        el("input", { type: "checkbox", checked: state.selected.has(job.id), "aria-label": `Select ${job.title}` })) : null,
      selectable ? null : el("td", {}, el("span", { className: "score" + scoreClass,
        title: (job.cv_variant ? `Best CV: ${job.cv_variant}` : "Not scored: only interested jobs are scored")
          + (job.outdated.length ? `. Scored against an earlier version of ${job.outdated.join(" and ")}: `
            + "Rescore edited CVs updates it" : "") },
        score == null ? "–" : score + (job.outdated.length ? "*" : ""))),
      el("td", {},
        el("button", { className: "title-btn", onclick: () => openDetail(job.id) }, job.title),
        el("div", { className: "meta" }, [job.company, job.location].filter(Boolean).join(" · ")),
        filtered && job.filter_reason ? el("div", { className: "meta" }, `Filtered: ${job.filter_reason}`) : null,
        el("div", { className: "flags" },
          duplicateFlag(job),
          job.elsewhere.length ? el("span", { className: "flag", title: "The same role advertised in another country; shown alongside" },
            `Same ad in ${[...new Set(job.elsewhere.map((d) => d.country || "another country"))].join(", ")}`) : null,
          job.overdue ? el("span", { className: "flag warn" }, "Follow-up overdue") : null,
          job.stale ? el("span", { className: "flag" }, "Applied 3+ weeks ago") : null,
          job.has_notes || current(job, "note") ? el("span", { className: "flag" }, "Notes") : null)),
      el("td", { className: "hide-sm meta", title: state.countryCounts.names[job.country] || null }, job.country || "–"),
      el("td", { className: "hide-sm" }, job.sites.map((s) => el("span", { className: "site" }, SITE_NAMES[s] || s))),
      el("td", { className: "hide-sm meta" }, fmtDate(job.posted_at) || fmtDate(job.first_seen_at)),
      el("td", { className: "hide-sm meta" }, fmtSalary(job.salary_min, job.salary_max, job.currency)),
      el("td", {}, statusCell),
      selectable ? null : el("td", { className: "hide-sm" }, el("input", { type: "date", "aria-label": "Follow-up date",
        value: current(job, "follow_up_on") ? fmtDate(current(job, "follow_up_on")) : "",
        onchange: (event) => setPending(job, "follow_up_on", event.target.value) })));
  })]);
}

// ---- Country and site filters ----------------------------------------------------------------
// A column filter that ticks several values. options: [value, name, job count]; chosen: null
// for all (every box ticked), else the ticked values; set(next) applies a new choice.
function renderMultiFilter(id, options, chosen, short, set) {
  const values = options.map(([value]) => value);
  fill(document.getElementById(`${id}-menu`), [
    options.length ? options.map(([value, name, count]) => el("label", {},
      el("input", { type: "checkbox", checked: !chosen || chosen.has(value), onchange: (event) => {
        const next = new Set(chosen || values);
        event.target.checked ? next.add(value) : next.delete(value);
        set(values.every((v) => next.has(v)) ? null : next); } }),
      name, el("span", { className: "meta" }, ` (${count})`)))
      : el("div", { className: "meta" }, "No jobs"),
    el("div", { className: "row" },
      el("button", { className: "btn small", disabled: !chosen, onclick: () => set(null) }, "All"),
      el("button", { className: "btn small", disabled: !!chosen && !chosen.size, onclick: () => set(new Set()) }, "None"))]);
  const picked = [...(chosen || [])].map(short);
  document.getElementById(`${id}-summary`).textContent = (!chosen ? "All" : !picked.length ? "None"
    : picked.length > 3 ? `${picked.length} selected` : picked.join(", ")) + " ▾";
}
function renderCountryFilter() {
  const { counts, names, unknown } = state.countryCounts;
  const name = (code) => code === unknown ? "Unknown" : `${names[code] || code} (${code})`;
  // A choice with no jobs on this tab stays listed, so it can be unticked.
  const codes = [...new Set([...Object.keys(counts), ...(state.countries || [])])]
    .sort((a, b) => (a === unknown) - (b === unknown) || name(a).localeCompare(name(b)));
  renderMultiFilter("country", codes.map((code) => [code, name(code), counts[code] || 0]), state.countries,
    (code) => code === unknown ? "Unknown" : code, (next) => { state.countries = next; refresh(); });
}
function renderSiteFilter() {
  const counts = state.countryCounts.sites || {};
  renderMultiFilter("site", Object.entries(SITE_NAMES).map(([site, name]) => [site, name, counts[site] || 0]),
    state.sites, (site) => SITE_NAMES[site], (next) => { state.sites = next; remember(); refresh(); });
}

// ---- Selecting rows (Inbox, Filtered, Not interested) for bulk changes -------------------
function pick(id, index, event) {
  const on = !state.selected.has(id);
  const anchor = event.shiftKey && state.lastPicked !== null ? state.lastPicked : index;
  // Shift-click sets every row between the last click and this one.
  for (let i = Math.min(anchor, index); i <= Math.max(anchor, index); i++) {
    const rowId = state.jobs[i] && state.jobs[i].id;
    if (rowId === undefined) continue;
    on ? state.selected.add(rowId) : state.selected.delete(rowId);
  }
  state.lastPicked = index;
  renderRows();
}
function markSelected(status, done = `marked ${label(status).toLowerCase()}`) {
  const jobs = state.jobs.filter((job) => state.selected.has(job.id));
  jobs.forEach((job) => stagePending(job, "status", status));
  state.selected.clear();
  state.lastPicked = null;
  renderSaveBar();
  renderRows();
  if (state.detail && jobs.some((job) => job.id === state.detail.id)) renderDetail();
  toast(`${jobs.length} job${jobs.length === 1 ? "" : "s"} ${done}: click Save to keep`);
}
function pageJobs() {
  const starts = pageStarts();
  return state.jobs.slice(starts[state.page] ?? 0, starts[state.page + 1] ?? state.jobs.length);
}
function renderBulk() {
  const inbox = state.view === "inbox";
  const selectable = inbox || RETURN_TABS.includes(state.view), n = state.selected.size;
  const onPage = pageJobs(), pageSelected = onPage.filter((job) => state.selected.has(job.id)).length;
  document.getElementById("select-col").hidden = !selectable;
  document.getElementById("select-col-filter").hidden = !selectable;
  const all = document.getElementById("select-all");
  all.checked = selectable && onPage.length > 0 && pageSelected === onPage.length;
  all.indeterminate = pageSelected > 0 && pageSelected < onPage.length;
  const bar = document.getElementById("bulk");
  bar.hidden = !selectable || !n;
  if (bar.hidden) return;
  fill(bar, [el("strong", {}, `${n} selected`),
    inbox ? [el("button", { className: "btn small", onclick: () => markSelected("interested") }, "Mark interested"),
      el("button", { className: "btn small", onclick: () => markSelected("not_interested") }, "Mark not interested")]
      : el("button", { className: "btn small", onclick: () => markSelected("new", "moved to the Inbox") }, "Move to Inbox"),
    el("button", { className: "btn small", onclick: () => { state.selected.clear(); state.lastPicked = null; renderRows(); } }, "Clear selection"),
    el("span", { className: "meta" }, "Shift-click a checkbox to select a range.")]);
}
document.getElementById("select-all").addEventListener("change", (event) => {
  // Ticks or clears this page's jobs; ticks on other pages stay.
  pageJobs().forEach((job) => event.target.checked ? state.selected.add(job.id) : state.selected.delete(job.id));
  state.lastPicked = null;
  renderRows();
});

// ---- Scoring (LLM match scores run on this machine, in the background) -------------------
function renderScoring() {
  const button = document.getElementById("score-btn"), chip = document.getElementById("scoring");
  const running = state.scoring && state.scoring.running;
  const { unscored, scoring_problem: problem } = state.summary || {};
  // Interested jobs, the only ones scored, live in My jobs: saving one moves it out of the Inbox.
  button.hidden = state.view !== "jobs";
  button.disabled = running || !unscored;
  // Scores made against a CV you've edited since; only shown when there are some.
  const rescore = document.getElementById("rescore-btn"), outdated = (state.summary || {}).outdated;
  rescore.hidden = state.view !== "jobs" || !outdated || !outdated.jobs;
  rescore.disabled = running;
  if (outdated && outdated.jobs) rescore.textContent = `Rescore edited CVs (${outdated.jobs})`;
  // Only a configuration problem needs explaining, usually the CVs chosen in
  // config/profile.toml; it stops tailor-cv, cover-letter, skill-gaps, and interview-prep too.
  button.title = problem ? `Scoring unavailable: ${problem}` : "";
  const notice = document.getElementById("cv-problem");
  notice.hidden = !problem;
  if (problem) fill(notice, [el("strong", {}, "Scoring, tailor-cv, cover-letter, skill-gaps, and interview-prep can't run: "), problem]);
  chip.hidden = !running;
  if (running) chip.textContent = `Scoring ${state.scoring.done} / ${state.scoring.total}…`;
}
async function startScoring(body, confirmText) {
  if (confirmText && !confirm(confirmText)) return;
  try {
    state.scoring = await api("/api/scoring", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    renderScoring();
    if (state.scoring.running) pollScoring();
    else toast("Nothing to score.");
  } catch (error) { toast(error.message, true); }
}
async function pollScoring() {
  try { state.scoring = await api("/api/scoring"); } catch (error) { toast(error.message, true); return; }
  renderScoring();
  if (state.scoring.running) { setTimeout(pollScoring, 2000); return; }
  const { scored, errors, done, total } = state.scoring;
  toast(`Scored ${scored.length} job${scored.length === 1 ? "" : "s"}` + (errors.length
    ? `; ${errors.length} failed (${errors[0].error})` : "")
    + (done < total ? `. Stopped: the other ${total - done} would fail the same way` : ""), errors.length > 0);
  await refresh();  // unsaved drafts are kept; rows just gain their new scores
  if (state.detail && !state.pending.size) await openDetail(state.detail.id);
}
document.getElementById("score-btn").addEventListener("click", () => {
  const jobs = state.summary.unscored, cvs = state.summary.cv_variants.length;
  startScoring({}, `Score ${jobs} interested job${jobs === 1 ? "" : "s"} against each of your CVs it has no score for?`
    + `\n\nThis makes at most ${jobs * cvs} LLM call${jobs * cvs === 1 ? "" : "s"}, newest jobs first. You can keep working while it runs.`);
});
document.getElementById("rescore-btn").addEventListener("click", () => {
  const { jobs, calls } = state.summary.outdated;
  startScoring({ rescore: true }, `Rescore ${jobs} interested job${jobs === 1 ? "" : "s"} against the CVs you've edited `
    + `since ${jobs === 1 ? "it was" : "they were"} scored?\n\nThis makes ${calls} LLM call${calls === 1 ? "" : "s"}`
    + " (and scores any CV a job has no score against yet). Scores against CVs you haven't edited are kept.");
});
async function copyCommand(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied: " + text); }
  catch (error) { toast("Copy failed; select the command and copy it instead.", true); }
}

// ---- Searches (config/searches.toml: what the daily import runs) ---------------------------------
const siteName = (s) => SITE_NAMES[s] || s;
function renderSearches() {
  const data = state.searches, box = document.getElementById("searches-view");
  const header = el("div", { className: "sv-card", id: "sv-top" },
    el("h2", {}, "Your searches"),
    el("div", {}, data.ok ? el("span", { className: "ok" }, "✓ Valid") : el("span", { className: "err" }, "✗ Can't be used"),
      el("span", { className: "meta" }, `  ·  from ${data.source || "(no config/profile.toml)"}`), "  ",
      el("button", { className: "btn small", onclick: () => refresh().then(() => toast("Searches reloaded")) }, "Reload")),
    data.ok ? el("p", { className: "meta", style: "margin:8px 0 0" },
      `${data.total_runs} Apify run${data.total_runs === 1 ? "" : "s"} per import, at most ${data.total_max_results} results. `
      + "Actors charge per result. Nothing runs from this page.") : null,
    data.error ? el("div", { className: "err-box" }, data.error) : null,
    el("div", { className: "next" }, el("strong", {}, "To change your searches"),
      el("p", { className: "meta", style: "margin:4px 0" },
        "Edit the file in your editor, then click Reload to check it. After each change, run this: it checks "
        + "the file, updates the copy the daily GitHub run uses, and filters Inbox jobs from your excluded employers or matching your excluded titles:"),
      el("div", { className: "cmd" }, el("code", {}, "uv run jobflow update-searches"),
        el("button", { className: "btn small", onclick: () => copyCommand("uv run jobflow update-searches") }, "Copy")),
      el("div", { className: "cmd" }, el("code", {}, "uv run jobflow ingest --site seek"),
        el("button", { className: "btn small", onclick: () => copyCommand("uv run jobflow ingest --site seek") }, "Copy")),
      el("p", { className: "meta", style: "margin:4px 0 0" }, "The last command runs the searches now (one site here) instead of waiting for the daily run; ",
        el("strong", {}, "IT COSTS APIFY CREDITS."))));
  fill(box, [renderContents(data), header, data.searches.map(renderSearchCard), renderReference(data.reference)]);
}
// Links to each card below. The header is sticky, so scroll to just under it.
function renderContents(data) {
  const link = (id, text) => el("a", { href: `#${id}`, onclick: (event) => {
    event.preventDefault();
    const target = document.getElementById(id);
    if (target.tagName === "DETAILS") target.open = true;
    const top = target.getBoundingClientRect().top + window.scrollY - document.querySelector("header").offsetHeight - 8;
    window.scrollTo({ top, behavior: "smooth" });
  } }, text);
  return el("nav", { className: "sv-card toc", "aria-label": "Contents" },
    el("h2", {}, "Contents"),
    el("ol", {},
      el("li", {}, link("sv-top", "Your searches"),
        data.searches.length ? el("ol", {}, data.searches.map((search, i) => el("li", {}, link(`sv-search-${i}`, search.name)))) : null),
      el("li", {}, link("sv-ref", "Allowed values"),
        el("ol", {},
          el("li", {}, link("sv-ref-posted", "posted_within_days")),
          el("li", {}, link("sv-ref-support", "How each site applies each setting")),
          el("li", {}, link("sv-ref-locations", "locations: countries and cities")),
          el("li", {}, link("sv-ref-classes", "seek_classifications (Seek only)"))))));
}
function renderSearchCard(search, index) {
  const salary = search.salary_min != null || search.salary_max != null
    ? `${search.salary_min ?? "any"} – ${search.salary_max ?? "any"} (${search.salary_period}); jobs without a salary are kept` : "any";
  const item = (label, value) => [el("dt", {}, label), el("dd", {}, value)];
  return el("div", { className: "sv-card", id: `sv-search-${index}` },
    el("h2", {}, search.name),
    search.warnings.length ? el("div", { className: "flags" },
      search.warnings.map((w) => el("span", { className: "flag warn" }, w))) : null,
    el("dl", { className: "kv" },
      item("Terms", search.terms.map((t) => el("span", { className: "pill" }, t))),
      item("Locations", Object.entries(search.locations).map(([country, cities]) =>
        el("span", { className: "pill" }, `${country}: ${cities.length ? cities.join(", ") : "whole country"}`))),
      item("Posted within", search.posted_within),
      item("Results", `up to ${search.max_results} per run` + (search.max_cost_usd ? `, cost cap $${search.max_cost_usd} per run` : "")
        + (search.radius_km ? `, ${search.radius_km} km radius` : "")),
      item("Salary", salary),
      item("Job types", search.job_types.length ? search.job_types.join(", ") : "any"),
      item("Work arrangements", search.work_arrangements.length ? search.work_arrangements.join(", ") : "any"),
      item("Seek classifications", search.seek_classifications.length ? search.seek_classifications.join(", ") : "any"),
      item("Excluded titles", search.exclude_title_terms.length
        ? search.exclude_title_terms.map((t) => el("span", { className: "pill" }, t)) : "none"),
      item("Kept titles", search.include_title_terms.length
        ? [search.include_title_terms.map((t) => el("span", { className: "pill" }, t)),
           el("span", { className: "meta" }, " (override excluded titles)")] : "none"),
      item("Excluded employers", search.exclude_companies.length
        ? search.exclude_companies.map((t) => el("span", { className: "pill" }, t)) : "none")),
    search.sites.map((site) => el("div", { className: "site-block" },
      el("strong", {}, siteName(site.site)),
      el("span", { className: "meta" }, ` · ${site.runs.length} run${site.runs.length === 1 ? "" : "s"}, up to ${site.max_results_per_run} results each`
        + (site.local_filters.length ? ` · filtered after import by ${site.local_filters.join(", ").replace(/_/g, " ")}` : "")),
      site.notes.map((note) => el("div", { className: "flags" }, el("span", { className: "flag warn" }, note))),
      el("details", {}, el("summary", { className: "meta" }, `Show the exact input sent to ${site.actor}`),
        site.runs.map((r) => [el("div", { className: "meta" }, r.label),
          el("pre", { className: "json" }, JSON.stringify(r.input, null, 2))])))));
}
function renderReference(ref) {
  const table = (head, rows) => el("div", { className: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, head.map((h) => el("th", {}, h)))),
    el("tbody", {}, rows.map((r) => el("tr", {}, r.map((c) => el("td", {}, c)))))));
  const filterNames = Object.keys(ref.filter_support[ref.sites[0]]);
  // One row per classification (in bold, covering all its subclassifications), then a row
  // per subclassification: the key to use in searches.toml, and the name Seek shows.
  const classList = el("tbody");
  const drawClasses = (q) => fill(classList, ref.seek_classifications.map((c) => {
    const subs = c.subclassifications.filter((sc) => !q || `${sc.key} ${sc.name}`.toLowerCase().includes(q));
    const own = !q || `${c.key} ${c.name}`.toLowerCase().includes(q);
    if (!own && !subs.length) return null;
    return [el("tr", { className: "top" }, el("td", {}, el("code", {}, c.key)),
        el("td", {}, c.name, el("span", { className: "meta" }, " (all subclassifications)"))),
      (own && !q ? c.subclassifications : subs).map((sc) =>
        el("tr", { className: "sub" }, el("td", {}, el("code", {}, sc.key)), el("td", {}, sc.name)))];
  }));
  drawClasses("");
  return el("div", { className: "sv-card ref", id: "sv-ref" },
    el("h2", {}, "Allowed values"),
    el("dl", { className: "kv" },
      [["sites", ref.sites], ["job_types", ref.job_types], ["work_arrangements", ref.work_arrangements],
       ["salary_period", ref.salary_periods]].map(([k, v]) => [el("dt", {}, el("code", {}, k)), el("dd", {}, v.join(", "))])),
    el("h3", { id: "sv-ref-posted" }, "posted_within_days"),
    table(["Value", ...ref.sites.map(siteName)], ref.posted_within.map((w) =>
      [`${w.days} (${w.name})`, ...ref.sites.map((s) => w.sites[s] ? "✓" : "wider, then filtered")])),
    el("h3", { id: "sv-ref-support" }, "How each site applies each setting"),
    el("p", { className: "meta", style: "margin:0 0 4px" }, "search: the site filters before you pay · local: filtered after import, only when the job's value is known · unsupported: not available"),
    table(["Setting", ...ref.sites.map(siteName)], filterNames.map((f) =>
      [f.replace(/_/g, " "), ...ref.sites.map((s) => ref.filter_support[s][f])])),
    el("h3", { id: "sv-ref-locations" }, "locations: countries and cities"),
    el("p", { className: "meta", style: "margin:0 0 4px" }, "Use [] for the whole country. UK also works for GB."),
    table(["Code", "Country", "Cities", "Sites"], ref.countries.map((c) =>
      [el("code", {}, c.code), c.name, c.cities.join(", "), c.sites.map(siteName).join(", ")])),
    el("details", { id: "sv-ref-classes" }, el("summary", {}, el("strong", {}, "seek_classifications (Seek only)")),
      el("input", { type: "search", placeholder: "Filter classifications", oninput: (e) => drawClasses(e.target.value.toLowerCase()) }),
      el("div", { className: "scroll" }, el("table", { className: "classes" },
        el("thead", {}, el("tr", {}, el("th", {}, "Key (in searches.toml)"), el("th", {}, "Name on Seek"))),
        classList))));
}

// ---- Detail drawer -----------------------------------------------------------------------
async function openDetail(id) {
  try {
    const drawer = document.getElementById("drawer");
    const sameJob = state.detail && state.detail.id === id;
    const scroll = sameJob ? drawer.scrollTop : 0;
    state.detail = await api(`/api/jobs/${id}`);
    renderDetail();
    drawer.scrollTop = scroll;  // a refresh of the same job keeps your place
  } catch (error) { toast(error.message, true); }
}
function closeDetail() { state.detail = null; document.getElementById("drawer").hidden = true; }
function renderDetail() {
  const job = state.detail;
  const listed = state.jobs.find((j) => j.id === job.id);
  // Edits go through the same pending changes as the table, using the list row if shown.
  const target = listed || { ...job, duplicates: job.duplicates, duplicate_in_progress: null };
  const drawer = document.getElementById("drawer");
  const inProgress = job.duplicates.filter((d) => ["applied", "interviewing", "offered", "accepted"].includes(d.status));
  // The CVs named in config/profile.toml this job has no score against yet.
  const unscored = state.summary.cv_variants.filter((v) => !job.matches.some((m) => m.variant === v));
  // The CVs edited since this job was scored against them.
  const outdated = job.matches.filter((m) => m.outdated).map((m) => m.variant);
  drawer.hidden = false;
  fill(drawer, [
    el("button", { className: "close", "aria-label": "Close", onclick: closeDetail }, "×"),
    el("h2", {}, job.title),
    el("div", { className: "meta" }, [`Job #${job.id}`, job.company, job.location, fmtSalary(job.salary_min, job.salary_max, job.currency)]
      .filter(Boolean).join(" · ")),
    el("div", { className: "meta" }, `Posted ${fmtDate(job.posted_at) || "unknown"} · added ${fmtDate(job.first_seen_at)}`
      + (job.applied_at ? ` · applied ${fmtDate(job.applied_at)}` : "")),
    el("div", { className: "flags", style: "margin-top:8px" }, job.sources.map((s) => safeUrl(s.url)
      ? el("a", { className: "btn small", href: safeUrl(s.url), target: "_blank", rel: "noopener noreferrer" },
          `Open on ${SITE_NAMES[s.source] || s.source} ↗`)
      : el("span", { className: "flag" }, SITE_NAMES[s.source] || s.source))),
    inProgress.length > 0 && ["interested", "applied"].includes(current(target, "status"))
      ? el("div", { className: "banner" }, `This may be the same role as job #${inProgress[0].id} `
          + `(${inProgress[0].company || "unknown employer"}), which is already ${label(inProgress[0].status).toLowerCase()}.`)
      : null,
    job.status === "filtered" && job.filter_reason ? el("div", { className: "banner" }, `Filtered on import: ${job.filter_reason}`) : null,
    el("div", { className: "edit" },
      el("label", { for: "d-status" }, "Status"),
      Object.assign(statusSelect(target), { id: "d-status" }),
      el("label", { for: "d-follow" }, "Follow up"),
      el("input", { id: "d-follow", type: "date", value: current(target, "follow_up_on") ? fmtDate(current(target, "follow_up_on")) : "",
        onchange: (event) => setPending(target, "follow_up_on", event.target.value) }),
      el("textarea", { placeholder: "Add a note (dated when saved), e.g. spoke to recruiter, salary discussed…",
        maxlength: 2000, oninput: (event) => setPending(target, "note", event.target.value) },
        current(target, "note") || "")),
    job.notes ? [el("h3", {}, "Notes"), el("div", { className: "notes" }, job.notes)] : null,
    // Only a saved Interested job is scored, once against each CV.
    el("div", { className: "h3-row" }, el("h3", {}, "Match with your CVs"),
      job.status === "interested" && unscored.length
        ? el("button", { className: "btn small", disabled: Boolean(state.scoring && state.scoring.running),
            title: "Ask the LLM to compare this job with each CV", onclick: () =>
              startScoring({ job_ids: [job.id] }) }, "Score")
        : job.status === "interested" && outdated.length
        ? el("button", { className: "btn small", disabled: Boolean(state.scoring && state.scoring.running),
            title: "Ask the LLM to compare this job again with each CV you've edited since", onclick: () =>
              startScoring({ job_ids: [job.id], rescore: true }) }, "Rescore")
        : null),
    job.matches.map(renderMatch),
    job.status === "interested" && unscored.length
      ? el("p", { className: "meta" }, `Not scored yet against ${unscored.join(", ")}.`)
      : !job.matches.length ? el("p", { className: "meta" },
          "Not scored: only interested jobs are scored. Mark it interested and save to score it.") : null,
    el("div", { className: "next" }, el("strong", {}, "Next steps for this job (in a terminal)"),
      nextSteps(job)),
    similarList("Highly similar jobs", job.duplicates.filter((d) => d.level === "highly similar")),
    similarList("Similar jobs", job.duplicates.filter((d) => d.level !== "highly similar")),
    similarList("Same ad in other countries", job.elsewhere),
    job.history.length ? [el("h3", {}, "Status history"),
      el("ul", { className: "history" }, job.history.map((h) => el("li", { className: "meta" },
        `${fmtDate(h.changed_at)}: ${label(h.from_status || "new")} → ${label(h.to_status)} (${h.via})`)))] : null,
    job.description ? [el("h3", {}, "Job description"), el("div", { className: "description" }, job.description)] : null,
    el("div", { className: "drawer-actions" },
      el("span", { className: "meta", id: "d-unsaved" }),
      el("button", { className: "btn", onclick: closeDetail }, "Close"),
      el("button", { className: "btn primary", id: "d-save", onclick: () => save() }, "Save"))]);
  renderSaveBar();
}
// The commands for this job, by its saved status: tailor-cv and cover-letter work on
// Interested jobs, interview-prep on Interviewing ones.
function nextSteps(job) {
  const steps = { interested: [["tailor-cv", "Tailored CV draft"], ["cover-letter", "Cover letter (optional)"]],
    interviewing: [["interview-prep", "Interview briefing"]] }[job.status];
  if (!steps) return el("p", { className: "meta", style: "margin:4px 0 0" },
    "tailor-cv and cover-letter work on Interested jobs, and interview-prep on Interviewing ones: "
    + "save one of those statuses to get its command here.");
  return steps.map(([name, what]) => {
    const command = `uv run jobflow ${name} --job ${job.id}`;
    return el("div", { className: "cmd" }, el("span", { className: "meta", style: "min-width:120px" }, what),
      el("code", {}, command), el("button", { className: "btn small", onclick: () => copyCommand(command) }, "Copy"));
  });
}
function similarList(title, jobs) {
  return jobs.length ? [el("h3", {}, title),
    el("ul", { className: "dups" }, jobs.map((d) => el("li", {},
      el("button", { className: "title-btn", onclick: () => openDetail(d.id) }, `#${d.id} ${d.title}`),
      el("span", { className: "meta" }, ` — ${[d.company, d.location].filter(Boolean).join(", ")}, `
        + `${label(d.status)}, title ${Math.round(d.title_similarity * 100)}% and ad ${Math.round(d.similarity * 100)}% alike`))))] : null;
}
function renderMatch(m) {
  const items = (title, list, withQuotes) => list.length ? [el("div", { className: "meta", style: "margin-top:6px" }, title),
    el("ul", {}, list.map((r) => el("li", {}, `[${r.importance}] ${r.requirement}`,
      withQuotes && r.evidence && r.evidence.length ? el("span", { className: "q" }, ` — “${r.evidence.join("”; “")}”`) : null,
      r.note ? el("span", { className: "q" }, ` (${r.note})`) : null)))] : null;
  return el("div", { className: "match" },
    el("strong", {}, `${m.variant}: ${Math.round(m.score)}/100`),
    m.outdated ? el("span", { className: "meta" }, " (scored against an earlier version of this CV)") : null,
    m.rationale ? el("div", { className: "meta" }, m.rationale) : null,
    items("Matched", m.matched, true), items("Partial", m.partial, true),
    items("Not evidenced in this CV (not proof you lack it)", m.not_evidenced, false));
}

// ---- Wiring ----------------------------------------------------------------------------
document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
  state.view = tab.dataset.view; remember(); refresh(); }));
let searchTimer;
qInput.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(refresh, 250); });
salaryInput.addEventListener("change", refresh);
COLUMN_FILTERS.forEach(([input]) => input.addEventListener("change", refresh));
sortInput.addEventListener("change", () => { remember(); refresh(); });
document.getElementById("page-size").addEventListener("change", (event) => {
  // Keep the first row you were looking at on screen.
  const top = pageStarts()[state.page];
  state.pageSize = Number(event.target.value); remember();
  const starts = pageStarts();
  goToPage(Math.max(0, starts.findLastIndex((start) => start <= top)));
});
document.getElementById("page-prev").addEventListener("click", () => goToPage(state.page - 1));
document.getElementById("page-next").addEventListener("click", () => goToPage(state.page + 1));
// Clicking the Posted or Salary heading sorts by it (newest or highest first); clicking the
// same heading again reverses the order.
document.querySelectorAll(".sort-head").forEach((head) => head.addEventListener("click", () => {
  const desc = head.dataset.sort, asc = `${desc}_asc`;
  sortInput.value = sortInput.value === desc ? asc : desc;
  remember(); refresh();
}));
// The export is fetched with the token in a header, then saved from memory under the
// file name the server gives.
async function downloadCsv(event) {
  event.preventDefault();
  try {
    const response = await fetch(event.currentTarget.dataset.path, { headers: { "X-Jobflow-Token": TOKEN } });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || `Export failed (${response.status})`);
    }
    const name = (/filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "") || [])[1] || "jobs.csv";
    const url = URL.createObjectURL(await response.blob());
    el("a", { href: url, download: name }).click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { toast(error.message, true); }
}
document.getElementById("export-all").addEventListener("click", downloadCsv);
document.getElementById("export-view").addEventListener("click", downloadCsv);
document.getElementById("save").addEventListener("click", () => save());
document.getElementById("discard").addEventListener("click", () => {
  if (confirm("Discard all unsaved changes?")) {
    state.pending.clear(); state.versions.clear(); renderAll(); if (state.detail) renderDetail();
  } });
// Keep the view current without disturbing edits: refresh when you come back to the
// tab and every few minutes, but never while there are unsaved changes.
async function autoRefresh() {
  if (document.hidden || state.pending.size) return;
  await refresh();
  if (state.detail && !state.pending.size) await openDetail(state.detail.id);
}
setInterval(autoRefresh, AUTO_REFRESH_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) autoRefresh(); });
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeDetail();
  if ((event.ctrlKey || event.metaKey) && event.key === "s") { event.preventDefault(); if (state.pending.size) save(); }
});
document.addEventListener("click", (event) => {
  document.querySelectorAll("details.multi[open]").forEach((menu) => {
    if (!menu.contains(event.target)) menu.open = false; });
});
recall();
refresh().then(async () => {
  try { state.scoring = await api("/api/scoring"); } catch (error) { return; }
  renderScoring();
  if (state.scoring.running) pollScoring();
});
