"""API tests for the web server, using the synthetic fixture corpus."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from tests.fixtures.synthetic import write_raw


@pytest.fixture(scope="module")
def client():
    tmp = Path(tempfile.mkdtemp())
    os.environ.update({"ASOF_DATA_DIR": str(tmp), "EMBEDDING_MODEL": "lsa-fallback", "USE_RERANKER": "false",
                       "NEO4J_URI": "", "GROQ_API_KEY": ""})
    from asof.index import build_index
    from asof.web import server
    from starlette.testclient import TestClient

    write_raw(tmp / "raw")
    build_index(verbose=False)
    server._state.cache_clear()
    return TestClient(server.app)


def test_health_and_stats(client):
    h = client.get("/api/health").json()
    assert h["documents"] == 7 and h["offline_llm"] is True
    s = client.get("/api/stats").json()
    assert s["relations"]["SUPERSEDES"] == 3 and s["superseded"] == 3


def test_examples(client):
    ex = client.get("/api/examples").json()
    assert ex and all("question" in e for e in ex)


def test_ask_point_in_time_vs_naive(client):
    q = {"question": "What was the loan to value ratio for gold loans as of December 2020?"}
    a = client.post("/api/ask", json={**q, "system": "asof"}).json()
    assert a["parsed"]["dates"] == ["2020-12-31"]
    assert all(c["doc"]["in_force"] for c in a["contexts"])
    assert a["timelines"] and a["timelines"][0]["versions"]
    n = client.post("/api/ask", json={**q, "system": "naive_rag"}).json()
    assert any(c["doc"]["in_force"] is False for c in n["contexts"])  # naive RAG retrieves the 2022 version


def test_ask_pinned_date_and_errors(client):
    a = client.post("/api/ask", json={"question": "gold loan LTV", "as_of": "2023-06-01"}).json()
    assert a["parsed"]["dates"] == ["2023-06-01"]
    assert {c["doc"]["doc_id"] for c in a["contexts"]} >= {"rbi-9003"}
    assert client.post("/api/ask", json={"question": ""}).status_code == 400
    assert client.post("/api/ask", json={"question": "x", "system": "nope"}).status_code == 400


def test_doc_endpoint(client):
    d = client.get("/api/doc/rbi-9001").json()
    types = {(r["direction"], r["type"]) for r in d["relations"]}
    assert ("in", "SUPERSEDES") in types and ("in", "AMENDS") in types
    assert [v["doc_id"] for v in d["timeline"]["versions"]][0] == "rbi-9001"
    assert client.get("/api/doc/nope").status_code == 404
    assert client.get("/").status_code == 200


def test_rate_limit(client, monkeypatch):
    from asof.web import server

    monkeypatch.setenv("ASK_LIMIT_PER_HOUR", "2")
    server._ASK_LOG.clear()
    codes = [client.post("/api/ask", json={"question": "kyc rules", "as_of": "2021-03-01"}).status_code for _ in range(3)]
    assert codes[:2] == [200, 200] and codes[2] == 429
    monkeypatch.setenv("ASK_LIMIT_PER_HOUR", "0")
    server._ASK_LOG.clear()
    assert client.post("/api/ask", json={"question": "kyc rules"}).status_code == 200


def test_rate_limit_uses_trusted_forwarded_ip(client, monkeypatch):
    from asof.web import server

    monkeypatch.setenv("ASK_LIMIT_PER_HOUR", "1")
    monkeypatch.setenv("ASOF_CLIENT_IP_HEADER", "x-forwarded-for")
    server._ASK_LOG.clear()
    ask = lambda xff: client.post("/api/ask", json={"question": "kyc rules"}, headers={"x-forwarded-for": xff}).status_code
    assert ask("1.1.1.1, 9.9.9.9") == 200
    assert ask("2.2.2.2, 9.9.9.9") == 429      # forged first entry does not help; AWS-added last entry counts
    assert ask("9.9.9.9, 8.8.8.8") == 200      # a different real visitor
    monkeypatch.setenv("ASK_LIMIT_PER_HOUR", "0")
    monkeypatch.setenv("ASOF_CLIENT_IP_HEADER", "")
    server._ASK_LOG.clear()


def test_keep_warm_event(client):
    assert client.post("/events", json={"asof": "warm"}).json() == {"ok": True}
