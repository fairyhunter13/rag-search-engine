"""Unified chat router — LLM-classified intent, calls right handler, returns humanized prose.

Every response is a flowing narrative written by the query-tier LLM, not a raw JSON
dump of structured data. The caller always gets a conversational answer with code
references embedded naturally in the text.

Intent classification uses the LLM (no keyword heuristics) so any phrasing is handled:
  debug_trace   — query IS a stack trace / error log
  debug         — question about a bug, failure, or "why does X not work"
  search        — find/locate/show specific code or files
  graph_callers — "what calls X", "callers of X"
  graph_callees — "what does X call", "downstream of X"
  graph_impact  — blast radius, "what breaks if I change X"
  architecture  — end-to-end system design, layers, "walk me through the whole system"
  global        — exhaustive list of ALL features / business processes
  feature       — default: how does X work, explain X, why is X designed this way
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

log = logging.getLogger(__name__)


async def _bridge_stream(llm: Any, messages: list[dict[str, Any]], max_tokens: int = 1024):
    """Bridge blocking llm.stream_chat() to an async generator via asyncio.Queue.

    Runs the blocking generator in a daemon thread and forwards tokens through a
    thread-safe queue. The caller gets a clean async generator of token strings.
    """
    import asyncio

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _run() -> None:
        try:
            for token in llm.stream_chat(messages, max_tokens=max_tokens):
                loop.call_soon_threadsafe(queue.put_nowait, token)
        except Exception as exc:
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)  # sentinel

    threading.Thread(target=_run, daemon=True).start()

    while True:
        item = await queue.get()
        if item is None:
            break
        if isinstance(item, Exception):
            raise item
        yield item

# ── LLM-based intent classification ──────────────────────────────────────────
# No keyword heuristics — the LLM classifies any phrasing correctly.

_VALID_INTENTS = frozenset([
    "debug_trace", "debug", "search",
    "graph_callers", "graph_callees", "graph_impact",
    "architecture", "global", "feature",
])

_CLASSIFY_SYSTEM = """\
Classify this code-intelligence query into exactly one intent.

Intents:
  debug_trace   — the query IS a stack trace / traceback / error log with file paths and line numbers
  debug         — question about a bug, error, failure, crash, "why fails", "not working" (NO stack trace)
  search        — explicit request to find, locate, or show specific code, files, or function implementations
  graph_callers — "what calls X", "callers of X", "who calls X"
  graph_callees — "what does X call", "callees of X", "downstream of X"
  graph_impact  — blast radius, "what breaks if I change X", "what depends on X"
  architecture  — overall system design, end-to-end flow, layers, "how the whole system works", "walk me through the system"
  global        — exhaustive list of ALL features, ALL business processes, complete inventory of everything
  feature       — default: how does X work, explain X, why is X designed this way, describe feature X

