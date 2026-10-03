"""Core data types shared across the pipeline."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Optional

# Relation types between regulatory documents.
SUPERSEDES = "SUPERSEDES"  # source fully replaces target (target stops being in force)
AMENDS = "AMENDS"          # source modifies part of target (target stays in force, with changes)
REFERS_TO = "REFERS_TO"    # plain citation, no legal effect on target

RELATION_TYPES = (SUPERSEDES, AMENDS, REFERS_TO)


@dataclass
class Document:
    """One RBI circular / master direction / master circular."""

    doc_id: str                 # stable id, e.g. "rbi-12586"
    title: str
    ref_no: str                 # e.g. "RBI/CEPD/2023-24/108"
    dept_ref: str               # e.g. "CEPD.PRD.No.S1239/13.01.001/2023-24"
    issue_date: date
    effective_date: date        # when it comes into force (defaults to issue_date)
    doc_type: str               # "master_direction" | "master_circular" | "circular"
    url: str
    text: str
    # Filled in by the temporal resolver (graph/temporal.py):
    valid_to: Optional[date] = None        # first day it is no longer in force
    superseded_by: Optional[str] = None
    # Master Directions are often edited in place ("Updated as on <date>"): the text we hold
    # is that later consolidated version, not necessarily the text as originally issued.
    updated_on: Optional[date] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("issue_date", "effective_date", "valid_to", "updated_on"):
            d[k] = d[k].isoformat() if d[k] else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Document":
        d = dict(d)
        for k in ("issue_date", "effective_date", "valid_to", "updated_on"):
            d[k] = date.fromisoformat(d[k]) if d.get(k) else None  # (old files lack updated_on)
        return cls(**d)

    def in_force_on(self, when: date) -> bool:
        if self.effective_date > when:
            return False
        return self.valid_to is None or when < self.valid_to


@dataclass
class Relation:
    source_id: str
    target_id: str
    rel_type: str
    effective_date: date
    evidence: str = ""          # the sentence that triggered the relation
    method: str = "regex"       # "regex" | "repeal_annex" | "title_lineage" | "llm"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["effective_date"] = self.effective_date.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Relation":
        d = dict(d)
        d["effective_date"] = date.fromisoformat(d["effective_date"])
        return cls(**d)


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    text: str
    position: int
    heading: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RetrievedChunk:
    chunk: Chunk
    doc: Document
    score: float
    sources: list[str] = field(default_factory=list)  # which retrievers found it
    status_note: str = ""                             # e.g. "amended by X on 2024-01-01"
    via: str = "search"                               # "search" | "graph_expansion"
    redact_after: Optional[date] = None               # hide sentences dated after this (in-place-updated text)
