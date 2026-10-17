"""Work on your CVs: reading and choosing them, scoring jobs against them, tailoring and
shortening a CV, cover letters, the fact checks, and skill gaps."""
import copy
import json
import os
import re
from concurrent.futures import Future

import pytest
from docx import Document

from jobflow.candidate import cover_letter
from jobflow.candidate.cover_letter import letter_fixed_by_hand
from jobflow.candidate.documents import write_docx
from jobflow.candidate.factcheck import check_letter, fact_check
from jobflow.candidate.matching import normalise_requirements, quote_in_cv, score_requirements
from jobflow.candidate.render import bullets, cut_order, without
from jobflow.candidate.skill_gaps import rank_skills
from jobflow.candidate.tailoring import cv_fixed_by_hand

CV_TEXT = """cv:
  name: Alex Example
  sections:
    experiences:
      - company: Example Corp
        highlights:
          - Built **Python** ETL pipelines processing 2M records daily
          - Presented dashboards to executives
"""

SOURCE_CV = {
    "name": "Alex Example", "email": "alex@example.com", "phone": "+61 400 000 000",
    "sections": {
        "experiences": [{"company": "Example Corp", "position": "Data Analyst",
                         "start_date": "2021-02", "end_date": "present",
                         "highlights": ["Built Python ETL pipelines processing 2M records daily",
                                        "Presented dashboards to executives using Tableau"]}],
        "education": [{"institution": "Example University", "area": "Statistics",
                       "degree": "BSc", "start_date": "2016", "end_date": "2019"}],
        "skills": [{"label": "Tools", "details": "Python, SQL, Tableau"}],
    },
}


def test_quote_verification_ignores_case_punctuation_and_markup():
    # Scores count only verified quotes: too strict a match drops real evidence and lowers scores,
    # too loose one counts a fragment ("Pres") as evidence.
    assert quote_in_cv("built python ETL pipelines", CV_TEXT)
    assert quote_in_cv("Built Python ETL pipelines processing 2M records daily.", CV_TEXT)
    assert not quote_in_cv("Built Spark pipelines", CV_TEXT)
    assert not quote_in_cv("", CV_TEXT)
    # Whole-word matching: "Pres" is not evidence of "Presented".
    assert not quote_in_cv("Pres", CV_TEXT)


def test_unverifiable_quotes_downgrade_status():
    # An LLM quote not in the CV would raise the score on evidence that doesn't exist; malformed
    # items from the LLM must be dropped, not crash scoring.
    items = normalise_requirements([
        {"requirement": "Python", "importance": "Must have", "status": "matched",
         "evidence": ["Built Python ETL pipelines"]},
        {"requirement": "Spark", "importance": "required", "status": "matched",
         "evidence": ["Built Spark pipelines"]},
        {"requirement": "Stakeholders", "importance": "nice to have", "status": "partial",
         "evidence": "Invented quote"},
        {"requirement": "Kubernetes", "importance": "preferred", "status": "not evidenced"},
        {"requirement": ""},
        "junk",
    ], CV_TEXT)
    assert [(i["requirement"], i["importance"], i["status"]) for i in items] == [
        ("Python", "required", "matched"),
        ("Spark", "required", "partial"),
        ("Stakeholders", "preferred", "not_evidenced"),
        ("Kubernetes", "preferred", "not_evidenced"),
    ]
    assert items[1]["unverified_evidence"] == ["Built Spark pipelines"]
    assert "not found verbatim" in items[1]["note"]
    assert items[2]["evidence"] == []


def _item(importance, status):
    return {"requirement": "r", "importance": importance, "status": status,
            "evidence": [], "unverified_evidence": [], "note": ""}


def test_score_is_computed_from_evidence_and_profile_weights():
    # The score is computed in code: a wrong formula ranks every job wrongly, and a job with no
    # preferred requirements must not be penalised for lacking them.
    items = [_item("required", "matched"), _item("required", "partial"),
             _item("preferred", "not_evidenced")]
    score, breakdown = score_requirements(items, 0.75, 0.25)
    # required credit 0.75, preferred 0 -> 0.75 * 0.75 = 0.5625
    assert score == 56
    assert breakdown["required"] == {"count": 2, "weight": 0.75, "credit": 0.75,
                                     "matched": 1, "partial": 1}
    # A job with no preferred requirements isn't penalised for them.
    assert score_requirements([_item("required", "matched")], 0.75, 0.25)[0] == 100


