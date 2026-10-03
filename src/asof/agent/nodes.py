"""LangGraph node functions. Each takes the state dict and returns a partial update,
so every step can be unit-tested on its own."""
from __future__ import annotations

import re
import time
from datetime import date
from typing import Any, Optional, TypedDict

from asof.agent.prompts import ANSWER_SYSTEM, COMPARE_SYSTEM, REWRITE_SYSTEM, format_context
from asof.agent.query import INTENT_COMPARE, INTENT_HISTORY, ParsedQuery, parse_query
from asof.dates import dates_mentioned
from asof.models import RetrievedChunk


class AgentState(TypedDict, total=False):
    question: str
    today: date
    as_of_override: Optional[date]
    parsed: ParsedQuery
    search_query: str
    groups: list  # [(label, [RetrievedChunk])]
    attempts: int
    answer: str
    citations: list
    contexts: list
    trace: list


def _fmt(d: date) -> str:
    return d.strftime("%d %b %Y")


def _trace(state: AgentState, step: str, detail: str, t0: float) -> list:
    return (state.get("trace") or []) + [{"step": step, "detail": detail, "ms": round((time.perf_counter() - t0) * 1000)}]


def normalize_citations(text: str) -> str:
    """Rewrite citation styles some models emit by habit into the plain [n] form.

    gpt-oss sometimes writes browsing-style markers such as 【1†L38-L41】, 【2】 or [3†L1-L4];
    without this they would be dropped as invalid and the answer flagged as ungrounded."""
    text = re.sub(r"【\s*(\d+)[^】]*】", r"[\1]", text)
    text = re.sub(r"\[(\d+)\s*†[^\]]*\]", r"[\1]", text)
    return text


