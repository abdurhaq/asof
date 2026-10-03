"""Turn cached RBI HTML pages into clean ``Document`` objects."""
from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Iterable, Optional

from asof.dates import find_dates, first_date
from asof.models import Document

RE_REF_NO = re.compile(r"\bRBI/(?:[A-Z]{2,8}/)?\d{4}-\d{2,4}/\d{1,4}\b")
# Department reference, e.g. "DOR.MCS.REC.No.89/01-01-032/2025-26" or "DBR.No.BP.BC.1/21.06.201/2015-16"
RE_DEPT_REF = re.compile(
    r"\b[A-Z][A-Za-z&]{1,9}(?:\s?\.\s?[A-Za-z0-9&()]{1,12}){0,6}\s?\.?\s?No\.?\s?[A-Za-z0-9.&()\s-]{0,30}?"
    r"(?:/[A-Za-z0-9.()\-]{1,20}){1,3}/\d{4}-\d{2}\b"
)
RE_EFFECTIVE = re.compile(
    r"(?:come|comes|came)\s+into\s+(?:force|effect)|with\s+effect\s+from|w\.\s?e\.\s?f\.?|effective\s+from|shall\s+be\s+effective",
    re.I,
)
RE_IMMEDIATE = re.compile(r"\bimmediate(?:ly)?\b|from\s+the\s+date\s+of\s+(?:issue|this\s+circular)", re.I)
RE_UPDATED = re.compile(r"updated\s+(?:as\s+on|as\s+of|upto|up\s+to|till|on)\s*", re.I)
RE_YEARS = re.compile(r"\b(19|20)\d{2}(-\d{2,4})?\b")


def norm_ref(ref: str) -> str:
    """Normalise a reference number for matching ("DBR. No. BP.BC.1/..." -> "dbr.no.bp.bc.1/...")."""
    return re.sub(r"\s+", "", ref).lower().rstrip(".")