def test_fact_check_accepts_reordering_and_omitted_bullets():
    # Tailoring reorders and drops bullets: if the fact check flagged that, every draft would fail
    # it and be drafted again, paying for the LLM each time.
    draft = copy.deepcopy(SOURCE_CV)
    draft["sections"]["experiences"][0]["highlights"] = [
        "Presented dashboards to executives using Tableau"]
    draft["sections"] = {"skills": draft["sections"]["skills"],
                         "experiences": draft["sections"]["experiences"],
                         "education": draft["sections"]["education"]}
    assert fact_check(SOURCE_CV, draft) == ([], [])


def test_fact_check_flags_factual_changes():
    # A tailored CV must not change contact details, titles, dates, numbers, or education: one sent
    # with an invented fact could cost you the job.
    draft = copy.deepcopy(SOURCE_CV)
    draft["phone"] = "+61 400 999 999"
    draft["website"] = "https://invented.example"
    draft["headline"] = "Analyst with 15 years at Google"
    job = draft["sections"]["experiences"][0]
    job["position"] = "Senior Data Analyst"
    job["start_date"] = "2020-02"
    job["highlights"] = ["Built Python ETL pipelines processing 5M records daily"]
    draft["sections"]["education"] = []
    errors, _ = fact_check(SOURCE_CV, draft)
    text = "\n".join(errors)
    assert "Header field 'phone'" in text
    assert "Header field 'website' was added." in text
    assert "position: 'Senior Data Analyst'" in text
    assert "start_date: '2020-02'" in text
    assert "Education entry removed: Example University" in text
    assert "Numbers not in the source CV: 15, 2020, 5." in errors


def test_fact_check_flags_removed_employer_and_warns_on_new_terms():
    # A dropped employer leaves a gap in your history, and a new tool name may be a skill you don't
    # have.
    draft = copy.deepcopy(SOURCE_CV)
    draft["sections"]["experiences"] = []
    draft["sections"]["skills"] = [{"label": "Tools",
                                    "details": "Python, SQL, Tableau, Kubernetes"}]
    errors, warnings = fact_check(SOURCE_CV, draft)
    assert "Employer removed: Example Corp." in errors
    assert warnings and "Kubernetes" in warnings[0]


def test_fact_check_warns_on_a_new_term_that_starts_a_list_or_a_bullet():
    # The first word of a sentence was never checked, and the draft leads with what the job
    # wants: an invented skill first in a list, or a bullet, went unmentioned.
    draft = copy.deepcopy(SOURCE_CV)
    draft["sections"]["skills"] = [{"label": "Tools",
                                    "details": "Kubernetes, Python, SQL, Tableau"}]
    draft["sections"]["experiences"][0]["highlights"] = [
        "Terraform ETL pipelines processing 2M records daily", "PyTorch dashboards for executives",
        "Spearheaded dashboards for executives using Tableau"]
    _, warnings = fact_check(SOURCE_CV, draft, "Data Engineer: Terraform and Kubernetes")
    # Spearheaded is a reworded verb, not a claim, and isn't in the ad.
    assert warnings == [f"New term not in the source CV (check it's supported): {term}."
                        for term in ("Kubernetes", "PyTorch", "Terraform")]


def test_fact_check_flags_a_changed_location_and_markup_rendercv_would_run():
    # RenderCV passes Typst commands and links through: a job ad could steer the LLM into hidden
    # text or a link the PDF doesn't show. A changed location is a false fact; a dropped one isn't.
    source = copy.deepcopy(SOURCE_CV)
    source["sections"]["experiences"][0]["location"] = "Sydney"
    moved = copy.deepcopy(source)
    moved["sections"]["experiences"][0]["location"] = "London"
    assert any("Location changed" in e for e in fact_check(source, moved)[0])
    dropped = copy.deepcopy(source)
    del dropped["sections"]["experiences"][0]["location"]
    assert fact_check(source, dropped)[0] == []
    # RenderCV passes Typst commands through: hidden white text in the PDF.
    hidden = copy.deepcopy(source)
    hidden["sections"]["experiences"][0]["highlights"].append(
        "#text(fill: white)[Kubernetes expert] and [Tableau](https://evil.example)")
    errors, _ = fact_check(source, hidden)
    assert errors == ["Markup not in the source CV, which RenderCV would run or hide in the PDF: "
                      "#text(fill: white)[Kubernetes expert], ](https://evil.example)."]


