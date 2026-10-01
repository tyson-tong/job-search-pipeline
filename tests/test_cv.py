import copy
import json
import os
from concurrent.futures import Future

import pytest
from docx import Document

from jobflow.candidate import cover_letter, review
from jobflow.candidate.cover_letter import letter_fixed_by_hand
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
    assert quote_in_cv("built python ETL pipelines", CV_TEXT)
    assert quote_in_cv("Built Python ETL pipelines processing 2M records daily.", CV_TEXT)
    assert not quote_in_cv("Built Spark pipelines", CV_TEXT)
    assert not quote_in_cv("", CV_TEXT)
    # Whole-word matching: "Pres" is not evidence of "Presented".
    assert not quote_in_cv("Pres", CV_TEXT)


def test_unverifiable_quotes_downgrade_status():
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
    draft = copy.deepcopy(SOURCE_CV)
    draft["sections"]["experiences"][0]["highlights"] = [
        "Presented dashboards to executives using Tableau"]
    draft["sections"] = {"skills": draft["sections"]["skills"],
                         "experiences": draft["sections"]["experiences"],
                         "education": draft["sections"]["education"]}
    assert fact_check(SOURCE_CV, draft) == ([], [])


def test_fact_check_flags_factual_changes():
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
    draft = copy.deepcopy(SOURCE_CV)
    draft["sections"]["experiences"] = []
    draft["sections"]["skills"] = [{"label": "Tools", "details": "Python, SQL, Tableau, Kubernetes"}]
    errors, warnings = fact_check(SOURCE_CV, draft)
    assert "Employer removed: Example Corp." in errors
    assert warnings and "Kubernetes" in warnings[0]


def test_fact_check_warns_on_a_new_term_that_starts_a_list_or_a_bullet():
    # The first word of a sentence was never checked, and the draft leads with what the job
    # wants: an invented skill first in a list, or a bullet, went unmentioned.
    draft = copy.deepcopy(SOURCE_CV)
    draft["sections"]["skills"] = [{"label": "Tools", "details": "Kubernetes, Python, SQL, Tableau"}]
    draft["sections"]["experiences"][0]["highlights"] = [
        "Terraform ETL pipelines processing 2M records daily", "PyTorch dashboards for executives",
        "Spearheaded dashboards for executives using Tableau"]
    _, warnings = fact_check(SOURCE_CV, draft, "Data Engineer: Terraform and Kubernetes")
    # Spearheaded is a reworded verb, not a claim, and isn't in the ad.
    assert warnings == ["Terms not in the source CV (check each is supported): Kubernetes, "
                        "PyTorch, Terraform."]


def test_fact_check_flags_a_changed_location_and_markup_rendercv_would_run():
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
    # draft you fixed yourself.
    import os
    draft, review = tmp_path / "CV_dev_draft.yaml", tmp_path / "CV_REVIEW.md"
    draft.write_text("cv: {}")
    review.write_text("review")
    os.utime(draft, ns=(1, 1))  # written by the run, before its review
    assert not cv_fixed_by_hand(tmp_path)
    os.utime(draft, ns=(2 * review.stat().st_mtime_ns,) * 2)  # edited since
    assert cv_fixed_by_hand(tmp_path)
    assert not letter_fixed_by_hand(tmp_path)
    (tmp_path / "cover-letter.docx").write_bytes(b"")
    assert letter_fixed_by_hand(tmp_path)


JOB = {"title": "Data Analyst", "company": "Acme",
       "description": "You will own 3 dashboards and work with Snowflake."}


def test_letter_check_flags_numbers_and_terms_from_nowhere():
    research = [{"title": "Acme news", "content": "Acme was founded in 1998 in Sydney."}]
    letter = ("Dear Hiring Manager,\nAt Example Corp I built Python ETL pipelines processing "
              "2M records daily, and I'd enjoy owning your 3 dashboards at a company founded in "
              "1998. I also cut costs by 40% using Kubernetes.\nAlex Example")
    errors, warnings = check_letter(letter, SOURCE_CV, JOB, research)
    # 2M (CV), 3 (job ad), and 1998 (research) are allowed; 40 appears nowhere.
    assert errors == ["Numbers not in your CV, the job ad, or the company research: 40."]
    assert warnings == ["Terms not in your CV, the job ad, or the company research (check each "
                        "is true): Kubernetes."]
    assert ("A [placeholder] is left in; fill it in or remove it."
            in check_letter("Dear [Name], I built dashboards.", SOURCE_CV, JOB, [])[1])


