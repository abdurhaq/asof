"""LLM wrappers. ``GroqLLM`` for real use, ``ExtractiveLLM`` for offline runs and tests."""
from __future__ import annotations

import re
import time


class LLM:
    name = "base"

    def complete(self, system: str, user: str, *, fast: bool = False, max_tokens: int = 900) -> str:
        raise NotImplementedError


class GroqLLM(LLM):
    """Groq-hosted open models through LangChain (``langchain-groq``).

    Defaults are OpenAI's open-weight gpt-oss models. They reason before answering and the
    reasoning counts against ``max_tokens``, so budgets are raised and effort is kept low.
    """

    def __init__(self, api_key: str, answer_model: str, fast_model: str, temperature: float = 0.0):
        from langchain_groq import ChatGroq

        self.name = f"groq:{answer_model}"
        self._models = {}
        for key, model, temp in (("answer", answer_model, temperature), ("fast", fast_model, 0.0)):
            kw = {"reasoning_effort": "low"} if "gpt-oss" in model else {}
            self._models[key] = (ChatGroq(model=model, api_key=api_key, temperature=temp, max_retries=3, **kw), model)

    def complete(self, system, user, *, fast=False, max_tokens=900):
        chat, model_name = self._models["fast" if fast else "answer"]
        if "gpt-oss" in model_name:
            max_tokens = max(max_tokens * 2, 512)  # leave room for the hidden reasoning
        model = chat.bind(max_tokens=max_tokens)
        for attempt in range(4):
            try:
                text = model.invoke([("system", system), ("human", user)]).content or ""
                return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
            except Exception as e:  # free tier rate limits -> back off (but a too-large request never fits)
                msg = str(e).lower()
                if ("rate" in msg or "429" in msg) and "413" not in msg and "too large" not in msg and attempt < 3:
                    time.sleep(8 * (attempt + 1))
                    continue
                raise
        return ""


class ExtractiveLLM(LLM):
    """No-API stand-in: quotes the most relevant sentences from the numbered context.

    Lets the whole app, the tests and the retrieval metrics run without an API key.
    """

    name = "extractive-offline"

    def complete(self, system, user, *, fast=False, max_tokens=900):
        if "REWRITE" in system:
            m = re.search(r"Question:\s*(.+)", user)
            return m.group(1).strip() if m else user
        if "JUDGE" in system:
            return '{"score": 1.0, "reason": "offline judge"}'
        blocks = re.findall(r"^\[(\d+)\][^\n]*\n(.*?)(?=^\[\d+\]|\Z)", user, flags=re.S | re.M)
        q = re.search(r"QUESTION:\s*(.+)", user)
        qwords = set(re.findall(r"[a-z]{4,}", (q.group(1) if q else "").lower()))
        picked = []
        for n, body in blocks[:3]:
            body = "\n".join(l for l in body.splitlines() if not l.startswith(("STATUS:", "===")))
            sents = [s.strip() for s in re.split(r"(?<=[.;])\s+|\n+", body)
                     if len(s.strip()) > 40 and s.strip()[-1] in ".;:" and "refer to" not in s.lower()]
            if not sents:
                continue

            def score(s: str) -> float:
                overlap = len(qwords & set(re.findall(r"[a-z]{4,}", s.lower())))
                return overlap + 0.5 * bool(re.search(r"\d", s)) + 0.5 * ("shall" in s.lower())

            best = max(sents, key=score)
            best = re.sub(r"^\(?\d{1,3}(?:\.\d+)*[.)]\s+", "", best)  # drop paragraph numbers
            picked.append(f"{best} [{n}]")
        if not picked:
            return "The provided documents do not contain enough information to answer this question."
        return "Most relevant passages (offline mode: add GROQ_API_KEY for a written answer):\n\n- " + "\n- ".join(picked)


def make_llm(settings) -> LLM:
    if settings.groq_api_key:
        try:
            return GroqLLM(settings.groq_api_key, settings.answer_model, settings.fast_model)
        except ImportError:
            print("langchain-groq not installed -> using the offline extractive answerer")
    return ExtractiveLLM()
