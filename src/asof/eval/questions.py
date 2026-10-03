"""Generate a temporal QA benchmark directly from the amendment graph.

Every SUPERSEDES edge gives two questions with known answers:
  * a date just BEFORE the replacement  -> the old document is the correct source
  * a date just AFTER the replacement   -> the new document is the correct source
Every AMENDS edge gives one question after the amendment, where the amending circular
must also be found.

Because gold labels come from the graph, the benchmark scales with the corpus and needs
no manual labelling. Hand-written questions can be added in data/eval/manual_questions.jsonl.
"""
from __future__ import annotations

import json
import random
import re
from datetime import date, timedelta
from pathlib import Path

from asof.graph.store import GraphStore
from asof.models import AMENDS, SUPERSEDES

TEMPLATES = [
    "As of {date}, what were the RBI instructions on {topic}?",
    "What did the RBI rules on {topic} require on {date}?",
    "On {date}, which RBI guidelines applied to {topic}?",
]


def topic_from_title(title: str) -> str:
    t = re.sub(r"\((?:SYNTHETIC)\)", "", title, flags=re.I)
    t = re.sub(r"^(master\s+(circular|direction)s?\s*[-–—:]*\s*)", "", t.strip(), flags=re.I)
    t = re.sub(r"reserve bank of india\s*\(([^)]*)\)\s*directions?,?\s*\d{4}", r"\1", t, flags=re.I)
    t = re.sub(r"\b(directions?|circular)\b,?\s*\d{4}", "", t, flags=re.I)
    t = re.sub(r"\b(19|20)\d{2}(-\d{2})?\b", "", t)
    t = re.sub(r"\s+", " ", t).strip(" -–,.")
    return t or title


def _fmt(d: date) -> str:
    return d.strftime("%d %B %Y")


def generate(store: GraphStore, today: date | None = None, seed: int = 7, max_questions: int = 300) -> list[dict]:
    rng = random.Random(seed)
    today = today or date.today()
    out: list[dict] = []
    for rel in store.all_relations():
        old, new = store.get(rel.target_id), store.get(rel.source_id)
        if old is None or new is None:
            continue
        if rel.rel_type == SUPERSEDES:
            t = rel.effective_date
            before = t - timedelta(days=min(45, max(1, (t - old.effective_date).days // 2)))
            if old.effective_date <= before < t:
                gold = store.version_as_of(old.doc_id, before)
                if gold:
                    out.append(_q(rng, "before_replacement", before, old.title, [gold.doc_id], rel))
            after = min(t + timedelta(days=45), today)
            if after >= t:
                gold = store.version_as_of(new.doc_id, after)
                if gold:
                    out.append(_q(rng, "after_replacement", after, old.title, [gold.doc_id], rel))
        elif rel.rel_type == AMENDS:
            after = min(rel.effective_date + timedelta(days=45), today)
            base = store.version_as_of(old.doc_id, after)
            if base and base.doc_id == old.doc_id and after >= rel.effective_date:
                q = _q(rng, "after_amendment", after, old.title, [old.doc_id], rel)
                q["amendment_doc_ids"] = [new.doc_id]
                out.append(q)
    rng.shuffle(out)
    return out[:max_questions]


def _q(rng, kind, when, title, gold, rel) -> dict:
    topic = topic_from_title(title)
    return {
        "id": f"{kind}:{rel.source_id}->{rel.target_id}",
        "kind": kind,
        "question": rng.choice(TEMPLATES).format(date=_fmt(when), topic=topic),
        "as_of": when.isoformat(),
        "gold_doc_ids": gold,
        "amendment_doc_ids": [],
    }


def save(questions: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for q in questions:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]
