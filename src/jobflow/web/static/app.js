"use strict";
// The job list: loading it, its tabs, columns, filters, paging, bulk selection, and
// scoring, then the page's wiring and start. Uses common.js and detail.js.

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
// Jobs in the Inbox, Filtered, and Not interested aren't followed up, and only Inbox jobs
// you pick (with Score selected) are scored there, so those sorts (and, for Filtered,
// Recently added) don't apply.
const HIDDEN_SORTS = { inbox: ["follow_up"], filtered: ["score", "added", "follow_up"],
  not_interested: ["score", "follow_up"] };
// The tabs with a Match column: My jobs, and the Inbox, where jobs you pick can be scored.
const SCORED_TABS = ["jobs", "inbox"];
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
  // Only My jobs and the Inbox have scored jobs; elsewhere a minimum match would hide every job.
  COLUMN_FILTERS.forEach(([input, param]) => {
    if (input.value && !(param === "score_min" && !SCORED_TABS.includes(state.view))) params.set(param, input.value); });
  params.set("sort", sortInput.value);
  return params;
}
// Each refresh's number: a slower, older one (two quick filter clicks, say) must not land
// after a newer one, showing jobs for a filter no longer chosen.
let refreshes = 0;
async function refresh() {
  const mine = ++refreshes;
  try {
    if (state.view === "searches") {
      const [summary, searches] = await Promise.all([api("/api/summary"), api("/api/searches")]);
      if (mine !== refreshes) return;
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
    if (mine !== refreshes) return;
    state.summary = summary;
    state.jobs = jobs;
    state.countryCounts = countries;
    const key = state.view + "?" + filterParams();
    if (key !== state.pageKey) { state.page = 0; state.pageKey = key; }
    state.lastPicked = null;  // rows may have moved
    state.loadedAt = new Date();
    renderAll();
  } catch (error) {
    if (mine === refreshes) toast(error.message, true);
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
// Only alerts: follow-ups overdue, an import that hasn't run lately, and sites whose runs
// failed lately; the status counts are in My jobs' menu.
function renderPipeline() {
  const box = document.getElementById("pipeline");
  // The daily import searches the past 24 hours, so a day it missed is jobs never seen.
  const { overdue, last_import: lastImport, failed_runs: failedRuns, late_after_hours: lateAfter } = state.summary;
  const hours = lastImport ? (Date.now() - new Date(lastImport)) / 3.6e6 : Infinity;
  fill(box, [
    overdue ? el("button", { className: "chip alert", title: "Jobs with a follow-up date in the past",
      onclick: () => { state.view = "jobs"; sortInput.value = "follow_up"; remember(); refresh(); } },
      "Follow-ups overdue ", el("b", {}, overdue)) : null,
    hours > lateAfter ? el("span", { className: "chip alert",
      title: "The daily import searches the past 24 hours, so jobs posted on a day it missed "
        + "aren't imported. Run `uv run jobflow status` to see why, and "
        + "`uv run jobflow ingest --posted-within-days 7` (or 14 or 30) to catch up." },
      !lastImport ? "No import yet" : hours < 72 ? `Last import ${Math.floor(hours)} hours ago`
        : `Last import ${Math.floor(hours / 24)} days ago`) : null,
    // A site whose runs failed lately, even when the other sites' succeeded.
    failedRuns.map((f) => el("span", { className: "chip alert",
      title: `Latest error: ${f.error || "unknown"}. Run \`uv run jobflow status\`, or see the `
        + "scraper_runs table, for details." },
      `${SITE_NAMES[f.site] || f.site}: ${f.failed} of ${f.total} run${f.total === 1 ? "" : "s"} failed`))]);
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
// filters. This page: just the rows on this page, in the same order.
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
  const selectable = state.view === "inbox" || RETURN_TABS.includes(state.view);
  const loaded = new Set(state.jobs.map((job) => job.id));
  state.selected.forEach((id) => { if (!selectable || !loaded.has(id)) state.selected.delete(id); });
  // After a save or refresh removes rows, stay on the last page that still exists.
  const starts = pageStarts();
  state.page = Math.min(state.page, starts.length - 1);
  const first = starts[state.page], end = starts[state.page + 1] ?? n;
  renderPager(starts);
  renderExports(first, end);
  renderBulk();
  // Only My jobs holds jobs that are followed up, and only it and the Inbox hold scored
  // jobs, so the other tabs have no Follow up or Match column.
  const scored = SCORED_TABS.includes(state.view);
  document.querySelectorAll(".fu-col").forEach((cell) => { cell.hidden = selectable; });
  document.querySelectorAll(".match-col").forEach((cell) => { cell.hidden = !scored; });
  if (!n) {
    // Job, Country, Sites, Posted, Salary, and Status, with the tab's optional columns.
    const columns = 6 + selectable + scored + !selectable;
    fill(body, [el("tr", {}, el("td", { colspan: columns, className: "empty" }, emptyMessage(total)))]);
    return;
  }
  fill(body, state.jobs.slice(first, end).map((job, offset) => jobRow(job, first + offset, selectable)));
}
function emptyMessage(total) {
  const none = nothingChosen();
  return none ? `No ${none} chosen: pick some in the ${WHERE_CHOSEN[none]}.`
    : state.view === "inbox" && !total ? "Inbox is empty: no new jobs to review."
    : state.view === "filtered" && !total ? "No filtered jobs."
    : state.view === "not_interested" && !total ? "No jobs marked not interested."
    : "No jobs match these filters.";
}
// One job's row; index is its place in state.jobs, for shift-click ranges and grouping.
function jobRow(job, index, selectable) {
  const grouped = job.group != null, abroad = job.area != null;
  const groupStart = grouped && (index === 0 || state.jobs[index - 1].group !== job.group);
  const areaStart = abroad && (index === 0 || state.jobs[index - 1].area !== job.area);
  return el("tr", { className: [state.pending.has(job.id) ? "dirty" : "", grouped ? "grouped" : "",
    groupStart ? "group-start" : "", abroad ? "abroad" : "", areaStart ? "area-start" : ""].filter(Boolean).join(" "),
    title: grouped ? "Similar jobs, shown together"
      : abroad ? "The same ad in another country, shown together" : null },
    selectable ? el("td", { className: "sel", onclick: (event) => pick(job.id, index, event) },
      el("input", { type: "checkbox", checked: state.selected.has(job.id), "aria-label": `Select ${job.title}` })) : null,
    SCORED_TABS.includes(state.view) ? el("td", {}, scoreBadge(job)) : null,
    el("td", {}, jobSummary(job)),
    el("td", { className: "hide-sm meta", title: state.countryCounts.names[job.country] || null }, job.country || "–"),
    el("td", { className: "hide-sm" }, job.sites.map((s) => el("span", { className: "site" }, SITE_NAMES[s] || s))),
    el("td", { className: "hide-sm meta" }, fmtDate(job.posted_at) || fmtDate(job.first_seen_at)),
    el("td", { className: "hide-sm meta" }, fmtSalary(job.salary_min, job.salary_max, job.currency)),
    el("td", {}, statusCell(job)),
    selectable ? null : el("td", { className: "hide-sm" }, el("input", { type: "date", "aria-label": "Follow-up date",
      value: current(job, "follow_up_on") ? fmtDate(current(job, "follow_up_on")) : "",
      onchange: (event) => setPending(job, "follow_up_on", event.target.value) })));
}
function scoreBadge(job) {
  const score = job.score == null ? null : Math.round(job.score);
  const scoreClass = score == null ? "" : score >= 70 ? " hi" : score >= 45 ? " mid" : " lo";
  return el("span", { className: "score" + scoreClass,
    title: (job.cv_variant ? `Best CV: ${job.cv_variant}`
      : state.view === "inbox" ? "Not scored: tick it and click Score selected to score it"
      : "Not scored yet")
      + (job.outdated.length ? `. Scored against an earlier version of ${job.outdated.join(" and ")}: `
        + "Rescore edited CVs updates it" : "") },
    score == null ? "–" : score + (job.outdated.length ? "*" : ""));
}
// The title (which opens the job panel), employer and place, and the row's flags.
function jobSummary(job) {
  return [
    el("button", { className: "title-btn", onclick: () => openDetail(job.id) }, job.title),
    el("div", { className: "meta" }, [job.company, job.location].filter(Boolean).join(" · ")),
    state.view === "filtered" && job.filter_reason ? el("div", { className: "meta" }, `Filtered: ${job.filter_reason}`) : null,
    el("div", { className: "flags" },
      duplicateFlag(job),
      job.elsewhere.length ? el("span", { className: "flag", title: "The same role advertised in another country; shown alongside" },
        `Same ad in ${[...new Set(job.elsewhere.map((d) => d.country || "another country"))].join(", ")}`) : null,
      job.overdue ? el("span", { className: "flag warn" }, "Follow-up overdue") : null,
      job.stale ? el("span", { className: "flag" }, "Applied 3+ weeks ago") : null,
      job.has_notes || current(job, "note") ? el("span", { className: "flag" }, "Notes") : null)];
}
// The Inbox, Filtered, and Not interested tabs' one-click buttons, else the status menu.
function statusCell(job) {
  if (!QUICK_STATUSES[state.view]) return statusSelect(job);
  return el("div", { className: "quick" }, QUICK_STATUSES[state.view].map((s) => {
    const on = current(job, "status") === s;
    // Clicking the chosen one again keeps the job where it is.
    return el("button", { className: "btn small" + (on ? " on" : ""), "aria-pressed": String(on),
      onclick: () => setPending(job, "status", on ? job.status : s) }, label(s));
  }));
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
      el("button", { className: "btn small", onclick: () => markSelected("not_interested") }, "Mark not interested"),
      el("button", { className: "btn small", disabled: Boolean(state.scoring && state.scoring.running),
        title: "Ask the LLM to compare the ticked jobs with each of your CVs, to help you decide",
        onclick: scoreSelected }, "Score selected")]
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
  // Score all jobs scores the jobs you're pursuing, which live in My jobs.
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
// Inbox jobs you tick, scored to help you triage them; each keeps its score if you mark it
// Interested.
function scoreSelected() {
  const ids = state.jobs.filter((job) => state.selected.has(job.id)).map((job) => job.id);
  const cvs = state.summary.cv_variants.length;
  startScoring({ job_ids: ids }, `Score ${ids.length} selected job${ids.length === 1 ? "" : "s"} against each of `
    + `your CVs it has no score for?\n\nThis makes at most ${ids.length * cvs} LLM call${ids.length * cvs === 1 ? "" : "s"}. `
    + "You can keep working while it runs.");
}
document.getElementById("score-btn").addEventListener("click", () => {
  const jobs = state.summary.unscored, cvs = state.summary.cv_variants.length;
  startScoring({}, `Score ${jobs} job${jobs === 1 ? "" : "s"} you're pursuing against each of your CVs it has no score for?`
    + `\n\nThis makes at most ${jobs * cvs} LLM call${jobs * cvs === 1 ? "" : "s"}, newest jobs first. You can keep working while it runs.`);
});
document.getElementById("rescore-btn").addEventListener("click", () => {
  const { jobs, calls } = state.summary.outdated;
  startScoring({ rescore: true }, `Rescore ${jobs} job${jobs === 1 ? "" : "s"} you're pursuing against the CVs you've edited `
    + `since ${jobs === 1 ? "it was" : "they were"} scored?\n\nThis makes ${calls} LLM call${calls === 1 ? "" : "s"}`
    + " (and scores any CV a job has no score against yet). Scores against CVs you haven't edited are kept.");
});

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
(async () => {
  recall();
  if (!state.statuses) {
    try { state.statuses = new Set((await api("/api/summary")).default_statuses); }
    catch (error) { toast(error.message, true); return; }
  }
  await refresh();
  try { state.scoring = await api("/api/scoring"); } catch (error) { return; }
  renderScoring();
  if (state.scoring.running) pollScoring();
})();
