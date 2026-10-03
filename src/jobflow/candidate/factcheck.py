"""Checking a tailored CV or a cover letter against what it may draw on: facts changed,
numbers made up, and new terms to confirm. The LLM drafts; these checks are code."""
from __future__ import annotations

import re
from typing import NamedTuple

from ..text import words


class Checked(NamedTuple):
    """A fact check's result: errors (likely factual changes, which stop the draft being
    written as a finished document) and warnings (wording a human should confirm)."""
    errors: list[str]
    warnings: list[str]

# Header fields other than these must stay exactly as in the source CV, and none may be
# added. The headline may be reworded, so it is checked like section text instead.
FREE_TEXT_FIELDS = ("sections", "headline")
FACT_FIELDS = ("company", "position", "institution", "area", "degree", "start_date",
               "end_date", "date", "name", "title")
# An entry's location may be left out, but not changed or made up.
LOCATION = "location"


def _entries(cv: dict):
    for section in (cv.get("sections") or {}).values():
        for entry in section if isinstance(section, list) else []:
            if isinstance(entry, dict):
                yield entry


def _strings(value: object):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif value is not None:
        yield str(value)


def _numbers(value: object) -> set[str]:
    return {n.replace(",", "") for text in _strings(value)
            for n in re.findall(r"\d+(?:[.,]\d+)*", text)}


def _technical(token: str) -> bool:
    """A token no ordinary word looks like: AWS, PyTorch, Node.js, C++."""
    return bool(re.search(r"[+#]|\w\.\w|^.+[A-Z]", token))


def _terms(texts, skip_first: bool, known: set[str] | frozenset[str] = frozenset()) -> set[str]:
    """Capitalised or technical tokens (e.g. AWS, Node.js, C++). With skip_first, the
    first word of a sentence, which is capitalised anyway, counts only if it is technical,
    starts a list ("Kubernetes, Python, SQL"), or is one of `known` (casefolded terms, such
    as the job ad's, which a draft is most tempted to claim)."""
    terms = set()
    for text in texts:
        for sentence in re.split(r"(?<=[.!?;:])\s+|\n", text):
            raw = sentence.split()
            tokens = [t.strip(".,;:()[]\"'*") for t in raw]
            for position, token in enumerate(tokens):
                if not token:
                    continue
                if skip_first and position == 0 and not (
                        _technical(token) or raw[0].endswith(",") or token.casefold() in known):
                    continue
                # Digits are left to the number check.
                if not re.search(r"\d", token) and (token[0].isupper() or _technical(token)):
                    terms.add(token)
    return terms


# What RenderCV passes to Typst as markup rather than text: commands such as
# #text(fill: white)[...] or #read("..."), $$ math $$, and links, whose address the PDF
# doesn't show. A draft must not add any (the job ad could ask the LLM for them).
_MARKUP = re.compile(r"#[A-Za-z][^\s()\[]*(?:\([^)]*\))?(?:\[[^\]]*\])?|\$\$.*?\$\$|\]\([^)]*\)")


def _markup(value: object) -> set[str]:
    return {m.group(0) for text in _strings(value) for m in _MARKUP.finditer(text)}


def _role(entry: dict) -> tuple[str, ...]:
    return tuple(words(entry.get(f)) for f in ("company", "position", "start_date"))


def _facts(entry: dict) -> tuple[str | None, ...]:
    """An entry's employer, position, dates, and education details together: a draft entry
    must match one source entry on all of them, so no date or title moves between entries."""
    return tuple(None if entry.get(f) is None else words(entry[f]) for f in FACT_FIELDS)


def _sections(cv: dict) -> list:
    """The parts of a CV that may be reworded: the headline and the sections. The other
    header fields are compared exactly (_header_errors)."""
    return [cv.get("headline"), cv.get("sections")]


def _header_errors(source_cv: dict, draft_cv: dict) -> list[str]:
    """Header fields (name, contact details, and so on) added, changed, or removed."""
    errors = []
    for field in sorted((set(source_cv) | set(draft_cv)) - set(FREE_TEXT_FIELDS)):
        if field not in source_cv:
            errors.append(f"Header field '{field}' was added.")
        elif draft_cv.get(field) != source_cv.get(field):
            errors.append(f"Header field '{field}' was changed or removed.")
    return errors


def _entry_errors(source_cv: dict, draft_cv: dict) -> list[str]:
    """Each draft entry against the source's: facts new or altered, facts that don't belong
    together, a changed location, and numbers taken from another entry."""
    errors = []
    source_facts = {(f, words(e[f])) for e in _entries(source_cv) for f in FACT_FIELDS
                    if e.get(f) is not None}
    # Each source entry's numbers, by its facts (entries with the same facts pooled).
    source_entries: dict[tuple, set[str]] = {}
    source_locations: dict[tuple, set[str]] = {}
    for e in _entries(source_cv):
        source_entries.setdefault(_facts(e), set()).update(_numbers(e))
        source_locations.setdefault(_facts(e), set()).add(words(e.get(LOCATION)))
    source_numbers = _numbers(_sections(source_cv))
    for entry in _entries(draft_cv):
        altered = False
        for field in FACT_FIELDS:
            if entry.get(field) is not None and (field, words(entry[field])) not in source_facts:
                errors.append(f"New or altered {field}: {entry[field]!r} is not in the source CV.")
                altered = True
        # Each value may exist somewhere in the source, yet not together: an end date
        # taken from a later job, or one employer's title given to another.
        facts = _facts(entry)
        if altered or not any(facts):
            continue
        shown = ", ".join(f"{f} {entry[f]!r}" for f in FACT_FIELDS if entry.get(f) is not None)
        if facts not in source_entries:
            errors.append(f"Entry doesn't match any one entry in the source CV ({shown}): a date, "
                          "title, or detail was moved, added, or dropped.")
            continue
        if entry.get(LOCATION) is not None \
                and words(entry[LOCATION]) not in source_locations[facts]:
            errors.append(f"Location changed ({shown}): {entry[LOCATION]!r} is not this entry's "
                          "location in the source CV.")
        # A number from another entry ("cut costs 30%" at one job, given to another). Numbers
        # in no entry at all are reported for the whole CV (fact_check).
        moved = sorted((_numbers(entry) - source_entries[facts]) & source_numbers)
        if moved:
            errors.append(f"Numbers from another entry in the source CV ({shown}): "
                          + ", ".join(moved) + ".")
    return errors