def make_nodes(index, llm, *, k: int = 6, use_graph: bool = True, temporal: bool = True,
               mode: str = "hybrid", rerank: bool = True, context_chars: int | None = None):
    from asof.agent.llm import ExtractiveLLM

    if context_chars is None:
        context_chars = getattr(getattr(index, "settings", None), "context_chars", 14000)
    retriever, store = index.retriever, index.store

    def understand(state: AgentState) -> dict:
        t0 = time.perf_counter()
        pq = parse_query(state["question"], today=state.get("today"))
        if state.get("as_of_override"):
            pq.dates = [state["as_of_override"]] if pq.intent != INTENT_COMPARE else pq.dates
            if pq.intent == "current":
                pq.intent = "as_of"
        detail = f"intent={pq.intent}; dates={[d.isoformat() for d in pq.dates]}; query='{pq.search_query}'"
        return {"parsed": pq, "search_query": pq.search_query, "attempts": 0,
                "trace": _trace(state, "understand", detail, t0)}

    def retrieve(state: AgentState) -> dict:
        t0 = time.perf_counter()
        pq: ParsedQuery = state["parsed"]
        groups = []
        for d in pq.dates:
            hits = retriever.search(state["search_query"], k=k, as_of=d if temporal else None, mode=mode, rerank=rerank)
            groups.append((f"In force on {_fmt(d)}", d, hits))
        n = sum(len(g[2]) for g in groups)
        return {"groups": groups, "trace": _trace(state, "retrieve", f"{n} passages ({mode}{', temporal filter' if temporal else ''}{', rerank' if rerank and retriever.reranker else ''})", t0)}

    def expand(state: AgentState) -> dict:
        """Graph step: annotate status, pull in amendments, and add lineage for history questions."""
        t0 = time.perf_counter()
        pq: ParsedQuery = state["parsed"]
        new_groups, added = [], 0
        for label, d, hits in state["groups"]:
            seen_docs = {h.doc.doc_id for h in hits}
            extra: list[RetrievedChunk] = []
            for h in hits:
                notes = []
                if not use_graph:  # baseline systems get no temporal knowledge at all
                    h.status_note = f"Issued {_fmt(h.doc.issue_date)}"
                    continue
                if h.doc.in_force_on(d):
                    notes.append(f"In force on {_fmt(d)}")
                else:
                    succ = store.get(h.doc.superseded_by) if h.doc.superseded_by else None
                    notes.append("NOT in force on " + _fmt(d) + (f" (replaced by {succ.ref_no} from {_fmt(h.doc.valid_to)})" if succ else ""))
                if use_graph:
                    for rel in store.amendments_as_of(h.doc.doc_id, d):
                        src = store.get(rel.source_id)
                        if src is None:
                            continue
                        notes.append(f"amended by {src.ref_no} w.e.f. {_fmt(rel.effective_date)}")
                        if src.doc_id not in seen_docs:
                            best = retriever.search(state["search_query"], k=1, doc_ids={src.doc_id}, rerank=False)
                            for b in best:
                                b.via = "graph: amendment"
                                b.status_note = f"Amends {h.doc.ref_no}; effective {_fmt(rel.effective_date)}"
                                extra.append(b)
                                seen_docs.add(src.doc_id)
                    if h.doc.in_force_on(d) and h.doc.valid_to:
                        succ = store.get(h.doc.superseded_by)
                        if succ:
                            notes.append(f"later replaced by {succ.ref_no} from {_fmt(h.doc.valid_to)}")
                if h.doc.updated_on and h.doc.updated_on > d:
                    notes.append(f"CAUTION: this text is the version updated as on {_fmt(h.doc.updated_on)}, after the "
                                 f"reference date; sentences dated after the reference date were removed, but other "
                                 f"later changes may remain")
                    h.redact_after = d
                h.status_note = "; ".join(notes)
            if use_graph and pq.intent == INTENT_HISTORY and hits:
                for v in store.lineage(hits[0].doc.doc_id):
                    if v.doc_id in seen_docs:
                        continue
                    best = retriever.search(state["search_query"], k=1, doc_ids={v.doc_id}, rerank=False)
                    for b in best:
                        b.via = "graph: lineage"
                        until = f" until {_fmt(v.valid_to)}" if v.valid_to else " (current)"
                        b.status_note = f"Version in force from {_fmt(v.effective_date)}{until}"
                        extra.append(b)
                        seen_docs.add(v.doc_id)
            added += len(extra)
            items = hits + extra
            if pq.intent == INTENT_HISTORY:
                items.sort(key=lambda rc: rc.doc.effective_date)
            new_groups.append((label, d, items))
        return {"groups": new_groups, "trace": _trace(state, "graph", f"+{added} passages from amendments/lineage", t0)}

    def route_after_retrieve(state: AgentState) -> str:
        n = sum(len(g[2]) for g in state["groups"])
        return "rewrite" if n == 0 and state.get("attempts", 0) < 1 else "expand"

    def rewrite(state: AgentState) -> dict:
        t0 = time.perf_counter()
        q = llm.complete(REWRITE_SYSTEM, f"Question: {state['search_query']}", fast=True, max_tokens=60)
        q = q.strip().strip('"') or state["search_query"]
        return {"search_query": q, "attempts": state.get("attempts", 0) + 1,
                "trace": _trace(state, "rewrite", f"query -> '{q}'", t0)}

    def generate(state: AgentState) -> dict:
        t0 = time.perf_counter()
        pq: ParsedQuery = state["parsed"]
        groups = [(label, items) for label, _, items in state["groups"]]
        if not any(items for _, items in groups):
            return {"answer": "I could not find any RBI document in the index that answers this for the requested date.",
                    "contexts": [], "trace": _trace(state, "generate", "no context", t0)}
        dates = " and ".join(_fmt(d) for d in pq.dates)
        system = COMPARE_SYSTEM if pq.intent == INTENT_COMPARE else ANSWER_SYSTEM
        note = llm.name
        budget = context_chars
        for attempt in range(2):
            ctx_text, flat = format_context(groups, max_chars=budget)
            user = f"REFERENCE DATE(S): {dates}\nQUESTION: {state['question']}\n\nCONTEXT:\n{ctx_text}"
            try:
                answer = llm.complete(system, user, max_tokens=700)
                break
            except Exception as e:  # noqa: BLE001
                msg = str(e).lower()
                if attempt == 0 and ("413" in msg or "too large" in msg or "context" in msg):
                    budget //= 2  # prompt too big for the provider's limit: halve the context and retry
                    continue
                # never leave the user with an error: show the best passages instead
                answer = ExtractiveLLM().complete(system, user)
                answer = "_The language model is unavailable right now, so here are the most relevant passages._\n\n" + answer
                note = f"fallback ({type(e).__name__})"
                break
        return {"answer": answer, "contexts": flat,
                "trace": _trace(state, "generate", f"{note}; {len(flat)} blocks, ~{len(user) // 4} tokens", t0)}

    def verify(state: AgentState) -> dict:
        """Keep only citations that point at real context blocks; report which docs were cited."""
        t0 = time.perf_counter()
        flat = state.get("contexts") or []
        answer = state.get("answer", "")
        used = []
        def fix(m):
            nums = [int(x) for x in re.findall(r"\d+", m.group(0))]
            ok = [n for n in nums if 1 <= n <= len(flat)]
            used.extend(ok)
            return "".join(f"[{n}]" for n in ok)
        answer = normalize_citations(answer)
        answer = re.sub(r"\[(\d+(?:\s*,\s*\d+)*)\]", fix, answer)
        cited = sorted(set(used))
        citations = [{"n": n, "doc_id": flat[n - 1].doc.doc_id, "chunk_id": flat[n - 1].chunk.chunk_id} for n in cited]
        detail = f"{len(cited)} valid citations" + ("" if cited or not flat else " - answer is UNGROUNDED")
        # temporal guard: the answer must not present provisions dated after the reference date
        pq: ParsedQuery = state["parsed"]
        ref = max(pq.dates)
        # dates that describe the sources themselves (e.g. "version updated on 14 Aug 2025") are fine
        meta_dates = {rc.doc.updated_on for rc in flat if getattr(rc.doc, "updated_on", None)}
        meta_dates |= {rc.doc.valid_to for rc in flat if rc.doc.valid_to}
        later = sorted({d for d in dates_mentioned(answer) if d > ref and d not in meta_dates})
        if later and pq.intent != INTENT_HISTORY:
            shown = ", ".join(_fmt(d) for d in later[:3])
            answer += (f"\n\n_Check: this answer mentions {shown}, after the reference date ({_fmt(ref)}). "
                       f"Provisions tied to that date were most likely added later and may not have applied on "
                       f"{_fmt(ref)}._")
            detail += f"; temporal guard flagged {len(later)} later date(s)"
        return {"answer": answer, "citations": citations, "trace": _trace(state, "verify", detail, t0)}

    return {
        "understand": understand, "retrieve": retrieve, "expand": expand, "rewrite": rewrite,
        "generate": generate, "verify": verify, "route_after_retrieve": route_after_retrieve,
    }