def test_fact_check_flags_a_removed_role_at_an_employer_still_listed():
    # Checked by employer alone, a draft could drop one of your roles there and still pass.
    source = copy.deepcopy(SOURCE_CV)
    source["sections"]["experiences"].append(
        {"company": "Example Corp", "position": "Junior Analyst", "start_date": "2019-06",
         "end_date": "2021-01", "highlights": ["Presented dashboards to executives"]})
    draft = copy.deepcopy(source)
    draft["sections"]["experiences"].pop()
    errors, _ = fact_check(source, draft)
    assert "Role removed at Example Corp: Junior Analyst 2019-06." in errors


def test_fact_check_flags_a_date_or_title_moved_between_entries():
    # Each value exists somewhere in the source, so a field-by-field check passed both.
    source = copy.deepcopy(SOURCE_CV)
    source["sections"]["experiences"].append(
        {"company": "Other Co", "position": "Senior Manager", "start_date": "2019-01",
         "end_date": "2021-01", "highlights": ["Led a team"]})
    longer = copy.deepcopy(source)
    longer["sections"]["experiences"][1]["end_date"] = "present"   # tenure extended
    assert any("doesn't match any one entry" in e for e in fact_check(source, longer)[0])
    promoted = copy.deepcopy(source)
    promoted["sections"]["experiences"].append(
        {"company": "Example Corp", "position": "Senior Manager", "start_date": "2021-02"})
    assert any("doesn't match any one entry" in e for e in fact_check(source, promoted)[0])


def test_fact_check_flags_a_number_moved_to_another_job():
    # Every number was somewhere in the source, so "cut costs 30%" could move to another job.
    source = copy.deepcopy(SOURCE_CV)
    source["sections"]["experiences"].append(
        {"company": "Other Co", "position": "Analyst", "start_date": "2018-01",
         "end_date": "2021-01", "highlights": ["Cut reporting costs by 30%"]})
    draft = copy.deepcopy(source)
    draft["sections"]["experiences"][0]["highlights"][1] = \
        "Presented dashboards to executives using Tableau, cutting costs by 30%"
    errors, _ = fact_check(source, draft)
    assert errors == ["Numbers from another entry in the source CV (company 'Example Corp', "
                      "position 'Data Analyst', start_date '2021-02', end_date 'present'): 30."]


def test_a_draft_fixed_by_hand_is_not_drafted_again(tmp_path):
    # A job whose draft failed the fact check is drafted again, which would overwrite a
    # draft you fixed yourself. Only a changed draft counts: one saved unchanged, or copied
    # or synced with its folder, has a newer file time and once looked fixed.
    from jobflow.candidate import tailoring

    draft, review = tmp_path / "CV_dev_draft.yaml", tmp_path / "CV_REVIEW.md"
    draft.write_text("cv: {}")
    tailoring._record_written(tmp_path, draft=tailoring._digest(draft))
    review.write_text("review")
    os.utime(draft, ns=(2 * review.stat().st_mtime_ns,) * 2)  # touched, not changed
    assert not cv_fixed_by_hand(tmp_path)
    draft.write_text("cv: {name: Fixed}")
    assert cv_fixed_by_hand(tmp_path)
    # A folder from before the record is judged by file times, so a fix there is kept too.
    (tmp_path / tailoring.WRITTEN).unlink()
    os.utime(review, ns=(1, 1))   # the draft was changed after its review was written
    assert cv_fixed_by_hand(tmp_path)
    assert not letter_fixed_by_hand(tmp_path)
    (tmp_path / "cover-letter.docx").write_bytes(b"")
    assert letter_fixed_by_hand(tmp_path)


JOB = {"title": "Data Analyst", "company": "Acme",
       "description": "You will own 3 dashboards and work with Snowflake."}


