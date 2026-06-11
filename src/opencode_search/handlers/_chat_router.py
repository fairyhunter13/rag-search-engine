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
import threading
import time
from typing import Any

from opencode_search.metrics import record_stream_error, record_stream_success

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

CRITICAL DISTINCTIONS:
- "search" means: show me the source code of X, find the file/function/class X, locate implementation of X.
  "search" is NOT for questions about dependencies, impact, or what-would-break.
- "graph_impact" means: what are the downstream consequences of changing a SPECIFIC NAMED symbol/contract/service.
  REQUIRES a concrete named target (e.g. "AuthService.Login", "payment gRPC contract", "the embedding model").
  "graph_impact" is NOT for vague "how are things connected" or "how do function calls relate" questions — those are "feature".
- "graph_callers" means: what upstream code calls/triggers/initiates X.
- "debug" means: a SPECIFIC bug, crash, or error (e.g. "my code fails with X", "this function throws Y").
  "debug" is NOT for GENERAL questions about how to debug, trace, or investigate bugs — those are "feature" or "architecture".

Intents:
  debug_trace   — the query IS a stack trace / traceback / error log with file paths and line numbers
  debug         — SPECIFIC bug/crash/error: "X fails", "why does Y throw Z", "not working" (NO stack trace) — requires a specific error scenario, not general debugging methodology
  search        — find/show/locate specific source code, files, or function implementations ONLY
  graph_callers — what calls X, what triggers X, what initiates X, what invokes X, what fires X, what starts X, which services call X, what causes X to run
  graph_callees — what does X call, callees of X, downstream of X
  graph_impact  — what breaks if I change NAMED-X, what depends on NAMED-X, blast radius of changing NAMED-X — REQUIRES a specific named symbol/contract/service
  architecture  — high-level design patterns, service topology, how is X architected, describe the design of X
  global        — comprehensive/holistic overview of the ENTIRE system, tell me about this project, what does this system do, overview of everything
  feature       — how does X work end-to-end, walk me through X flow, explain X feature, trace request path through X, how are function calls related in Y, how do the services connect

Examples (correct answers):
  Q: "What triggers the fulfillment picking process?" → {"intent": "graph_callers"}
  Q: "What services would break if I change the campaign gRPC contract?" → {"intent": "graph_impact"}
  Q: "what's the blast radius of removing AuthService.Login?" → {"intent": "graph_impact"}
  Q: "what breaks if I change the embedding model?" → {"intent": "graph_impact"}
  Q: "what depends on the user service interface?" → {"intent": "graph_impact"}
  Q: "what is impacted by changing the inventory contract?" → {"intent": "graph_impact"}
  Q: "what would be affected by modifying the payment processor?" → {"intent": "graph_impact"}
  Q: "what invokes the payment processor?" → {"intent": "graph_callers"}
  Q: "what initiates the checkout flow?" → {"intent": "graph_callers"}
  Q: "find the checkout handler" → {"intent": "search"}
  Q: "show me the embedder implementation" → {"intent": "search"}
  Q: "how does the order flow work end-to-end?" → {"intent": "feature"}
  Q: "how are function calls traced and related in the business processes?" → {"intent": "feature"}
  Q: "how do the services connect to each other?" → {"intent": "feature"}
  Q: "how can I debug a bug and find which line of code to fix?" → {"intent": "feature"}
  Q: "what is the debugging process in this codebase?" → {"intent": "feature"}
  Q: "describe the overall architecture" → {"intent": "architecture"}
  Q: "tell me about this project comprehensively" → {"intent": "global"}

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
        # Strict-only: if JSON parse didn't yield a valid intent, raise so caller emits error
        raise ValueError(f"LLM returned unparseable intent: {text[:120]!r}")
    except ValueError:
        raise
    except Exception as e:
        log.debug("LLM intent classification failed: %s", e)

    return "search"


