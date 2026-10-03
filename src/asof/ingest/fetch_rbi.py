"""Download RBI regulatory documents (run this on your own computer).

Three sources are supported:

  md   - every Master Direction linked from the Master Directions page
  mc   - every Master Circular linked from the Master Circulars page
  ids  - a sweep over notification ids (NotificationUser.aspx?Id=N), which covers
         ordinary circulars and older versions of master circulars

Pages are cached in data/raw/, so re-running only fetches what is missing.
Please be polite to the RBI website: keep the default delay (1.5 s) or higher.

Examples
  python -m asof.ingest.fetch_rbi --source md
  python -m asof.ingest.fetch_rbi --source mc
  python -m asof.ingest.fetch_rbi --source ids --start 12400 --end 12900
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from asof.config import get_settings

BASE = "https://www.rbi.org.in/Scripts/"
LISTINGS = {
    "md": (BASE + "BS_ViewMasDirections.aspx", re.compile(r"BS_ViewMasDirections\.aspx\?id=(\d+)", re.I)),
    "mc": (BASE + "BS_ViewMasterCirculars.aspx", re.compile(r"BS_ViewMasterCirculars\.aspx\?Id=(\d+)", re.I)),
}
DETAIL_URL = {
    "md": BASE + "BS_ViewMasDirections.aspx?id={id}",
    "mc": BASE + "BS_ViewMasterCirculars.aspx?Id={id}&Mode=0",
    "ids": BASE + "NotificationUser.aspx?Id={id}&Mode=0",
}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 asof-research-project"
    )
}


def _get(session, url: str, retries: int = 3, timeout: int = 30) -> str | None:
    for attempt in range(retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200 and len(r.text) > 500:
                return r.text
            if r.status_code == 404:
                return None
        except Exception as e:  # network hiccup - back off and retry
            print(f"  ! {type(e).__name__} on {url} (attempt {attempt + 1})")
        time.sleep(2 * (attempt + 1))
    return None


def _listing(session, source: str) -> list[tuple[str, str]]:
    """Return [(id, anchor_text)] from a listing page."""
    from bs4 import BeautifulSoup

    url, pattern = LISTINGS[source]
    html = _get(session, url)
    if not html:
        raise SystemExit(f"Could not load listing page {url}")
    soup = BeautifulSoup(html, "html.parser")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        m = pattern.search(a["href"])
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append((m.group(1), a.get_text(" ", strip=True)))
    return out


def fetch(source: str, start: int = 0, end: int = 0, delay: float = 1.5, limit: int = 0) -> int:
    import requests

    settings = get_settings()
    raw_dir = settings.raw_dir
    raw_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()

    if source in LISTINGS:
        items = _listing(session, source)
        print(f"Found {len(items)} documents on the {source.upper()} listing page")
    else:
        items = [(str(i), "") for i in range(start, end + 1)]

    if limit:
        items = items[:limit]

    saved = 0
    for n, (doc_num, listing_title) in enumerate(items, 1):
        html_path = raw_dir / f"{source}_{doc_num}.html"
        meta_path = raw_dir / f"{source}_{doc_num}.json"
        miss_path = raw_dir / f"{source}_{doc_num}.miss"
        if html_path.exists() or miss_path.exists():
            continue
        url = DETAIL_URL[source].format(id=doc_num)
        html = _get(session, url)
        time.sleep(delay)
        if html is None:
            continue  # network problem - try again on the next run
        if "RBI/" not in html:
            miss_path.write_text("", encoding="utf-8")  # empty id / not a regulatory document: don't ask again
            continue
        html_path.write_text(html, encoding="utf-8")
        meta_path.write_text(
            json.dumps({"source": source, "num": doc_num, "url": url, "listing_title": listing_title}),
            encoding="utf-8",
        )
        saved += 1
        if saved % 10 == 0:
            print(f"  saved {saved} (checked {n}/{len(items)})")
    print(f"Done: {saved} new pages saved to {raw_dir}")
    return saved


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=["md", "mc", "ids"], required=True)
    p.add_argument("--start", type=int, default=0, help="first notification id (ids mode)")
    p.add_argument("--end", type=int, default=0, help="last notification id (ids mode)")
    p.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    p.add_argument("--limit", type=int, default=0, help="stop after N documents (0 = no limit)")
    a = p.parse_args()
    if a.source == "ids" and not (a.start and a.end):
        p.error("--source ids needs --start and --end")
    fetch(a.source, a.start, a.end, a.delay, a.limit)


if __name__ == "__main__":
    main()