def test_letter_check_flags_numbers_and_terms_from_nowhere():
    # A cover letter with a figure or skill from nowhere states something untrue; ones from the CV,
    # the ad, or the research are fine.
    research = [{"title": "Acme news", "content": "Acme was founded in 1998 in Sydney."}]
    letter = ("Dear Hiring Manager,\nAt Example Corp I built Python ETL pipelines processing "
              "2M records daily, and I'd enjoy owning your 3 dashboards at a company founded in "
              "1998. I also cut costs by 40% using Kubernetes.\nAlex Example")
    errors, warnings = check_letter(letter, SOURCE_CV, JOB, research)
    # 2M (CV), 3 (job ad), and 1998 (research) are allowed; 40 appears nowhere.
    assert errors == ["Numbers not in your CV, the job ad, or the company research: 40."]
    assert warnings == ["New term not in your CV, the job ad, or the company research (check "
                        "it's true): Kubernetes."]
    assert ("A [placeholder] is left in; fill it in or remove it."
            in check_letter("Dear [Name], I built dashboards.", SOURCE_CV, JOB, [])[1])


@pytest.fixture
def letter_llm(monkeypatch, no_database):
    """write_letter for JOB from SOURCE_CV, with no company research. Call it with the
    letter the LLM drafts and the reviewer's answer: a dict, or an exception it raises."""
    monkeypatch.setattr(cover_letter, "load_cv", lambda job_id, variant: (
        JOB, "main", "CV variant main.", SOURCE_CV, ""))
    monkeypatch.setattr(cover_letter, "research", lambda job: ([], []))

    def answers(draft: str, review: dict | Exception) -> None:
        def complete(system, user, **kw):
            if not kw.get("reviewer"):
                return draft, "m"
            if isinstance(review, Exception):
                raise review
            return json.dumps(review), "m"
        monkeypatch.setattr(cover_letter, "complete", complete)
    return answers


def test_letter_failing_the_check_is_never_written_as_a_docx(tmp_path, letter_llm):
    # A .docx looks ready to send: one with an unchecked claim, or an older one left beside a failed
    # letter, could be sent by mistake.
    letter = "Dear Hiring Manager,\n\nI cut costs by 40% at Example Corp.\n\nAlex Example"
    letter_llm(letter, {"critique": [], "cover_letter": letter})
    # A cover-letter.docx from an earlier run would look like this letter, checked.
    (tmp_path / "cover-letter.docx").write_bytes(b"old")
    target, errors, _ = cover_letter.write_letter(1, output_dir=tmp_path)
    assert errors and target.name == "COVER_LETTER_REVIEW.md"
    assert not (tmp_path / "cover-letter.docx").exists()
    assert letter in target.read_text()


def test_a_revision_with_more_fact_check_errors_is_discarded(tmp_path, letter_llm):
    # The reviewer's revision can add claims: kept regardless, it would replace a letter that passed
    # the check with one that doesn't.
    draft = "Dear Hiring Manager,\n\nAt Example Corp I built Python ETL pipelines.\n\nAlex Example"
    revised = "Dear Hiring Manager,\n\nI cut costs by 40% at Example Corp.\n\nAlex Example"
    letter_llm(draft, {"critique": ["Add a metric"], "cover_letter": revised})
    target, errors, _ = cover_letter.write_letter(1, output_dir=tmp_path)
    assert not errors and target.name == "cover-letter.docx"
    report = (tmp_path / "COVER_LETTER_REVIEW.md").read_text()
    assert "failed the fact check, so the first draft is kept" in report


def test_a_revision_adding_unsupported_terms_is_discarded():
    # Kept on error count alone, a revision with as many errors could add any number of
    # terms the CV doesn't support.
    from jobflow.candidate.factcheck import Checked
    from jobflow.candidate.review import Reviewer, revise

    def run(revision_warnings):
        kept = revise("draft", Checked([], ["New term: Python."]),
                      Reviewer(review=lambda draft: ({"critique": []}, "m"),
                               revision_of=lambda answer: "revision",
                               check=lambda revision: Checked([], revision_warnings)))
        return kept.draft, kept.notes
    assert run(["New term: Python.", "New term: Kubernetes."]) == (
        "draft", ["Reviewed by model m.", "The reviewer's revision added terms not in the "
                  "source (more warnings than the first draft), so the first draft is kept."])
    assert run(["New term: Python."])[0] == "revision"


