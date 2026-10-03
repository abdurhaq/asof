ANSWER_SYSTEM = """You are AsOf, a research assistant for Reserve Bank of India (RBI) regulations.
You answer questions about what the rules were ON A SPECIFIC REFERENCE DATE.

Rules:
1. Use ONLY the numbered context blocks. Never use outside knowledge about RBI rules.
2. Put a citation like [2] after every factual claim. Only cite block numbers that exist. Use exactly
   this plain [n] form - never 【...】, † or line references.
3. Each block has a STATUS line saying whether that document was in force on the reference date,
   whether it had been amended, and whether it was replaced later. Respect it:
   - Base the answer on documents in force on the reference date.
   - If an amendment applies, give the amended position and cite the amendment too.
   - If a document was replaced AFTER the reference date, you may say so in one sentence.
   - If a STATUS line carries a CAUTION that the text was updated after the reference date, say so,
     and do not present provisions with dates after the reference date as applicable on that date.
     In that case never claim the text "reflects the provisions in force" on the reference date or that
     "no later amendment changed" something: you cannot know that. Say instead that the source is a later
     consolidated version and the user should confirm against the circular as issued.
4. If the context does not answer the question, say so plainly; do not guess.
5. Start with a one-sentence direct answer, then give supporting points. Keep it under 250 words.
6. Always state the reference date you answered for, e.g. "As of 31 March 2021, ...".
"""

COMPARE_SYSTEM = ANSWER_SYSTEM + """
This is a COMPARISON question. The context is grouped by reference date. Describe what the
position was on each date, then list the concrete differences. Cite blocks for each side.
"""

REWRITE_SYSTEM = """REWRITE the user's question into a short search query using the vocabulary of RBI
circulars (e.g. "KYC" -> "Know Your Customer (KYC) Directions customer due diligence").
Drop dates. Return only the query text."""

JUDGE_SYSTEM = """You are a strict JUDGE checking whether an answer is supported by its sources.
Given CONTEXT and ANSWER, score from 0 to 1 the fraction of the answer's factual claims that are
directly supported by the context. Return JSON only: {"score": <float>, "reason": "<short>"}"""


def redact_later_sentences(text: str, after) -> str:
    """For text RBI edited in place: drop sentences that mention a date after ``after`` - they
    were almost certainly added by later amendments - and leave a visible marker instead."""
    import re

    from asof.dates import dates_mentioned

    parts = re.split(r"(?<=[.;])\s+|\n+", text)
    kept, dropped = [], 0
    for sent in parts:
        if any(d > after for d in dates_mentioned(sent)):
            dropped += 1
            continue
        kept.append(sent)
    out = " ".join(kept)
    if dropped:
        out += f" [{dropped} sentence(s) mentioning dates after the reference date omitted - added by later amendments]"
    return out


def format_context(groups: list[tuple[str, list]], max_chars: int = 14000,
                   block_chars: int = 1500) -> tuple[str, list]:
    """groups = [(label, [RetrievedChunk, ...]), ...] -> (prompt text, flat numbered list).

    Keeps the prompt inside a character budget (~4 chars per token) so it fits free-tier
    token-per-minute limits: each block is cut to ``block_chars`` and lower-ranked blocks are
    dropped once a group's share of the budget is used (the first block of a group is always kept).
    """
    lines, flat = [], []
    share = max_chars // max(1, len(groups))
    for label, items in groups:
        if len(groups) > 1:
            lines.append(f"=== {label} ===")
        used = 0
        for rc in items:
            body = rc.chunk.text.split("\n", 1)[-1].strip()  # drop the contextual header line
            if getattr(rc, "redact_after", None):
                body = redact_later_sentences(body, rc.redact_after)
            if len(body) > block_chars:
                body = body[:block_chars].rsplit(" ", 1)[0] + " ..."
            d = rc.doc
            block = [f"[{len(flat) + 1}] {d.title} | {d.ref_no} | issued {d.issue_date.isoformat()}",
                     f"STATUS: {rc.status_note}", body, ""]
            size = sum(len(x) + 1 for x in block)
            if used and used + size > share:
                break
            flat.append(rc)
            lines.extend(block)
            used += size
    return "\n".join(lines), flat
