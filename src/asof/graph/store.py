"""Graph storage for documents and their legal relationships.

``GraphStore`` defines the temporal queries the agent needs. Two backends:

* ``NetworkXStore`` - in-memory, zero setup; used for tests and offline runs.
* ``Neo4jStore``    - Neo4j / AuraDB; Cypher variable-length paths for lineage queries.

Both answer the same questions, so the rest of the system does not care which one is used.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import date
from typing import Optional

from asof.models import AMENDS, SUPERSEDES, Document, Relation


class GraphStore(ABC):
    # ---------- primitives each backend implements ----------
    @abstractmethod
    def load(self, docs: list[Document], relations: list[Relation]) -> None: ...

    @abstractmethod
    def get(self, doc_id: str) -> Optional[Document]: ...

    @abstractmethod
    def edges_out(self, doc_id: str, rel_type: str) -> list[Relation]:
        """Relations where doc_id is the source (doc_id -> X)."""

    @abstractmethod
    def edges_in(self, doc_id: str, rel_type: str) -> list[Relation]:
        """Relations where doc_id is the target (X -> doc_id)."""

    @abstractmethod
    def all_documents(self) -> list[Document]: ...

    @abstractmethod
    def all_relations(self) -> list[Relation]: ...

    # ---------- temporal logic shared by all backends ----------
    def superseded_by(self, doc_id: str) -> list[Relation]:
        return sorted(self.edges_in(doc_id, SUPERSEDES), key=lambda r: r.effective_date)

    def version_as_of(self, doc_id: str, when: date, _depth: int = 0) -> Optional[Document]:
        """Follow the supersession chain to the document actually in force on ``when``.

        Moves forward when the document had already been replaced, and backward when it
        had not yet come into force. Returns None when no version was in force.
        """
        doc = self.get(doc_id)
        if doc is None or _depth > 25:
            return None
        if doc.effective_date > when:  # not yet in force -> look at what it replaced
            for rel in sorted(self.edges_out(doc_id, SUPERSEDES), key=lambda r: r.effective_date, reverse=True):
                older = self.version_as_of(rel.target_id, when, _depth + 1)
                if older is not None:
                    return older
            return None
        for rel in self.superseded_by(doc_id):
            if rel.effective_date <= when:  # already replaced -> move forward
                return self.version_as_of(rel.source_id, when, _depth + 1)
        return doc

    def amendments_as_of(self, doc_id: str, when: date) -> list[Relation]:
        return sorted(
            (r for r in self.edges_in(doc_id, AMENDS) if r.effective_date <= when),
            key=lambda r: r.effective_date,
        )

    def lineage(self, doc_id: str) -> list[Document]:
        """All versions of this regulation, oldest first (walks SUPERSEDES both ways)."""
        seen: dict[str, Document] = {}
        stack = [doc_id]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            d = self.get(cur)
            if d is None:
                continue
            seen[cur] = d
            stack.extend(r.target_id for r in self.edges_out(cur, SUPERSEDES))
            stack.extend(r.source_id for r in self.edges_in(cur, SUPERSEDES))
            if len(seen) > 60:
                break
        return sorted(seen.values(), key=lambda d: d.effective_date)


class NetworkXStore(GraphStore):
    def __init__(self):
        import networkx as nx

        self.g = nx.MultiDiGraph()
        self.docs: dict[str, Document] = {}

    def load(self, docs, relations):
        for d in docs:
            self.docs[d.doc_id] = d
            self.g.add_node(d.doc_id)
        for r in relations:
            self.g.add_edge(r.source_id, r.target_id, key=r.rel_type, rel=r)

    def get(self, doc_id):
        return self.docs.get(doc_id)

    def edges_out(self, doc_id, rel_type):
        if doc_id not in self.g:
            return []
        return [data["rel"] for _, _, k, data in self.g.out_edges(doc_id, keys=True, data=True) if k == rel_type]

    def edges_in(self, doc_id, rel_type):
        if doc_id not in self.g:
            return []
        return [data["rel"] for _, _, k, data in self.g.in_edges(doc_id, keys=True, data=True) if k == rel_type]

    def all_documents(self):
        return list(self.docs.values())

    def all_relations(self):
        return [data["rel"] for _, _, data in self.g.edges(data=True)]


def open_neo4j_driver(uri: str, user: str, password: str):
    """Open and verify a driver. Newer AuraDB instances use the instance id (the first part of
    the host name) as the username instead of "neo4j", so that is tried as a fallback."""
    import re

    from neo4j import GraphDatabase
    from neo4j.exceptions import AuthError

    candidates = [user or "neo4j"]
    m = re.match(r"neo4j(?:\+s|\+ssc)?://([a-z0-9]+)\.databases\.neo4j\.io", uri or "")
    if m and m.group(1) not in candidates:
        candidates.append(m.group(1))
    last: Exception | None = None
    for u in candidates:
        # Aura closes idle connections: check pooled connections before reuse and recycle them
        try:
            drv = GraphDatabase.driver(uri, auth=(u, password), keep_alive=True,
                                       max_connection_lifetime=240, liveness_check_timeout=30)
        except Exception:  # noqa: BLE001 - older/newer drivers without these options
            drv = GraphDatabase.driver(uri, auth=(u, password))
        try:
            drv.verify_connectivity()
            return drv, u
        except AuthError as e:
            drv.close()
            last = e
    raise last  # type: ignore[misc]


class Neo4jStore(GraphStore):
    """Documents are (:Document) nodes; relations are typed edges with an effective_date.

    Full document text stays in the local JSONL file (Neo4j keeps metadata only), so
    ``docs`` must be passed in when connecting to an already-loaded database.
    """

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j",
                 docs: Optional[list[Document]] = None):
        self._creds = (uri, user, password)
        self.driver, self.user = open_neo4j_driver(uri, user, password)
        # "neo4j" is the default database on older instances; newer ones name it after the instance id,
        # so both empty and "neo4j" mean "use the instance's default database".
        self.database = None if database in (None, "", "neo4j") else database
        self.docs: dict[str, Document] = {d.doc_id: d for d in (docs or [])}
        self._cache: dict[tuple[str, str, str], list[Relation]] = {}

    def close(self):
        self.driver.close()

    def _run(self, cypher: str, **params):
        from neo4j.exceptions import ServiceUnavailable, SessionExpired

        for attempt in range(3):
            try:
                with self.driver.session(database=self.database) as s:
                    return list(s.run(cypher, **params))
            except (ServiceUnavailable, SessionExpired, OSError):
                if attempt == 2:
                    raise
                # connection dropped (e.g. idle timeout on AuraDB Free): reconnect and retry
                time.sleep(1 + 2 * attempt)
                try:
                    self.driver.close()
                except Exception:  # noqa: BLE001
                    pass
                self.driver, self.user = open_neo4j_driver(*self._creds)
        return []

    def load(self, docs, relations):
        self.docs.update({d.doc_id: d for d in docs})
        self._cache.clear()
        # full rebuild: remove documents from any previous build first
        self._run("MATCH (d:Document) DETACH DELETE d")
        self._run("CREATE CONSTRAINT doc_id IF NOT EXISTS FOR (d:Document) REQUIRE d.doc_id IS UNIQUE")
        rows = [
            {k: v for k, v in d.to_dict().items() if k != "text"} for d in docs
        ]
        for i in range(0, len(rows), 500):
            self._run(
                """
                UNWIND $rows AS row
                MERGE (d:Document {doc_id: row.doc_id})
                SET d.title = row.title, d.ref_no = row.ref_no, d.dept_ref = row.dept_ref,
                    d.doc_type = row.doc_type, d.url = row.url,
                    d.issue_date = date(row.issue_date), d.effective_date = date(row.effective_date),
                    d.valid_to = CASE WHEN row.valid_to IS NULL THEN NULL ELSE date(row.valid_to) END,
                    d.superseded_by = row.superseded_by
                """,
                rows=rows[i: i + 500],
            )
        for rel_type in (SUPERSEDES, AMENDS, "REFERS_TO"):
            rel_rows = [r.to_dict() for r in relations if r.rel_type == rel_type]
            for i in range(0, len(rel_rows), 500):
                # relationship types cannot be parameterised in Cypher, hence the f-string
                self._run(
                    f"""
                    UNWIND $rows AS row
                    MATCH (s:Document {{doc_id: row.source_id}}), (t:Document {{doc_id: row.target_id}})
                    MERGE (s)-[r:{rel_type}]->(t)
                    SET r.effective_date = date(row.effective_date), r.evidence = row.evidence,
                        r.method = row.method
                    """,
                    rows=rel_rows[i: i + 500],
                )

    def get(self, doc_id):
        return self.docs.get(doc_id)

    def _edges(self, doc_id: str, rel_type: str, direction: str) -> list[Relation]:
        key = (doc_id, rel_type, direction)
        if key not in self._cache:
            pattern = f"(s:Document)-[r:{rel_type}]->(t:Document {{doc_id: $id}})" if direction == "in" \
                else f"(s:Document {{doc_id: $id}})-[r:{rel_type}]->(t:Document)"
            recs = self._run(
                f"MATCH {pattern} RETURN s.doc_id AS s, t.doc_id AS t, "
                f"toString(r.effective_date) AS eff, r.evidence AS ev, r.method AS m",
                id=doc_id,
            )
            self._cache[key] = [
                Relation(r["s"], r["t"], rel_type, date.fromisoformat(r["eff"]), r["ev"] or "", r["m"] or "")
                for r in recs
            ]
        return self._cache[key]

    def edges_out(self, doc_id, rel_type):
        return self._edges(doc_id, rel_type, "out")

    def edges_in(self, doc_id, rel_type):
        return self._edges(doc_id, rel_type, "in")

    def lineage(self, doc_id):
        """Single Cypher round-trip: every document connected by a SUPERSEDES path."""
        recs = self._run(
            """
            MATCH (d:Document {doc_id: $id})
            OPTIONAL MATCH (d)-[:SUPERSEDES*1..20]-(v:Document)
            WITH d, collect(DISTINCT v) AS vs
            UNWIND ([d] + vs) AS x
            RETURN DISTINCT x.doc_id AS id
            """,
            id=doc_id,
        )
        docs = [self.docs[r["id"]] for r in recs if r["id"] in self.docs]
        return sorted(docs, key=lambda d: d.effective_date)

    def all_documents(self):
        return list(self.docs.values())

    def all_relations(self):
        recs = self._run(
            "MATCH (s:Document)-[r]->(t:Document) RETURN s.doc_id AS s, t.doc_id AS t, type(r) AS ty, "
            "toString(r.effective_date) AS eff, r.evidence AS ev, r.method AS m"
        )
        return [Relation(r["s"], r["t"], r["ty"], date.fromisoformat(r["eff"]), r["ev"] or "", r["m"] or "")
                for r in recs]
