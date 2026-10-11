"""Triage graph: classify -> summarise -> route, escalating to review on low confidence.

START --(decision supplied?)--> route
  |                               ^
  +--> classify --> summarise ----+
                                  |
              route --(confidence < threshold)--> escalate --> END
                    \\-------------------------------------> END
"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.config import Settings
from app.llm import ROUTES, LLMClient


class TriageState(TypedDict, total=False):
    subject: str | None
    body: str
    category: str
    confidence: float
    summary: str
    routed_to: str
    needs_review: bool


def build_flow(llm: LLMClient, settings: Settings):
    async def classify(state: TriageState) -> TriageState:
        result = await llm.classify(state["subject"], state["body"])
        return {"category": result.category, "confidence": result.confidence}

    async def summarise(state: TriageState) -> TriageState:
        return {"summary": await llm.summarise(state["subject"], state["body"])}

    def route(state: TriageState) -> TriageState:
        # Even an escalated ticket carries a suggested team for the reviewer.
        return {"routed_to": ROUTES[state["category"]], "needs_review": False}

    def escalate(state: TriageState) -> TriageState:
        return {"needs_review": True}

    def start(state: TriageState) -> str:
        # A decision supplied up front (cache hit or rules fallback) skips the LLM but
        # still passes the escalation check, so the threshold lives in one place.
        return "route" if "category" in state else "classify"

    def check_confidence(state: TriageState) -> str:
        return "escalate" if state["confidence"] < settings.confidence_threshold else END

    graph = StateGraph(TriageState)
    for node in (classify, summarise, route, escalate):
        graph.add_node(node.__name__, node)
    graph.add_conditional_edges(START, start, ["route", "classify"])
    graph.add_edge("classify", "summarise")
    graph.add_edge("summarise", "route")
    graph.add_conditional_edges("route", check_confidence, ["escalate", END])
    graph.add_edge("escalate", END)
    return graph.compile()