Respond with ONLY valid JSON: {"intent": "<name>"}"""


async def classify_intent_llm(query: str) -> str:
    """Classify query intent via LLM — handles any phrasing, no keyword brittle matching."""
    import asyncio
    import re as _re

    from opencode_search.enricher import create_query_llm_client

    try:
        llm = await asyncio.to_thread(create_query_llm_client)
        if llm is None:
            return "feature"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _CLASSIFY_SYSTEM},
            {"role": "user", "content": query[:500]},
        ]
        raw = await asyncio.to_thread(llm.chat, messages, max_tokens=32)
        text = raw if isinstance(raw, str) else (raw.get("content", "") if isinstance(raw, dict) else str(raw))
        m = _re.search(r'"intent"\s*:\s*"(\w+)"', text)
        if m:
            intent = m.group(1)
            if intent in _VALID_INTENTS:
                return intent
        # Scan response text for a known intent name (ordered: specific → generic)
        text_lower = text.lower()
        for intent in ["debug_trace", "graph_callers", "graph_callees", "graph_impact",
                        "architecture", "debug", "global", "search", "feature"]:
            if intent in text_lower:
                return intent
    except Exception as e:
        log.debug("LLM intent classification failed: %s", e)

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

    Identifies: business process (community), algorithm step, file:line range, root cause.
    Based on Agentless fault localization: file-level → function-level → line-level narrowing.
    """
    import asyncio

    from opencode_search.enricher import create_query_llm_client
    from opencode_search.handlers._feature import handle_ask_feature
    from opencode_search.handlers._kb_chat import _fetch_community_context
    from opencode_search.handlers._query import handle_search_code

    t0 = time.perf_counter()

    # Parallel: feature trace + code search + community context
    feature_task = handle_ask_feature(query=query, project_path=project_path, top_k=12)
    code_task = handle_search_code(query=query, project_paths=[project_path], top_k=8)
    community_task = _fetch_community_context(query, project_path, top_k=8, include_federation=False)

    feature_result, code_result, (_, comm_list, _) = await asyncio.gather(
        feature_task, code_task, community_task, return_exceptions=False
    )

    # Core feature data
    algo = feature_result.get("algorithm", "") if isinstance(feature_result, dict) else ""
    eps = feature_result.get("entry_points", []) if isinstance(feature_result, dict) else []
    rationale = feature_result.get("design_rationale", "") if isinstance(feature_result, dict) else ""
    call_chain = feature_result.get("call_chain", []) if isinstance(feature_result, dict) else []
    code_results = code_result.get("results", []) if isinstance(code_result, dict) else []

    # Look up line ranges for entry points (Agentless-style function-level localization)
    async def _lookup_lines(symbol: str) -> tuple[int, int]:
        try:
            from opencode_search.handlers._graph import handle_get_symbol
            r = await handle_get_symbol(symbol=symbol, project_path=project_path)
            return r.get("start_line", 0), r.get("end_line", 0)
        except Exception:
            return 0, 0

    ep_lines_tasks = [_lookup_lines(ep.get("symbol", "")) for ep in eps[:4]]
    ep_lines_results = await asyncio.gather(*ep_lines_tasks, return_exceptions=True)

    # Build rich context: business process + algorithm + lines
    ctx_lines: list[str] = []

    # Business process context (community semantic_type labels)
    if comm_list:
        ctx_lines.append("[BUSINESS PROCESS CONTEXT]")
        for c in comm_list[:5]:
            st = c.get("semantic_type", "utility")
            ctx_lines.append(f'  Community: "{c["title"]}" (semantic_type: {st})')
            ctx_lines.append(f'  Summary: {c["summary"][:200]}')

    # Algorithm steps
    if algo:
        ctx_lines.append("\n[ALGORITHM STEPS]")
        ctx_lines.append(algo[:600])

    if rationale:
        ctx_lines.append("\n[DESIGN RATIONALE]")
        ctx_lines.append(rationale[:400])

    # Entry points with line ranges
    if eps:
        ctx_lines.append("\n[ENTRY POINTS WITH LINE RANGES]")
        for i, ep in enumerate(eps[:4]):
            sym = ep.get("symbol", "unknown")
            f = ep.get("file", "")
            lines_res = ep_lines_results[i] if i < len(ep_lines_results) else (0, 0)
            if isinstance(lines_res, tuple) and lines_res[0]:
                ctx_lines.append(f"  {sym}() → {f} lines {lines_res[0]}–{lines_res[1]}")
            else:
                ctx_lines.append(f"  {sym}() → {f}")

    # Call chain
    if call_chain:
        ctx_lines.append("\n[CALL CHAIN]")
        for step in call_chain[:6]:
            name = step.get("name") or step.get("qualified_name", "")
            f = step.get("file", "")
            d = step.get("depth", "")
            ctx_lines.append(f"  depth={d}: {name} ({f})")

    # Code samples
    ctx_lines.append("\n[CODE SAMPLES]")
    for r in code_results[:5]:
        snippet = (r.get("content") or "")[:300].replace("\n", " ")
        ctx_lines.append(f"  {r.get('path', '')}: {snippet}")

    context = "\n".join(ctx_lines)

    # LLM with explicit 4-part structure request
    system = (
        "You are a senior software engineer performing root cause analysis. "
        "Using the provided context, identify and state FOUR things with these exact headings:\n\n"
        "**BUSINESS PROCESS:** Name the community/domain where the bug originates "
        "(use the community names provided)\n"
        "**ALGORITHM STEP:** Which step of the algorithm/workflow where the bug manifests "
        "(number it, e.g. 'Step 3: message appending')\n"
        "**FILE & LINE RANGE:** Exact function name and file:line range "
        "(use the provided line data)\n"
        "**ROOT CAUSE:** Why this specific location causes the observed symptom — "
        "include race conditions, edge cases, or logic errors\n\n"
        "Be specific. Reference exact files and functions. "
        "If line data is not provided, estimate based on the call chain."
    )

    root_cause = "(Analysis unavailable)"
    hotspot_files = [ep.get("file", "") for ep in eps[:4] if ep.get("file")]
    business_process = comm_list[0]["title"] if comm_list else ""
    algorithm_step = ""
    line_range = ""

    # Build line_range from best entry point
    if eps and ep_lines_results:
        best_ep = eps[0]
        best_lines = ep_lines_results[0] if ep_lines_results else (0, 0)
        if isinstance(best_lines, tuple) and best_lines[0]:
            line_range = f"{best_ep.get('file', '')}:{best_lines[0]}–{best_lines[1]}"
        else:
            line_range = best_ep.get("file", "")

    try:
        llm = await asyncio.to_thread(create_query_llm_client)
        if llm:
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system},
                {"role": "user", "content": f"Analyse for root cause:\n\n{context}\n\nQuestion: {query}"},
            ]
            root_cause = await asyncio.to_thread(llm.chat, messages, max_tokens=1024)
    except Exception as e:
        log.warning("NL debug LLM failed: %s", e)
        if algo:
            root_cause = (
                f"**BUSINESS PROCESS:** {business_process or 'Unknown'}\n"
                f"**ALGORITHM STEP:** Review the algorithm for edge cases\n"
                f"**FILE & LINE RANGE:** {line_range or 'See entry points above'}\n"
                f"**ROOT CAUSE:** Based on feature trace: {algo[:300]}"
            )

    # Extract algorithm_step from algo if LLM didn't provide it
    if algo and not algorithm_step:
        first_line = algo.split("\n")[0] if "\n" in algo else algo[:100]
        algorithm_step = first_line

    return {
        "frames": [],
        "root_cause": root_cause,
        "fix_recommendation": None,
        "hotspot_files": hotspot_files,
        "communities_involved": [c["title"] for c in comm_list[:4]],
        "business_process": business_process,
        "algorithm_step": algorithm_step,
        "line_range": line_range,
        "confidence": "medium" if eps else "low",
        "elapsed_ms": round((time.perf_counter() - t0) * 1000),
    }