def test_a_reviewer_every_job_would_fail_on_stops_the_batch(tmp_path, letter_llm):
    # A misspelt LLM_REVIEW_MODEL (404) was caught like a network blip: every job got an
    # unreviewed draft, and the batch went on paying for drafts.
    from jobflow.llm import LLMError

    letter_llm("Dear Hiring Manager,\n\nI built Python ETL pipelines.\n\nAlex Example",
               LLMError("The model `gpt-typo` does not exist (LLM provider, HTTP 404)", fatal=True))
    with pytest.raises(LLMError) as caught:
        cover_letter.write_letter(1, output_dir=tmp_path)
    assert caught.value.fatal and not list(tmp_path.iterdir())


def test_skill_ranking_counts_only_real_gaps_once():
    # The LLM groups gaps by id: repeated, unknown, or reused ids would count a gap twice or invent
    # one, and rank skills wrongly.
    gaps = [{"id": 0, "job_id": 1, "job": "A", "requirement": "AWS", "importance": "required",
             "status": "not_evidenced"},
            {"id": 1, "job_id": 2, "job": "B", "requirement": "Amazon Web Services",
             "importance": "preferred", "status": "partial"},
            {"id": 2, "job_id": 2, "job": "B", "requirement": "CFA", "importance": "required",
             "status": "not_evidenced"}]
    groups = [{"skill": "AWS", "gap_ids": [0, 1, 1, 99, "x"]},   # repeats and unknown ids
              {"skill": "Cloud", "gap_ids": [0]},               # id already used above
              {"skill": "CFA", "gap_ids": ["2"]}]
    skills, ungrouped = rank_skills(gaps, groups)
    assert [(s["skill"], s["weight"], s["jobs"], s["required"]) for s in skills] == [
        ("AWS", 1.25, 2, 1), ("CFA", 1.0, 1, 1)]
    assert ungrouped == 0
    assert rank_skills(gaps, "not a list") == ([], 3)


def test_shortening_never_empties_an_entry_or_rewrites_anything():
    # Cutting to fit is done in code so nothing is rewritten: an emptied entry or a changed bullet
    # would put an unchecked CV in front of an employer.
    cv = copy.deepcopy(SOURCE_CV)
    cv["sections"]["experiences"].append({"company": "Other Co", "highlights": ["Only bullet"]})
    found = bullets(cv)
    # Rank everything least relevant first, with junk and repeated ids.
    order = cut_order(found, [3, 3, 0, 1, 2, 99, "x"])
    assert [b["text"] for b in order] == ["Built Python ETL pipelines processing 2M records daily"]
    trimmed = without(cv, order)
    assert trimmed["sections"]["experiences"][0]["highlights"] == [
        "Presented dashboards to executives using Tableau"]
    assert trimmed["sections"]["experiences"][1]["highlights"] == ["Only bullet"]
    assert fact_check(cv, trimmed) == ([], [])  # only removals, so nothing to flag
    # Ids sent as digit strings count as numbers (they were once dropped, so each entry's
    # first bullet, usually its strongest, was cut first); with no ranking at all, each
    # entry's last bullets go first.
    assert [b["text"] for b in cut_order(found, ["0"])] == [
        "Built Python ETL pipelines processing 2M records daily"]
    assert [b["text"] for b in cut_order(found, None)] == [
        "Presented dashboards to executives using Tableau"]
    assert cv["sections"]["experiences"][0]["highlights"][0].startswith("Built")  # input untouched


def test_cutting_to_fit_cuts_the_fewest_least_relevant_bullets(tmp_path, monkeypatch):
    # Cutting too many bullets throws away your best evidence; cutting the wrong ones keeps the
    # least relevant.
    import yaml

    from jobflow.candidate import render

    cv = copy.deepcopy(SOURCE_CV)
    cv["sections"]["experiences"][0]["highlights"] += ["Third", "Fourth", "Fifth"]
    cv["sections"]["experiences"].append({"company": "Other Co", "highlights": ["Only bullet"]})
    # One page holds 4 bullets; there are 6. Least relevant first: 4, 3, 2, 1, 0, 5.
    monkeypatch.setattr(render, "render", lambda source: source)
    monkeypatch.setattr(render, "pdf_pages", lambda source: 1 + (len(bullets(
        yaml.safe_load(source.read_text())["cv"])) > 4))
    monkeypatch.setattr(render, "rank_bullets", lambda job, found: [4, 3, 2, 1, 0, 5])
    monkeypatch.setattr(render, "page_gaps", lambda pdf: [])
    fitted, cut, _, warnings = render.fit(JOB, {"cv": cv}, cv, "CV_dev_draft.yaml", max_pages=1)
    assert [b["text"] for b in cut] == ["Fifth", "Fourth"] and not warnings
    assert fitted["sections"]["experiences"][1]["highlights"] == ["Only bullet"]