def test_letter_failing_the_check_is_never_written_as_a_docx(tmp_path, monkeypatch):
    class Conn:
        def execute(self, *args): return self
        def __enter__(self): return self
        def __exit__(self, *args): pass

    letter = "Dear Hiring Manager,\n\nI cut costs by 40% at Example Corp.\n\nAlex Example"
    monkeypatch.setattr(cover_letter, "load_cv", lambda job_id, variant: (
        JOB, "main", "CV variant main.", SOURCE_CV, ""))
    researched = Future()
    researched.set_result(([], []))
    monkeypatch.setattr(cover_letter, "research_soon", lambda job_id: researched)
    monkeypatch.setattr(review, "connect", Conn)
    monkeypatch.setattr(cover_letter, "complete", lambda system, user, **kw: (
        json.dumps({"critique": [], "cover_letter": letter}) if kw.get("reviewer")
        else letter, "m"))
    # A cover-letter.docx from an earlier run would look like this letter, checked.
    (tmp_path / "cover-letter.docx").write_bytes(b"old")
    target, errors, _ = cover_letter.write_letter(1, output_dir=tmp_path)
    assert errors and target.name == "COVER_LETTER_REVIEW.md"
    assert not (tmp_path / "cover-letter.docx").exists()
    assert letter in target.read_text()


def test_a_revision_with_more_fact_check_errors_is_discarded(tmp_path, monkeypatch):
    class Conn:
        def execute(self, *args): return self
        def __enter__(self): return self
        def __exit__(self, *args): pass

    draft = "Dear Hiring Manager,\n\nAt Example Corp I built Python ETL pipelines.\n\nAlex Example"
    revised = "Dear Hiring Manager,\n\nI cut costs by 40% at Example Corp.\n\nAlex Example"
    monkeypatch.setattr(cover_letter, "load_cv", lambda job_id, variant: (
        JOB, "main", "CV variant main.", SOURCE_CV, ""))
    researched = Future()
    researched.set_result(([], []))
    monkeypatch.setattr(cover_letter, "research_soon", lambda job_id: researched)
    monkeypatch.setattr(review, "connect", Conn)
    monkeypatch.setattr(cover_letter, "complete", lambda system, user, **kw: (
        json.dumps({"critique": ["Add a metric"], "cover_letter": revised}) if kw.get("reviewer")
        else draft, "m"))
    target, errors, _ = cover_letter.write_letter(1, output_dir=tmp_path)
    assert not errors and target.name == "cover-letter.docx"
    report = (tmp_path / "COVER_LETTER_REVIEW.md").read_text()
    assert "failed the fact check, so the first draft is kept" in report


def test_a_reviewer_every_job_would_fail_on_stops_the_batch(tmp_path, monkeypatch):
    # A misspelt LLM_REVIEW_MODEL (404) was caught like a network blip: every job got an
    # unreviewed draft, and the batch went on paying for drafts.
    from jobflow.llm import LLMError

    def complete(system, user, **kw):
        if kw.get("reviewer"):
            raise LLMError("The model `gpt-typo` does not exist (LLM provider, HTTP 404)",
                           fatal=True)
        return "Dear Hiring Manager,\n\nI built Python ETL pipelines.\n\nAlex Example", "m"
    monkeypatch.setattr(cover_letter, "load_cv", lambda job_id, variant: (
        JOB, "main", "CV variant main.", SOURCE_CV, ""))
    researched = Future()
    researched.set_result(([], []))
    monkeypatch.setattr(cover_letter, "research_soon", lambda job_id: researched)
    monkeypatch.setattr(cover_letter, "complete", complete)
    with pytest.raises(LLMError) as caught:
        cover_letter.write_letter(1, output_dir=tmp_path)
    assert caught.value.fatal and not list(tmp_path.iterdir())