# ── Architecture handler (PathRAG-inspired layered synthesis) ─────────────────

_ARCH_LAYER_MAP = {
    "entry_point": "API & Entry Layer",
    "handler": "API & Entry Layer",
    "router": "API & Entry Layer",
    "api": "API & Entry Layer",
    "business_process": "Business Logic Layer",
    "workflow": "Business Logic Layer",
    "service": "Business Logic Layer",
    "processor": "Business Logic Layer",
    "storage": "Data Layer",
    "repository": "Data Layer",
    "data_access": "Data Layer",
    "database": "Data Layer",
    "utility": "Infrastructure Layer",
    "infrastructure": "Infrastructure Layer",
    "config": "Infrastructure Layer",
    "client": "Infrastructure Layer",
}

_ARCH_LAYER_ORDER = [
    "API & Entry Layer",
    "Business Logic Layer",
    "Data Layer",
    "Infrastructure Layer",
]

_ARCH_SYSTEM_PROMPT = (
    "You are a senior software architect. "
    "Write a comprehensive end-to-end architecture narrative structured as FOUR sections:\n"
    "1. **API & Entry Layer** — how requests/events enter the system "
    "(HTTP routes, CLI commands, MCP tools, background jobs)\n"
    "2. **Business Logic Layer** — the core business workflows and services "
    "that process incoming requests\n"
    "3. **Data Layer** — how data is read and written "
    "(graph DB, vector DB, file system, caches)\n"
    "4. **Infrastructure Layer** — LLM/AI integration, external services, "
    "configuration, monitoring\n\n"
    "For each layer name the key files and functions. "
    "Close with a concrete example: one request traced through all four layers. "
    "Use ONLY the provided context — never fabricate files or functions not present."
)


