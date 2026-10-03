"""Command line entry point.

  python -m asof.cli fetch --source md            download Master Directions
  python -m asof.cli build                        parse -> relations -> chunks -> vectors -> graph
  python -m asof.cli ask "What were the KYC rules as of March 2021?"
  python -m asof.cli eval [--answers --limit 40]  benchmark vs naive RAG
  python -m asof.cli audit                        export relations to CSV for manual checking
  python -m asof.cli serve                        start the web app on http://localhost:8000
  python -m asof.cli doctor                       check packages, Groq, Neo4j and the RBI website
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="asof", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download RBI pages")
    f.add_argument("--source", choices=["md", "mc", "ids"], required=True)
    f.add_argument("--start", type=int, default=0)
    f.add_argument("--end", type=int, default=0)
    f.add_argument("--delay", type=float, default=1.5)
    f.add_argument("--limit", type=int, default=0)

    sub.add_parser("build", help="build the index from data/raw")

    a = sub.add_parser("ask", help="ask one question")
    a.add_argument("question")
    a.add_argument("--as-of", help="YYYY-MM-DD (overrides the date in the question)")

    e = sub.add_parser("eval", help="run the benchmark")
    e.add_argument("--answers", action="store_true")
    e.add_argument("--limit", type=int, default=0)
    e.add_argument("--regenerate", action="store_true")

    sub.add_parser("audit", help="write data/eval/relations_audit.csv for manual precision checks")

    sub.add_parser("doctor", help="check packages, API keys, Neo4j and the RBI website")

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)

    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows consoles: never crash on characters like the rupee sign
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass

    if args.cmd == "fetch":
        from asof.ingest.fetch_rbi import fetch

        if args.source == "ids" and not (args.start and args.end):
            p.error("--source ids needs --start and --end")
        fetch(args.source, args.start, args.end, args.delay, args.limit)

    elif args.cmd == "build":
        from asof.index import build_index

        build_index()

    elif args.cmd == "ask":
        from asof.agent.graph import ask, build_agent
        from asof.agent.llm import make_llm
        from asof.index import load_index

        idx = load_index()
        agent = build_agent(idx, make_llm(idx.settings), k=idx.settings.top_k)
        res = ask(agent, args.question, as_of=date.fromisoformat(args.as_of) if args.as_of else None)
        print("\n" + res["answer"] + "\n")
        for c in res.get("citations", []):
            d = idx.docs[c["doc_id"]]
            print(f"  [{c['n']}] {d.title} ({d.ref_no}, {d.issue_date})  {d.url}")
        print("\nTrace:")
        for t in res.get("trace", []):
            print(f"  {t['step']:<10} {t['ms']:>5} ms  {t['detail']}")

    elif args.cmd == "eval":
        from asof.eval import run_eval

        sys.argv = ["run_eval"] + (["--answers"] if args.answers else []) + \
                   (["--limit", str(args.limit)] if args.limit else []) + (["--regenerate"] if args.regenerate else [])
        run_eval.main()

    elif args.cmd == "audit":
        from asof.config import get_settings
        from asof.index import load_index

        idx = load_index(use_reranker=False)
        out = get_settings().data_dir / "eval" / "relations_audit.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["source_ref", "relation", "target_ref", "effective", "method", "evidence", "correct? (y/n)"])
            for r in idx.relations:
                src, tgt = idx.docs[r.source_id], idx.docs[r.target_id]
                w.writerow([src.ref_no, r.rel_type, tgt.ref_no, r.effective_date, r.method, r.evidence[:300], ""])
        import random

        sample = random.Random(42).sample(idx.relations, min(50, len(idx.relations)))
        sample_path = out.with_name("relations_audit_sample50.csv")
        with sample_path.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["source_title", "source_ref", "relation", "target_title", "target_ref", "effective",
                        "method", "evidence", "correct? (y/n)"])
            for r in sample:
                src, tgt = idx.docs[r.source_id], idx.docs[r.target_id]
                w.writerow([src.title, src.ref_no, r.rel_type, tgt.title, tgt.ref_no, r.effective_date, r.method,
                            r.evidence[:300], ""])
        print(f"Wrote {len(idx.relations)} relations to {out}")
        print(f"Wrote a random sample of {len(sample)} to {sample_path} - label the last column y/n.")

    elif args.cmd == "doctor":
        from asof.doctor import run

        sys.exit(run())

    elif args.cmd == "serve":
        import uvicorn

        uvicorn.run("asof.web.server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
