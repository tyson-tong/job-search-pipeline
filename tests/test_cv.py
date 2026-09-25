import copy

from jobflow.candidate.matching import normalise_requirements, quote_in_cv, score_requirements
from jobflow.candidate.tailoring import fact_check

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
    job = draft["sections"]["experiences"][0]
    job["position"] = "Senior Data Analyst"
    job["start_date"] = "2020-02"
    job["highlights"] = ["Built Python ETL pipelines processing 5M records daily"]
    draft["sections"]["education"] = []
    errors, _ = fact_check(SOURCE_CV, draft)
    text = "\n".join(errors)
    assert "Header field 'phone'" in text
    assert "position: 'Senior Data Analyst'" in text
    assert "start_date: '2020-02'" in text
    assert "Education entry removed: Example University" in text
    assert "Numbers not in the source CV: 2020, 5." in errors


def test_fact_check_flags_removed_employer_and_warns_on_new_terms():
    draft = copy.deepcopy(SOURCE_CV)
    draft["sections"]["experiences"] = []
    draft["sections"]["skills"] = [{"label": "Tools", "details": "Python, SQL, Tableau, Kubernetes"}]
    errors, warnings = fact_check(SOURCE_CV, draft)
    assert "Employer removed: Example Corp." in errors
    assert warnings and "Kubernetes" in warnings[0]
