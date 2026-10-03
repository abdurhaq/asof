"""Deterministic query understanding: reference date(s) and intent.

Kept rule-based on purpose - it is fast, free, testable, and dates are exactly the thing
we cannot afford an LLM to get wrong.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from asof.dates import RE_DMY, RE_ISO, RE_MDY, RE_MY, RE_NUM, RE_Y, day_before, parse_loose_date

INTENT_CURRENT, INTENT_AS_OF, INTENT_COMPARE, INTENT_HISTORY = "current", "as_of", "compare", "history"

RE_HISTORY = re.compile(r"\b(history|evolution|evolved|timeline|over the years|how has .* changed|all versions)\b", re.I)
RE_COMPARE = re.compile(r"\b(between|compare|comparison|difference|differ|changed?\s+from|vs\.?|versus)\b", re.I)
RE_BEFORE = re.compile(r"\b(before|prior to|until|till)\s*$", re.I)
DATE_PATTERNS = (RE_MDY, RE_DMY, RE_ISO, RE_NUM, RE_MY, RE_Y)
FILLER = re.compile(
    r"\b(as\s+(of|on|at)|as\s+it\s+stood\s+(on|in)|in\s+force\s+(on|in)|on|in|during|before|after|prior\s+to|"
    r"until|till|between|and|from|to|what\s+(was|were|is|are)\s+the|what\s+(was|were|is|are))\s*$",
    re.I,
)


@dataclass
class ParsedQuery:
    question: str
    search_query: str
    intent: str
    dates: list[date] = field(default_factory=list)  # reference dates, oldest first

    @property
    def as_of(self) -> date:
        return self.dates[-1]


def _date_spans(q: str) -> list[tuple[int, int]]:
    spans = []
    for rx in DATE_PATTERNS:
        for m in rx.finditer(q):
            if not any(s <= m.start() < e for s, e in spans):
                spans.append((m.start(), m.end()))
    return sorted(spans)


def parse_query(question: str, today: Optional[date] = None) -> ParsedQuery:
    today = today or date.today()
    spans = _date_spans(question)
    dates: list[date] = []
    for s, e in spans:
        piece = question[s:e]
        before = bool(RE_BEFORE.search(question[max(0, s - 12): s]))
        d = parse_loose_date(piece, end_of_period=not before)
        if d is None:
            continue
        if before:  # "before July 2022" -> 30 June 2022
            d = day_before(d)
        dates.append(min(d, today))

    # search query = question with the date phrases (and the words that introduce them) removed
    sq, last = [], 0
    for s, e in spans:
        sq.append(FILLER.sub("", question[last:s]))
        last = e
    sq.append(question[last:])
    search_query = re.sub(r"\s+", " ", " ".join(sq)).strip(" ,?.") or question

    if RE_HISTORY.search(question):
        intent = INTENT_HISTORY
        dates = dates or [today]
    elif len(dates) >= 2 and RE_COMPARE.search(question):
        intent = INTENT_COMPARE
        dates = sorted(set(dates))[:2]
    elif dates:
        intent = INTENT_AS_OF
        dates = [dates[0]]
    else:
        intent = INTENT_CURRENT
        dates = [today]
    return ParsedQuery(question, search_query, intent, sorted(dates))