async def _handle_architecture(
    query: str,
    project_path: str,
    conversation_history: list[dict] | None = None,
) -> dict[str, Any]:
    """Architecture end-to-end narrative — four-layer synthesis (PathRAG-inspired)."""
    import asyncio

    from opencode_search.enricher import create_query_llm_client
    from opencode_search.handlers._kb_chat import _fetch_community_context

    t0 = time.perf_counter()

    from opencode_search.handlers._kb_chat import _fetch_code_context

    async def _get_patterns() -> dict:
        try:
            from opencode_search.handlers._graph import handle_detect_patterns
            return await handle_detect_patterns(project_path=project_path)
        except Exception:
            return {}

    (_, comm_list, comm_count), (code_ctx, _code_sources, _), patterns = await asyncio.gather(
        _fetch_community_context(query, project_path, top_k=25, include_federation=False),
        _fetch_code_context(query, project_path, top_k=12),
        _get_patterns(),
    )

    # Group communities by semantic_type into architectural layers
    layers: dict[str, list[dict]] = {name: [] for name in _ARCH_LAYER_ORDER}
    for comm in comm_list:
        st = (comm.get("semantic_type") or "utility").lower()
        layer = _ARCH_LAYER_MAP.get(st, "Business Logic Layer")
        layers[layer].append(comm)

    # Build layered context string
    ctx_sections: list[str] = []
    for layer_name in _ARCH_LAYER_ORDER:
        comms = layers[layer_name]
        if comms:
            comm_lines = "\n".join(
                f"  [{c['semantic_type']}] {c['title']}: {c['summary'][:200]}"
                for c in comms[:6]
            )
            ctx_sections.append(f"**{layer_name}:**\n{comm_lines}")

    # Append code locations — anchors LLM to real file paths
    if code_ctx:
        ctx_sections.append(f"**Actual code locations (use these exact paths):**\n{code_ctx}")

    # Append detected patterns
    arch_type = patterns.get("architecture", "")
    frameworks = [
        (f.get("name") or str(f)) if isinstance(f, dict) else str(f)
        for f in (patterns.get("key_frameworks") or [])
    ]
    if arch_type or frameworks:
        p_parts = []
        if arch_type:
            p_parts.append(f"Architecture type: {arch_type}")
        if frameworks:
            p_parts.append(f"Key frameworks: {', '.join(frameworks[:6])}")
        ctx_sections.append("**Detected patterns:**\n" + "\n".join(p_parts))

    arch_context = "\n\n".join(ctx_sections) if ctx_sections else "(No architecture data — run build(action='pipeline') first)"

    # LLM synthesis
    llm = await asyncio.to_thread(create_query_llm_client)
    model_name = getattr(llm, "model", type(llm).__name__) if llm else "none"
    answer = "(Architecture analysis unavailable)"

    if llm:
        try:
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": f"{_ARCH_SYSTEM_PROMPT}\n\nArchitecture context:\n{arch_context}"},
            ]
            for turn in (conversation_history or [])[-4:]:
                role = turn.get("role", "")
                content = turn.get("content", "")
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})
            messages.append({"role": "user", "content": query})
            answer = await asyncio.to_thread(llm.chat, messages, max_tokens=1536)
        except Exception as e:
            log.warning("Architecture LLM failed: %s", e)
            lines = [f"**Architecture Overview — {comm_count} communities indexed:**"]
            for layer_name in _ARCH_LAYER_ORDER:
                comms = layers[layer_name]
                if comms:
                    lines.append(f"\n**{layer_name}:**")
                    for c in comms[:4]:
                        lines.append(f"- {c['title']}: {c['summary'][:100]}")
            answer = "\n".join(lines)

    sources = list(dict.fromkeys(
        c["title"] for ln in _ARCH_LAYER_ORDER for c in layers[ln]
    ))[:8]

    return {
        "answer": answer,
        "intent": "architecture",
        "sources": sources,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000),
        "model": model_name,
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
    intent = mode if mode not in ("auto", "quick", "comprehensive") else await classify_intent_llm(query)

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

    # ── architecture: end-to-end layered narrative ───────────────────────────
    elif intent == "architecture":
        result = await _handle_architecture(query, project_path, conversation_history)
        answer = result.get("answer", "")
        sources = result.get("sources", [])
        model_name = result.get("model", "")

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
    """Async generator — yields NDJSON events with real-time token streaming.

    Event types:
      {"type": "thinking"}              — keepalive while context is loading / LLM is busy
      {"type": "token", "text": "<t>"}  — one raw token from the LLM (or chunk fallback)
      {"type": "done", "intent": "...", "sources": [...], "elapsed_ms": N, "model": "..."}

    For feature/search intents (the common case) this uses Ollama's native streaming API so
    the user sees the first token within ~3-5s (context assembly) rather than waiting 30-60s
    for the full response.  For global/MAP-REDUCE intents it falls back to heartbeat + chunk.
    """
    import asyncio
    import time as _time

    t0 = _time.perf_counter()
    intent = mode if mode not in ("auto", "quick", "comprehensive") else await classify_intent_llm(query)

    # ── search: no LLM — yield prose immediately ──────────────────────────────
    if intent == "search":
        from opencode_search.handlers._query import handle_search_code
        result = await handle_search_code(query=query, project_paths=[project_path], top_k=10)
        answer = _prose_search(result, query)
        sources = [r.get("path", "") for r in result.get("results", []) if r.get("path")]
        for i in range(0, max(len(answer), 1), chunk_size):
            chunk = answer[i:i + chunk_size]
            if chunk:
                yield {"type": "token", "text": chunk}
            await asyncio.sleep(0)
        yield {"type": "done", "intent": "search", "sources": sources[:10],
               "elapsed_ms": round((_time.perf_counter() - t0) * 1000), "model": ""}
        return

    # ── feature: context assembly → native LLM streaming ─────────────────────
    if intent == "feature":
        async for event in _stream_feature(
            query=query, project_path=project_path,
            conversation_history=conversation_history, t0=t0,
        ):
            yield event
        return

    # ── architecture: layered synthesis → native LLM streaming ───────────────
    if intent == "architecture":
        async for event in _stream_architecture(
            query=query, project_path=project_path,
            conversation_history=conversation_history, t0=t0,
        ):
            yield event
        return

    # ── global / MAP-REDUCE / debug / graph: heartbeat approach ───────────────
    task = asyncio.ensure_future(handle_chat_auto(
        query=query, project_path=project_path,
        conversation_history=conversation_history, mode=mode,
    ))

    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        except TimeoutError:
            yield {"type": "thinking"}

    result = await task
    answer = result.get("answer", "")
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


