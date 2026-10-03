"""Hybrid retrieval: BM25 + dense vectors fused with Reciprocal Rank Fusion,
an optional point-in-time filter, and optional cross-encoder re-ranking."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Optional

import numpy as np

from asof.models import Chunk, Document, RetrievedChunk
from asof.retrieval.bm25 import BM25


def rrf(rankings: list[np.ndarray], k: int = 60) -> dict[int, float]:
    """Reciprocal Rank Fusion over several ranked lists of indices."""
    fused: dict[int, float] = defaultdict(float)
    for ranked in rankings:
        for rank, idx in enumerate(ranked):
            fused[int(idx)] += 1.0 / (k + rank + 1)
    return fused


class HybridRetriever:
    def __init__(self, chunks: list[Chunk], docs: dict[str, Document], embeddings: np.ndarray,
                 embedder, reranker=None):
        self.chunks = chunks
        self.docs = docs
        self.emb = embeddings
        self.embedder = embedder
        self.reranker = reranker
        self.bm25 = BM25([c.text for c in chunks])
        # per-chunk validity interval as day ordinals, so the point-in-time mask is one numpy expression
        far = date(9999, 12, 31).toordinal()
        self._eff = np.array([docs[c.doc_id].effective_date.toordinal() for c in chunks], dtype=np.int64)
        self._until = np.array([docs[c.doc_id].valid_to.toordinal() if docs[c.doc_id].valid_to else far
                                for c in chunks], dtype=np.int64)
        self._doc_chunks: dict[str, list[int]] = defaultdict(list)
        for i, c in enumerate(chunks):
            self._doc_chunks[c.doc_id].append(i)

    def _mask(self, as_of: Optional[date], doc_ids: Optional[set[str]] = None) -> np.ndarray:
        if doc_ids is not None:
            m = np.zeros(len(self.chunks), dtype=bool)
            for d in doc_ids:
                m[self._doc_chunks.get(d, [])] = True
        else:
            m = np.ones(len(self.chunks), dtype=bool)
        if as_of is not None:
            t = as_of.toordinal()
            m &= (self._eff <= t) & (t < self._until)
        return m

    def search(
        self,
        query: str,
        k: int = 6,
        *,
        as_of: Optional[date] = None,
        mode: str = "hybrid",           # "hybrid" | "vector" | "bm25"
        candidate_k: int = 40,
        rerank: bool = True,
        rerank_k: int = 20,
        max_per_doc: int = 3,
        doc_ids: Optional[set[str]] = None,
    ) -> list[RetrievedChunk]:
        """``as_of`` = None disables the temporal filter (this is what naive RAG does)."""
        mask = self._mask(as_of, doc_ids)
        if not mask.any():
            return []
        rankings, sources = [], {}
        if mode in ("hybrid", "vector"):
            qv = self.embedder.encode_query(query)
            dense = self.emb @ qv
            dense[~mask] = -np.inf
            top = np.argsort(-dense)[:candidate_k]
            top = top[np.isfinite(dense[top])]
            rankings.append(top)
            for i in top:
                sources.setdefault(int(i), []).append("vector")
        if mode in ("hybrid", "bm25"):
            sparse = self.bm25.scores(query)
            sparse[~mask] = -np.inf
            top = np.argsort(-sparse)[:candidate_k]
            top = top[np.isfinite(sparse[top]) & (sparse[top] > 0)]
            rankings.append(top)
            for i in top:
                sources.setdefault(int(i), []).append("bm25")

        fused = rrf(rankings)
        cand = sorted(fused, key=fused.get, reverse=True)[:candidate_k]
        scores = {i: fused[i] for i in cand}
        if rerank and self.reranker is not None and cand:
            # cross-encoders are expensive on CPU: re-rank only the head of the fused list
            head, tail = cand[:rerank_k], cand[rerank_k:]
            ce = self.reranker.scores(query, [self.chunks[i].text for i in head])
            scores = dict(zip(head, ce))
            floor = min(ce) - 1.0 if ce else 0.0
            scores.update({i: floor - n for n, i in enumerate(tail)})
            cand = sorted(head, key=scores.get, reverse=True) + tail

        out, per_doc = [], defaultdict(int)
        for i in cand:
            c = self.chunks[i]
            if per_doc[c.doc_id] >= max_per_doc:
                continue
            per_doc[c.doc_id] += 1
            out.append(RetrievedChunk(c, self.docs[c.doc_id], float(scores[i]), sources.get(i, [])))
            if len(out) >= k:
                break
        return out
