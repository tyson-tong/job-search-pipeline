"""Small text helpers shared across jobflow."""
from __future__ import annotations

import re

# Chinese characters and Japanese kana: written without spaces between words, so each is
# a word of its own, and "實習" (internship) is found in "數據分析實習生" as whole words.
_UNSPACED = re.compile(r"([\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])")


def words(text: object) -> str:
    """Lower-case words separated by single spaces, ignoring punctuation other than + and #,
    so "C++" and "C#" stay themselves rather than the word "c". Letters of any script count
    ("Ingénieur" is one word, not "ing nieur"), and each Chinese character or kana is a word.
    Used to compare titles, search terms, and CV text regardless of case, punctuation, or
    markup."""
    text = _UNSPACED.sub(r" \1 ", str(text or "").casefold())
    return " ".join(re.sub(r"[^\w+#]|_", " ", text).split())


def plural(count: int, word: str, suffix: str = "s") -> str:
    """"1 job", "2 jobs"; `suffix` for other plurals ("search", suffix="es")."""
    return f"{count} {word}{'' if count == 1 else suffix}"


def whole_number(value: object) -> int | None:
    """An id the LLM gave, as a number: 3 or "3"; None for anything else (True included,
    which Python would otherwise count as 1, and "²", a digit int() can't read)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return int(value) if isinstance(value, str) and value.isascii() and value.isdecimal() \
        else None


def job_label(job_id: int, title: str, company: str | None) -> str:
    """A job as messages name it: "#42 Data Analyst (Acme)"."""
    return f"#{job_id} {title} ({company or 'unknown employer'})"
