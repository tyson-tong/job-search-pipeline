// The Searches tab: your config/searches.toml, the Apify runs it plans, and the allowed
// values. app.js's refresh calls renderSearches.
import { refresh } from "./app.js";
import { copyCommand, el, fill, state, toast } from "./common.js";
import { SITE_NAMES } from "./format.js";

const siteName = (s) => SITE_NAMES[s] || s;
export function renderSearches() {
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
        + "the file, updates the copy the daily GitHub run uses, filters Inbox jobs your searches would now filter, "
        + "and brings back Filtered jobs your searches would no longer filter:"),
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
