// Ticking rows in the Inbox, Filtered, Not interested, and Archived tabs for a bulk change.
import { el, fill, page, pageStarts, RETURN_TABS, renderSaveBar, stagePending, state, toast } from "./common.js";
import { renderDetail } from "./detail.js";
import { label } from "./format.js";
import { scoreSelected } from "./scoring.js";

export function pick(id, index, event) {
  const on = !state.selected.has(id);
  const anchor = event.shiftKey && state.lastPicked !== null ? state.lastPicked : index;
  // Shift-click sets every row between the last click and this one.
  for (let i = Math.min(anchor, index); i <= Math.max(anchor, index); i++) {
    const rowId = state.jobs[i] && state.jobs[i].id;
    if (rowId === undefined) continue;
    on ? state.selected.add(rowId) : state.selected.delete(rowId);
  }
  state.lastPicked = index;
  page.renderRows();
}
function markSelected(status, done = `marked ${label(status).toLowerCase()}`) {
  const jobs = state.jobs.filter((job) => state.selected.has(job.id));
  jobs.forEach((job) => stagePending(job, "status", status));
  state.selected.clear();
  state.lastPicked = null;
  renderSaveBar();
  page.renderRows();
  if (state.detail && jobs.some((job) => job.id === state.detail.id)) renderDetail();
  toast(`${jobs.length} job${jobs.length === 1 ? "" : "s"} ${done}: click Save to keep`);
}
function pageJobs() {
  const starts = pageStarts();
  return state.jobs.slice(starts[state.page] ?? 0, starts[state.page + 1] ?? state.jobs.length);
}
export function renderBulk() {
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
    el("button", { className: "btn small", onclick: () => { state.selected.clear(); state.lastPicked = null; page.renderRows(); } }, "Clear selection"),
    el("span", { className: "meta" }, "Shift-click a checkbox to select a range.")]);
}
document.getElementById("select-all").addEventListener("change", (event) => {
  // Ticks or clears this page's jobs; ticks on other pages stay.
  pageJobs().forEach((job) => event.target.checked ? state.selected.add(job.id) : state.selected.delete(job.id));
  state.lastPicked = null;
  page.renderRows();
});

