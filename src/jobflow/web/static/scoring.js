// Scoring: LLM match scores, run by the tracker on this machine in the background, with
// the page polling their progress.
import { api, el, fill, page, state, toast } from "./common.js";
import { plural } from "./format.js";

export function renderScoring() {
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
export async function startScoring(body, confirmText) {
  if (confirmText && !confirm(confirmText)) return;
  try {
    state.scoring = await api("/api/scoring", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    renderScoring();
    if (state.scoring.running) pollScoring();
    else toast("Nothing to score.");
  } catch (error) { toast(error.message, true); }
}
export async function pollScoring() {
  try { state.scoring = await api("/api/scoring"); } catch (error) { toast(error.message, true); return; }
  renderScoring();
  if (state.scoring.running) { setTimeout(pollScoring, 2000); return; }
  const { scored, errors, done, total } = state.scoring;
  toast(`Scored ${plural(scored.length, "job")}` + (errors.length
    ? `; ${errors.length} failed (${errors[0].error})` : "")
    + (done < total ? `. Stopped: the other ${total - done} would fail the same way` : ""), errors.length > 0);
  await page.refresh();  // unsaved drafts are kept; rows just gain their new scores
  if (state.detail && !state.pending.size) await page.openDetail(state.detail.id);
}
// Inbox jobs you tick, scored to help you triage them; each keeps its score if you mark it
// Interested.
export function scoreSelected() {
  const ids = state.jobs.filter((job) => state.selected.has(job.id)).map((job) => job.id);
  const cvs = state.summary.cv_variants.length;
  startScoring({ job_ids: ids }, `Score ${plural(ids.length, "selected job")} against each of `
    + `your CVs it has no score for?\n\nThis makes at most ${plural(ids.length * cvs, "LLM call")}. `
    + "You can keep working while it runs.");
}
document.getElementById("score-btn").addEventListener("click", () => {
  const jobs = state.summary.unscored, cvs = state.summary.cv_variants.length;
  startScoring({}, `Score ${plural(jobs, "job")} you're pursuing against each of your CVs it has no score for?`
    + `\n\nThis makes at most ${plural(jobs * cvs, "LLM call")}, newest jobs first. You can keep working while it runs.`);
});
document.getElementById("rescore-btn").addEventListener("click", () => {
  const { jobs, calls } = state.summary.outdated;
  startScoring({ rescore: true }, `Rescore ${plural(jobs, "job")} you're pursuing against the CVs you've edited `
    + `since ${jobs === 1 ? "it was" : "they were"} scored?\n\nThis makes ${plural(calls, "LLM call")}`
    + " (and scores any CV a job has no score against yet). Scores against CVs you haven't edited are kept.");
});

