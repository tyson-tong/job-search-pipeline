import copy
import json

import pytest
from concurrent.futures import Future

from jobflow.candidate.matching import normalise_requirements, quote_in_cv, score_requirements
from jobflow.candidate import cover_letter
from jobflow.candidate.cover_letter import check_letter, letter_fixed_by_hand
from jobflow.candidate.skill_gaps import rank_skills
from jobflow.candidate.tailoring import bullets, cut_order, cv_fixed_by_hand, fact_check, without

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
    monkeypatch.setattr(cover_letter, "connect", Conn)
    monkeypatch.setattr(cover_letter, "complete", lambda system, user, **kw: (
        json.dumps({"critique": [], "cover_letter": letter}) if kw.get("reviewer")
        else letter, "m"))
    target, errors, _ = cover_letter.write_letter(1, output_dir=tmp_path)
    assert errors and target.name == "COVER_LETTER_REVIEW.md"
    assert not (tmp_path / "cover-letter.docx").exists()
    assert letter in target.read_text()


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


def test_an_empty_assessment_is_never_saved_as_a_score(monkeypatch):
    # A job is scored only once: saving an answer listing no requirements would fix its
    # score at 0 for good.
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
    monkeypatch.setattr(matching, "chosen_sources", lambda: {"dev": CV_TEXT})
    monkeypatch.setattr(matching, "complete", lambda *a, **k: ('{"requirements": []}', "m"))
    with pytest.raises(RuntimeError, match="no requirements"):
        matching.match_job(1)
    assert not any("INSERT" in sql for sql in Conn.sql)


def test_a_failed_company_search_is_a_note_not_a_failed_job(monkeypatch):
    from jobflow.candidate import tailoring

    def down(company, title):
        raise RuntimeError("432 Client Error: usage limit exceeded")
    monkeypatch.setenv("WEB_SEARCH_API_KEY", "k")
    monkeypatch.setattr(tailoring, "web_search", down)
    sources, notes = tailoring.research({"company": "Acme", "title": "Analyst"})
    assert sources == [] and "web search failed" in notes[0]
