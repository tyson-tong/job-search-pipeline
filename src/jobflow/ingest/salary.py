"""Reading a salary from a job ad: the amounts or range it states and their pay period (said,
or guessed from the amounts' size), as an annual (min, max). Free text such as "$90k - $120k +
super" or "HK$35,000", or Indeed's salary object."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

# Multipliers to an annual amount. Hourly assumes a 38-hour week, daily a 5-day week.
ANNUAL_FACTOR = {"hourly": 38 * 52, "daily": 5 * 52, "weekly": 52, "monthly": 12,
                 "annual": 1}
PERIOD_WORDS = (
    ("hourly", r"hour|hr\b|/h\b|p\.?h\b|ph\b"),
    ("daily", r"\bday|daily|p\.?d\b|/d\b"),
    ("weekly", r"week|/wk|p\.?w\b"),
    ("monthly", r"month|/mo\b|p\.?m\b"),
    ("annual", r"year|annum|annual|p\.?a\b|/yr|\byr\b"),
)
INDEED_PERIODS = {"HOUR": "hourly", "DAY": "daily", "WEEK": "weekly", "MONTH": "monthly",
                  "YEAR": "annual"}
# The amounts each period's pay can plausibly be, in any of the searched currencies (a
# Hong Kong salary is often HK$100,000 a month). A period word next to an amount outside
# its range is about something else, as in "$120k package, flexible hours".
PLAUSIBLE = {"hourly": (5, 1_000), "daily": (50, 10_000), "weekly": (200, 50_000),
             "monthly": (500, 500_000), "annual": (1_000, math.inf)}
# An amount with no period stated below this is monthly pay, and above it annual. Hong Kong
# and Singapore salaries are usually quoted by the month, and in larger numbers.
MONTHLY_BELOW = {"HK": 200_000, "SG": 30_000}
MONTHLY_BELOW_ELSEWHERE = 20_000
# An annual salary above this is a misread, never a real offer; the database's salary
# columns also can't hold much more, and one value too big would fail a whole import.
MAX_ANNUAL = 50_000_000


def _period_from_amount(low: float, high: float, country: str | None = None) -> str:
    """The period of amounts with no period stated, from their size and the job's country.
    Where pay is quoted by the month (Hong Kong, Singapore), S$2,500 is a month's pay, not a
    day's, and so is a range that starts under the monthly cutoff (S$25,000 - 35,000)."""
    monthly_below = MONTHLY_BELOW.get(country or "")
    if high < 300:
        return "hourly"
    if high < (1000 if monthly_below else 3000):
        return "daily"
    amount = low if monthly_below else high
    return "monthly" if amount < (monthly_below or MONTHLY_BELOW_ELSEWHERE) else "annual"


def _period(text: str, span: tuple[int, int], low: float, high: float,
            ignore: list[tuple[int, int]], country: str | None) -> tuple[str, bool]:
    """The period the salary text states for the amounts at text[span[0]:span[1]]: the
    period word nearest after them (then nearest before), skipping words inside `ignore`
    and any whose period is implausible for the amount. Else from the amounts. With
    whether the text stated it (False: guessed from the amounts)."""
    start, end = span
    found = []
    for period, pattern in PERIOD_WORDS:
        for word in re.finditer(pattern, text, re.I):
            if not any(a <= word.start() < b for a, b in ignore):
                after = word.start() >= end
                distance = word.start() - end if after else start - word.end()
                found.append((not after, distance, period))
    for *_, period in sorted(found):
        least, most = PLAUSIBLE[period]
        if least <= high < most:
            return period, True
    return _period_from_amount(low, high, country), False


def _next_word_end(text: str, start: int) -> int:
    """Where the word after `start` (and the space before it) ends."""
    following = re.match(r"\s*\w*", text[start:])
    return start + (following.end() if following else 0)


# An annualised salary: (min, max, whether its period was guessed from the amounts).
Pay = tuple[float, float, bool]


def _annual(low: float, high: float, period: str, stated: bool = True) -> Pay | None:
    factor = ANNUAL_FACTOR[period]
    return None if high * factor > MAX_ANNUAL else (low * factor, high * factor, not stated)


