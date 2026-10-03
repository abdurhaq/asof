"""`python -m asof.cli doctor` - check that everything the project needs is reachable.

Never prints secret values, only whether they are set and whether they work.
"""
from __future__ import annotations

import importlib
import sys


def _line(ok: bool | None, msg: str) -> None:
    tag = {True: "[ OK ]", False: "[FAIL]", None: "[WARN]"}[ok]
    print(f"{tag} {msg}", flush=True)


def run() -> int:
    from asof.config import get_settings

    s = get_settings()
    critical_ok = True

    v = sys.version_info
    py_ok = (3, 10) <= (v.major, v.minor) <= (3, 12)
    _line(py_ok if py_ok else None, f"Python {v.major}.{v.minor}.{v.micro} (3.10-3.12 recommended)")

    for mod in ("numpy", "sklearn", "networkx", "bs4", "requests", "langgraph", "langchain_groq",
                "neo4j", "sentence_transformers", "starlette", "uvicorn", "dotenv"):
        try:
            importlib.import_module(mod)
            _line(True, f"package {mod}")
        except Exception as e:  # noqa: BLE001
            critical_ok = False
            _line(False, f"package {mod}: {type(e).__name__}: {e}")

    _line(True, f"data folder: {s.data_dir}")

    # Groq
    if not s.groq_api_key:
        _line(None, "GROQ_API_KEY is empty - answers will use the offline extractive mode")
    else:
        try:
            from asof.agent.llm import GroqLLM

            llm = GroqLLM(s.groq_api_key, s.answer_model, s.fast_model)
            for fast, name in ((True, s.fast_model), (False, s.answer_model)):
                out = llm.complete("Reply with the single word OK.", "Say OK.", fast=fast, max_tokens=50)
                _line(bool(out), f"Groq model {name} replied {out[:20]!r}")
        except Exception as e:  # noqa: BLE001
            critical_ok = False
            _line(False, f"Groq API call failed: {type(e).__name__}: {str(e)[:200]}")

    # Neo4j
    if not s.neo4j_uri:
        _line(None, "NEO4J_URI is empty - the in-memory graph will be used")
    else:
        try:
            from asof.graph.store import open_neo4j_driver

            drv, user = open_neo4j_driver(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
            with drv:
                with drv.session(database=None if s.neo4j_database in ("", "neo4j") else s.neo4j_database) as sess:
                    n = sess.run("MATCH (d:Document) RETURN count(d) AS n").single()["n"]
            note = "" if user == s.neo4j_user else f" (logged in as '{user}', the instance id)"
            _line(True, f"Neo4j connection works{note}; {n} Document nodes stored")
        except Exception as e:  # noqa: BLE001
            critical_ok = False
            _line(False, f"Neo4j connection failed: {type(e).__name__}: {str(e)[:200]} "
                         "(check the AuraDB instance is Running, and copy NEO4J_URI / NEO4J_USERNAME / "
                         "NEO4J_PASSWORD exactly from the credentials file)")

    # RBI website
    try:
        import requests

        from asof.ingest.fetch_rbi import HEADERS, LISTINGS

        r = requests.get(LISTINGS["md"][0], headers=HEADERS, timeout=30)
        _line(r.status_code == 200, f"RBI website reachable (HTTP {r.status_code}, {len(r.text):,} bytes)")
        if r.status_code != 200:
            critical_ok = False
    except Exception as e:  # noqa: BLE001
        critical_ok = False
        _line(False, f"RBI website not reachable: {type(e).__name__}: {str(e)[:200]}")

    print("DOCTOR RESULT:", "ALL GOOD" if critical_ok else "PROBLEMS FOUND", flush=True)
    return 0 if critical_ok else 1
