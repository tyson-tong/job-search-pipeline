"use strict";
// The job panel (the drawer a job's title opens): its details, scores, next steps, similar
// jobs, and edits, which go through the same unsaved changes as the list (common.js).

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
  drawer.hidden = false;
  fill(drawer, [
    el("button", { className: "close", "aria-label": "Close", onclick: closeDetail }, "×"),
    detailHeader(job, target),
    el("div", { className: "edit" },
      el("label", { for: "d-status" }, "Status"),
      Object.assign(statusSelect(target), { id: "d-status" }),
      el("label", { for: "d-follow" }, "Follow up"),
      el("input", { id: "d-follow", type: "date", value: current(target, "follow_up_on") ? fmtDate(current(target, "follow_up_on")) : "",
        onchange: (event) => setPending(target, "follow_up_on", event.target.value) }),
      el("textarea", { placeholder: "Add a note (dated when saved), e.g. spoke to recruiter, salary discussed…",
        maxlength: state.summary.max_note, oninput: (event) => setPending(target, "note", event.target.value) },
        current(target, "note") || "")),
    job.notes ? [el("h3", {}, "Notes"), el("div", { className: "notes" }, job.notes)] : null,
    matchSection(job),
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
// The panel's top: title, facts, links to each site, and warnings.
function detailHeader(job, target) {
  const inProgress = job.duplicates.filter((d) => state.summary.in_progress.includes(d.status));
  return [
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
    job.status === "filtered" && job.filter_reason ? el("div", { className: "banner" }, `Filtered on import: ${job.filter_reason}`) : null];
}
// The job's scores against each CV, with Score or Rescore when there's something to do.
// Only a saved Inbox or Interested job is scored here, once against each CV; any job you're
// pursuing (Interested onwards) is rescored.
function matchSection(job) {
  // The CVs named in config/profile.toml this job has no score against yet.
  const unscored = state.summary.cv_variants.filter((v) => !job.matches.some((m) => m.variant === v));
  // The CVs edited since this job was scored against them.
  const outdated = job.matches.filter((m) => m.outdated).map((m) => m.variant);
  const busy = Boolean(state.scoring && state.scoring.running);
  const scorable = ["new", "interested"].includes(job.status);
  const pursuing = state.summary.pursuing.includes(job.status);
  return [
    el("div", { className: "h3-row" }, el("h3", {}, "Match with your CVs"),
      scorable && unscored.length
        ? el("button", { className: "btn small", disabled: busy,
            title: "Ask the LLM to compare this job with each CV", onclick: () =>
              startScoring({ job_ids: [job.id] }) }, "Score")
        : pursuing && outdated.length
        ? el("button", { className: "btn small", disabled: busy,
            title: "Ask the LLM to compare this job again with each CV you've edited since", onclick: () =>
              startScoring({ job_ids: [job.id], rescore: true }) }, "Rescore")
        : null),
    job.matches.map(renderMatch),
    scorable && unscored.length
      ? el("p", { className: "meta" }, `Not scored yet against ${unscored.join(", ")}.`)
      : !job.matches.length ? el("p", { className: "meta" },
          pursuing ? "Not scored: the tracker scores Inbox and interested jobs; tailor-cv and "
            + "cover-letter score a job they're given with --job."
            : "Not scored: only Inbox jobs and jobs you're pursuing are scored.") : null];
}
// The commands for this job, by its saved status: tailor-cv and cover-letter work on any job
// you're pursuing (with --job; on their own, on Interested ones), interview-prep on
// Interviewing ones.
function nextSteps(job) {
  const drafts = [["tailor-cv", "Tailored CV draft"], ["cover-letter", "Cover letter (optional)"]];
  const steps = state.summary.pursuing.includes(job.status)
    ? drafts.concat(job.status === "interviewing" ? [["interview-prep", "Interview briefing"]] : []) : null;
  if (!steps) return el("p", { className: "meta", style: "margin:4px 0 0" },
    "tailor-cv and cover-letter work on jobs you're pursuing (Interested onwards), and interview-prep "
    + "on Interviewing ones: save one of those statuses to get its command here.");
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
