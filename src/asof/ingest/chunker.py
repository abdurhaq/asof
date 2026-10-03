"""Paragraph-aware chunking with a contextual header on every chunk.

Each chunk starts with "[<title> | <ref no> | issued <date>]" so that both the
keyword and the vector index know which document a paragraph belongs to
(a cheap version of "contextual retrieval").
"""
from __future__ import annotations

import re

from asof.dates import first_date
from asof.ingest.parse import RE_DEPT_REF, RE_REF_NO
from asof.models import Chunk, Document

BOILERPLATE = re.compile(
    r"^\s*(madam\s*/?\s*dear\s+sir|dear\s+sir|madam|yours\s+faithfully|yours\s+sincerely|encl|"
    r"\(?[a-z .]+\)?\s*(chief\s+)?general\s+manager|all\s+[a-z ,/&-]{3,80}$)",
    re.I,
)
RE_PARA_START = re.compile(r"^\s*(?:\d{1,3}(?:\.\d{1,3})*\.?|\([a-z0-9]{1,4}\)|[ivx]{1,5}\.)\s+", re.I)


def _paragraphs(text: str) -> list[str]:
    lines = [l.strip() for l in text.split("\n")]
    paras, buf = [], []
    for line in lines:
        if not line:
            if buf:
                paras.append(" ".join(buf))
                buf = []
            continue
        if RE_PARA_START.match(line) and buf:
            paras.append(" ".join(buf))
            buf = []
        buf.append(line)
    if buf:
        paras.append(" ".join(buf))
    return [p for p in paras if len(p) > 20 and not BOILERPLATE.match(p)]


def header(doc: Document) -> str:
    return f"[{doc.title} | {doc.ref_no} | issued {doc.issue_date.isoformat()}]"


def _is_letterhead(p: str, doc: Document) -> bool:
    """Title / reference-number / date lines that the chunk header already carries."""
    s = p.strip()
    if s.lower() == doc.title.lower() or s in (doc.ref_no, doc.dept_ref):
        return True
    if RE_REF_NO.fullmatch(s) or RE_DEPT_REF.fullmatch(s):
        return True
    return len(s) < 40 and first_date(s) is not None


def chunk_document(doc: Document, max_chars: int = 1200, overlap_paras: int = 1) -> list[Chunk]:
    paras = [p for p in _paragraphs(doc.text) if not _is_letterhead(p, doc)]
    chunks: list[Chunk] = []
    buf: list[str] = []
    size = 0

    def flush():
        if buf:
            body = "\n".join(buf)
            chunks.append(Chunk(f"{doc.doc_id}#c{len(chunks)}", doc.doc_id, f"{header(doc)}\n{body}", len(chunks), doc.title))

    for p in paras:
        # very long paragraphs are split on sentence boundaries
        pieces = [p] if len(p) <= max_chars else re.split(r"(?<=[.;])\s+", p)
        for piece in pieces:
            if size + len(piece) > max_chars and buf:
                flush()
                buf = buf[-overlap_paras:] if overlap_paras else []
                size = sum(len(b) for b in buf)
            buf.append(piece)
            size += len(piece)
    flush()
    return chunks


def chunk_corpus(docs: list[Document], **kw) -> list[Chunk]:
    out: list[Chunk] = []
    for d in docs:
        out.extend(chunk_document(d, **kw))
    return out
