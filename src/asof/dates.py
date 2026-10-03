"""Dependency-free date parsing for RBI documents and user questions."""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Optional

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9

_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"

# "December 29, 2023" / "Dec 29 2023"
RE_MDY = re.compile(rf"\b{_MON}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I)
# "29 December 2023" / "29th Dec, 2023"
RE_DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON}\.?,?\s+(\d{{4}})\b", re.I)
# "29.12.2023" / "29/12/2023" / "29-12-2023" (Indian day-first convention)
RE_NUM = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b")
# "2023-12-29"
RE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# "March 2024" (month + year, no day)
RE_MY = re.compile(rf"\b{_MON}\.?,?\s+(\d{{4}})\b", re.I)
# bare year "in 2019"
RE_Y = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")


def _safe(y: int, m: int, d: int) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def find_dates(text: str) -> list[tuple[int, date]]:
    """Return (char_offset, date) for every full date (day+month+year) in text."""
    out: list[tuple[int, date]] = []
    for m in RE_MDY.finditer(text):
        d = _safe(int(m.group(3)), MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
        if d:
            out.append((m.start(), d))
    for m in RE_DMY.finditer(text):
        d = _safe(int(m.group(3)), MONTHS[m.group(2).lower()[:3]], int(m.group(1)))
        if d:
            out.append((m.start(), d))
    for m in RE_ISO.finditer(text):
        d = _safe(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            out.append((m.start(), d))
    for m in RE_NUM.finditer(text):
        d = _safe(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if d:
            out.append((m.start(), d))
    out.sort(key=lambda x: x[0])
    # de-duplicate overlapping matches at the same offset
    seen, uniq = set(), []
    for off, d in out:
        if off not in seen:
            uniq.append((off, d))
            seen.add(off)
    return uniq


def first_date(text: str) -> Optional[date]:
    found = find_dates(text)
    return found[0][1] if found else None


def parse_loose_date(text: str, *, end_of_period: bool = False) -> Optional[date]:
    """Parse the first date-like expression in a user question.

    Handles full dates, "March 2024" and bare years. For partial dates, returns the
    last day of the period when ``end_of_period`` is True (useful for "as of 2021"),
    otherwise the first day.
    """
    full = first_date(text)
    if full:
        return full
    m = RE_MY.search(text)
    if m:
        y, mon = int(m.group(2)), MONTHS[m.group(1).lower()[:3]]
        if end_of_period:
            return date(y, mon, calendar.monthrange(y, mon)[1])
        return date(y, mon, 1)
    m = RE_Y.search(text)
    if m:
        y = int(m.group(1))
        return date(y, 12, 31) if end_of_period else date(y, 1, 1)
    return None


def day_before(d: date) -> date:
    return d - timedelta(days=1)


def dates_mentioned(text: str) -> list[date]:
    """Full dates plus month-year mentions ("June 2026" -> 1 June 2026), for temporal checks."""
    out = [d for _, d in find_dates(text)]
    spans = [(m.start(), m.end()) for rx in (RE_MDY, RE_DMY) for m in rx.finditer(text)]
    for m in RE_MY.finditer(text):
        if not any(s <= m.start() < e for s, e in spans):
            out.append(date(int(m.group(2)), MONTHS[m.group(1).lower()[:3]], 1))
    return out