def test_format_for_a_job_already_tailored_comes_from_its_checked_draft(tmp_path, monkeypatch):
    # tailor-cv --format docx after a PDF-only run used to skip the job as done.
    import yaml

    from jobflow.candidate import tailoring

    source = tmp_path / "CV_dev.yaml"
    source.write_text(yaml.safe_dump({"cv": SOURCE_CV}))
    monkeypatch.setattr(tailoring, "chosen_files", lambda: {"dev": source})
    folder = tmp_path / "acme" / "analyst-1"
    folder.mkdir(parents=True)
    draft = folder / "CV_dev_draft.yaml"
    draft.write_text(yaml.safe_dump({"cv": SOURCE_CV}))
    assert tailoring.add_format(folder, "docx") == folder / "CV_dev_draft.docx"
    assert tailoring.add_format(folder, "docx") is None   # already there
    # A draft edited since the .docx was written gets a new one, so no stale CV is sent.
    trimmed = copy.deepcopy(SOURCE_CV)
    trimmed["sections"]["experiences"][0]["highlights"].pop()
    draft.write_text(yaml.safe_dump({"cv": trimmed}))
    docx = folder / "CV_dev_draft.docx"
    assert tailoring.add_format(folder, "docx") == docx
    assert "Presented dashboards" not in " ".join(p.text for p in Document(docx).paragraphs)
    # A draft edited since into something the check fails never becomes a .tex.
    edited = copy.deepcopy(SOURCE_CV)
    edited["sections"]["experiences"][0]["position"] = "Head of Data"
    draft.write_text(yaml.safe_dump({"cv": edited}))
    with pytest.raises(RuntimeError, match="fails the fact check"):
        tailoring.add_format(folder, "latex")
    assert not (folder / "CV_dev_draft.tex").exists()
    # A typo in a hand-edited draft once crashed the whole run before any job started.
    draft.write_text("cv:\n  name: [Alex\n")
    with pytest.raises(RuntimeError, match="isn't valid YAML"):
        tailoring.add_format(folder, "latex")


@pytest.fixture
def tailor_llm(monkeypatch, no_database):
    """tailor_cv for JOB from SOURCE_CV (the dev CV), with no company research. Call it
    with the cv mapping the LLM drafts, which the reviewer hands back unchanged."""
    import yaml

    from jobflow.candidate import tailoring

    monkeypatch.setattr(tailoring, "load_source", lambda job_id, variant: (
        JOB, "dev", "CV variant dev.", {"cv": SOURCE_CV}, yaml.safe_dump({"cv": SOURCE_CV})))
    researched: Future = Future()
    researched.set_result(([], []))
    monkeypatch.setattr(tailoring, "research_soon", lambda job: researched)

    def drafts(cv: dict) -> None:
        draft = yaml.safe_dump({"cv": cv})
        monkeypatch.setattr(tailoring, "complete", lambda system, user, **kw: (
            json.dumps({"critique": [], "cv_yaml": draft}) if kw.get("reviewer") else draft, "m"))
    return drafts


def test_a_draft_failing_the_check_leaves_no_older_cv_beside_it(tmp_path, tailor_llm):
    # A PDF, .docx, or .tex from an earlier run would look like this draft, checked.
    from jobflow.candidate import tailoring

    invented = copy.deepcopy(SOURCE_CV)
    invented["sections"]["experiences"][0]["position"] = "Head of Data"
    tailor_llm(invented)
    for suffix in (".pdf", ".docx", ".tex"):
        (tmp_path / f"CV_dev_draft{suffix}").write_bytes(b"from an earlier run")
    # And from an earlier run with another CV (--redo --cv), which passed the check.
    for suffix in (".yaml", ".pdf"):
        (tmp_path / f"CV_strat_draft{suffix}").write_bytes(b"from an earlier run")
    target, errors, _ = tailoring.tailor_cv(1, output_dir=tmp_path, fmt="docx")
    assert errors and target == tmp_path / "CV_dev_draft.yaml"
    assert sorted(p.name for p in tmp_path.glob("CV_*_draft.*")) == ["CV_dev_draft.yaml"]


