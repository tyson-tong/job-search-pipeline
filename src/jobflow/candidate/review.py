"""What tailor-cv and cover-letter share: a hiring-manager reviewer's revision, kept only
if it passes the fact check at least as well as the first draft; the review file written
beside the draft; and the record of the run."""
from __future__ import annotations

import sys
from pathlib import Path

from .. import db
from ..llm import LLMError
from . import documents


def revise(draft, checked: tuple[list[str], list[str]], review, revision_of, check,
           notes: list[str]):
    """The reviewer's revision of `draft` if it has no more fact-check errors than the
    draft's own check, else the draft. review(draft) returns (the reviewer's answer, the
    reviewer's model); revision_of(answer) is the revised draft, or None if there is none;
    check(revision) is its (errors, warnings). What happened is added to `notes`; a failed
    review keeps the draft, unless the provider will refuse every request (a fatal
    LLMError), which is raised. Returns (draft or revision, its (errors, warnings), critique)."""
    critique: list[str] = []
    try:
        answer, reviewer = review(draft)
        notes.append(f"Reviewed by model {reviewer}.")
        points = answer.get("critique") or []
        # Asked for a list; one string would otherwise be listed a character at a time.
        points = [points] if isinstance(points, str) else points
        critique = [str(c) for c in points if str(c).strip()]
        revision = revision_of(answer)
        if revision is not None:
            rechecked = check(revision)
            if len(rechecked[0]) <= len(checked[0]):
                return revision, rechecked, critique
            notes.append("The reviewer's revision failed the fact check, so the first draft "
                         "is kept.")
    except Exception as exc:
        # A bad reviewer key or model, or no credit left, fails every job the same way:
        # stop the batch rather than write unreviewed drafts for all of them.
        if isinstance(exc, LLMError) and exc.fatal:
            raise
        print(f"warning: review failed ({exc}); keeping the first draft", file=sys.stderr)
        notes.append(f"The review failed ({exc}), so this is the unreviewed first draft.")
    return draft, checked, critique


def write_review(path: Path, title: str, intro: list[str], notes: list[str],
                 errors: list[str], warnings: list[str], critique: list[str], what: str,
                 extra: list[str] = ()) -> None:
    """The review, as Markdown at `path` and a .docx copy beside it: the intro lines, the
    notes, the fact-check errors and warnings, the reviewer's critique of the first draft
    (`what`, the CV or the letter, is what the reviewer revised), then any `extra` lines."""
    report = [f"# {title}", "", *intro, ""]
    report += [f"- {n}" for n in notes] + ([""] if notes else [])
    report += [f"## Fact-check errors ({len(errors)})", ""]
    report += [f"- {e}" for e in errors] or ["- None found."]
    report += ["", f"## Warnings ({len(warnings)})", ""]
    report += [f"- {w}" for w in warnings] or ["- None."]
    report += ["", "## Reviewer's critique of the first draft", ""]
    if critique:
        report += [f"The reviewer then revised the {what}; that revision is the {what} "
                   "written, unless a note above says the first draft was kept. Some points "
                   "may already be addressed.", ""]
    report += [f"- {c}" for c in critique] or ["- None."]
    report += list(extra)
    text = "\n".join(report)
    path.write_text(text + "\n")
    documents.write_review_docx(text, path.with_suffix(".docx"))


def record_run(job_id: int, kind: str, errors: list[str], model: str) -> None:
    """Record the finished run: NEEDS-FIX if its draft failed the fact check, so the next
    run drafts the job again (unless you fix it yourself), else SUCCEEDED."""
    with db.connect() as conn:
        conn.execute("INSERT INTO workflow_runs(job_id,kind,status,model_name,finished_at) "
                     "VALUES (%s,%s,%s,%s,now())",
                     (job_id, kind, "NEEDS-FIX" if errors else "SUCCEEDED", model))
