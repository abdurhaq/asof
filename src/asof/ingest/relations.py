"""Extract SUPERSEDES / AMENDS / REFERS_TO links between regulatory documents.

Three complementary signals are combined:

1. Sentence cues   - a reference number (or "circular dated <date>") in a sentence with
                     cue words such as "in supersession of" or "partial modification".
2. Repeal annexes  - Master Directions end with "List of circulars repealed/consolidated";
                     every reference below that heading is superseded.
3. Title lineage   - master circulars are re-issued every year under the same title;
                     each new edition supersedes the previous one.
"""
from __future__ import annotations

import bisect
import re
from collections import defaultdict
from dataclasses import dataclass

from asof.dates import find_dates
from asof.ingest.parse import RE_DEPT_REF, RE_REF_NO, norm_ref, normalise_title
from asof.models import AMENDS, REFERS_TO, SUPERSEDES, Document, Relation

SUPERSEDE_CUES = re.compile(
    r"in\s+supersession\s+of|supersed(?:e|es|ed|ing)|stands?\s+repealed|hereby\s+repealed|"
    r"(?:are|is|be|been|shall\s+stand)\s+(?:hereby\s+)?(?:repealed|withdrawn|rescinded|cancelled)|"
    r"\brepeal(?:ed|s)?\b|\brescind",
    re.I,
)
AMEND_CUES = re.compile(
    r"partial\s+modification|\bamend(?:ed|ment|ments|ing|s)?\b|\bmodif(?:y|ied|ication)\b|"
    r"\bsubstitut(?:e|ed|ion)\b|\binserted\b|\brevis(?:e|ed|ion)\b|\bshall\s+be\s+read\s+as\b",
    re.I,
)
REPEAL_ANNEX_HEADING = re.compile(
    r"(?:list\s+of\s+)?(?:circulars?|notifications?|instructions?|directions?)[^\n]{0,80}"
    r"(repealed|consolidated|withdrawn|superseded)",
    re.I,
)
# Titles of circulars whose whole purpose is to change an earlier instruction
AMENDMENT_TITLE = re.compile(r"^\s*(amendments?|modifications?|revision|revised|changes?|partial\s+modification)\b|"
                             r"\bamendments?\s+to\b", re.I)
CONTEXT_BEFORE = 250  # characters before a reference that are read for cue words
RE_DATED = re.compile(r"(?:circular|notification|letter|direction)s?[^.\n]{0,60}?dated\s+", re.I)
RE_SENTENCE_SPLIT = re.compile(r"(?<=[.;:])\s+(?=[A-Z(\d])|\n+")


@dataclass
class _Ref:
    key: str          # normalised reference number, or "date:YYYY-MM-DD"
    raw: str
    sentence: str
    offset: int


