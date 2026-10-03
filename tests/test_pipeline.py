"""End-to-end tests on the synthetic fixture corpus (no network, no API key needed).

Run:  pytest -q
"""
from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

import pytest

from asof.agent.graph import ask, build_agent
from asof.agent.llm import ExtractiveLLM
from asof.agent.query import parse_query
from asof.config import Settings
from asof.dates import find_dates, parse_loose_date
from asof.eval import questions as qmod
from asof.eval.run_eval import evaluate
from asof.index import build_index, load_index
from asof.ingest.chunker import chunk_document
from asof.ingest.parse import parse_raw_dir
from asof.ingest.relations import extract_relations
from asof.graph.temporal import compute_validity
from asof.models import AMENDS, REFERS_TO, SUPERSEDES
from tests.fixtures.synthetic import write_raw

TODAY = date(2026, 9, 27)


@pytest.fixture(scope="module")
def index():
    tmp = Path(tempfile.mkdtemp())
    s = Settings()
    s.data_dir = tmp
    s.embedding_model = "lsa-fallback"
    s.use_reranker = False
    s.neo4j_uri = ""
    write_raw(s.raw_dir)
    build_index(s, verbose=False)
    return load_index(s)


# ---------------- dates & query understanding ----------------
def test_find_dates_formats():
    text = "dated December 29, 2023 and 01.04.2022 and 5th June 2024 and 2021-03-31"
    assert [d for _, d in find_dates(text)] == [date(2023, 12, 29), date(2022, 4, 1), date(2024, 6, 5), date(2021, 3, 31)]


def test_parse_loose_date_periods():
    assert parse_loose_date("as of March 2021", end_of_period=True) == date(2021, 3, 31)
    assert parse_loose_date("in 2019", end_of_period=True) == date(2019, 12, 31)


def test_query_intents():
    q = parse_query("What was the gold loan LTV as of March 2021?", today=TODAY)
    assert q.intent == "as_of" and q.dates == [date(2021, 3, 31)]
    assert "2021" not in q.search_query and "gold loan LTV" in q.search_query
    q = parse_query("How did KYC updation rules change between January 2020 and January 2024?", today=TODAY)
    assert q.intent == "compare" and q.dates == [date(2020, 1, 31), date(2024, 1, 31)]
    q = parse_query("KYC rules before July 2022", today=TODAY)
    assert q.dates == [date(2022, 6, 30)]
    assert parse_query("current KYC rules", today=TODAY).intent == "current"
    assert parse_query("history of gold loan rules", today=TODAY).intent == "history"


# ---------------- ingestion ----------------
def _docs():
    tmp = Path(tempfile.mkdtemp())
    write_raw(tmp)
    return {d.doc_id: d for d in parse_raw_dir(tmp)}


def test_parse_metadata():
    docs = _docs()
    assert len(docs) == 7
    md = docs["rbi-9003"]
    assert md.ref_no == "RBI/2022-23/05"
    assert md.dept_ref == "DOR.CRE.REC.No.05/21.04.048/2022-23"
    assert md.issue_date == date(2022, 4, 1)
    assert md.effective_date == date(2022, 5, 1)  # "come into force with effect from May 1, 2022"
    assert md.doc_type == "master_direction"
    assert docs["rbi-9001"].doc_type == "master_circular"


def test_relations_and_validity():
    docs = _docs()
    rels, _ = extract_relations(list(docs.values()))
    got = {(r.source_id, r.target_id): r.rel_type for r in rels}
    assert got[("rbi-9002", "rbi-9001")] == AMENDS
    assert got[("rbi-9003", "rbi-9001")] == SUPERSEDES
    assert got[("rbi-9003", "rbi-9002")] == SUPERSEDES
    assert got[("rbi-9005", "rbi-9004")] == SUPERSEDES
    assert got[("rbi-9006", "rbi-9004")] == REFERS_TO
    assert got[("rbi-9007", "rbi-9005")] == AMENDS
    compute_validity(list(docs.values()), rels)
    assert docs["rbi-9001"].valid_to == date(2022, 5, 1)
    assert docs["rbi-9004"].valid_to == date(2023, 1, 10)
    assert docs["rbi-9005"].valid_to is None


