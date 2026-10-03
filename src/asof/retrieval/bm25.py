"""Small, dependency-free Okapi BM25 (keeps exact terms like 'LCR' or 'Rs 5,000 crore' findable)."""
from __future__ import annotations

import math
import re
from collections import Counter

import numpy as np

STOP = set(
    "a an and are as at be by for from has have in is it its of on or that the this to was were will with "
    "shall may any all such under which been being their these those into than other also per".split()
)
TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")


def tokenize(text: str) -> list[str]:
    toks = []
    for t in TOKEN.findall(text.lower()):
        if t in STOP or len(t) < 2 and not t.isdigit():
            continue
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]  # crude plural folding: "banks" -> "bank"
        toks.append(t)
    return toks


class BM25:
    """BM25 with all document-term weights precomputed into a sparse matrix:
    scoring a query is one column slice + row sum (milliseconds for ~30k chunks)."""

    def __init__(self, corpus: list[str], k1: float = 1.5, b: float = 0.75):
        from scipy.sparse import csc_matrix

        self.k1, self.b = k1, b
        counts = [Counter(tokenize(t)) for t in corpus]
        n = len(counts)
        lengths = np.array([sum(c.values()) for c in counts], dtype=float)
        avgdl = float(lengths.mean()) if n else 0.0
        self.vocab: dict[str, int] = {}
        df = Counter()
        for c in counts:
            df.update(c.keys())
        rows, cols, vals = [], [], []
        for i, c in enumerate(counts):
            norm = k1 * (1 - b + b * lengths[i] / avgdl) if avgdl else k1
            for t, f in c.items():
                j = self.vocab.setdefault(t, len(self.vocab))
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                rows.append(i)
                cols.append(j)
                vals.append(idf * f * (k1 + 1) / (f + norm))
        self.matrix = csc_matrix((vals, (rows, cols)), shape=(n, max(1, len(self.vocab))), dtype=np.float32)
        self.n = n

    def scores(self, query: str) -> np.ndarray:
        cols = sorted({self.vocab[t] for t in tokenize(query) if t in self.vocab})
        if not cols:
            return np.zeros(self.n)
        return np.asarray(self.matrix[:, cols].sum(axis=1)).ravel().astype(float)
