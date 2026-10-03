"""The AsOf agent as a LangGraph state machine.

    understand -> retrieve --(no hits, first try)--> rewrite -> retrieve
                           \\-> expand (graph) -> generate -> verify -> END

If LangGraph is not installed, an equivalent sequential runner is used so the
pipeline (and the tests) still work.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from asof.agent.nodes import AgentState, make_nodes


class _SequentialGraph:
    """Minimal stand-in with the same edges as the LangGraph version."""

    def __init__(self, n):
        self.n = n

    def invoke(self, state: dict) -> dict:
        state = dict(state)
        state.update(self.n["understand"](state))
        state.update(self.n["retrieve"](state))
        while self.n["route_after_retrieve"](state) == "rewrite":
            state.update(self.n["rewrite"](state))
            state.update(self.n["retrieve"](state))
        for step in ("expand", "generate", "verify"):
            state.update(self.n[step](state))
        return state


def build_agent(index, llm, *, k: int = 6, use_graph: bool = True, temporal: bool = True,
                mode: str = "hybrid", rerank: bool = True):
    n = make_nodes(index, llm, k=k, use_graph=use_graph, temporal=temporal, mode=mode, rerank=rerank)
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        return _SequentialGraph(n)

    g = StateGraph(AgentState)
    for name in ("understand", "retrieve", "rewrite", "expand", "generate", "verify"):
        g.add_node(name, n[name])
    g.add_edge(START, "understand")
    g.add_edge("understand", "retrieve")
    g.add_conditional_edges("retrieve", n["route_after_retrieve"], {"rewrite": "rewrite", "expand": "expand"})
    g.add_edge("rewrite", "retrieve")
    g.add_edge("expand", "generate")
    g.add_edge("generate", "verify")
    g.add_edge("verify", END)
    return g.compile()


def ask(agent, question: str, *, as_of: Optional[date] = None, today: Optional[date] = None) -> dict:
    return agent.invoke({"question": question, "as_of_override": as_of, "today": today or date.today(), "trace": []})