_EXTRACT_SYMBOL_SYSTEM = (
    "Extract the code symbol (function name, class name, variable, or method) "
    "that the user wants to look up in the call graph. "
    "Return ONLY the symbol name as a single word/identifier, nothing else. "
    "Examples:\n"
    "  'what calls handle_pipeline?' → handle_pipeline\n"
    "  'who calls ProcessOrder' → ProcessOrder\n"
    "  'impact of changing UserService' → UserService\n"
    "  'callees of db.connect' → db.connect\n"
    "If no clear symbol, return the most likely identifier from the query."
)


async def _extract_symbol_llm(query: str) -> str:
    """Extract the target symbol from a graph query using the LLM."""
    import asyncio

    from opencode_search.enricher import create_query_llm_client

    try:
        llm = await asyncio.to_thread(create_query_llm_client)
        if llm is None:
            return query.split()[-1] if query.split() else query
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _EXTRACT_SYMBOL_SYSTEM},
            {"role": "user", "content": query[:300]},
        ]
        raw = await asyncio.to_thread(llm.chat, messages, max_tokens=24)
        text = raw if isinstance(raw, str) else (raw.get("content", "") if isinstance(raw, dict) else str(raw))
        # Clean up: strip whitespace, punctuation, quotes
        symbol = text.strip().strip("'\"` \t\n").split()[0] if text.strip() else ""
        if symbol:
            return symbol
    except Exception as e:
        log.debug("LLM symbol extraction failed: %s", e)
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

    # handle_detect_impact returns {callers_by_depth, total_affected, symbol}
    if result.get("error"):
        return f"No impact data found for the query: **{query}**."

    callers_by_depth: dict = result.get("callers_by_depth", {})
    total = result.get("total_affected", 0)
    symbol = result.get("symbol", query)

    if callers_by_depth and total > 0:
        lines = [f"**Impact analysis for `{symbol}`:** {total} caller(s) affected.\n"]
        for depth_key in sorted(callers_by_depth.keys(), key=int):
            callers = callers_by_depth[depth_key]
            depth = int(depth_key)
            label = "Direct callers" if depth == 1 else f"Depth-{depth} callers"
            lines.append(f"{label} ({len(callers)}):")
            for c in callers[:6]:
                name = c.get("qualified_name") or c.get("name", "unknown")
                file = c.get("file", "")
                lines.append(f"- `{name}`" + (f" in `{file}`" if file else ""))
            if len(callers) > 6:
                lines.append(f"  … and {len(callers) - 6} more")
        return "\n".join(lines)

    # Legacy format: changed_symbols / affected_communities
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