def test_a_cv_photo_is_copied_beside_the_draft_and_never_outside_it(tmp_path):
    # RenderCV reads cv.photo relative to the YAML, so every draft of a CV with a photo
    # failed to render: no PDF, and its length never checked.
    from jobflow.candidate.render import copy_photo

    cv_folder, draft_folder = tmp_path / "cv", tmp_path / "out" / "acme" / "analyst-1"
    (cv_folder / "img").mkdir(parents=True)
    draft_folder.mkdir(parents=True)
    (cv_folder / "img" / "me.png").write_bytes(b"png")
    copy_photo({"photo": "img/me.png"}, cv_folder, draft_folder)
    assert (draft_folder / "img" / "me.png").read_bytes() == b"png"
    (tmp_path / "outside.png").write_bytes(b"x")   # found, but its copy would leave the folder
    copy_photo({"photo": "../outside.png"}, cv_folder, draft_folder)
    assert not (draft_folder.parent / "outside.png").exists()


def test_the_cv_files_named_in_the_profile_are_read_as_text(tmp_path, monkeypatch):
    # A .docx or .tex CV read wrongly (LaTeX commands or comments left in) is scored against text
    # that isn't what an employer sees.
    from jobflow.candidate import cvs

    write_docx(SOURCE_CV, tmp_path / "Resume 2026.docx")
    (tmp_path / "strat-cv.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\textbf{Built} ETL 100\\% % note\n"
        "\\end{document}\n")
    monkeypatch.setattr(cvs, "cv_directory", lambda: tmp_path)
    monkeypatch.setattr(cvs, "chosen_cvs", lambda: ["Resume 2026.docx", "strat-cv.tex"])
    sources = cvs.chosen_sources()
    assert "Built Python ETL pipelines" in sources["Resume 2026"]
    assert " ".join(sources["strat-cv"].split()) == "Built ETL 100%"


def test_only_the_one_or_two_cv_files_named_in_the_profile_are_used(tmp_path, monkeypatch):
    # Each CV is one LLM call per job, sending the whole CV and ad: more CVs multiply the
    # bill, so the profile must name 1 or 2 files exactly, and nothing runs until it does.
    from jobflow.candidate import cvs
    for name in ("CV_dev.yaml", "CV_dev.pdf", "CV_harvard.yaml", "CV_strat.yaml"):
        (tmp_path / name).write_text("cv:\n  name: " + name + "\n")
    (tmp_path / "~$CV_dev.docx").write_bytes(b"Word lock file")
    monkeypatch.setattr(cvs, "cv_directory", lambda: tmp_path)

    def chosen(*names):
        monkeypatch.setattr(cvs, "chosen_cvs", lambda: list(names))
        return {variant: path.name for variant, path in cvs.chosen_files().items()}

    with pytest.raises(RuntimeError, match=re.escape('set cvs = ["CV_dev.pdf", "CV_dev.yaml"]')):
        chosen()
    assert chosen("CV_strat.yaml", "CV_dev.pdf") == {"strat": "CV_strat.yaml", "dev": "CV_dev.pdf"}
    with pytest.raises(RuntimeError, match="names 3 CVs"):
        chosen("CV_dev.yaml", "CV_harvard.yaml", "CV_strat.yaml")
    with pytest.raises(RuntimeError, match="names dev, which isn't a CV file"):
        chosen("dev")
    with pytest.raises(RuntimeError, match=re.escape("names ~$CV_dev.docx, which isn't")):
        chosen("~$CV_dev.docx")
    with pytest.raises(RuntimeError, match="same CV in two formats"):
        chosen("CV_dev.yaml", "CV_dev.pdf")


