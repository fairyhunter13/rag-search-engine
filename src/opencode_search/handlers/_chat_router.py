"""Unified chat router — auto-detects intent, calls right handler, returns humanized prose.

Every response is a flowing narrative written by the query-tier LLM, not a raw JSON
dump of structured data. The caller always gets a conversational answer with code
references embedded naturally in the text.

Intent hierarchy (first match wins):
  debug_trace  — query contains a recognisable stack trace
  debug        — mentions bug/fail/error/inconsistency/why fails
  search       — explicit "find / where is / search for" without explanation keywords
  graph_callers — "what calls X / callers of X"
  graph_callees — "what does X call / callees / downstream"
  graph_impact  — "what breaks if / impact / blast radius"
  global        — "list all / all features / exhaustive / business processes"
  feature       — default (how does X work, why is X designed, explain X)
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

log = logging.getLogger(__name__)

# ── Intent patterns ───────────────────────────────────────────────────────────

_DEBUG_KEYWORDS = frozenset([
    "bug", "fail", "failure", "error", "wrong", "inconsistency", "inconsistent",
    "why does", "why is it", "not working", "broken", "crash", "exception",
    "issue", "problem", "panic", "traceback", "segfault", "unexpected", "corrupt",
    "race condition", "deadlock", "null pointer", "nil pointer", "undefined",
])

_SEARCH_KEYWORDS = frozenset([
    "find", "where is", "where are", "which file", "which files",
    "locate", "search for", "show me the code for", "show the implementation",
])

_EXPLANATION_OVERRIDE = frozenset([
    "how", "why", "what", "explain", "describe", "understand", "tell me",
])

_GLOBAL_KEYWORDS = frozenset([
    "list all", "all features", "every feature", "everything", "complete list",
    "exhaustive", "business processes", "all functionalities", "what are the",
    "give me all",
])

_STACK_TRACE_PATTERNS = re.compile(
    r'File "[^"]+", line \d+'  # Python
    r'|at .+\(.+\.(java|kt|scala):\d+\)'  # Java/Kotlin
    r'|goroutine \d+ \['  # Go
    r'|\.go:\d+ \+'  # Go alt
    r'|at \S+ \(/.+\.(js|ts):\d+'  # JS
    r'|\.rs:\d+\b'  # Rust
)

_CALLER_PATTERNS = re.compile(
    r'\b(what calls|who calls|callers of|called by|callers)\b', re.IGNORECASE
)
_CALLEE_PATTERNS = re.compile(
    r'\b(what does .+ call|callees of|downstream of|calls from|what .+ calls)\b', re.IGNORECASE
)
_IMPACT_PATTERNS = re.compile(
    r'\b(what breaks|blast radius|impact of|if i change|what depends)\b', re.IGNORECASE
)


def classify_intent(query: str) -> str:
    """Classify query intent. Returns one of the intent strings listed in module docstring."""
    q_lower = query.lower()

    # Stack trace → debug_trace
    if _STACK_TRACE_PATTERNS.search(query):
        return "debug_trace"

    # Explicit debug keywords
    if any(kw in q_lower for kw in _DEBUG_KEYWORDS):
        return "debug"

    # Graph callers
    if _CALLER_PATTERNS.search(query):
        return "graph_callers"

    # Graph callees
    if _CALLEE_PATTERNS.search(query):
        return "graph_callees"

    # Graph impact
    if _IMPACT_PATTERNS.search(query):
        return "graph_impact"

    # Global/exhaustive synthesis
    if any(kw in q_lower for kw in _GLOBAL_KEYWORDS):
        return "global"

    # Search (only if no explanation words present)
    if any(kw in q_lower for kw in _SEARCH_KEYWORDS) and not any(kw in q_lower for kw in _EXPLANATION_OVERRIDE):
        return "search"

    # Default: feature/architecture explanation
    return "feature"


def _extract_symbol(query: str) -> str:
    """Best-effort extraction of a symbol name from a graph query."""
    # "what calls handle_pipeline" → "handle_pipeline"
    patterns = [
        re.compile(r"(?:callers? of|what calls?|who calls?|callees? of|what does (\S+) call|downstream of|impact of|if i change)\s+['\"]?(\w+)['\"]?", re.IGNORECASE),
        re.compile(r"['\"](\w+)['\"]"),
        re.compile(r"\b([A-Za-z_]\w*(?:\.\w+)*)\b"),
    ]
    for pat in patterns:
        m = pat.search(query)
        if m:
            # Return last non-empty group
            groups = [g for g in m.groups() if g]
            if groups:
                return groups[-1]
    # Fallback: longest word
    words = re.findall(r"\b[A-Za-z_]\w{2,}\b", query)
    if words:
        return max(words, key=len)
    return query.split()[-1] if query.split() else query


# ── Narrative composers ───────────────────────────────────────────────────────

def _prose_search(result: dict, query: str) -> str:
    """Turn raw search results into a readable narrative."""
    results = result.get("results", [])
    if not results:
        return (
            f"I searched the codebase for **{query}** but found no matching code. "
            "Try broadening the query or check that the project is fully indexed."
        )
    lines = [
        f"Here are the most relevant code locations for **{query}**:\n"
    ]
    for i, r in enumerate(results[:8], 1):
        path = r.get("path", "")
        score = r.get("score", 0)
        snippet = (r.get("content") or "").strip()[:200].replace("\n", " ")
        lang = r.get("language", "")
        lines.append(
            f"{i}. **`{path}`** (relevance: {score:.2f}"
            + (f", {lang}" if lang else "")
            + ")"
        )
        if snippet:
            lines.append(f"   > {snippet}…")
    return "\n".join(lines)


def _prose_graph(result: dict, query: str, direction: str) -> str:
    """Turn graph result into readable narrative."""
    key = "callers" if direction == "callers" else "callees"
    nodes = result.get(key, [])
    symbol = result.get("symbol", "this symbol")

    if not nodes:
        return (
            f"No {direction} found for **{symbol}** in the indexed codebase. "
            "The symbol may not be in the graph yet — try re-indexing."
        )

    lines = [f"**{direction.capitalize()} of `{symbol}`:**\n"]
    for n in nodes[:12]:
        name = n.get("qualified_name") or n.get("name", "unknown")
        file = n.get("file_path") or n.get("file", "")
        depth = n.get("depth", "")
        conf = n.get("confidence", "")
        line_str = f"- `{name}`"
        if file:
            line_str += f" in `{file}`"
        if depth:
            line_str += f" (depth {depth})"
        if conf:
            line_str += f" — confidence: {conf}"
        lines.append(line_str)
    return "\n".join(lines)


def _prose_impact(result: dict, query: str) -> str:
    """Turn impact analysis into readable narrative."""
    narrative = result.get("narrative") or result.get("summary", "")
    if narrative:
        return narrative
    changed = result.get("changed_symbols", [])
    affected = result.get("affected_communities", [])
    if not changed and not affected:
        return f"No impact data found for the query: **{query}**."
    lines = [f"**Impact analysis for `{query}`:**\n"]
    if changed:
        lines.append("Directly affected symbols:")
        for s in changed[:8]:
            lines.append(f"- `{s.get('name', s)}`")
    if affected:
        lines.append("\nArchitecture areas affected:")
        for c in affected[:5]:
            lines.append(f"- {c.get('title', c)}")
    return "\n".join(lines)


def _prose_feature(result: dict, query: str) -> str:
    """Turn feature trace result into humanized narrative."""
    if result.get("status") != "ok":
        err = result.get("error", "No results found.")
        return f"Could not trace the feature for **{query}**: {err}"

    parts: list[str] = []

    # Algorithm
    algo = result.get("algorithm") or ""
    if algo:
        parts.append(f"**How it works:**\n{algo}")

    # Entry points
    eps = result.get("entry_points", [])
    if eps:
        ep_lines = ["**Entry points:**"]
        for ep in eps[:5]:
            f = ep.get("file", "")
            fn = ep.get("symbol") or ep.get("function", "")
            ep_lines.append(f"- `{fn}` in `{f}`")
        parts.append("\n".join(ep_lines))

    # Call chain
    chain = result.get("call_chain", [])
    if chain:
        chain_lines = ["**Call chain:**"]
        for step in chain[:8]:
            name = step.get("name") or step.get("qualified_name", "")
            f = step.get("file", "")
            depth = step.get("depth", "")
            indent = "  " * (int(depth) if str(depth).isdigit() else 0)
            chain_lines.append(f"{indent}→ `{name}`" + (f" (`{f}`)" if f else ""))
        parts.append("\n".join(chain_lines))

    # Design rationale
    rationale = result.get("design_rationale") or ""
    if rationale:
        parts.append(f"**Why it's designed this way:**\n{rationale}")

    # Services
    services = result.get("involved_services", [])
    if services:
        parts.append("**Involved services:** " + ", ".join(f"`{s}`" for s in services[:6]))

    return "\n\n".join(parts) if parts else f"Feature trace for **{query}** found no structured data."


def _prose_debug(result: dict) -> str:
    """Turn debug trace result into readable root cause report."""
    root = result.get("root_cause", "")
    fix = result.get("fix_recommendation", "")
    conf = result.get("confidence", "")
    files = result.get("hotspot_files", [])
    communities = result.get("communities_involved", [])

    parts = []
    if conf:
        label = {"high": "🔴 High confidence", "medium": "🟡 Medium confidence", "low": "⚪ Low confidence"}.get(conf, conf)
        parts.append(f"**{label} root cause analysis:**\n")

    if root:
        parts.append(root)

    if files:
        parts.append("\n**Likely bug location(s):**")
        for f in files[:4]:
            parts.append(f"- `{f}`")

    if fix:
        parts.append(f"\n**Recommended fix:**\n{fix}")

    if communities:
        parts.append("\n**Architecture areas involved:** " + ", ".join(communities[:4]))

    return "\n".join(parts) if parts else "Could not determine root cause from available context."


# ── NL debug (no stack trace) ─────────────────────────────────────────────────

async def _handle_nl_debug(query: str, project_path: str) -> dict[str, Any]:
    """Debug investigation from natural language — no stack trace required.

    Strategy:
    1. Feature-trace the problematic code/feature
    2. Use KB chat with a "code review / bug hunt" system prompt
    3. Return structured debug result
    """
    from opencode_search.enricher import create_query_llm_client
    from opencode_search.handlers._feature import handle_ask_feature
    from opencode_search.handlers._query import handle_search_code

    t0 = time.perf_counter()

    # Get feature context
    feature_result = await handle_ask_feature(query=query, project_path=project_path, top_k=12)
    code_result = await handle_search_code(query=query, project_paths=[project_path], top_k=8)

    # Build investigation context
    algo = feature_result.get("algorithm", "")
    eps = feature_result.get("entry_points", [])
    rationale = feature_result.get("design_rationale", "")
    code_results = code_result.get("results", [])

    ctx_lines: list[str] = ["[FEATURE UNDERSTANDING]"]
    if algo:
        ctx_lines.append(f"Algorithm: {algo[:600]}")
    if rationale:
        ctx_lines.append(f"Design rationale: {rationale[:400]}")
    if eps:
        ctx_lines.append("\nEntry points:")
        for ep in eps[:4]:
            ctx_lines.append(f"  {ep.get('file', '')}:{ep.get('symbol', '')}")

    ctx_lines.append("\n[CODE SAMPLES]")
    for r in code_results[:5]:
        snippet = (r.get("content") or "")[:300].replace("\n", " ")
        ctx_lines.append(f"  {r.get('path', '')}: {snippet}")

    context = "\n".join(ctx_lines)

    # LLM investigation
    system = (
        "You are a senior software engineer doing a security and bug audit. "
        "Based on the provided code context, identify: "
        "1) Potential bugs, race conditions, or logic errors "
        "2) Inconsistencies between the algorithm description and expected behaviour "
        "3) Edge cases that are not handled "
        "4) Any design smell that could cause production issues. "
        "Be specific: name exact files, functions, and lines. "
        "Format: Root Cause (if any), Inconsistencies Found, Risk Assessment, Recommendations."
    )

    root_cause = "(Analysis unavailable)"
    hotspot_files = [ep.get("file", "") for ep in eps[:4] if ep.get("file")]

    try:
        llm = create_query_llm_client()
        resp = await llm.chat(
            messages=[{"role": "user", "content": f"Analyse this for bugs and inconsistencies:\n\n{context}\n\nOriginal question: {query}"}],
            system=system,
        )
        root_cause = resp.get("content", "") if isinstance(resp, dict) else str(resp)
    except Exception as e:
        log.warning("NL debug LLM failed: %s", e)
        root_cause = f"Based on the feature trace, the code works as follows: {algo}"

    return {
        "frames": [],
        "root_cause": root_cause,
        "fix_recommendation": None,
        "hotspot_files": hotspot_files,
        "communities_involved": [ep.get("file", "")[:30] for ep in eps[:3]],
        "confidence": "medium" if eps else "low",
        "elapsed_ms": round((time.perf_counter() - t0) * 1000),
    }


# ── Main router ───────────────────────────────────────────────────────────────

async def handle_chat_auto(
    query: str,
    project_path: str,
    conversation_history: list[dict] | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    """Unified chat handler with automatic intent routing and humanized prose output.

    Args:
        query: Natural language question or bug report (with or without stack trace).
        project_path: Indexed project path.
        conversation_history: Prior turns [{role: "user"|"assistant", content: str}].
        mode: "auto" (default) | "quick" | "comprehensive" | explicit intent string.

    Returns:
        {
            "answer": str,        # humanized prose, ready to display
            "intent": str,        # detected intent
            "sources": list[str], # cited file paths
            "elapsed_ms": int,
            "model": str,
        }
    """
    t0 = time.perf_counter()
    intent = mode if mode not in ("auto", "quick", "comprehensive") else classify_intent(query)

    sources: list[str] = []
    answer = ""
    model_name = ""

    # ── debug_trace: has a stack trace ────────────────────────────────────────
    if intent == "debug_trace":
        from opencode_search.handlers._debug_trace import handle_debug_trace
        result = await handle_debug_trace(
            traceback=query, project_path=project_path, include_fix=True
        )
        answer = _prose_debug(result)
        sources = result.get("hotspot_files", [])

    # ── debug: natural language bug investigation ─────────────────────────────
    elif intent == "debug":
        result = await _handle_nl_debug(query, project_path)
        answer = _prose_debug(result)
        sources = result.get("hotspot_files", [])

    # ── graph_callers / graph_callees / graph_impact ──────────────────────────
    elif intent in ("graph_callers", "graph_callees", "graph_impact"):
        symbol = _extract_symbol(query)
        if intent == "graph_callers":
            from opencode_search.handlers._graph import handle_get_callers
            result = await handle_get_callers(symbol=symbol, project_path=project_path, depth=3)
            answer = _prose_graph(result, query, "callers")
            sources = list({n.get("file_path", "") for n in result.get("callers", []) if n.get("file_path")})
        elif intent == "graph_callees":
            from opencode_search.handlers._graph import handle_get_callees
            result = await handle_get_callees(symbol=symbol, project_path=project_path, depth=3)
            answer = _prose_graph(result, query, "callees")
            sources = list({n.get("file_path", "") for n in result.get("callees", []) if n.get("file_path")})
        else:
            from opencode_search.handlers._impact import handle_detect_impact
            result = await handle_detect_impact(symbol=symbol, project_path=project_path)
            answer = _prose_impact(result, query)
            sources = result.get("affected_files", [])

    # ── search: explicit code lookup ──────────────────────────────────────────
    elif intent == "search":
        from opencode_search.handlers._query import handle_search_code
        result = await handle_search_code(query=query, project_paths=[project_path], top_k=10)
        answer = _prose_search(result, query)
        sources = [r.get("path", "") for r in result.get("results", []) if r.get("path")]

    # ── global: exhaustive feature list → MAP-REDUCE ─────────────────────────
    elif intent == "global":
        from opencode_search.handlers._kb_chat import handle_kb_chat
        result = await handle_kb_chat(
            query=query, project_path=project_path,
            mode="comprehensive", top_k=30,
            conversation_history=conversation_history,
        )
        answer = result.get("answer", "")
        sources = result.get("sources", [])
        model_name = result.get("model", "")

    # ── feature: how does X work — try feature trace first, then kb_chat ─────
    else:
        # Parallel: feature trace + kb_chat context
        import asyncio

        from opencode_search.handlers._feature import handle_ask_feature
        from opencode_search.handlers._kb_chat import handle_kb_chat
        feature_task = handle_ask_feature(query=query, project_path=project_path, top_k=12)
        # Always use quick mode here — feature trace already provides the detailed code trace,
        # and running MAP-REDUCE concurrently with feature trace saturates OLLAMA_NUM_PARALLEL=2
        # (each MAP batch that should take 30s takes 60s due to queuing, pushing total > 300s).
        kb_task = handle_kb_chat(query=query, project_path=project_path, mode="quick", top_k=20,
                                 conversation_history=conversation_history)

        feature_result, kb_result = await asyncio.gather(feature_task, kb_task, return_exceptions=True)

        if isinstance(kb_result, Exception):
            kb_result = {}
        if isinstance(feature_result, Exception):
            feature_result = {}

        # Prefer kb_chat answer (richer context) but supplement with feature trace
        kb_answer = kb_result.get("answer", "") if isinstance(kb_result, dict) else ""
        feature_prose = _prose_feature(feature_result, query) if isinstance(feature_result, dict) and feature_result.get("status") == "ok" else ""

        if kb_answer and feature_prose and len(feature_prose) > 100:
            # Combine: KB answer (comprehensive) + feature trace details
            answer = f"{kb_answer}\n\n---\n\n**Detailed trace:**\n\n{feature_prose}"
        elif kb_answer:
            answer = kb_answer
        elif feature_prose:
            answer = feature_prose
        else:
            answer = f"No results found for: **{query}**. Try rebuilding the knowledge base with `build(action='pipeline')`."

        kb_sources = kb_result.get("sources", []) if isinstance(kb_result, dict) else []
        feat_sources = [ep.get("file", "") for ep in (feature_result.get("entry_points", []) if isinstance(feature_result, dict) else [])]
        sources = list(set(kb_sources + feat_sources))
        model_name = kb_result.get("model", "") if isinstance(kb_result, dict) else ""

    elapsed = round((time.perf_counter() - t0) * 1000)
    sources = [s for s in sources if s][:10]

    return {
        "answer": answer,
        "intent": intent,
        "sources": sources,
        "elapsed_ms": elapsed,
        "model": model_name,
    }


async def handle_chat_auto_stream(
    query: str,
    project_path: str,
    conversation_history: list[dict] | None = None,
    mode: str = "auto",
    chunk_size: int = 40,
):
    """Async generator wrapping handle_chat_auto() — yields NDJSON events.

    Each yielded dict is one of:
      {"type": "thinking"}              — keepalive while LLM is processing (every ~10s)
      {"type": "token", "text": "<chunk>"}
      {"type": "done", "intent": "...", "sources": [...], "elapsed_ms": 0, "model": "..."}

    If the LLM client supports streaming, real tokens are yielded.
    Otherwise the full response is split into chunk_size-char pieces for
    immediate progressive display.
    """
    import asyncio

    # Run LLM call as a concurrent task; yield keepalives every 10s so the
    # HTTP connection doesn't time out during long MAP-REDUCE operations.
    task = asyncio.ensure_future(handle_chat_auto(
        query=query,
        project_path=project_path,
        conversation_history=conversation_history,
        mode=mode,
    ))

    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        except asyncio.TimeoutError:
            yield {"type": "thinking"}

    result = await task
    answer: str = result.get("answer", "")

    for i in range(0, max(len(answer), 1), chunk_size):
        chunk = answer[i:i + chunk_size]
        if chunk:
            yield {"type": "token", "text": chunk}
        await asyncio.sleep(0)

    yield {
        "type": "done",
        "intent": result.get("intent", ""),
        "sources": result.get("sources", []),
        "elapsed_ms": result.get("elapsed_ms", 0),
        "model": result.get("model", ""),
    }
