# Architecture and design decisions

## 1. Data model

```
(:Document {doc_id, title, ref_no, dept_ref, doc_type, issue_date, effective_date, valid_to, superseded_by})
(:Document)-[:SUPERSEDES {effective_date, evidence, method}]->(:Document)   new fully replaces old
(:Document)-[:AMENDS     {effective_date, evidence, method}]->(:Document)   new changes part of old
(:Document)-[:REFERS_TO  {effective_date, evidence, method}]->(:Document)   plain citation
```

A document is **in force** on date `t` when `effective_date <= t < valid_to`
(`valid_to` = the effective date of the first document that supersedes it; empty if still in force).

Full text lives in `data/processed/documents.jsonl`; Neo4j keeps metadata and edges only, so the graph stays
small and fast even on AuraDB Free.

## 2. Ingestion pipeline (`src/asof/ingest`)

| Step | File | Notes |
|---|---|---|
| Download | `fetch_rbi.py` | Listing pages for Master Directions / Circulars, plus a sweep over notification ids. Cached, polite (1.5 s delay), resumable. |
| Parse | `parse.py` | Finds the notice body, reference number (`RBI/2023-24/108`), department reference (`DOR.XXX.No.../2023-24`), issue date, **effective date** ("shall come into force with effect from …", "immediately"), document type. De-duplicates by reference number. |
| Relations | `relations.py` | See below. |
| Validity | `graph/temporal.py` | Earliest supersession wins; inconsistent dates are ignored rather than producing empty intervals. |
| Chunking | `chunker.py` | Splits on numbered paragraphs, ~1200 chars, 1-paragraph overlap, strips letterhead boilerplate, and prefixes each chunk with `[title | ref no | issued date]` so both BM25 and embeddings know which document a paragraph belongs to. |

### Relation extraction

Three independent signals, highest-confidence type wins when they disagree:

1. **Sentence cues.** For every reference number found in a sentence, classify by cue words in that sentence:
   `in supersession of / stand repealed / withdrawn` → SUPERSEDES; `partial modification / amend / substituted` → AMENDS;
   otherwise REFERS_TO. References by date only ("circular dated 1 July 2019") are resolved when exactly one document
   has that issue date.
2. **Opening-paragraph rule.** Amendment circulars usually say *"Please refer to circular X"* in paragraph 1 and
   *"it has been decided to amend…"* in paragraph 2 — the cue is not in the same sentence as the reference,
   so a document-level rule upgrades that reference to AMENDS.
3. **Repeal annex.** Master Directions end with "List of circulars repealed/consolidated". Every reference
   after that heading (in the last 60% of the text) is SUPERSEDES.
4. **Title lineage.** Master Circulars are re-issued every year under the same title; each edition supersedes the previous.

Guards: no self-links; a document cannot amend or supersede a *later* document.

**Why rules and not an LLM?** Thousands of references × LLM calls is slow and costly on a free tier, and rule output is
deterministic and auditable (`python -m asof.cli audit`). A natural extension is to send only *ambiguous* references to a
small LLM for classification.

## 3. Retrieval (`src/asof/retrieval`)

- **BM25** (own implementation, ~50 lines) catches exact terms: "LCR", "V-CIP", "Rs 5,000 crore", paragraph numbers.
- **Dense vectors**: `BAAI/bge-small-en-v1.5` (384-d, CPU-friendly) with the BGE query instruction prefix.
- **Reciprocal Rank Fusion** (`k=60`) merges the two rankings without having to calibrate their score scales.
- **Cross-encoder re-ranking**: `cross-encoder/ms-marco-MiniLM-L-6-v2` on the top 20 fused candidates. (`BAAI/bge-reranker-base` was tried first but took ~10 s per query on a laptop CPU; the MiniLM model is ~10x faster.)
- **Point-in-time filter** is applied as a mask *before* ranking, so the top-k is always k in-force passages
  (filtering after ranking can leave you with 1 usable result out of 6).
- **Diversity**: at most 3 passages per document.

## 4. The agent (`src/asof/agent`) — LangGraph

```
START → understand → retrieve ─┬─(0 hits, first try)→ rewrite → retrieve
                               └→ graph → generate → verify → END
```

| Node | What it does |
|---|---|
| understand | Rule-based date and intent parsing: "as of March 2021" → 31 Mar 2021 (end of period); "before July 2022" → 30 Jun 2022; two dates + "between/compare" → comparison; "history/evolution" → history; no date → today. Also strips date phrases from the search query. |
| retrieve | Hybrid search once per reference date. |
| rewrite | If nothing is found, a fast LLM (gpt-oss-20b) rewrites the query into RBI vocabulary, once. |
| graph | For each hit: status note (in force? amended by whom? replaced later?), pulls in amending circulars effective by the date (`AMENDS` edges), and for history questions the full lineage (`SUPERSEDES*`). |
| generate | gpt-oss-120b on Groq (reasoning effort "low"). The prompt includes a STATUS line per block and strict citation rules. |
| verify | Removes citations that point to non-existent blocks and flags answers with no valid citation. |

Each node is a plain function `state → partial update`, so each can be unit-tested; `graph.py` wires them with
`StateGraph` (with a sequential fallback if LangGraph is not installed).

## 5. Evaluation (`src/asof/eval`)

- **Self-labelling benchmark.** For every SUPERSEDES edge old → new effective on `t`: a question dated shortly
  *before* `t` (gold = old) and shortly *after* `t` (gold = new). For every AMENDS edge: a question after the
  amendment (gold = base document, and the amendment must be retrieved).
- **Systems compared:** naive vector RAG, hybrid RAG (BM25+vector+rerank), AsOf (hybrid + time filter + graph).
- **Metrics:** Hit@k, MRR, Stale@k (share of retrieved documents not in force), amendment recall; with `--answers`
  also stale citations, citation rate and LLM-judged faithfulness.
- Stale@k for AsOf is 0 **by construction** when validity intervals are right — so its real quality depends on
  relation-extraction precision, which is why the audit step exists.

## 6. Serving

Starlette API (`/api/ask`, `/api/doc/{id}`, `/api/stats`, `/api/examples`, `/api/health`) + a no-build vanilla JS UI:
time slider over a documents-per-year histogram, answer with clickable citations, agent trace with per-step latency,
Gantt-style version timeline, side-by-side comparison with naive RAG, and a document drawer showing graph links with
the evidence sentence for each.

## 7. Scaling notes (good interview talking points)

- Vectors are a NumPy matrix — fine up to ~100k chunks. Beyond that: FAISS/HNSW, or Neo4j's native vector index so
  the time filter and vector search run in one Cypher query.
- The validity mask is O(chunks) per query; with many dates precompute interval trees or store `valid_from/valid_to`
  on chunk nodes and filter in the index.
- Incremental updates: new circulars only add nodes/edges; re-run `compute_validity` for affected targets.
