"""Cross-encoder re-ranking. Default: cross-encoder/ms-marco-MiniLM-L-6-v2 (22M parameters) - about
10x faster on a laptop CPU than BAAI/bge-reranker-base, which made each query take ~10 s.
Optional: falls back to a no-op."""
from __future__ import annotations


class Reranker:
    def __init__(self, model_name: str):
        from sentence_transformers import CrossEncoder

        self.model = CrossEncoder(model_name, max_length=384)

    def scores(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        return [float(s) for s in self.model.predict([(query, p) for p in passages], batch_size=16)]


def make_reranker(model_name: str, enabled: bool = True):
    if not enabled:
        return None
    try:
        return Reranker(model_name)
    except ImportError:
        print("sentence-transformers not installed -> re-ranking disabled")
        return None