async def _stream_feature(
    query: str,
    project_path: str,
    conversation_history: list[dict] | None,
    t0: float,
    chunk_size: int = 40,
):
    """Stream the feature-intent chat response with native Ollama token streaming.

    Runs context assembly (code + community + wiki) and the feature trace concurrently.
    Streams KB answer tokens as they arrive, then appends feature trace on completion.
    """
    import asyncio
    import time as _time

    from opencode_search.enricher import create_query_llm_client
    from opencode_search.handlers._kb_chat import (
        _SYSTEM_PROMPT,
        _fetch_code_context,
        _fetch_community_context,
        _fetch_wiki_context,
    )

    # Step 1: kick off feature trace + context assembly in parallel
    feature_task = asyncio.ensure_future(
        _run_feature_trace(query, project_path)
    )
    code_task = asyncio.ensure_future(_fetch_code_context(query, project_path, top_k=15))
    community_task = asyncio.ensure_future(
        _fetch_community_context(query, project_path, top_k=20, include_federation=False)
    )
    wiki_task = asyncio.ensure_future(_fetch_wiki_context(query, project_path, top_k=5))

    # Step 2: context assembly (usually 2-5s)
    (code_ctx, code_sources, _), (comm_ctx, _, _), (wiki_ctx, _) = await asyncio.gather(
        code_task, community_task, wiki_task
    )

    # Step 3: build messages
    sections: list[str] = []
    if code_ctx:
        sections.append(f"[CODE LOCATIONS]\n{code_ctx}")
    if comm_ctx:
        sections.append(f"[ARCHITECTURE COMMUNITIES]\n{comm_ctx}")
    if wiki_ctx:
        sections.append(f"[WIKI KNOWLEDGE]\n{wiki_ctx}")

    if not sections:
        answer_fallback = (
            "No indexed content found. "
            "Run build(action='pipeline') to index the project first."
        )
        for i in range(0, len(answer_fallback), chunk_size):
            yield {"type": "token", "text": answer_fallback[i:i + chunk_size]}
        yield {"type": "done", "intent": "feature", "sources": [], "elapsed_ms": 0, "model": ""}
        feature_task.cancel()
        return

    context = "\n\n".join(sections)
    system_content = f"{_SYSTEM_PROMPT}\n\nContext from the knowledge base:\n{context}"
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
    for turn in (conversation_history or [])[-6:]:
        role = turn.get("role", "")
        content = turn.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": query})

    # Step 4: get LLM and stream tokens
    llm = await asyncio.to_thread(create_query_llm_client)
    model_name = getattr(llm, "model", type(llm).__name__) if llm else "none"

    if llm is None:
        err = "LLM unavailable. Check OPENCODE_QUERY_LLM_PROVIDER / OPENCODE_LLM_PROVIDER."
        for i in range(0, len(err), chunk_size):
            yield {"type": "token", "text": err[i:i + chunk_size]}
    elif hasattr(llm, "stream_chat"):
        # Native streaming — tokens arrive in real time
        async for token in _bridge_stream(llm, messages, max_tokens=1024):
            yield {"type": "token", "text": token}
    else:
        # Fallback: blocking call + chunk
        answer = await asyncio.to_thread(llm.chat, messages, max_tokens=1024)
        for i in range(0, max(len(answer), 1), chunk_size):
            chunk = answer[i:i + chunk_size]
            if chunk:
                yield {"type": "token", "text": chunk}
            await asyncio.sleep(0)

    # Step 5: append feature trace as supplement (if ready and meaningful)
    feature_result = await feature_task
    if isinstance(feature_result, dict) and feature_result.get("status") == "ok":
        feature_prose = _prose_feature(feature_result, query)
        if feature_prose and len(feature_prose) > 100:
            supplement = f"\n\n---\n\n**Detailed trace:**\n\n{feature_prose}"
            for i in range(0, len(supplement), chunk_size):
                chunk = supplement[i:i + chunk_size]
                if chunk:
                    yield {"type": "token", "text": chunk}
                await asyncio.sleep(0)
        feat_sources = [ep.get("file", "") for ep in feature_result.get("entry_points", [])]
    else:
        feat_sources = []

    all_sources = list(dict.fromkeys([s for s in (code_sources + feat_sources) if s]))[:10]
    yield {
        "type": "done",
        "intent": "feature",
        "sources": all_sources,
        "elapsed_ms": round((_time.perf_counter() - t0) * 1000),
        "model": model_name,
    }