def test_skill_ranking_counts_only_real_gaps_once():
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
    assert cv["sections"]["experiences"][0]["highlights"][0].startswith("Built")  # input untouched


def _assess(prompt: str) -> tuple[str, str]:
    """The LLM's answer to a scoring prompt: evidence for CV dev, a failure for CV strat."""
    if '"cv_variant": "strat"' in prompt:
        raise RuntimeError("Read timed out")
    return json.dumps({"requirements": [{"requirement": "Python", "status": "matched",
                                         "evidence": ["Built Python ETL pipelines"]}]}), "m"


@pytest.mark.parametrize("cvs, answer, message", [
    # Saving an answer listing no requirements would fix the score at 0 until the CV changes.
    (["dev"], lambda prompt: ('{"requirements": []}', "m"), "no requirements"),
    # Saving the one CV that worked would leave the job scored without the other.
    (["dev", "strat"], _assess, "timed out"),
])
def test_a_failed_assessment_saves_no_score(monkeypatch, cvs, answer, message):
    # A score stands until you edit the CV and rescore, so nothing may be saved from a failure.
    from jobflow.candidate import matching

    class Conn:
        sql = []
        def execute(self, sql, *args):
            self.sql.append(sql)
            self.last = sql
            return self
        def fetchall(self): return []
        def fetchone(self): return ("interested",) if "status" in self.last else (False,)
        def __enter__(self): return self
        def __exit__(self, *args): pass

    monkeypatch.setattr(matching, "connect", Conn)
    monkeypatch.setattr(matching, "matching_weights", lambda: (0.75, 0.25))
    monkeypatch.setattr(matching, "fetch_job", lambda job_id: JOB)
    monkeypatch.setattr(matching, "chosen_sources", lambda: {cv: CV_TEXT for cv in cvs})
    monkeypatch.setattr(matching, "complete", lambda system, user, **kw: answer(user))
    with pytest.raises(RuntimeError, match=message):
        matching.match_job(1)
    assert not any("INSERT" in sql for sql in Conn.sql)


def test_cutting_to_fit_cuts_the_fewest_least_relevant_bullets(tmp_path, monkeypatch):
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
    warnings = []
    fitted, cut, _ = render.fit(JOB, {"cv": cv}, cv, "CV_dev_draft.yaml", 1, warnings)
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
    os.utime(draft, ns=(docx.stat().st_mtime_ns + 10**9,) * 2)
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


def test_a_draft_failing_the_check_leaves_no_older_cv_beside_it(tmp_path, monkeypatch):
    # A PDF, .docx, or .tex from an earlier run would look like this draft, checked.
    import yaml

    from jobflow.candidate import tailoring

    class Conn:
        def execute(self, *args): return self
        def __enter__(self): return self
        def __exit__(self, *args): pass

    invented = copy.deepcopy(SOURCE_CV)
    invented["sections"]["experiences"][0]["position"] = "Head of Data"
    draft = yaml.safe_dump({"cv": invented})
    monkeypatch.setattr(tailoring, "load_source", lambda job_id, variant: (
        JOB, "dev", "CV variant dev.", {"cv": SOURCE_CV}, yaml.safe_dump({"cv": SOURCE_CV})))
    researched = Future()
    researched.set_result(([], []))
    monkeypatch.setattr(tailoring, "research_soon", lambda job_id: researched)
    monkeypatch.setattr(review, "connect", Conn)
    monkeypatch.setattr(tailoring, "complete", lambda system, user, **kw: (
        json.dumps({"critique": [], "cv_yaml": draft}) if kw.get("reviewer") else draft, "m"))
    for suffix in (".pdf", ".docx", ".tex"):
        (tmp_path / f"CV_dev_draft{suffix}").write_bytes(b"from an earlier run")
    target, errors, _ = tailoring.tailor_cv(1, output_dir=tmp_path, fmt="docx")
    assert errors and target == tmp_path / "CV_dev_draft.yaml"
    assert sorted(p.name for p in tmp_path.glob("CV_dev_draft.*")) == ["CV_dev_draft.yaml"]