# One amount in a salary text: an optional currency, the number, and an optional k ("$80k");
# a percentage ("11.5% super") is matched too, so that it is never taken for pay.
AMOUNT = re.compile(r"(?P<cur>[$£€]|\b(?:A|AU|NZ|S|US|HK)\$)?\s*"
                    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<k>[kK]\b)?(?P<pct>\s*%)?")
# What joins the two ends of a range: "$60 - $80k", "$120,000 to $140,000", "between $90k
# and $110k".
RANGE_SEPARATOR = re.compile(r"\s*(?:-|to|and)\s*", re.I)


@dataclass(frozen=True)
class _Pay:
    """A salary expression found in the text: one amount, or a range's two ends."""
    low: float
    high: float
    start: int
    end: int
    marked: bool   # a currency or a k on either end: an amount, not a count of something


def _amount(match: re.Match[str]) -> float:
    return float(match["num"].replace(",", "")) * (1000 if match["k"] else 1)


def _expressions(text: str) -> list[_Pay]:
    """Each amount and range in the text, in order. A k on one end of a range is the whole
    range's ("$60 - $80k" is 60,000 to 80,000, "$80k-90" 80,000 to 90,000)."""
    found = [m for m in AMOUNT.finditer(text) if not m["pct"]]
    expressions, i = [], 0
    while i < len(found):
        a = found[i]
        b = found[i + 1] if i + 1 < len(found) else None
        if b is not None and RANGE_SEPARATOR.fullmatch(text[a.end():b.start()]):
            low, high = _amount(a), _amount(b)
            if a["k"] and not b["k"] and high < 1000:
                high *= 1000
            elif b["k"] and not a["k"] and low < 1000:
                low *= 1000
            # "$100k and 2 weeks leave" is no range.
            if low <= high:
                expressions.append(_Pay(low, high, a.start(), b.end(),
                                        bool(a["cur"] or a["k"] or b["cur"] or b["k"])))
                i += 2
                continue
        expressions.append(_Pay(_amount(a), _amount(a), a.start(), a.end(),
                                bool(a["cur"] or a["k"])))
        i += 1
    return expressions


def _parse_text(text: str, country: str | None) -> Pay | None:
    """Annualised (min, max) from one salary text: its largest amount or range, so a sign-on
    bonus beside the salary isn't taken for its bottom end."""
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    expressions = _expressions(text)
    # Once a currency is marked, an unmarked number is something else: "2 weeks", "38 hours".
    if any(e.marked for e in expressions):
        pay = [e for e in expressions if e.marked]
    else:
        pay = expressions
    pay = [e for e in pay if e.high >= 5]
    if not pay:
        return None
    chosen = max(pay, key=lambda e: e.high)
    # A number that isn't pay takes its next word with it, so "2 weeks leave" and
    # "11.5% super" don't read as a period.
    ignore = [(e.start, _next_word_end(text, e.end)) for e in expressions if e not in pay]
    ignore += [(m.start(), _next_word_end(text, m.end()))
               for m in AMOUNT.finditer(text) if m["pct"]]
    period, stated = _period(text, (chosen.start, chosen.end), chosen.low, chosen.high, ignore,
                             country)
    return _annual(chosen.low, chosen.high, period, stated)


def parse_pay(value: object, country: str | None = None) -> Pay | None:
    """Annualised (min, max, estimated) from Indeed's salary object, free-text salary
    strings, or a list of them (each read on its own, then combined), in the job's country
    when known (it decides whether an amount with no period is monthly). estimated: no
    period was stated, so it was guessed from the amounts' size.

    Returns None when no amount can be found (for example "Competitive"), or when the
    amount is too large to be a real salary.
    """
    if isinstance(value, dict):
        low, high = value.get("min"), value.get("max")
        amounts = [float(a) for a in (low, high) if isinstance(a, (int, float)) and a > 0]
        if not amounts:
            return None
        stated = INDEED_PERIODS.get(str(value.get("type") or "").upper())
        period = stated or _period_from_amount(min(amounts), max(amounts), country)
        return _annual(min(amounts), max(amounts), period, bool(stated))
    if isinstance(value, list):
        parts = [parse_pay(v, country) for v in value if isinstance(v, str)]
        found = [part for part in parts if part]
        return (min(p[0] for p in found), max(p[1] for p in found),
                any(p[2] for p in found)) if found else None
    if not isinstance(value, str) or not value.strip():
        return None
    return _parse_text(value, country)
