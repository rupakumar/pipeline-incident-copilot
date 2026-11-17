"""Corrective-RAG subgraph: retrieve → grade → (retry with a rewritten query) → cite."""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from pipeline_incident_copilot.agents.knowledge_agent import KnowledgeAgent


class KnowledgeState(TypedDict, total=False):
    log_analysis: Any
    queries: list[str]
    docs: list
    relevant: list
    retries: int
    knowledge: Any


def build_corrective_rag(agent: KnowledgeAgent):
    g = StateGraph(KnowledgeState)
    g.add_node("retrieve", agent.retrieve)
    g.add_node("grade", agent.grade)
    g.add_node("rewrite", agent.rewrite)
    g.add_node("cite", agent.cite)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "grade")
    g.add_conditional_edges("grade", lambda s: "rewrite" if agent.needs_retry(s) else "cite", ["rewrite", "cite"])
    g.add_edge("rewrite", "retrieve")
    g.add_edge("cite", END)
    return g.compile()
