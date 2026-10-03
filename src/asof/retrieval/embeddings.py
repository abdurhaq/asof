"""Dense embeddings.

Default: ``BAAI/bge-small-en-v1.5`` via sentence-transformers (runs locally on CPU, free).
Fallback: TF-IDF + truncated SVD ("LSA"), used automatically when sentence-transformers
is not installed (e.g. in CI) so the whole pipeline stays runnable offline.
"""
from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np

BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder:
    name = "base"

    def encode_docs(self, texts: list[str]) -> np.ndarray: ...

    def encode_query(self, text: str) -> np.ndarray: ...

    def save(self, path: Path) -> None:  # only needed for fitted models
        pass


class SentenceTransformerEmbedder(Embedder):
    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer

        self.name = model_name
        self.model = SentenceTransformer(model_name)
        self.prefix = BGE_QUERY_PREFIX if "bge" in model_name.lower() else ""

    def encode_docs(self, texts):
        return self.model.encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=True).astype(np.float32)

    def encode_query(self, text):
        return self.model.encode([self.prefix + text], normalize_embeddings=True)[0].astype(np.float32)


class LSAEmbedder(Embedder):
    """TF-IDF + SVD. Weaker than a neural model, but needs no download."""

    name = "lsa-fallback"

    def __init__(self, dims: int = 256):
        self.dims = dims
        self.vec = None
        self.svd = None

    def fit(self, texts: list[str]) -> "LSAEmbedder":
        from sklearn.decomposition import TruncatedSVD
        from sklearn.feature_extraction.text import TfidfVectorizer

        self.vec = TfidfVectorizer(sublinear_tf=True, ngram_range=(1, 2), min_df=1, stop_words="english")
        X = self.vec.fit_transform(texts)
        n = max(2, min(self.dims, X.shape[1] - 1, X.shape[0] - 1))
        self.svd = TruncatedSVD(n_components=n, random_state=0).fit(X)
        return self

    def _embed(self, texts):
        Z = self.svd.transform(self.vec.transform(texts))
        Z /= np.linalg.norm(Z, axis=1, keepdims=True) + 1e-9
        return Z.astype(np.float32)

    def encode_docs(self, texts):
        return self._embed(texts)

    def encode_query(self, text):
        return self._embed([text])[0]

    def save(self, path: Path):
        with path.open("wb") as f:
            pickle.dump({"vec": self.vec, "svd": self.svd, "dims": self.dims}, f)

    @classmethod
    def load(cls, path: Path) -> "LSAEmbedder":
        with path.open("rb") as f:
            d = pickle.load(f)
        e = cls(d["dims"])
        e.vec, e.svd = d["vec"], d["svd"]
        return e


def make_embedder(model_name: str, corpus: list[str] | None = None, index_dir: Path | None = None) -> Embedder:
    """Prefer sentence-transformers; otherwise fit (or load) the LSA fallback."""
    if model_name != "lsa-fallback":
        try:
            return SentenceTransformerEmbedder(model_name)
        except ImportError:
            print("sentence-transformers not installed -> using the offline LSA fallback embedder")
    lsa_path = index_dir / "lsa.pkl" if index_dir else None
    if corpus is None and lsa_path and lsa_path.exists():
        return LSAEmbedder.load(lsa_path)
    if corpus is None:
        raise ValueError("LSA fallback needs the corpus to fit on")
    return LSAEmbedder().fit(corpus)