def test_chunks_have_context_header():
    doc = _docs()["rbi-9001"]
    chunks = chunk_document(doc, max_chars=200)
    assert len(chunks) >= 2
    assert all(c.text.startswith("[Master Circular") for c in chunks)


# ---------------- graph ----------------
def test_version_as_of(index):
    st = index.store
    assert st.version_as_of("rbi-9001", date(2021, 1, 1)).doc_id == "rbi-9001"
    assert st.version_as_of("rbi-9001", date(2023, 1, 1)).doc_id == "rbi-9003"
    assert st.version_as_of("rbi-9003", date(2020, 1, 1)).doc_id == "rbi-9001"  # walks backwards
    assert st.version_as_of("rbi-9003", date(2018, 1, 1)) is None
    assert [r.source_id for r in st.amendments_as_of("rbi-9001", date(2021, 1, 1))] == ["rbi-9002"]
    assert st.amendments_as_of("rbi-9001", date(2020, 1, 1)) == []
    assert [d.doc_id for d in st.lineage("rbi-9001")] == ["rbi-9001", "rbi-9002", "rbi-9003"]


# ---------------- retrieval ----------------
def test_temporal_filter(index):
    hits = index.retriever.search("loan to value ratio gold ornaments", as_of=date(2021, 1, 1))
    assert hits and all(h.doc.in_force_on(date(2021, 1, 1)) for h in hits)
    assert "rbi-9003" not in {h.doc.doc_id for h in hits}
    naive = index.retriever.search("loan to value ratio gold ornaments", as_of=None)
    assert "rbi-9003" in {h.doc.doc_id for h in naive}  # naive RAG mixes versions


# ---------------- agent ----------------
def test_agent_amendment_expansion(index):
    agent = build_agent(index, ExtractiveLLM(), k=2)
    res = ask(agent, "What was the LTV limit for gold loans as of December 2020?", today=TODAY)
    ids = [c.doc.doc_id for c in res["contexts"]]
    assert "rbi-9001" in ids and "rbi-9002" in ids  # base circular + its amendment
    assert "rbi-9003" not in ids
    assert res["citations"], res["answer"]
    assert [t["step"] for t in res["trace"]] == ["understand", "retrieve", "graph", "generate", "verify"]


def test_agent_compare(index):
    agent = build_agent(index, ExtractiveLLM())
    res = ask(agent, "How did periodic KYC updation change between March 2020 and March 2024?", today=TODAY)
    assert res["parsed"].intent == "compare"
    labels = [g[0] for g in res["groups"]]
    assert len(labels) == 2
    first, second = ({c.doc.doc_id for c in g[2]} for g in res["groups"])
    assert "rbi-9004" in first and "rbi-9005" not in first
    assert "rbi-9005" in second and "rbi-9004" not in second


def test_verify_drops_bad_citations(index):
    class BadLLM(ExtractiveLLM):
        def complete(self, system, user, **kw):
            return "LTV is 75% [1] and something else [99]."
    agent = build_agent(index, BadLLM())
    res = ask(agent, "gold loan LTV as of 2019", today=TODAY)
    assert "[99]" not in res["answer"] and [c["n"] for c in res["citations"]] == [1]


# ---------------- evaluation ----------------
def test_eval_asof_beats_naive(index):
    qs = qmod.generate(index.store, today=TODAY)
    assert {q["kind"] for q in qs} >= {"before_replacement", "after_replacement", "after_amendment"}
    rep = evaluate(index, qs, k=3, today=TODAY, verbose=False)
    naive, asof = rep["systems"]["naive_rag"]["overall"], rep["systems"]["asof"]["overall"]
    assert asof["stale"] == 0.0
    assert asof["stale"] < naive["stale"]
    assert asof["hit"] >= naive["hit"]
    assert asof["amend_recall"] == 1.0


# ---------------- real-page structure (regressions found on the live RBI corpus) ----------------
NESTED_PAGE = """<html><body><table class="tablebg"><tr><td class="tableheader"><b>Master Circular - Housing Finance
(Updated as on July 1, 2023)</b></td></tr><tr><td>
<p>(</p><p>168 kb</p><p>)</p>
<p>RBI/2023-24/10 DOR.CRE.REC.No.1/08.12.001/2023-24</p><p>July 1, 2023</p>
<p>1. This Master Circular consolidates instructions on housing finance.</p>
<p>2. Banks shall ensure loan to value ratios are within the limits.</p>
<p>Annex - List of circulars consolidated in the Master Circular</p>
<table class="tablebg"><tr><td>1.</td><td>RBI/2019-20/5 DOR.CRE.No.9/08.12.001/2019-20</td><td>July 1, 2019</td></tr></table>
</td></tr></table></body></html>"""


