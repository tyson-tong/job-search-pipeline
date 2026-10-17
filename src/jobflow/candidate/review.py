"""What tailor-cv and cover-letter share: a hiring-manager reviewer's revision, kept only
if it passes the fact check at least as well as the first draft; the review file written
beside the draft; and the record of the run."""
from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from .. import db
from ..llm import LLMError
from . import documents
from .factcheck import Checked


class Revised(NamedTuple):
    """What revise kept: the draft or the reviewer's revision, its fact check, the
    reviewer's critique of the first draft, and notes on what happened, for the review."""
    draft: Any
    checked: Checked
    critique: list[str]
    notes: list[str]


@dataclass(frozen=True, kw_only=True)
class Reviewer:
    """How one kind of draft (a CV or a letter) is reviewed: review(draft) returns (the
    reviewer's answer, the reviewer's model); revision_of(answer) is the revised draft in
    it, or None if there is none; check(revision) fact-checks a revision."""
    review: Callable[[Any], tuple[dict, str]]
    revision_of: Callable[[dict], Any]
    check: Callable[[Any], Checked]


def revise(draft, checked: Checked, reviewer: Reviewer) -> Revised:
    """The reviewer's revision of `draft` if it has fewer fact-check errors than the
    draft's own check, or as many and no more warnings; else the draft. A failed review
    keeps the draft, unless the provider will refuse every request (a fatal LLMError),
    which is raised."""
    critique: list[str] = []
    notes: list[str] = []
    try:
        answer, model = reviewer.review(draft)
        notes.append(f"Reviewed by model {model}.")
        points = answer.get("critique") or []
        # Asked for a list; one string would otherwise be listed a character at a time.
        points = [points] if isinstance(points, str) else points
        critique = [str(c) for c in points if str(c).strip()]
        revision = reviewer.revision_of(answer)
        if revision is not None:
            rechecked = reviewer.check(revision)
            # Fewer errors wins; with as many, fewer warnings (new terms to confirm), so a
            # revision can't slip in unsupported terms while the errors stay level.
            if (len(rechecked.errors), len(rechecked.warnings)) \
                    <= (len(checked.errors), len(checked.warnings)):
                return Revised(revision, rechecked, critique, notes)
            notes.append("The reviewer's revision failed the fact check, so the first draft "
                         "is kept." if len(rechecked.errors) > len(checked.errors) else
                         "The reviewer's revision added terms not in the source (more "
                         "warnings than the first draft), so the first draft is kept.")
    except Exception as exc:
        # A bad reviewer key or model, or no credit left, fails every job the same way:
        # stop the batch rather than write unreviewed drafts for all of them.
        if isinstance(exc, LLMError) and exc.fatal:
            raise
        print(f"warning: review failed ({exc}); keeping the first draft", file=sys.stderr)
        notes.append(f"The review failed ({exc}), so this is the unreviewed first draft.")
    return Revised(draft, checked, critique, notes)


def write_review(path: Path, *, title: str, intro: list[str], notes: list[str],
                 checked: Checked, critique: list[str], what: str,
                 extra: Sequence[str] = ()) -> None:
    """The review, as Markdown at `path` and a .docx copy beside it: the intro lines, the
    notes, the fact check's errors and warnings, the reviewer's critique of the first draft
    (`what`, the CV or the letter, is what the reviewer revised), then any `extra` lines."""
    errors, warnings = checked
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


def record_run(job_id: int, kind: str, needs_fix: bool, model: str) -> None:
    """Record the finished run: NEEDS-FIX if its draft can't be used as it is (it failed
    the fact check, or RenderCV couldn't render it), so the next run drafts the job again
    (unless you fix it yourself), else SUCCEEDED."""
    with db.connect() as conn:
        db.record_workflow_run(conn, job_id, kind, "NEEDS-FIX" if needs_fix else "SUCCEEDED",
                               model)