def normalise_title(title: str) -> str:
    """Title without years/boilerplate - used to link successive versions of the same document."""
    t = title.lower()
    t = re.sub(r"master\s+(circular|direction)s?", " ", t)
    t = re.sub(r"reserve bank of india|\(|\)|directions?|updated|as on.*$", " ", t)
    t = RE_YEARS.sub(" ", t)
    t = re.sub(r"[^a-z ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def classify_doc_type(title: str) -> str:
    t = title.lower()
    if "master direction" in t or re.search(r"\)\s*directions?,?\s*\d{4}", t):
        return "master_direction"
    if "master circular" in t:
        return "master_circular"
    return "circular"


def _doc_type(title: str, text: str) -> str:
    t = classify_doc_type(title)
    if t == "circular":
        head = text[:1500].lower()
        if re.search(r"these\s+directions\s+(shall|may)\s+be\s+called|\)\s*directions?,\s*(19|20)\d{2}", head):
            return "master_direction"
    return t


def extract_effective_date(text: str, issue_date) -> tuple:
    """Return (effective_date, evidence). Falls back to the issue date.

    Only the opening part of the notice is searched: Master Directions updated in place
    mention later "with effect from" dates of individual amendments further down."""
    head = text[: max(4000, len(text) // 4)]
    for m in RE_EFFECTIVE.finditer(head):
        window = head[m.start(): m.end() + 160]
        dates = find_dates(window)
        if dates and dates[0][1] >= issue_date - timedelta(days=31):
            return dates[0][1], window.strip()
        if RE_IMMEDIATE.search(window):
            return issue_date, window.strip()
    return issue_date, ""


RE_JUNK_LINE = re.compile(r"^\s*(\(|\)|\d+(\.\d+)?\s*[kKmM][bB]|print|download|hindi|english)\s*$", re.I)


def _clean_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    lines = [l for l in text.split("\n") if not RE_JUNK_LINE.match(l)]  # "( 168 kb )" PDF-size links etc.
    text = "\n".join(lines)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def clean_title(title: str) -> str:
    """Display title: drop '(Updated as on ...)', '[notes]' and a trailing 'dated <date>'."""
    t = re.sub(r"\s+", " ", title).strip()
    t = re.sub(r"\(\s*updated\s+(as\s+on|upto|till)[^)]*\)", "", t, flags=re.I)
    t = re.sub(r"\[[^\]]*\]?$", "", t)  # trailing bracketed note (sometimes unterminated)
    t = re.sub(r"\s+dated\s+\S+\s+\d{1,2},?\s+\d{4}\s*$", "", t, flags=re.I)
    return t.strip(" -\u2013,") or title.strip()


def _main_content(soup):
    """Find the element that holds the whole notice on an RBI page.

    RBI pages nest the notice in ``table.tablebg``; annex tables inside it are also
    ``tablebg`` and can contain reference numbers, so take the LARGEST block that holds
    a reference number (the whole notice), never the smallest (which may be just an annex).
    """
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    candidates = soup.select("table.tablebg, div#doublescroll, #example-min, td.tablecontent2, td.tablecontent1")
    candidates = [c for c in candidates if RE_REF_NO.search(c.get_text(" "))]
    if candidates:
        tables = [c for c in candidates if c.name == "table"]
        return max(tables or candidates, key=lambda c: len(c.get_text()))
    return soup.body or soup


SITE_WORDS = re.compile(r"^(notifications?|master (directions?|circulars?)|press releases?|reserve bank of india|home|"
                        r"circulars?|what's new|print|download)$", re.I)


def _good_title(t: str) -> bool:
    return (12 < len(t) < 300 and not RE_REF_NO.search(t) and not SITE_WORDS.match(t)
            and not t.lower().startswith(("madam", "dear", "all ", "the chairman", "chairman"))
            and not RE_DEPT_REF.fullmatch(t) and first_date(t) is None or ("direction" in t.lower() and 12 < len(t) < 300))


def _title(soup, content, content_text: str, listing_title: str) -> str:
    if listing_title and len(listing_title) > 8:
        return listing_title
    # 1) header cell of the notice, 2) first bold line inside the notice, 3) anywhere on the page
    for scope, sels in ((soup, ("td.tableheader", ".tableheader")), (content, ("b", "strong", "h1", "h2", "h3")),
                        (soup, ("h1", "h2"))):
        for sel in sels:
            for el in scope.select(sel):
                t = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
                if _good_title(t):
                    return t
    lines = [l.strip() for l in content_text.splitlines() if _good_title(l.strip())]
    return lines[0] if lines else "Untitled"


def parse_html(html: str, meta: dict) -> Optional[Document]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    content = _main_content(soup)
    for br in content.find_all("br"):
        br.replace_with("\n")
    for p in content.find_all(["p", "tr", "li", "div"]):
        p.insert_after("\n")
    text = _clean_text(content.get_text("\n"))

    ref = RE_REF_NO.search(text)
    if not ref:
        return None
    head = text[ref.start(): ref.start() + 600]
    issue_date = first_date(head) or first_date(text)
    if not issue_date:
        return None
    dept = RE_DEPT_REF.search(head)
    raw_title = _title(soup, content, text, meta.get("listing_title", ""))
    title = clean_title(raw_title)
    updated_on = None
    for src in (raw_title, text[:1500]):
        m = RE_UPDATED.search(src)
        if m:
            updated_on = first_date(src[m.end(): m.end() + 40])
            if updated_on:
                break
    effective, _ = extract_effective_date(text, issue_date)
    return Document(
        doc_id=f"rbi-{meta['num']}",
        title=title,
        ref_no=ref.group(0),
        dept_ref=dept.group(0).strip() if dept else "",
        issue_date=issue_date,
        effective_date=effective,
        doc_type=_doc_type(title, text),
        url=meta.get("url", ""),
        text=text,
        updated_on=updated_on if updated_on and updated_on > issue_date else None,
    )


def parse_raw_dir(raw_dir: Path) -> list[Document]:
    """Parse every cached page, de-duplicating the same document fetched via different listings."""
    docs: dict[str, Document] = {}
    for html_path in sorted(raw_dir.glob("*.html")):
        meta_path = html_path.with_suffix(".json")
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {
            "num": html_path.stem.split("_")[-1]
        }
        try:
            doc = parse_html(html_path.read_text(encoding="utf-8", errors="ignore"), meta)
        except Exception as e:  # one bad page should not stop the pipeline
            print(f"  ! could not parse {html_path.name}: {e}")
            continue
        if doc is None:
            continue
        key = norm_ref(doc.ref_no)
        # keep the version with the longest text (listing pages sometimes truncate)
        if key not in docs or len(doc.text) > len(docs[key].text):
            docs[key] = doc
    return list(docs.values())


def save_documents(docs: Iterable[Document], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for d in docs:
            f.write(json.dumps(d.to_dict(), ensure_ascii=False) + "\n")


def load_documents(path: Path) -> list[Document]:
    with path.open(encoding="utf-8") as f:
        return [Document.from_dict(json.loads(line)) for line in f if line.strip()]
