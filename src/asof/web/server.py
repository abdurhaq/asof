"""HTTP API + static front-end (Starlette, served by uvicorn).

Endpoints
  GET  /                   the web app
  GET  /api/health         corpus / model / backend info
  GET  /api/stats          counts and a documents-per-year histogram
  GET  /api/examples       example questions generated from the amendment graph
  POST /api/ask            {question, as_of?, system?: "asof"|"naive_rag"}
  GET  /api/doc/{doc_id}   full document, its relations and version timeline
  POST /events             keep-warm ping (AWS Lambda scheduled event)
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import Counter, defaultdict, deque
from datetime import date
from functools import lru_cache
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from asof.agent.graph import ask, build_agent
from asof.agent.llm import ExtractiveLLM, make_llm
from asof.eval.questions import topic_from_title
from asof.eval.run_eval import SYSTEMS
from asof.models import AMENDS, REFERS_TO, SUPERSEDES

STATIC = Path(__file__).parent / "static"


@lru_cache(maxsize=1)
def _state():
    from asof.index import load_index

    idx = load_index()
    llm = make_llm(idx.settings)
    agents = {name: build_agent(idx, llm, k=idx.settings.top_k, **cfg) for name, cfg in SYSTEMS.items()}
    return idx, llm, agents


def _doc_meta(d, when: date | None = None) -> dict:
    m = {
        "doc_id": d.doc_id, "title": d.title, "ref_no": d.ref_no, "dept_ref": d.dept_ref,
        "doc_type": d.doc_type, "url": d.url,
        "issue_date": d.issue_date.isoformat(), "effective_date": d.effective_date.isoformat(),
        "valid_to": d.valid_to.isoformat() if d.valid_to else None, "superseded_by": d.superseded_by,
        "updated_on": d.updated_on.isoformat() if getattr(d, "updated_on", None) else None,
    }
    if when:
        m["in_force"] = d.in_force_on(when)
    return m


def _timeline(idx, doc_id: str) -> dict:
    """Versions (supersession chain) + amendments of each version, for the timeline chart."""
    st = idx.store
    versions = st.lineage(doc_id)
    amendments = []
    for v in versions:
        for r in st.edges_in(v.doc_id, AMENDS):
            src = st.get(r.source_id)
            if src:
                amendments.append({"target": v.doc_id, "effective_date": r.effective_date.isoformat(), **_doc_meta(src)})
    return {"versions": [_doc_meta(v) for v in versions], "amendments": amendments}


async def health(_: Request):
    idx, llm, _a = _state()
    meta_path = idx.settings.index_dir / "index_meta.json"
    import json

    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    return JSONResponse({
        "documents": len(idx.docs), "chunks": len(idx.chunks), "relations": len(idx.relations),
        "llm": llm.name, "offline_llm": isinstance(llm, ExtractiveLLM),
        "graph_backend": type(idx.store).__name__, "embedder": meta.get("embedder", "?"),
        "reranker": bool(idx.retriever.reranker),
    })


async def stats(_: Request):
    idx, _l, _a = _state()
    docs = list(idx.docs.values())
    by_year = Counter(d.issue_date.year for d in docs)
    return JSONResponse({
        "by_type": Counter(d.doc_type for d in docs),
        "relations": Counter(r.rel_type for r in idx.relations),
        "superseded": sum(d.valid_to is not None for d in docs),
        "by_year": [{"year": y, "count": by_year[y]} for y in sorted(by_year)],
        "min_date": min(d.issue_date for d in docs).isoformat() if docs else None,
        "max_date": max(d.issue_date for d in docs).isoformat() if docs else None,
    })


async def examples(_: Request):
    """Pick documents with the richest histories and phrase questions about them."""
    idx, _l, _a = _state()
    st = idx.store
    scored = []
    for r in idx.relations:
        if r.rel_type in (SUPERSEDES, AMENDS):
            scored.append((r.effective_date, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    out, seen = [], set()
    for eff, r in scored:
        old = st.get(r.target_id)
        if not old:
            continue
        topic = topic_from_title(old.title)
        if topic.lower() in seen or len(topic) > 70:
            continue
        seen.add(topic.lower())
        before = old.effective_date + (eff - old.effective_date) / 2
        if r.rel_type == SUPERSEDES:
            out.append({"question": f"How did the rules on {topic} change between {before:%B %Y} and {eff:%B %Y}?", "kind": "compare"})
            out.append({"question": f"What were the RBI instructions on {topic} as of {before.day} {before:%B %Y}?", "kind": "as_of"})
        else:
            out.append({"question": f"What were the rules on {topic} as of {eff:%B %Y}, including amendments?", "kind": "amendment"})
        if len(out) >= 7:
            break
    if seen:
        out.append({"question": f"Show the history of the rules on {next(iter(seen))}", "kind": "history"})
    return JSONResponse(out[:8])


# Optional per-visitor limit for public deployments (protects the Groq free-tier quota).
# ASK_LIMIT_PER_HOUR=0 (the default) disables it; the AWS deploy sets it to 30.
_ASK_LOG: dict[str, deque] = defaultdict(deque)


def _rate_limited(request: Request) -> bool:
    limit = int(os.getenv("ASK_LIMIT_PER_HOUR", "0") or 0)
    if limit <= 0:
        return False
    # Behind AWS Lambda the socket address is the local adapter, so the deploy sets
    # ASOF_CLIENT_IP_HEADER=x-forwarded-for. Its LAST entry is the one AWS adds itself; earlier
    # entries come from the visitor and could be forged to dodge the limit.
    header = os.getenv("ASOF_CLIENT_IP_HEADER", "").strip()
    forwarded = request.headers.get(header, "") if header else ""
    if forwarded:
        ip = forwarded.split(",")[-1].strip()
    else:
        ip = request.client.host if request.client else "?"
    now, log = time.monotonic(), _ASK_LOG[ip]
    while log and now - log[0] > 3600:
        log.popleft()
    if len(log) >= limit:
        return True
    log.append(now)
    if len(_ASK_LOG) > 10000:  # bound memory: drop visitors with no recent questions
        for k in [k for k, v in _ASK_LOG.items() if not v or now - v[-1] > 3600]:
            _ASK_LOG.pop(k, None)
    return False


async def ask_api(request: Request):
    if _rate_limited(request):
        return JSONResponse({"error": "This public demo allows a limited number of questions per hour per visitor "
                                      "(it runs on a free LLM quota). Please try again later."}, status_code=429)
    body = await request.json()
    question = (body.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": "question is required"}, status_code=400)
    system = body.get("system") or "asof"
    if system not in SYSTEMS:
        return JSONResponse({"error": f"unknown system {system}"}, status_code=400)
    as_of = date.fromisoformat(body["as_of"]) if body.get("as_of") else None
    idx, llm, agents = _state()
    try:
        res = await asyncio.to_thread(ask, agents[system], question, as_of=as_of)
    except Exception as e:  # surface LLM/API errors to the UI instead of a blank 500
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)
    pq = res["parsed"]
    ref = pq.dates[-1]
    contexts = []
    for i, rc in enumerate(res.get("contexts") or [], 1):
        group_date = next((d for _, d, items in res["groups"] if rc in items), ref)
        contexts.append({
            "n": i, "chunk_id": rc.chunk.chunk_id, "text": rc.chunk.text.split("\n", 1)[-1],
            "score": round(rc.score, 4), "retrievers": rc.sources, "via": rc.via,
            "status_note": rc.status_note, "group_date": group_date.isoformat(),
            "doc": _doc_meta(rc.doc, group_date),
        })
    cited_ids = list(dict.fromkeys(c["doc_id"] for c in res.get("citations", [])))
    timelines = {}
    for did in cited_ids[:4] or [c["doc"]["doc_id"] for c in contexts[:2]]:
        key = min((v.doc_id for v in idx.store.lineage(did)), default=did)
        timelines.setdefault(key, _timeline(idx, did))
    return JSONResponse({
        "system": system, "answer": res.get("answer", ""), "citations": res.get("citations", []),
        "contexts": contexts, "trace": res.get("trace", []),
        "parsed": {"intent": pq.intent, "dates": [d.isoformat() for d in pq.dates], "search_query": pq.search_query},
        "timelines": list(timelines.values()), "llm": llm.name,
    })


async def doc_api(request: Request):
    idx, _l, _a = _state()
    d = idx.docs.get(request.path_params["doc_id"])
    if d is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    st = idx.store
    rels = []
    for rt in (SUPERSEDES, AMENDS, REFERS_TO):
        for r in st.edges_out(d.doc_id, rt):
            o = st.get(r.target_id)
            if o:
                rels.append({"direction": "out", "type": rt, "effective_date": r.effective_date.isoformat(),
                             "evidence": r.evidence, "method": r.method, "doc": _doc_meta(o)})
        for r in st.edges_in(d.doc_id, rt):
            o = st.get(r.source_id)
            if o:
                rels.append({"direction": "in", "type": rt, "effective_date": r.effective_date.isoformat(),
                             "evidence": r.evidence, "method": r.method, "doc": _doc_meta(o)})
    return JSONResponse({"doc": {**_doc_meta(d), "text": d.text}, "relations": rels, "timeline": _timeline(idx, d.doc_id)})


async def warm(_: Request):
    """Keep-warm hook: AWS Lambda forwards scheduled (non-HTTP) events here (Lambda Web Adapter's
    pass-through path), so a ping every few minutes keeps the models and index loaded."""
    await asyncio.to_thread(_state)
    return JSONResponse({"ok": True})


async def index_page(_: Request):
    # cache-busting: the version is the newest static-file timestamp, so browsers always load the
    # current app.js/app.css after an update instead of a stale cached copy
    version = str(int(max(p.stat().st_mtime for p in STATIC.iterdir())))
    html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__V__", version)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


app = Starlette(routes=[
    Route("/", index_page),
    Route("/api/health", health),
    Route("/api/stats", stats),
    Route("/api/examples", examples),
    Route("/api/ask", ask_api, methods=["POST"]),
    Route("/api/doc/{doc_id}", doc_api),
    Route("/events", warm, methods=["POST"]),
    Mount("/static", StaticFiles(directory=STATIC), name="static"),
])