_ARCH_SYSTEM_PROMPT = (
    "You are a senior software architect. "
    "Write a comprehensive end-to-end architecture narrative. "
    "Organise the code communities into meaningful architectural layers or domains based on "
    "their semantic type and purpose. For each layer or domain, name the key files and functions. "
    "Close with a concrete example: one request traced through all layers. "
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
    from opencode_search.handlers._kb_chat import (
        _fetch_code_context,
        _fetch_community_context,
        _fetch_wiki_context,
    )

    t0 = time.perf_counter()

    async def _get_patterns() -> dict:
        try:
            from opencode_search.handlers._graph import handle_detect_patterns
            return await handle_detect_patterns(project_path=project_path)
        except Exception:
            return {}

    (_, comm_list, comm_count), (code_ctx, _code_sources, _), patterns, (wiki_ctx, _) = \
        await asyncio.gather(
            _fetch_community_context(query, project_path, top_k=25, include_federation=False),
            _fetch_code_context(query, project_path, top_k=12),
            _get_patterns(),
            _fetch_wiki_context(query, project_path, top_k=5),
        )

    ctx_sections: list[str] = []

    if comm_list:
        comm_lines = "\n".join(
            f"  [{c.get('semantic_type', 'utility')}] {c['title']}: {(c.get('summary') or '')[:200]}"
            for c in comm_list[:30]
        )
        ctx_sections.append(f"**Code communities ({len(comm_list)} indexed):**\n{comm_lines}")

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

    if wiki_ctx:
        ctx_sections.append(f"**Wiki knowledge:**\n{wiki_ctx}")

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
            for c in comm_list[:12]:
                st = c.get("semantic_type", "")
                prefix = f"[{st}] " if st else ""
                lines.append(f"- {prefix}{c['title']}: {(c.get('summary') or '')[:100]}")
            answer = "\n".join(lines)

    sources = list(dict.fromkeys(c["title"] for c in comm_list))[:8]

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
        symbol = await _extract_symbol_llm(query)
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
            from opencode_search.handlers._graph import handle_detect_impact
            result = await handle_detect_impact(symbol=symbol, project_path=project_path)
            answer = _prose_impact(result, query)
            sources = result.get("affected_files", [])
            # Fallback to feature handler when no impact data was found
            if "No impact data found" in answer:
                import asyncio

                from opencode_search.handlers._feature import handle_ask_feature
                from opencode_search.handlers._kb_chat import handle_kb_chat
                feat, kb = await asyncio.gather(
                    handle_ask_feature(query=query, project_path=project_path, top_k=12),
                    handle_kb_chat(query=query, project_path=project_path, mode="quick", top_k=20),
                    return_exceptions=True,
                )
                if isinstance(kb, Exception):
                    kb = {}
                if isinstance(feat, Exception):
                    feat = {}
                kb_answer = kb.get("answer", "") if isinstance(kb, dict) else ""
                feat_prose = _prose_feature(feat, query) if isinstance(feat, dict) and feat.get("status") == "ok" else ""
                if kb_answer and feat_prose and len(feat_prose) > 100:
                    answer = f"{kb_answer}\n\n---\n\n**Detailed trace:**\n\n{feat_prose}"
                elif kb_answer:
                    answer = kb_answer
                elif feat_prose:
                    answer = feat_prose
                sources = list(set(kb.get("sources", []) + [ep.get("file", "") for ep in feat.get("entry_points", []) if isinstance(feat, dict)]))

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

    # Parallelize: classify intent AND prefetch code search simultaneously.
    # Code prefetch completes in ~0.3s (vector lookup) while LLM classification
    # takes ~1-2s — both finish before the first token is ready regardless.
    if mode not in ("auto", "quick", "comprehensive"):
        intent = mode
        code_prefetch = None
    else:
        from opencode_search.handlers._query import handle_search_code as _sc
        intent_task = asyncio.ensure_future(classify_intent_llm(query))
        code_prefetch_task = asyncio.ensure_future(
            _sc(query=query, project_paths=[project_path], top_k=10)
        )
        intent = await intent_task
        code_prefetch = await code_prefetch_task

    # ── search: no LLM — yield prose immediately ──────────────────────────────
    if intent == "search":
        from opencode_search.handlers._query import handle_search_code
        result = code_prefetch if code_prefetch is not None else await handle_search_code(query=query, project_paths=[project_path], top_k=10)
        answer = _prose_search(result, query)
        sources = [r.get("path", "") for r in result.get("results", []) if r.get("path")]
        record_stream_success()
        for i in range(0, max(len(answer), 1), chunk_size):
            chunk = answer[i:i + chunk_size]
            if chunk:
                yield {"type": "token", "text": chunk}
            await asyncio.sleep(0)
        yield {"type": "done", "intent": "search", "sources": sources[:10],
               "elapsed_ms": round((_time.perf_counter() - t0) * 1000), "model": "vector-index"}
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

    # ── global: MAP heartbeats → stream REDUCE synthesis ─────────────────────
    if intent == "global":
        async for event in _stream_global(
            query=query, project_path=project_path,
            conversation_history=conversation_history, t0=t0,
        ):
            yield event
        return

    # ── debug / debug_trace: streaming root-cause analysis ───────────────────
    if intent in ("debug", "debug_trace"):
        async for event in _stream_debug(
            query=query, project_path=project_path,
            is_trace=(intent == "debug_trace"), t0=t0,
        ):
            yield event
        return

    # ── graph: heartbeat approach (fast lookups, streaming not needed) ────────
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
    record_stream_success()
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

    # Step 2: context assembly — heartbeat every 10s under GPU load
    _ctx_task2 = asyncio.ensure_future(asyncio.gather(code_task, community_task, wiki_task))
    while not _ctx_task2.done():
        try:
            await asyncio.wait_for(asyncio.shield(_ctx_task2), timeout=10.0)
        except TimeoutError:
            yield {"type": "thinking"}
    (code_ctx, code_sources, _), (comm_ctx, _, _), (wiki_ctx, _) = await _ctx_task2

    # Step 3: build messages
    sections: list[str] = []
    if code_ctx:
        sections.append(f"[CODE LOCATIONS]\n{code_ctx}")
    if comm_ctx:
        sections.append(f"[ARCHITECTURE COMMUNITIES]\n{comm_ctx}")
    if wiki_ctx:
        sections.append(f"[WIKI KNOWLEDGE]\n{wiki_ctx}")

    if not sections:
        yield {
            "type": "error",
            "code": "no_content",
            "message": "No indexed content found. Run build(action='pipeline') to index the project first.",
            "intent": "feature",
        }
        yield {
            "type": "done", "intent": "feature", "sources": [],
            "elapsed_ms": round((_time.perf_counter() - t0) * 1000), "model": "",
        }
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
        record_stream_error("feature")
        yield {
            "type": "error", "code": "llm_unavailable",
            "message": "LLM unavailable. Check OPENCODE_QUERY_LLM_PROVIDER / OPENCODE_LLM_PROVIDER.",
            "intent": "feature",
        }
        yield {
            "type": "done", "intent": "feature", "sources": [],
            "elapsed_ms": round((_time.perf_counter() - t0) * 1000), "model": "none",
        }
        return
    elif hasattr(llm, "stream_chat"):
        # Native streaming — tokens arrive in real time
        try:
            async for token in _bridge_stream(llm, messages, max_tokens=1024):
                yield {"type": "token", "text": token}
            record_stream_success()
        except Exception as _se:
            log.warning("_stream_feature: stream_chat failed: %s", _se)
            record_stream_error("feature")
            yield {"type": "error", "code": "stream_failed", "message": str(_se), "intent": "feature"}
    else:
        # Fallback: blocking call + chunk
        try:
            answer = await asyncio.to_thread(llm.chat, messages, max_tokens=1024)
            record_stream_success()
            for i in range(0, max(len(answer), 1), chunk_size):
                chunk = answer[i:i + chunk_size]
                if chunk:
                    yield {"type": "token", "text": chunk}
                await asyncio.sleep(0)
        except Exception as _ce:
            log.warning("_stream_feature: chat failed: %s", _ce)
            record_stream_error("feature")
            yield {"type": "error", "code": "chat_failed", "message": str(_ce), "intent": "feature"}

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


async def _stream_global(
    query: str,
    project_path: str,
    conversation_history: list[dict] | None,
    t0: float,
    chunk_size: int = 40,
):
    """Stream global/comprehensive intent: MAP heartbeats then stream REDUCE synthesis."""
    import asyncio
    import time as _time

    from opencode_search.config import load_registry as _load_registry
    from opencode_search.enricher import create_query_llm_client
    from opencode_search.handlers._kb_chat import (
        _MAP_BATCH_SIZE,
        _fetch_code_context,
        _fetch_community_context,
        _fetch_hierarchy_communities,
        _fetch_wiki_context,
    )

    # Detect federation root — use cross-repo synthesis when sub-repos are indexed
    _reg = await asyncio.to_thread(_load_registry)
    _entry = _reg.get(project_path)
    _is_fed = bool(_entry and _entry.federation)

    # For federation roots, expand code search across all indexed members
    _fed_members = [m for m in (_entry.federation if _entry else [])
                    if _reg.get(m) and _reg[m].indexed_at is not None]

    # Parallel context assembly: vector similarity + structural hierarchy + wiki
    # Heartbeat every 10s — top_k=60 + federation can take 10-20s under load
    _ctx_task = asyncio.ensure_future(asyncio.gather(
        _fetch_code_context(query, project_path, top_k=15, extra_paths=_fed_members or None),
        _fetch_community_context(query, project_path, top_k=60, include_federation=_is_fed),
        _fetch_wiki_context(query, project_path, top_k=5),
        _fetch_hierarchy_communities(project_path, max_count=30),
    ))
    while not _ctx_task.done():
        try:
            await asyncio.wait_for(asyncio.shield(_ctx_task), timeout=10.0)
        except TimeoutError:
            yield {"type": "thinking"}
    (_, code_sources, _), (_, comm_list, _), (wiki_ctx, _), hier_comms = await _ctx_task

    # Merge hierarchy communities (structural breadth) with vector-similarity communities
    # Deduplicate by title to avoid repeating the same community in MAP batches
    seen_titles: set[str] = {c["title"] for c in comm_list}
    for hc in hier_comms:
        if hc["title"] and hc["title"] not in seen_titles:
            comm_list.append(hc)
            seen_titles.add(hc["title"])

    # Bound the MAP fan-out. Each MAP batch is one 8B-model LLM call; a large
    # federation root can assemble ~90 communities → ~11 serial-ish calls → a
    # multi-minute synthesis that heat-soaks the GPU mid-run and throttles every
    # subsequent call (violating the global SLO). Cap to the most-relevant head —
    # vector-similarity communities lead the list, so the cap keeps the strongest
    # query matches plus leading hierarchy breadth.
    import os as _os
    _map_cap = int(_os.environ.get("OPENCODE_GLOBAL_MAP_MAX_COMMUNITIES", "40"))
    if len(comm_list) > _map_cap:
        comm_list = comm_list[:_map_cap]

    llm = await asyncio.to_thread(create_query_llm_client)
    model_name = getattr(llm, "model", type(llm).__name__) if llm else "none"

    if llm is None:
        yield {
            "type": "error",
            "code": "llm_unavailable",
            "message": "LLM unavailable. Check OPENCODE_QUERY_LLM_PROVIDER.",
            "intent": "global",
        }
        yield {
            "type": "done", "intent": "global", "sources": [],
            "elapsed_ms": round((_time.perf_counter() - t0) * 1000), "model": "none",
        }
        return

    # MAP phase — run all batches in parallel (semaphore=2), emit heartbeats
    batches: list[list[str]] = []
    for i in range(0, len(comm_list), _MAP_BATCH_SIZE):
        batch = comm_list[i:i + _MAP_BATCH_SIZE]
        batches.append([
            f"[{c['semantic_type']}] {c['title']}: {c['summary']}"
            for c in batch
        ])

    sem = asyncio.Semaphore(2)

    async def _map_one(summaries: list[str]) -> str:
        async with sem:
            return await asyncio.to_thread(llm.map_query, query, summaries)

    map_tasks = [asyncio.ensure_future(_map_one(b)) for b in batches]
    partial: list[str] = []
    pending = set(map_tasks)
    while pending:
        done, pending = await asyncio.wait(pending, timeout=10.0)
        for t in done:
            try:
                result = t.result()
                if isinstance(result, str) and result.strip():
                    partial.append(result)
            except Exception:
                pass
        if pending:
            yield {"type": "thinking"}

    if not partial:
        fallback = "No community data found. Run build(action='pipeline') to index the project."
        for i in range(0, len(fallback), chunk_size):
            yield {"type": "token", "text": fallback[i:i + chunk_size]}
        yield {
            "type": "done", "intent": "global",
            "sources": code_sources[:10],
            "elapsed_ms": round((_time.perf_counter() - t0) * 1000),
            "model": model_name,
        }
        return

    # REDUCE phase — stream synthesis tokens if supported
    from opencode_search.handlers._kb_chat import _SYSTEM_PROMPT

    reduce_system = (
        f"{_SYSTEM_PROMPT}\n\n"
        "You are synthesizing partial findings from a large codebase into a comprehensive answer. "
        "Be specific, reference component names, list features exhaustively, avoid repetition."
    )
    reduce_context = "\n\n".join(f"Finding {i+1}:\n{p}" for i, p in enumerate(partial[:20]))
    if wiki_ctx:
        reduce_context += f"\n\n[WIKI KNOWLEDGE]\n{wiki_ctx}"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": f"{reduce_system}\n\nPartial findings:\n{reduce_context}"},
    ]
    for turn in (conversation_history or [])[-4:]:
        role = turn.get("role", "")
        content = turn.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": query})

    if hasattr(llm, "stream_chat"):
        try:
            async for token in _bridge_stream(llm, messages, max_tokens=2048):
                yield {"type": "token", "text": token}
            record_stream_success()
        except Exception as _se:
            log.warning("_stream_global: stream_chat failed: %s", _se)
            record_stream_error("global")
            yield {"type": "error", "code": "stream_failed", "message": str(_se), "intent": "global"}
    else:
        try:
            answer = await asyncio.to_thread(llm.chat, messages, max_tokens=2048)
            record_stream_success()
            for i in range(0, max(len(answer), 1), chunk_size):
                chunk = answer[i:i + chunk_size]
                if chunk:
                    yield {"type": "token", "text": chunk}
                await asyncio.sleep(0)
        except Exception as _ce:
            log.warning("_stream_global: chat failed: %s", _ce)
            record_stream_error("global")
            yield {"type": "error", "code": "chat_failed", "message": str(_ce), "intent": "global"}

    yield {
        "type": "done",
        "intent": "global",
        "sources": code_sources[:10],
        "elapsed_ms": round((_time.perf_counter() - t0) * 1000),
        "model": model_name,
    }


