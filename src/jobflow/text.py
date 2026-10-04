"""Small text helpers shared across jobflow."""
from __future__ import annotations

import re


def words(text: object) -> str:
    """Lower-case words separated by single spaces, ignoring punctuation other than + and #,
    so "C++" and "C#" stay themselves rather than the word "c". Used to compare titles,
    search terms, and CV text regardless of case, punctuation, or markup."""
    return re.sub(r"[^a-z0-9+#]+", " ", str(text or "").casefold()).strip()


def plural(count: int, word: str, suffix: str = "s") -> str:
    """"1 job", "2 jobs"; `suffix` for other plurals ("search", suffix="es")."""
    return f"{count} {word}{'' if count == 1 else suffix}"


def whole_number(value: object) -> int | None:
    """An id the LLM gave, as a number: 3 or "3"; None for anything else (True included,
    which Python would otherwise count as 1)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return int(value) if isinstance(value, str) and value.isdigit() else None