def _sentences(text: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for part in RE_SENTENCE_SPLIT.split(text):
        idx = text.find(part, pos)
        if part.strip():
            out.append((idx, part.strip()))
        pos = max(pos, idx + len(part))
    return out


def find_references(doc: Document) -> list[_Ref]:
    own = {norm_ref(doc.ref_no), norm_ref(doc.dept_ref)} if doc.dept_ref else {norm_ref(doc.ref_no)}
    refs: list[_Ref] = []
    for offset, sent in _sentences(doc.text):
        found_number = False
        for rx in (RE_DEPT_REF, RE_REF_NO):
            for m in rx.finditer(sent):
                key = norm_ref(m.group(0))
                if key in own:
                    continue
                found_number = True
                refs.append(_Ref(key, m.group(0), sent, offset + m.start()))
        if not found_number:  # fall back to "circular dated <date>"
            for m in RE_DATED.finditer(sent):
                dates = find_dates(sent[m.end(): m.end() + 40])
                if dates and dates[0][1] != doc.issue_date:
                    refs.append(_Ref(f"date:{dates[0][1].isoformat()}", m.group(0), sent, offset + m.start()))
    return refs


def _classify(sentence: str) -> str:
    if SUPERSEDE_CUES.search(sentence):
        return SUPERSEDES
    if AMEND_CUES.search(sentence):
        return AMENDS
    return REFERS_TO


def _context(text: str, offset: int) -> str:
    """Text that governs a reference: the start of its own line, plus the list introduction
    ("...the following circulars stand withdrawn:") when the reference is a list item."""
    line_start = text.rfind("\n", 0, offset) + 1
    ctx = text[max(line_start, offset - CONTEXT_BEFORE): offset]
    before = text[max(0, line_start - 600): line_start]
    intro = [l for l in before.split("\n") if l.strip().endswith(":")]
    if intro:
        ctx = intro[-1] + " " + ctx
    return ctx


def _annex_start(text: str) -> tuple[int, str]:
    """(offset, kind) of the 'list of circulars repealed/consolidated' section, or (-1, "")."""
    matches = list(REPEAL_ANNEX_HEADING.finditer(text))
    # the annex is near the end; ignore mentions in the first 40% of the document
    matches = [m for m in matches if m.start() > 0.4 * len(text)]
    if not matches:
        return -1, ""
    return matches[0].start(), matches[0].group(1).lower()


class RefResolver:
    """Maps a reference key to a document id in the corpus."""

    def __init__(self, docs: list[Document]):
        self.by_ref: dict[str, str] = {}
        self.by_date: dict[str, list[str]] = defaultdict(list)
        for d in docs:
            self.by_ref[norm_ref(d.ref_no)] = d.doc_id
            if d.dept_ref:
                self.by_ref[norm_ref(d.dept_ref)] = d.doc_id
            self.by_date[d.issue_date.isoformat()].append(d.doc_id)
        self._sorted = sorted(self.by_ref)

    def resolve(self, key: str) -> str | None:
        if key.startswith("date:"):
            ids = self.by_date.get(key[5:], [])
            return ids[0] if len(ids) == 1 else None  # ambiguous dates are skipped
        if key in self.by_ref:
            return self.by_ref[key]
        # tolerate small formatting differences (e.g. a trailing suffix) via sorted-neighbour prefix check
        if len(key) > 15:
            i = bisect.bisect_left(self._sorted, key)
            for j in (i - 1, i):
                if 0 <= j < len(self._sorted):
                    k = self._sorted[j]
                    if len(k) > 15 and (k.startswith(key) or key.startswith(k)):
                        return self.by_ref[k]
        return None


def extract_relations(docs: list[Document]) -> tuple[list[Relation], dict]:
    """Return (relations, stats)."""
    resolver = RefResolver(docs)
    by_id = {d.doc_id: d for d in docs}
    best: dict[tuple[str, str], Relation] = {}
    stats = defaultdict(int)
    rank = {SUPERSEDES: 3, AMENDS: 2, REFERS_TO: 1}

    def add(rel: Relation) -> None:
        if rel.source_id == rel.target_id:
            return
        src, tgt = by_id[rel.source_id], by_id[rel.target_id]
        if tgt.issue_date > src.issue_date:  # a document cannot change a later one
            return
        k = (rel.source_id, rel.target_id)
        if k not in best or rank[rel.rel_type] > rank[best[k].rel_type]:
            best[k] = rel

    for doc in docs:
        refs = find_references(doc)
        annex, annex_kind = _annex_start(doc.text)
        # A consolidation list repeals the listed circulars in a Master Direction, but a Master
        # Circular only compiles them (it is re-issued every year), so that list is just citations.
        annex_supersedes = annex_kind in ("repealed", "withdrawn", "superseded") or (
            annex_kind == "consolidated" and doc.doc_type == "master_direction")
        # doc-level signals for amendment circulars ("Please refer to X ... it has been decided to amend")
        amend_title = bool(AMENDMENT_TITLE.search(doc.title))
        opening_amends = doc.doc_type == "circular" and (amend_title or bool(AMEND_CUES.search(doc.text[:2000])))
        for r in refs:
            stats["references_found"] += 1
            target = resolver.resolve(r.key)
            if not target:
                stats["unresolved"] += 1
                continue
            stats["resolved"] += 1
            if annex >= 0 and r.offset >= annex:
                rel_type = SUPERSEDES if annex_supersedes else REFERS_TO
                method = "repeal_annex" if annex_supersedes else "consolidation_list"
            else:
                # read the sentence plus a little text before it: cue words often sit on the previous
                # line ("In supersession of the following circulars:" followed by a list)
                context = _context(doc.text, r.offset) + " " + r.sentence
                rel_type, method = _classify(r.sentence), "regex"
                if rel_type == REFERS_TO:
                    ctx_type = _classify(context)
                    if ctx_type != REFERS_TO:
                        rel_type, method = ctx_type, "regex_context"
                if rel_type == REFERS_TO and opening_amends and r.offset < 1500:
                    rel_type, method = AMENDS, "amendment_opening"
            evidence = r.sentence if method != "regex_context" else context.strip()
            add(Relation(doc.doc_id, target, rel_type, doc.effective_date, evidence[-400:], method))

    # title lineage for re-issued master circulars / directions
    groups: dict[tuple[str, str], list[Document]] = defaultdict(list)
    for d in docs:
        if d.doc_type in ("master_circular", "master_direction"):
            key = normalise_title(d.title)
            if len(key) > 8:
                groups[(d.doc_type, key)].append(d)
    for group in groups.values():
        group.sort(key=lambda d: d.issue_date)
        for older, newer in zip(group, group[1:]):
            if newer.issue_date > older.issue_date:
                add(Relation(newer.doc_id, older.doc_id, SUPERSEDES, newer.effective_date,
                             f"Re-issued edition of '{older.title}'", "title_lineage"))

    rels = list(best.values())
    for r in rels:
        stats[f"rel_{r.rel_type}"] += 1
        stats[f"method_{r.method}"] += 1
    return rels, dict(stats)
