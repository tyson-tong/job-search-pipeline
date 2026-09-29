"""Checking a tailored CV or a cover letter against what it may draw on: facts changed,
numbers made up, and new terms to confirm. The LLM drafts; these checks are code."""
from __future__ import annotations

import re

from .cvs import normalise_text

# Header fields other than these must stay exactly as in the source CV, and none may be
# added. The headline may be reworded, so it is checked like section text instead.
FREE_TEXT_FIELDS = ("sections", "headline")
FACT_FIELDS = ("company", "position", "institution", "area", "degree", "start_date",
               "end_date", "date", "name", "title")


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


def _terms(texts, skip_first: bool) -> set[str]:
    """Capitalised or technical tokens (e.g. AWS, Node.js, C++), optionally ignoring
    the first word of each sentence, which is capitalised anyway."""
    terms = set()
    for text in texts:
        for sentence in re.split(r"(?<=[.!?;:])\s+|\n", text):
            tokens = [t.strip(".,;:()[]\"'*") for t in sentence.split()]
            for position, token in enumerate(tokens):
                if not token or (skip_first and position == 0):
                    continue
                # Digits are left to the number check.
                if not re.search(r"\d", token) and (token[0].isupper()
                                                    or re.search(r"[+#]|\w\.\w", token)):
                    terms.add(token)
    return terms


def _role(entry: dict) -> tuple[str, str, str]:
    return tuple(normalise_text(entry.get(f)) for f in ("company", "position", "start_date"))


def _facts(entry: dict) -> tuple[str | None, ...]:
    """An entry's employer, position, dates, and education details together: a draft entry
    must match one source entry on all of them, so no date or title moves between entries."""
    return tuple(None if entry.get(f) is None else normalise_text(entry[f]) for f in FACT_FIELDS)


def fact_check(source_cv: dict, draft_cv: dict) -> tuple[list[str], list[str]]:
    """Compare a tailored draft with its source CV.

    Errors are likely factual changes (render is skipped); warnings are wording
    changes a human should confirm.
    """
    errors, warnings = [], []
    for field in sorted((set(source_cv) | set(draft_cv)) - set(FREE_TEXT_FIELDS)):
        if field not in source_cv:
            errors.append(f"Header field '{field}' was added.")
        elif draft_cv.get(field) != source_cv.get(field):
            errors.append(f"Header field '{field}' was changed or removed.")
    source_facts = {(f, normalise_text(e[f])) for e in _entries(source_cv) for f in FACT_FIELDS
                    if e.get(f) is not None}
    # Each source entry's numbers, by its facts (entries with the same facts pooled).
    source_entries: dict[tuple, set[str]] = {}
    for e in _entries(source_cv):
        source_entries.setdefault(_facts(e), set()).update(_numbers(e))
    source_numbers = _numbers([source_cv.get("headline"), source_cv.get("sections")])
    for entry in _entries(draft_cv):
        altered = False
        for field in FACT_FIELDS:
            if entry.get(field) is not None and (field, normalise_text(entry[field])) not in source_facts:
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
        # A number from another entry ("cut costs 30%" at one job, given to another). Numbers
        # in no entry at all are reported below, for the whole CV.
        moved = sorted((_numbers(entry) - source_entries[facts]) & source_numbers)
        if moved:
            errors.append(f"Numbers from another entry in the source CV ({shown}): "
                          + ", ".join(moved) + ".")
    draft_entries = [(normalise_text(e.get("institution")), normalise_text(e.get("area")), normalise_text(e.get("degree")))
                     for e in _entries(draft_cv)]
    draft_companies = {normalise_text(e.get("company")) for e in _entries(draft_cv)}
    # Each role, not just each employer: two roles at one employer are two entries.
    draft_roles = {_role(e) for e in _entries(draft_cv)}
    for entry in _entries(source_cv):
        if entry.get("institution") and (normalise_text(entry.get("institution")), normalise_text(entry.get("area")),
                                         normalise_text(entry.get("degree"))) not in draft_entries:
            errors.append(f"Education entry removed: {entry.get('institution')} "
                          f"{entry.get('degree') or ''} {entry.get('area') or ''}".rstrip() + ".")
        if entry.get("company") and normalise_text(entry["company"]) not in draft_companies:
            errors.append(f"Employer removed: {entry['company']}.")
        elif entry.get("company") and _role(entry) not in draft_roles:
            role = " ".join(str(entry.get(f) or "") for f in ("position", "start_date")).strip()
            errors.append(f"Role removed at {entry['company']}: {role or 'an entry'}.")
    # The other header fields are compared exactly above, so only these are scanned here.
    source_sections = [source_cv.get("headline"), source_cv.get("sections")]
    draft_sections = [draft_cv.get("headline"), draft_cv.get("sections")]
    new_numbers = sorted(_numbers(draft_sections) - source_numbers)
    if new_numbers:
        errors.append("Numbers not in the source CV: " + ", ".join(new_numbers) + ".")
    source_vocab = {t.casefold() for t in _terms(_strings(source_sections), skip_first=False)}
    source_words = set(normalise_text(" ".join(_strings(source_sections))).split())
    new_terms = sorted(t for t in _terms(_strings(draft_sections), skip_first=True)
                       if t.casefold() not in source_vocab
                       and not set(normalise_text(t).split()) <= source_words)
    if new_terms:
        warnings.append("Terms not in the source CV (check each is supported): "
                        + ", ".join(new_terms) + ".")
    return list(dict.fromkeys(errors)), warnings


# Capitalised words any letter uses, never worth a warning.
LETTER_WORDS = {"i", "i'd", "i'm", "i've", "i'll", "dear", "hiring", "manager", "team",
                "sincerely", "regards", "kind", "best", "yours", "thank", "thanks",
                "monday", "tuesday", "wednesday", "thursday", "friday", "january", "february",
                "march", "april", "may", "june", "july", "august", "september", "october",
                "november", "december"}


def check_letter(letter: str, source_cv: dict | str, job: dict,
                 sources: list[dict]) -> tuple[list[str], list[str]]:
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
    words = set(normalise_text(" ".join(_strings(allowed))).split())
    new_terms = sorted(t for t in _terms([letter], skip_first=True)
                       if t.casefold() not in vocab and not set(normalise_text(t).split()) <= words)
    if new_terms:
        warnings.append("Terms not in your CV, the job ad, or the company research (check each "
                        "is true): " + ", ".join(new_terms) + ".")
    if re.search(r"\[[^\]]*\]", letter):
        warnings.append("A [placeholder] is left in; fill it in or remove it.")
    return errors, warnings