def test_a_redraft_cut_off_part_way_is_drafted_again_not_kept_as_yours(tmp_path, monkeypatch,
                                                                      tailor_llm):
    # The new draft was written beside the last run's review; cut off before its own review
    # (the balance ran out while shortening it), it looked edited by hand and was never
    # drafted again.
    from jobflow.candidate import tailoring
    from jobflow.llm import LLMError

    (tmp_path / "CV_dev_draft.yaml").write_text("cv: {}")
    (tmp_path / "CV_REVIEW.md").write_text("the last run's review")
    tailor_llm(SOURCE_CV)

    def out_of_credit(*args, **kwargs):
        raise LLMError("no credit", fatal=True)
    monkeypatch.setattr(tailoring, "fit", out_of_credit)
    with pytest.raises(LLMError):
        tailoring.tailor_cv(1, output_dir=tmp_path)
    assert not tailoring.cv_fixed_by_hand(tmp_path) and not tailoring.cv_written(tmp_path)


def test_a_draft_rendercv_cant_render_is_drafted_again(tmp_path, monkeypatch, tailor_llm):
    # Recorded as done, it had no PDF, was never fitted to its page limit, and was skipped
    # by every later run.
    from jobflow.candidate import tailoring
    from jobflow.candidate.render import RenderFailed

    tailor_llm(SOURCE_CV)

    def unrenderable(*args, **kwargs):
        raise RenderFailed("invalid date")
    monkeypatch.setattr(tailoring, "fit", unrenderable)
    recorded = []
    monkeypatch.setattr(tailoring, "record_run", lambda job_id, kind, needs_fix, model:
                        recorded.append(needs_fix))
    target, errors, _ = tailoring.tailor_cv(1, output_dir=tmp_path)
    assert not errors and recorded == [True]
    # The draft is kept, so a fix by hand is still recognised.
    assert target.exists() and not tailoring.cv_fixed_by_hand(tmp_path)


def test_a_render_stopped_by_ctrl_c_fails_the_job(tmp_path, monkeypatch):
    # Ctrl+C stops RenderCV too; taken for a draft RenderCV couldn't render, the job was
    # recorded as done, with no PDF and never shortened to its page limit.
    import subprocess

    from jobflow.candidate import render
    # Killed by the signal while starting, or exiting with 130 once RenderCV catches it.
    for code in (-2, 130):
        monkeypatch.setattr(render.subprocess, "run",
                            lambda *a, code=code, **k: subprocess.CompletedProcess(a, code, "", ""))
        with pytest.raises(RuntimeError, match="stopped") as caught:
            render.render(tmp_path / "CV_dev_draft.yaml")
        assert not isinstance(caught.value, render.RenderFailed)


def test_rendercv_renders_a_draft_where_tailor_cv_expects_it(tmp_path):
    # Every other test fakes RenderCV. An upgrade that changed its command line (--pdf-path,
    # -nomd) would leave every tailored CV "couldn't render", with no test failing.
    from jobflow.candidate import render

    _, cut, pdf, warnings = render.fit(JOB, {"cv": SOURCE_CV}, copy.deepcopy(SOURCE_CV),
                                       "CV_dev_draft.yaml", max_pages=1, keep_pdf=tmp_path)
    assert pdf == tmp_path / "CV_dev_draft.pdf" and render.pdf_pages(pdf) == 1
    assert not cut and not warnings


def test_a_design_change_to_a_yaml_cv_is_not_an_edit(tmp_path, monkeypatch):
    # Only the cv section is scored. A new theme or font marking every score out of date
    # would have Rescore edited CVs pay to redo them all.
    import yaml

    from jobflow.candidate import cvs, matching

    monkeypatch.setattr(cvs, "cv_directory", lambda: tmp_path)
    monkeypatch.setattr(cvs, "chosen_cvs", lambda: ["CV_dev.yaml"])

    def digest(document):
        (tmp_path / "CV_dev.yaml").write_text(yaml.safe_dump(document))
        return matching.current_digests()["dev"]
    before = digest({"cv": SOURCE_CV, "design": {"theme": "classic"}})
    assert digest({"cv": SOURCE_CV, "design": {"theme": "sb2nov"},
                   "locale": {"language": "english"}}) == before
    edited = copy.deepcopy(SOURCE_CV)
    edited["sections"]["skills"][0]["details"] += ", Spark"
    assert digest({"cv": edited, "design": {"theme": "sb2nov"}}) != before
