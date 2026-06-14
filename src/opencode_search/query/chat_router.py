"""7-intent chat classifier + dispatcher.

Intents: search, graph_callers, graph_callees, graph_impact,
         architecture, global, feature.

graph_* intents extract the symbol name from the query text and delegate
to query.graph_handler.  All others delegate to query.ask.
"""
from __future__ import annotations

from opencode_search.embed.embedder import Embedder
from opencode_search.graph.llm import chat as _llm_chat
from opencode_search.graph.store import GraphStore
from opencode_search.index.store import VectorStore

_INTENTS = (
    "search", "graph_callers", "graph_callees", "graph_impact",
    "architecture", "global", "feature",
)

_CLASSIFY_PROMPT = """\
Classify the user query into ONE intent from: {intents}.
- search: find specific code, files, or functions
- graph_callers: what calls symbol X?
- graph_callees: what does symbol X call?
- graph_impact: what breaks if symbol X changes?
- architecture: overall system design / how does X work at a high level?
- global: comprehensive project-wide synthesis
- feature: end-to-end trace of a feature or user flow

Query: {query}
Reply with ONLY the single intent word."""


def classify_intent(query: str) -> str:
    """Classify query into one of 7 intents via LLM. Falls back to 'search'."""
    prompt = _CLASSIFY_PROMPT.format(intents=", ".join(_INTENTS), query=query)
    try:
        result = _llm_chat(prompt).strip().lower()
        for intent in _INTENTS:
            if intent in result:
                return intent
    except Exception:
        pass
    return "search"


def _extract_symbol(query: str, store: GraphStore | None = None) -> str:
    """LLM-extract the target symbol; fuzzy-resolve against graph store if given."""
    try:
        raw = _llm_chat(
            f"Query: {query}\n"
            "What specific function, class, or symbol name is the user asking about? "
            "Reply with ONLY the identifier name, nothing else.",
        ).strip()
        symbol = raw if (raw and len(raw) < 80 and " " not in raw) else (raw.split() or [""])[0]
    except Exception:
        symbol = ""
    if not symbol:
        symbol = query.split()[0] if query.split() else query
    if store is not None:
        exact = store._con.execute(
            "SELECT name FROM symbols WHERE name=? OR qualified_name=? LIMIT 1",
            (symbol, symbol),
        ).fetchone()
        if not exact:
            fuzzy = store._con.execute(
                "SELECT name FROM symbols WHERE name LIKE ? LIMIT 1", (f"%{symbol}%",),
            ).fetchone()
            if fuzzy:
                symbol = fuzzy[0]
    return symbol


def route(
    query: str,
    embedder: Embedder,
    vstore: VectorStore,
    gstore: GraphStore,
) -> tuple[str, str]:
    """Classify intent and dispatch. Returns (intent, answer)."""
    from opencode_search.query import ask as ask_mod
    from opencode_search.query import graph_handler
    from opencode_search.query import search as search_mod

    intent = classify_intent(query)

    if intent == "search":
        results = search_mod.search(query, embedder, vstore, scope="code", top_k=5)
        if not results:
            return intent, "No results found."
        lines = [f"{r['path']}:{r['start_line']}\n{r['content'][:200]}" for r in results]
        return intent, "\n\n".join(lines)

    if intent.startswith("graph_"):
        symbol = _extract_symbol(query, gstore)
        if intent == "graph_callers":
            found = graph_handler.callers(symbol, gstore)
            return intent, "\n".join(r["name"] for r in found) or f"No callers of '{symbol}'."
        if intent == "graph_callees":
            found = graph_handler.callees(symbol, gstore)
            return intent, "\n".join(r["name"] for r in found) or f"No callees of '{symbol}'."
        return intent, graph_handler.impact_narrative(symbol, gstore)

    scope = {"architecture": "architecture", "global": "global", "feature": "feature"}.get(
        intent, "all"
    )
    chunks = search_mod.search(query, embedder, vstore, scope="all", top_k=8)
    return intent, ask_mod.ask(query, chunks, gstore, scope=scope)