async def _stream_debug(
    query: str,
    project_path: str,
    is_trace: bool,
    t0: float,
    chunk_size: int = 40,
):
    """Stream debug and debug_trace intent responses.

    debug_trace: run handle_debug_trace in background, heartbeat while waiting,
                 then stream the formatted prose result.
    debug: assemble context in parallel (heartbeat if slow), then stream LLM synthesis.
    """
    import asyncio
    import time as _time

    from opencode_search.enricher import create_query_llm_client

    if is_trace:
        # ── debug_trace: parse stack trace + localize ──────────────────────────
        from opencode_search.handlers._debug_trace import handle_debug_trace
        task = asyncio.ensure_future(
            handle_debug_trace(traceback=query, project_path=project_path, include_fix=True)
        )
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
            except TimeoutError:
                yield {"type": "thinking"}
        try:
            result = await task
        except Exception as exc:
            result = {"root_cause": f"Debug trace failed: {exc}", "hotspot_files": []}
        answer = _prose_debug(result)
        record_stream_success()
        for i in range(0, max(len(answer), 1), chunk_size):
            chunk = answer[i:i + chunk_size]
            if chunk:
                yield {"type": "token", "text": chunk}
            await asyncio.sleep(0)
        yield {
            "type": "done",
            "intent": "debug_trace",
            "sources": result.get("hotspot_files", [])[:10],
            "elapsed_ms": round((_time.perf_counter() - t0) * 1000),
            "model": "debug_trace",
        }
        return

    # ── debug: NL root-cause investigation with LLM streaming ─────────────────
    from opencode_search.handlers._kb_chat import _fetch_community_context
    from opencode_search.handlers._query import handle_search_code

    async def _bounded_feature_trace() -> dict:
        try:
            return await asyncio.wait_for(_run_feature_trace(query, project_path), timeout=40.0)
        except TimeoutError:
            return {}

    # Parallel context assembly — feature trace bounded at 40s to keep total latency reasonable
    feature_task = asyncio.ensure_future(_bounded_feature_trace())
    code_task = asyncio.ensure_future(
        handle_search_code(query=query, project_paths=[project_path], top_k=8)
    )
    community_task = asyncio.ensure_future(
        _fetch_community_context(query, project_path, top_k=8, include_federation=False)
    )
    pending = {feature_task, code_task, community_task}
    while pending:
        _, pending = await asyncio.wait(pending, timeout=10.0)
        if pending:
            yield {"type": "thinking"}

    feature_result = feature_task.result() if not feature_task.exception() else {}
    code_result = code_task.result() if not code_task.exception() else {}
    _, comm_list, _ = community_task.result() if not community_task.exception() else ("", [], 0)

    algo = feature_result.get("algorithm", "") if isinstance(feature_result, dict) else ""
    eps = feature_result.get("entry_points", []) if isinstance(feature_result, dict) else []
    rationale = feature_result.get("design_rationale", "") if isinstance(feature_result, dict) else ""
    call_chain = feature_result.get("call_chain", []) if isinstance(feature_result, dict) else []
    code_results = code_result.get("results", []) if isinstance(code_result, dict) else []

    ctx_lines: list[str] = []
    if comm_list:
        ctx_lines.append("[BUSINESS PROCESS CONTEXT]")
        for c in comm_list[:5]:
            st = c.get("semantic_type", "utility")
            ctx_lines.append(f'  Community: "{c["title"]}" (semantic_type: {st})')
            ctx_lines.append(f'  Summary: {c["summary"][:200]}')
    if algo:
        ctx_lines.append("\n[ALGORITHM STEPS]")
        ctx_lines.append(algo[:600])
    if rationale:
        ctx_lines.append("\n[DESIGN RATIONALE]")
        ctx_lines.append(rationale[:400])
    if eps:
        ctx_lines.append("\n[ENTRY POINTS]")
        for ep in eps[:4]:
            ctx_lines.append(f"  {ep.get('symbol', '')} → {ep.get('file', '')}")
    if call_chain:
        ctx_lines.append("\n[CALL CHAIN]")
        for step in call_chain[:6]:
            name = step.get("name") or step.get("qualified_name", "")
            f = step.get("file", "")
            d = step.get("depth", "")
            ctx_lines.append(f"  depth={d}: {name} ({f})")
    ctx_lines.append("\n[CODE SAMPLES]")
    for r in code_results[:5]:
        snippet = (r.get("content") or "")[:300].replace("\n", " ")
        ctx_lines.append(f"  {r.get('path', '')}: {snippet}")

    context = "\n".join(ctx_lines)
    debug_system = (
        "You are a senior software engineer performing root cause analysis. "
        "Using the provided context, identify and state FOUR things with these exact headings:\n\n"
        "**BUSINESS PROCESS:** Name the community/domain where the bug originates\n"
        "**ALGORITHM STEP:** Which step of the algorithm/workflow where the bug manifests\n"
        "**FILE & LINE RANGE:** Exact function name and file location\n"
        "**ROOT CAUSE:** Why this specific location causes the observed symptom — "
        "include race conditions, edge cases, or logic errors\n\n"
        "Be specific. Reference exact files and functions."
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": debug_system},
        {"role": "user", "content": f"Analyse for root cause:\n\n{context}\n\nQuestion: {query}"},
    ]

    llm = await asyncio.to_thread(create_query_llm_client)
    model_name = getattr(llm, "model", type(llm).__name__) if llm else "none"

    if llm is None:
        record_stream_error("debug")
        yield {
            "type": "error", "code": "llm_unavailable",
            "message": "LLM unavailable. Check OPENCODE_QUERY_LLM_PROVIDER.",
            "intent": "debug",
        }
        yield {
            "type": "done", "intent": "debug", "sources": [],
            "elapsed_ms": round((_time.perf_counter() - t0) * 1000), "model": "none",
        }
        return
    elif hasattr(llm, "stream_chat"):
        try:
            async for token in _bridge_stream(llm, messages, max_tokens=1024):
                yield {"type": "token", "text": token}
            record_stream_success()
        except Exception as _se:
            log.warning("_stream_debug: stream_chat failed: %s", _se)
            record_stream_error("debug")
            yield {"type": "error", "code": "stream_failed", "message": str(_se), "intent": "debug"}
    else:
        try:
            answer = await asyncio.to_thread(llm.chat, messages, max_tokens=1024)
            record_stream_success()
            for i in range(0, max(len(answer), 1), chunk_size):
                chunk = answer[i:i + chunk_size]
                if chunk:
                    yield {"type": "token", "text": chunk}
                await asyncio.sleep(0)
        except Exception as _ce:
            log.warning("_stream_debug: chat failed: %s", _ce)
            record_stream_error("debug")
            yield {"type": "error", "code": "chat_failed", "message": str(_ce), "intent": "debug"}

    hotspot_files = [ep.get("file", "") for ep in eps[:4] if ep.get("file")]
    yield {
        "type": "done",
        "intent": "debug",
        "sources": hotspot_files[:10],
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
    from opencode_search.handlers._kb_chat import (
        _fetch_code_context,
        _fetch_community_context,
        _fetch_wiki_context,
    )

    async def _get_patterns() -> dict:
        try:
            from opencode_search.handlers._graph import handle_detect_patterns
            return await handle_detect_patterns(project_path=project_path)
        except Exception:
            return {}

    # Parallel context assembly — heartbeat every 10s so the UI shows elapsed time
    _gather_task = asyncio.ensure_future(asyncio.gather(
        _fetch_community_context(query, project_path, top_k=25, include_federation=False),
        _fetch_code_context(query, project_path, top_k=12),
        _get_patterns(),
        _fetch_wiki_context(query, project_path, top_k=5),
    ))
    while not _gather_task.done():
        try:
            await asyncio.wait_for(asyncio.shield(_gather_task), timeout=10.0)
        except TimeoutError:
            yield {"type": "thinking"}
    (_, comm_list, _comm_count), (code_ctx, _, _), patterns, (wiki_ctx, _) = await _gather_task

    ctx_sections: list[str] = []

    if comm_list:
        comm_lines = "\n".join(
            f"  [{c.get('semantic_type', 'utility')}] {c['title']}: {(c.get('summary') or '')[:200]}"
            for c in comm_list[:30]
        )
        ctx_sections.append(f"**Code communities ({len(comm_list)} indexed):**\n{comm_lines}")

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

    if wiki_ctx:
        ctx_sections.append(f"**Wiki knowledge:**\n{wiki_ctx}")

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
        record_stream_error("architecture")
        yield {
            "type": "error", "code": "llm_unavailable",
            "message": "LLM unavailable. Check OPENCODE_QUERY_LLM_PROVIDER / OPENCODE_LLM_PROVIDER.",
            "intent": "architecture",
        }
        yield {
            "type": "done", "intent": "architecture", "sources": [],
            "elapsed_ms": round((_time.perf_counter() - t0) * 1000), "model": "none",
        }
        return
    elif hasattr(llm, "stream_chat"):
        try:
            async for token in _bridge_stream(llm, messages, max_tokens=1536):
                yield {"type": "token", "text": token}
            record_stream_success()
        except Exception as _se:
            log.warning("_stream_architecture: stream_chat failed: %s", _se)
            record_stream_error("architecture")
            yield {"type": "error", "code": "stream_failed", "message": str(_se), "intent": "architecture"}
    else:
        try:
            answer = await asyncio.to_thread(llm.chat, messages, max_tokens=1536)
            record_stream_success()
            for i in range(0, max(len(answer), 1), chunk_size):
                chunk = answer[i:i + chunk_size]
                if chunk:
                    yield {"type": "token", "text": chunk}
                await asyncio.sleep(0)
        except Exception as _ce:
            log.warning("_stream_architecture: chat failed: %s", _ce)
            record_stream_error("architecture")
            yield {"type": "error", "code": "chat_failed", "message": str(_ce), "intent": "architecture"}

    sources = list(dict.fromkeys(c["title"] for c in comm_list))[:8]
    yield {
        "type": "done",
        "intent": "architecture",
        "sources": sources,
        "elapsed_ms": round((_time.perf_counter() - t0) * 1000),
        "model": model_name,
    }