def _education(entry: dict) -> tuple[str, ...]:
    return tuple(words(entry.get(f)) for f in ("institution", "area", "degree"))


def _removed_errors(source_cv: dict, draft_cv: dict) -> list[str]:
    """Education entries, employers, and roles in the source that the draft dropped."""
    errors = []
    draft_education = {_education(e) for e in _entries(draft_cv)}
    draft_companies = {words(e.get("company")) for e in _entries(draft_cv)}
    # Each role, not just each employer: two roles at one employer are two entries.
    draft_roles = {_role(e) for e in _entries(draft_cv)}
    for entry in _entries(source_cv):
        if entry.get("institution") and _education(entry) not in draft_education:
            errors.append(f"Education entry removed: {entry.get('institution')} "
                          f"{entry.get('degree') or ''} {entry.get('area') or ''}".rstrip() + ".")
        if entry.get("company") and words(entry["company"]) not in draft_companies:
            errors.append(f"Employer removed: {entry['company']}.")
        elif entry.get("company") and _role(entry) not in draft_roles:
            role = " ".join(str(entry.get(f) or "") for f in ("position", "start_date")).strip()
            errors.append(f"Role removed at {entry['company']}: {role or 'an entry'}.")
    return errors


def _new_terms(source_cv: dict, draft_cv: dict, job_text: str) -> list[str]:
    """Capitalised or technical terms in the draft that the source doesn't have; the job
    ad's are looked for even at the start of a sentence."""
    source_text = list(_strings(_sections(source_cv)))
    source_vocab = {t.casefold() for t in _terms(source_text, skip_first=False)}
    source_words = set(words(" ".join(source_text)).split())
    job_vocab = {t.casefold() for t in _terms([job_text], skip_first=False)}
    return sorted(t for t in _terms(_strings(_sections(draft_cv)), skip_first=True, known=job_vocab)
                  if t.casefold() not in source_vocab
                  and not set(words(t).split()) <= source_words)


def fact_check(source_cv: dict, draft_cv: dict, job_text: str = "") -> Checked:
    """Compare a tailored draft with its source CV. job_text, the job's title and ad, names
    the terms a draft is most tempted to add, so they're looked for everywhere.

    Errors are likely factual changes (render is skipped); warnings are wording
    changes a human should confirm.
    """
    errors = (_header_errors(source_cv, draft_cv) + _entry_errors(source_cv, draft_cv)
              + _removed_errors(source_cv, draft_cv))
    new_numbers = sorted(_numbers(_sections(draft_cv)) - _numbers(_sections(source_cv)))
    if new_numbers:
        errors.append("Numbers not in the source CV: " + ", ".join(new_numbers) + ".")
    new_markup = sorted(_markup(_sections(draft_cv)) - _markup(_sections(source_cv)))
    if new_markup:
        errors.append("Markup not in the source CV, which RenderCV would run or hide in the "
                      "PDF: " + ", ".join(new_markup) + ".")
    new_terms = _new_terms(source_cv, draft_cv, job_text)
    warnings = (["Terms not in the source CV (check each is supported): "
                 + ", ".join(new_terms) + "."] if new_terms else [])
    return Checked(list(dict.fromkeys(errors)), warnings)


# Capitalised words any letter uses, never worth a warning.
LETTER_WORDS = {"i", "i'd", "i'm", "i've", "i'll", "dear", "hiring", "manager", "team",
                "sincerely", "regards", "kind", "best", "yours", "thank", "thanks",
                "monday", "tuesday", "wednesday", "thursday", "friday", "january", "february",
                "march", "april", "may", "june", "july", "august", "september", "october",
                "november", "december"}


def check_letter(letter: str, source_cv: dict | str, job: dict,
                 sources: list[dict]) -> Checked:
    """Check a cover letter against what it may draw on: the source CV, the job ad, and
    the company research. Errors are numbers found in none of them; warnings are new
    capitalised or technical terms, and placeholders left in."""
    allowed = [source_cv, job.get("title"), job.get("company"), job.get("description"),
               [s.get("title") for s in sources], [s.get("content") for s in sources]]
    errors, warnings = [], []
    new_numbers = sorted(_numbers(letter) - _numbers(allowed))
    if new_numbers:
        errors.append("Numbers not in your CV, the job ad, or the company research: "
                      + ", ".join(new_numbers) + ".")
    vocab = {t.casefold() for t in _terms(_strings(allowed), skip_first=False)} | LETTER_WORDS
    allowed_words = set(words(" ".join(_strings(allowed))).split())
    new_terms = sorted(t for t in _terms([letter], skip_first=True)
                       if t.casefold() not in vocab and not set(words(t).split()) <= allowed_words)
    if new_terms:
        warnings.append("Terms not in your CV, the job ad, or the company research (check each "
                        "is true): " + ", ".join(new_terms) + ".")
    if re.search(r"\[[^\]]*\]", letter):
        warnings.append("A [placeholder] is left in; fill it in or remove it.")
    return Checked(errors, warnings)
