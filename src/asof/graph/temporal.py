"""Turn relations into validity intervals: each document is in force on
[effective_date, valid_to), where valid_to is the date its first replacement took effect."""
from __future__ import annotations

from asof.models import SUPERSEDES, Document, Relation


def compute_validity(docs: list[Document], relations: list[Relation]) -> list[Document]:
    by_id = {d.doc_id: d for d in docs}
    for d in docs:
        d.valid_to, d.superseded_by = None, None
    for r in sorted((r for r in relations if r.rel_type == SUPERSEDES), key=lambda r: r.effective_date):
        target = by_id.get(r.target_id)
        if target is None or target.valid_to is not None:
            continue  # keep the earliest replacement
        if r.effective_date <= target.effective_date:
            continue  # inconsistent dates - ignore rather than create an empty interval
        target.valid_to = r.effective_date
        target.superseded_by = r.source_id
    return docs


def build_store(docs: list[Document], relations: list[Relation], settings=None):
    """Create the configured graph store (Neo4j if NEO4J_URI is set, else NetworkX) and load it."""
    from asof.graph.store import Neo4jStore, NetworkXStore

    if settings is not None and settings.use_neo4j:
        store = Neo4jStore(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password,
                           settings.neo4j_database, docs=docs)
    else:
        store = NetworkXStore()
    store.load(docs, relations)
    return store


def connect_store(docs: list[Document], relations: list[Relation], settings=None):
    """Connect to an already-loaded store (Neo4j) or rebuild the in-memory one."""
    from asof.graph.store import Neo4jStore, NetworkXStore

    if settings is not None and settings.use_neo4j:
        return Neo4jStore(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password,
                          settings.neo4j_database, docs=docs)
    store = NetworkXStore()
    store.load(docs, relations)
    return store
