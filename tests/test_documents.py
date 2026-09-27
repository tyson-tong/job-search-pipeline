import re
from datetime import date

from docx import Document

from jobflow.candidate.documents import write_docx, write_latex, write_letter_docx

CV = {
    "name": "José Example", "headline": "Data analyst", "email": "jose@example.com", "phone": "tel:+61-400-000-000",
    "sections": {
        "experience": [{"company": "Example Corp", "position": "Data Analyst",
                        "start_date": "2021-02", "end_date": "present", "location": "Sydney",
                        "highlights": ["Built **Python** ETL pipelines processing 2M records daily"]}],
        "education": [{"institution": "Example University", "area": "Statistics", "degree": "BSc",
                       "start_date": "2016", "end_date": "2019"}],
        "publications": [{"title": "Fast Joins", "authors": ["J. Example"], "journal": "VLDB",
                          "date": "2020-05"}],
        "skills": [{"label": "Tools", "details": "Python, SQL"}],
        "awards": ["Dean's list"],
    },
}
EVERY_FACT = ["José Example", "Data analyst", "jose@example.com", "+61-400-000-000", "Example Corp", "Data Analyst",
              "Feb 2021 – present", "Sydney", "Built Python ETL pipelines processing 2M records daily",
              "Example University", "BSc in Statistics", "2016 – 2019", "Fast Joins", "J. Example",
              "VLDB", "May 2020", "Tools:", "Python, SQL", "Dean's list"]


def _words(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_docx_cv_and_letter_keep_every_word(tmp_path):
    write_docx(CV, tmp_path / "cv.docx")
    docx_text = _words(" ".join(p.text for p in Document(tmp_path / "cv.docx").paragraphs))
    for fact in EVERY_FACT:
        assert fact in docx_text, fact

    letter = "Dear Jane Smith,\n\nI built ETL pipelines.\n\nKind regards,\nJosé Example"
    write_letter_docx(letter, CV, {"title": "Analyst", "company": "Acme"}, tmp_path / "l.docx",
                      date(2026, 9, 27))
    paragraphs = [p.text for p in Document(tmp_path / "l.docx").paragraphs]
    assert paragraphs[2] == "Jane Smith\nAcme"   # addressed to the greeting's name
    assert paragraphs[-3:] == ["Dear Jane Smith,", "I built ETL pipelines.",
                               "Kind regards,\nJosé Example"]


def test_latex_cv_text_can_never_run_commands(tmp_path):
    cv = {"name": "A & B", "sections": {"summary": [
        r"**Lead** \input{/etc/passwd} 100% $5 #1 a_b {x} ~ ^", "[Confidential] project"]}}
    write_latex(cv, tmp_path / "cv.tex")
    tex = (tmp_path / "cv.tex").read_text()
    assert r"\textbf{Lead} \textbackslash{}input\{/etc/passwd\} 100\% \$5 \#1 a\_b \{x\} "\
           r"\textasciitilde{} \textasciicircum{}" in tex
    assert r"\input{" not in tex and r"A \& B" in tex
    # A leading [ would otherwise be read as an optional argument and lose its brackets.
    assert "{[}Confidential{]} project" in tex


def test_every_cv_file_is_found_once_and_read_as_text(tmp_path, monkeypatch):
    from jobflow.candidate import cvs

    write_docx(CV, tmp_path / "Resume_CV_2026.docx")
    (tmp_path / "CV_dev.yaml").write_text("cv:\n  name: José Example\n")
    (tmp_path / "CV_dev.pdf").write_bytes(b"an export of CV_dev.yaml")  # same CV: skipped
    (tmp_path / "strat-cv.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\textbf{Built} ETL 100\\% % note\n"
        "\\end{document}\n")
    (tmp_path / "~$CV_dev.docx").write_bytes(b"Word lock file")
    (tmp_path / "cover-letter.docx").write_bytes(b"not a CV")
    (tmp_path / "CV_notes.txt").write_text("not a CV format")
    monkeypatch.setattr(cvs, "cv_directory", lambda: tmp_path)
    assert {name: path.name for name, path in cvs.cv_files().items()} == {
        "Resume_CV_2026": "Resume_CV_2026.docx", "dev": "CV_dev.yaml", "strat-cv": "strat-cv.tex"}
    sources = cvs.cv_sources()
    assert "Built Python ETL pipelines" in sources["Resume_CV_2026"]
    assert _words(sources["strat-cv"]).strip() == "Built ETL 100%"


def test_letter_from_a_non_yaml_cv_takes_its_header_only_from_the_cv(tmp_path):
    cv_text = ("José Example\njose@example.com | +61 400 000 000\n"
               "Example Corp 2016 - 2019: processed 2,000,000 records")
    letter = "Dear Hiring Manager,\n\nI built ETL pipelines.\n\nKind regards,\nJosé Example"
    write_letter_docx(letter, cv_text, {"title": "Analyst", "company": "Acme"},
                      tmp_path / "l.docx", date(2026, 9, 27))
    paragraphs = [p.text for p in Document(tmp_path / "l.docx").paragraphs]
    # Dates and figures are never taken for a phone number.
    assert paragraphs[0] == "José Example\njose@example.com\n+61 400 000 000"