async def _run_feature_trace(query: str, project_path: str) -> dict[str, Any]:
    """Run handle_ask_feature safely, returning {} on any error."""
    try:
        from opencode_search.handlers._feature import handle_ask_feature
        return await handle_ask_feature(query=query, project_path=project_path, top_k=12)
    except Exception:
        return {}


async def _stream_architecture(
    query: str,
    project_path: str,
    conversation_history: list[dict] | None,
    t0: float,
    chunk_size: int = 40,
):
    """Stream the architecture-intent response with native Ollama token streaming."""
    import asyncio
    import time as _time

    from opencode_search.enricher import create_query_llm_client
    from opencode_search.handlers._kb_chat import _fetch_code_context, _fetch_community_context

    async def _get_patterns() -> dict:
        try:
            from opencode_search.handlers._graph import handle_detect_patterns
            return await handle_detect_patterns(project_path=project_path)
        except Exception:
            return {}

    # Parallel context assembly
    (_, comm_list, _comm_count), (code_ctx, _, _), patterns = await asyncio.gather(
        _fetch_community_context(query, project_path, top_k=25, include_federation=False),
        _fetch_code_context(query, project_path, top_k=12),
        _get_patterns(),
    )

    # Group and build layered context (same as _handle_architecture)
    layers: dict[str, list[dict]] = {name: [] for name in _ARCH_LAYER_ORDER}
    for comm in comm_list:
        st = (comm.get("semantic_type") or "utility").lower()
        layers[_ARCH_LAYER_MAP.get(st, "Business Logic Layer")].append(comm)

    ctx_sections: list[str] = []
    for layer_name in _ARCH_LAYER_ORDER:
        comms = layers[layer_name]
        if comms:
            ctx_sections.append(
                f"**{layer_name}:**\n"
                + "\n".join(
                    f"  [{c['semantic_type']}] {c['title']}: {c['summary'][:200]}"
                    for c in comms[:6]
                )
            )

    if code_ctx:
        ctx_sections.append(f"**Actual code locations (use these exact paths):**\n{code_ctx}")

    arch_type = patterns.get("architecture", "")
    frameworks = [
        (f.get("name") or str(f)) if isinstance(f, dict) else str(f)
        for f in (patterns.get("key_frameworks") or [])
    ]
    if arch_type or frameworks:
        p_parts = []
        if arch_type:
            p_parts.append(f"Architecture type: {arch_type}")
        if frameworks:
            p_parts.append(f"Key frameworks: {', '.join(frameworks[:6])}")
        ctx_sections.append("**Detected patterns:**\n" + "\n".join(p_parts))

    arch_context = "\n\n".join(ctx_sections) if ctx_sections else "(No architecture data indexed)"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": f"{_ARCH_SYSTEM_PROMPT}\n\nArchitecture context:\n{arch_context}"},
    ]
    for turn in (conversation_history or [])[-4:]:
        role = turn.get("role", "")
        content = turn.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": query})

    llm = await asyncio.to_thread(create_query_llm_client)
    model_name = getattr(llm, "model", type(llm).__name__) if llm else "none"

    if llm is None:
        err = "LLM unavailable. Check OPENCODE_QUERY_LLM_PROVIDER / OPENCODE_LLM_PROVIDER."
        for i in range(0, len(err), chunk_size):
            yield {"type": "token", "text": err[i:i + chunk_size]}
    elif hasattr(llm, "stream_chat"):
        async for token in _bridge_stream(llm, messages, max_tokens=1536):
            yield {"type": "token", "text": token}
    else:
        answer = await asyncio.to_thread(llm.chat, messages, max_tokens=1536)
        for i in range(0, max(len(answer), 1), chunk_size):
            chunk = answer[i:i + chunk_size]
            if chunk:
                yield {"type": "token", "text": chunk}
            await asyncio.sleep(0)

    sources = list(dict.fromkeys(
        c["title"] for ln in _ARCH_LAYER_ORDER for c in layers[ln]
    ))[:8]
    yield {
        "type": "done",
        "intent": "architecture",
        "sources": sources,
        "elapsed_ms": round((_time.perf_counter() - t0) * 1000),
        "model": model_name,
    }
