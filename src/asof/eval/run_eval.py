"""Benchmark AsOf against baselines on the temporal QA set.

Systems
  naive_rag   dense vector search only, no date awareness            (typical tutorial RAG)
  hybrid_rag  BM25 + vector with RRF (+ cross-encoder rerank), no date awareness
  asof        hybrid + point-in-time filter + graph expansion (amendments / lineage)

Retrieval metrics (free, no LLM calls)
  hit@k            gold document is among the retrieved passages
  mrr              1 / rank of the first gold passage
  stale@k          share of retrieved documents NOT in force on the question date  (lower is better)
  amend_recall     for post-amendment questions: amending circular was retrieved

Answer metrics (with --answers; uses the Groq LLM)
  cited_stale      share of cited documents not in force on the question date   (lower is better)
  citation_rate    answers with at least one valid citation
  faithfulness     LLM-as-judge: share of claims supported by the context

Usage
  python -m asof.eval.run_eval                       # retrieval metrics, all systems
  python -m asof.eval.run_eval --answers --limit 40  # + answer metrics on 40 questions
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from datetime import date
from pathlib import Path

from asof.agent.graph import ask, build_agent
from asof.agent.llm import ExtractiveLLM, make_llm
from asof.agent.prompts import JUDGE_SYSTEM
from asof.config import get_settings
from asof.eval import questions as qmod

SYSTEMS = {
    "naive_rag": dict(mode="vector", temporal=False, use_graph=False, rerank=False),
    "hybrid_rag": dict(mode="hybrid", temporal=False, use_graph=False, rerank=True),
    "asof": dict(mode="hybrid", temporal=True, use_graph=True, rerank=True),
}


def _retrieval_metrics(result: dict, q: dict, docs: dict) -> dict:
    as_of = date.fromisoformat(q["as_of"])
    ctx = result.get("contexts") or []
    ids = [c.doc.doc_id for c in ctx]
    gold = set(q["gold_doc_ids"])
    rank = next((i + 1 for i, d in enumerate(ids) if d in gold), None)
    uniq = list(dict.fromkeys(ids))
    stale = [d for d in uniq if not docs[d].in_force_on(as_of)]
    m = {
        "hit": 1.0 if rank else 0.0,
        "mrr": 1.0 / rank if rank else 0.0,
        "stale": len(stale) / len(uniq) if uniq else 0.0,
    }
    if q.get("amendment_doc_ids"):
        m["amend_recall"] = 1.0 if set(q["amendment_doc_ids"]) & set(ids) else 0.0
    return m


def _answer_metrics(result: dict, q: dict, docs: dict, llm) -> dict:
    as_of = date.fromisoformat(q["as_of"])
    cited = list(dict.fromkeys(c["doc_id"] for c in result.get("citations", [])))
    m = {
        "citation_rate": 1.0 if cited else 0.0,
        "cited_stale": (sum(not docs[d].in_force_on(as_of) for d in cited) / len(cited)) if cited else 0.0,
    }
    ctx = "\n\n".join(c.chunk.text for c in (result.get("contexts") or []))[:12000]
    raw = llm.complete(JUDGE_SYSTEM, f"CONTEXT:\n{ctx}\n\nANSWER:\n{result.get('answer', '')}", fast=True, max_tokens=120)
    mm = re.search(r'"score"\s*:\s*([0-9.]+)', raw)
    if mm:
        m["faithfulness"] = min(1.0, float(mm.group(1)))
    return m


def evaluate(index, questions: list[dict], *, systems=None, answers: bool = False, llm=None,
             k: int = 6, today: date | None = None, verbose: bool = True) -> dict:
    systems = systems or list(SYSTEMS)
    offline = ExtractiveLLM()
    report: dict = {"n_questions": len(questions), "k": k, "systems": {}}
    for name in systems:
        agent = build_agent(index, llm if answers else offline, k=k, **SYSTEMS[name])
        rows, t0 = [], time.perf_counter()
        for i, q in enumerate(questions, 1):
            res = ask(agent, q["question"], as_of=date.fromisoformat(q["as_of"]), today=today)
            row = {"id": q["id"], "kind": q.get("kind", "manual"), **_retrieval_metrics(res, q, index.docs)}
            if answers:
                row.update(_answer_metrics(res, q, index.docs, llm))
            rows.append(row)
            if verbose and i % 25 == 0:
                print(f"  {name}: {i}/{len(questions)}")
        agg = {}
        for key in ("hit", "mrr", "stale", "amend_recall", "citation_rate", "cited_stale", "faithfulness"):
            vals = [r[key] for r in rows if key in r]
            if vals:
                agg[key] = round(statistics.mean(vals), 4)
        agg["latency_ms"] = round((time.perf_counter() - t0) * 1000 / max(1, len(questions)))
        by_kind = {}
        for kind in sorted({r["kind"] for r in rows}):
            sub = [r for r in rows if r["kind"] == kind]
            by_kind[kind] = {"n": len(sub), "hit": round(statistics.mean(r["hit"] for r in sub), 4),
                             "stale": round(statistics.mean(r["stale"] for r in sub), 4)}
        report["systems"][name] = {"overall": agg, "by_kind": by_kind, "rows": rows}
        if verbose:
            print(f"{name:11s} {agg}")
    return report


def to_markdown(report: dict) -> str:
    cols = [("hit", "Hit@k"), ("mrr", "MRR"), ("stale", "Stale@k (lower better)"),
            ("amend_recall", "Amendment recall"), ("cited_stale", "Stale citations"),
            ("faithfulness", "Faithfulness"), ("latency_ms", "Latency (ms)")]
    present = [c for c in cols if any(c[0] in s["overall"] for s in report["systems"].values())]
    lines = [f"Questions: {report['n_questions']} | k = {report['k']}", "",
             "| System | " + " | ".join(c[1] for c in present) + " |",
             "|---|" + "---|" * len(present)]
    for name, s in report["systems"].items():
        o = s["overall"]
        cells = []
        for key, _ in present:
            v = o.get(key)
            cells.append("-" if v is None else (f"{v:.0f}" if key == "latency_ms" else f"{v:.1%}" if key != "mrr" else f"{v:.3f}"))
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> None:
    from asof.index import load_index

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--answers", action="store_true", help="also generate answers with the LLM (uses API quota)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--k", type=int, default=6)
    p.add_argument("--regenerate", action="store_true", help="rebuild the auto-generated question set")
    a = p.parse_args()

    s = get_settings()
    index = load_index(s)
    eval_dir = s.data_dir / "eval"
    auto_path = eval_dir / "temporal_questions.jsonl"
    if a.regenerate or not auto_path.exists():
        qs = qmod.generate(index.store)
        qmod.save(qs, auto_path)
        print(f"Generated {len(qs)} temporal questions -> {auto_path}")
    questions = qmod.load(auto_path) + qmod.load(eval_dir / "manual_questions.jsonl")
    if a.limit:
        questions = questions[: a.limit]
    llm = make_llm(s) if a.answers else None
    if a.answers and isinstance(llm, ExtractiveLLM):
        print("No GROQ_API_KEY found - answer metrics will use the offline answerer.")
    report = evaluate(index, questions, answers=a.answers, llm=llm, k=a.k)
    (eval_dir / "results.json").write_text(json.dumps(report, indent=2))
    md = to_markdown(report)
    (eval_dir / "results.md").write_text(md)
    print("\n" + md)


if __name__ == "__main__":
    main()
