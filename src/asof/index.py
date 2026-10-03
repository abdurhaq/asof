"""Build and load everything the app needs: documents, relations, chunks, vectors, graph."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from asof.config import Settings, get_settings
from asof.graph.store import GraphStore
from asof.graph.temporal import build_store, compute_validity, connect_store
from asof.ingest.chunker import chunk_corpus
from asof.ingest.parse import load_documents, parse_raw_dir, save_documents
from asof.ingest.relations import extract_relations
from asof.models import Chunk, Document, Relation
from asof.retrieval.embeddings import make_embedder
from asof.retrieval.hybrid import HybridRetriever
from asof.retrieval.rerank import make_reranker


@dataclass
class Index:
    docs: dict[str, Document]
    relations: list[Relation]
    chunks: list[Chunk]
    retriever: HybridRetriever
    store: GraphStore
    settings: Settings


def _write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def _hash(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


def _embed_with_cache(embedder, texts: list[str], s: Settings, log) -> np.ndarray:
    """Embed only chunks whose text changed since the last build (a rebuild after a parser fix
    then takes minutes instead of the ~45 min a full CPU embedding run needs).

    The cache is keyed by chunk-text hash and embedding model. The first time, it is seeded from
    the previous build's embeddings.npy + chunks.jsonl. The LSA fallback is refitted per corpus,
    so it is never cached."""
    if embedder.name == "lsa-fallback":
        return embedder.encode_docs(texts)
    cache_file = s.index_dir / "embedding_cache.npz"
    cache: dict[str, np.ndarray] = {}
    if cache_file.exists():
        z = np.load(cache_file, allow_pickle=False)
        if str(z["model"]) == embedder.name:
            cache = dict(zip(z["keys"].tolist(), z["vecs"]))
    else:  # seed from the previous build, if it used the same model
        meta_p, emb_p, ch_p = s.index_dir / "index_meta.json", s.index_dir / "embeddings.npy", s.processed_dir / "chunks.jsonl"
        if meta_p.exists() and emb_p.exists() and ch_p.exists():
            if json.loads(meta_p.read_text()).get("embedder") == embedder.name:
                old = np.load(emb_p)
                old_texts = [r["text"] for r in _read_jsonl(ch_p)]
                if len(old_texts) == len(old):
                    cache = {_hash(t): v for t, v in zip(old_texts, old)}
    keys = [_hash(t) for t in texts]
    missing = [i for i, k in enumerate(keys) if k not in cache]
    log(f"  embedding cache: {len(texts) - len(missing)} reused, {len(missing)} to embed")
    if missing:
        new = embedder.encode_docs([texts[i] for i in missing])
        for i, v in zip(missing, new):
            cache[keys[i]] = v
    emb = np.stack([cache[k] for k in keys]).astype(np.float32)
    s.index_dir.mkdir(parents=True, exist_ok=True)
    np.savez(cache_file, model=np.array(embedder.name), keys=np.array(keys), vecs=emb)
    return emb


def build_index(settings: Settings | None = None, docs: list[Document] | None = None,
                verbose: bool = True) -> dict:
    """raw HTML -> documents -> relations -> validity -> chunks -> embeddings -> graph."""
    s = settings or get_settings()
    log = print if verbose else (lambda *a, **k: None)
    if docs is None:
        log(f"Parsing pages in {s.raw_dir} ...")
        docs = parse_raw_dir(s.raw_dir)
    log(f"  {len(docs)} documents")
    if not docs:
        raise SystemExit("No documents found. Run the fetcher first (see README).")
    from collections import Counter

    types = Counter(d.doc_type for d in docs)
    log(f"  types: {dict(types)} | untitled: {sum(d.title == 'Untitled' for d in docs)} | "
        f"no dept ref: {sum(not d.dept_ref for d in docs)} | "
        f"effective date differs from issue date: {sum(d.effective_date != d.issue_date for d in docs)}")
    for d in sorted(docs, key=lambda d: d.issue_date)[-3:]:
        log(f"  sample: {d.issue_date} | {d.ref_no} | {d.title[:80]}")

    rels, stats = extract_relations(docs)
    compute_validity(docs, rels)
    log(f"  {len(rels)} relations  {stats}")
    log(f"  {sum(d.valid_to is not None for d in docs)} documents have been superseded")

    chunks = chunk_corpus(docs)
    log(f"  {len(chunks)} chunks; embedding with {s.embedding_model} ...")
    texts = [c.text for c in chunks]
    embedder = make_embedder(s.embedding_model, corpus=texts)
    emb = _embed_with_cache(embedder, texts, s, log)

    s.processed_dir.mkdir(parents=True, exist_ok=True)
    s.index_dir.mkdir(parents=True, exist_ok=True)
    save_documents(docs, s.processed_dir / "documents.jsonl")
    _write_jsonl(s.processed_dir / "relations.jsonl", (r.to_dict() for r in rels))
    _write_jsonl(s.processed_dir / "chunks.jsonl", (c.to_dict() for c in chunks))
    np.save(s.index_dir / "embeddings.npy", emb)
    embedder.save(s.index_dir / "lsa.pkl")
    (s.index_dir / "index_meta.json").write_text(json.dumps(
        {"embedder": embedder.name, "n_docs": len(docs), "n_chunks": len(chunks), "relation_stats": stats},
        indent=2))

    log("Loading graph store" + (" (Neo4j)" if s.use_neo4j else " (in-memory NetworkX)") + " ...")
    store = build_store(docs, rels, s)
    if hasattr(store, "close"):
        store.close()
    log("Index built.")
    return {"n_docs": len(docs), "n_relations": len(rels), "n_chunks": len(chunks), **stats}


def load_index(settings: Settings | None = None, use_reranker: bool | None = None) -> Index:
    s = settings or get_settings()
    docs_list = load_documents(s.processed_dir / "documents.jsonl")
    rels = [Relation.from_dict(r) for r in _read_jsonl(s.processed_dir / "relations.jsonl")]
    chunks = [Chunk(**c) for c in _read_jsonl(s.processed_dir / "chunks.jsonl")]
    emb = np.load(s.index_dir / "embeddings.npy")
    meta = json.loads((s.index_dir / "index_meta.json").read_text())
    embedder = make_embedder(meta["embedder"], index_dir=s.index_dir)
    reranker = make_reranker(s.reranker_model, s.use_reranker if use_reranker is None else use_reranker)
    docs = {d.doc_id: d for d in docs_list}
    retriever = HybridRetriever(chunks, docs, emb, embedder, reranker)
    store = connect_store(docs_list, rels, s)
    return Index(docs, rels, chunks, retriever, store, s)