def test_main_content_is_whole_notice_not_annex():
    from asof.ingest.parse import parse_html

    d = parse_html(NESTED_PAGE, {"num": "1", "url": ""})
    assert d.ref_no == "RBI/2023-24/10" and d.issue_date == date(2023, 7, 1)
    assert "consolidates instructions on housing finance" in d.text
    assert "168 kb" not in d.text
    assert d.title == "Master Circular - Housing Finance"


def test_master_circular_consolidation_list_is_not_supersession():
    from asof.ingest.parse import parse_html

    mc = parse_html(NESTED_PAGE, {"num": "1", "url": ""})
    old = parse_html(NESTED_PAGE.replace("RBI/2023-24/10 DOR.CRE.REC.No.1/08.12.001/2023-24", "RBI/2019-20/5 DOR.CRE.No.9/08.12.001/2019-20")
                     .replace("July 1, 2023", "July 1, 2019").replace("Master Circular - Housing Finance", "Housing loans"),
                     {"num": "2", "url": ""})
    rels, _ = extract_relations([mc, old])
    kinds = {(r.source_id, r.target_id): (r.rel_type, r.method) for r in rels}
    assert kinds[("rbi-1", "rbi-2")] == (REFERS_TO, "consolidation_list")


def test_bm25_ranks_exact_terms():
    from asof.retrieval.bm25 import BM25

    bm = BM25(["liquidity coverage ratio LCR for banks", "gold loan to value", "LCR LCR reporting"])
    s = bm.scores("LCR")
    assert s[2] > s[0] > 0 and s[1] == 0


def test_updated_in_place_is_detected():
    from asof.ingest.parse import parse_html

    page = NESTED_PAGE.replace("Master Circular - Housing Finance", "Master Direction - KYC Direction, 2016")
    d = parse_html(page, {"num": "3", "url": ""})
    assert d.updated_on == date(2023, 7, 1) or d.updated_on is None  # "(Updated as on July 1, 2023)" == issue date -> None
    page2 = page.replace("Updated as on July 1, 2023", "Updated as on June 12, 2025")
    d2 = parse_html(page2, {"num": "4", "url": ""})
    assert d2.updated_on == date(2025, 6, 12)
    assert d2.title == "Master Direction - KYC Direction, 2016"


def test_redaction_and_temporal_guard():
    from asof.agent.prompts import redact_later_sentences
    from asof.dates import dates_mentioned

    txt = "Low risk customers every ten years. Refresh by June 30, 2026, whichever is later. High risk every two years."
    out = redact_later_sentences(txt, date(2021, 3, 31))
    assert "2026" not in out and "ten years" in out and "omitted" in out
    assert date(2026, 6, 1) in dates_mentioned("until June 2026")



def test_temporal_guard_flags_later_dates(index):
    class LateLLM(ExtractiveLLM):
        def complete(self, system, user, **kw):
            return "As of 31 March 2021, update KYC by 30 Jun 2026 [1]."

    agent = build_agent(index, LateLLM())
    res = ask(agent, "KYC updation rules as of March 2021", today=TODAY)
    assert "after the reference date" in res["answer"] and "30 Jun 2026" in res["answer"]


def test_temporal_guard_ignores_source_metadata_dates(index):
    class MetaLLM(ExtractiveLLM):
        def complete(self, system, user, **kw):
            return "As of 31 Dec 2020 the LTV was 90 per cent [1]. The circular was replaced on 1 May 2022."

    agent = build_agent(index, MetaLLM())
    res = ask(agent, "gold loan LTV as of December 2020", today=TODAY)
    assert "after the reference date" not in res["answer"]  # 1 May 2022 is the replacement date of a source


def test_normalize_citations_from_browsing_style_markers():
    from asof.agent.nodes import normalize_citations

    s = "Every 2 years【1†L38-L41】; board policy【2】 and notices [3†L1-L4]. Plain [4] stays."
    assert normalize_citations(s) == "Every 2 years[1]; board policy[2] and notices [3]. Plain [4] stays."
