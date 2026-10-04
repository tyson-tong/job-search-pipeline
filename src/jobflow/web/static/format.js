// Labels and formatting the page's modules share. Nothing here touches the page, so the
// tests can import it under node (tests/test_web.py).
export const LABELS = { new: "New", interested: "Interested", not_interested: "Not interested", applied: "Applied",
  interviewing: "Interviewing", offered: "Offered", accepted: "Accepted", declined: "Declined",
  rejected: "Rejected", withdrawn: "Withdrawn", filtered: "Filtered", archived: "Archived" };
export const SITE_NAMES = { linkedin: "LinkedIn", indeed: "Indeed", seek: "Seek" };

export const safeUrl = (url) => /^https?:\/\//i.test(url || "") ? url : null;
export const label = (status) => LABELS[status] || status;
export const fmtDate = (value) => value ? String(value).slice(0, 10) : "";
// estimated: the ad gave no pay period, so it was guessed from the amount ("~").
export function fmtSalary(min, max, currency, estimated = false) {
  if (min == null) return "";
  const k = (n) => `${Math.round(n / 1000)}k`;
  return (estimated ? "~" : "") + (min === max ? k(min) : `${k(min)}–${k(max)}`)
    + (currency ? ` ${currency}` : "") + " /yr";
}
// Whether a draft value is what the job has saved, so it isn't an unsaved change. A note
// starts empty (it's added, not edited); a follow-up date is compared by its day; any other
// field, such as a status, whole: cut to a date's 10 characters, "not_interested" never
// matched itself, so undoing a change left one unsaved.
export function isSaved(field, value, saved) {
  const original = field === "note" ? "" : field === "follow_up_on" ? fmtDate(saved) : (saved ?? "");
  return (value ?? "") === original;
}
