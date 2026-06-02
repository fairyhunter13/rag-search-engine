"""Natural language impact analysis — wraps existing blast-radius traversal with LLM narrative.

graph(symbol, relation="impact_narrative") routes here.
Unlike graph(relation="impact") which returns a raw list of callers, this generates
a plain-English summary: risk level, affected domains, what to test.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


async def handle_impact_narrative(
    symbol: str,
    project_path: str,
    depth: int = 3,
    include_federation: bool = True,
) -> dict[str, Any]:
    """Generate a natural-language impact analysis for changing a symbol.

    1. Runs the existing blast-radius traversal (handle_detect_impact)
    2. Groups impacted symbols by community (architecture domain)
    3. Calls LLM to synthesize a human-readable impact narrative

    Returns:
        summary: Plain-English impact description
        risk: "low" | "medium" | "high"
        impact_count: Number of directly/transitively impacted callers
        affected_domains: List of community titles impacted
        callers: Raw caller list (first 20)
    """
    import asyncio

    from opencode_search.enricher import create_llm_client
    from opencode_search.handlers._graph import _open_graph, handle_detect_impact

    # Get raw blast radius
    impact_data = await handle_detect_impact(
        symbol=symbol,
        project_path=project_path,
        include_federation=include_federation,
    )

    callers = impact_data.get("callers", [])
    impact_count = len(callers)

    if impact_count == 0:
        return {
            "summary": f"`{symbol}` has no detected callers. Safe to change without blast radius.",
            "risk": "low",
            "impact_count": 0,
            "affected_domains": [],
            "callers": [],
            "symbol": symbol,
        }

    # Group by community to identify affected architecture domains
    caller_names = [c.get("qualified_name") or c.get("name") or "unknown" for c in callers[:50]]

    # Find community memberships for impacted symbols
    affected_communities: set[str] = set()
    def _get_domains(path: str) -> list[str]:
        gs = _open_graph(path)
        if gs is None:
            return []
        import contextlib
        try:
            comms = {c.id: c for c in gs.get_communities(order_by_size=True)}
            domains = []
            for caller in callers[:50]:
                node = gs.get_node(caller.get("qualified_name") or caller.get("name") or "")
                if node and node.community_id and node.community_id in comms:
                    title = comms[node.community_id].title
                    if title:
                        domains.append(title)
            return list(set(domains))
        except Exception:
            return []
        finally:
            with contextlib.suppress(Exception):
                gs.close()

    affected_domains = await asyncio.to_thread(_get_domains, project_path)
    affected_communities.update(affected_domains)

    # LLM narrative
    try:
        llm = await asyncio.to_thread(create_llm_client)
        parsed = await asyncio.to_thread(
            llm.impact_narrative,
            symbol,
            caller_names,
            list(affected_communities),
            impact_count,
        )
        # Parse RISK/SUMMARY/ACTION from the response
        risk = "medium"
        summary_lines = []
        action = ""
        for line in parsed.splitlines():
            if line.startswith("RISK:"):
                risk = line[5:].strip().lower()
            elif line.startswith("SUMMARY:"):
                summary_lines.append(line[8:].strip())
            elif line.startswith("ACTION:"):
                action = line[7:].strip()
        summary = " ".join(summary_lines) or parsed
    except Exception as exc:
        log.debug("impact_narrative: LLM failed: %s", exc)
        summary = f"`{symbol}` has {impact_count} callers across {len(affected_communities)} domains."
        risk = "medium" if impact_count > 10 else "low"
        action = ""

    return {
        "summary": summary,
        "action": action,
        "risk": risk,
        "impact_count": impact_count,
        "affected_domains": list(affected_communities)[:10],
        "callers": callers[:20],
        "symbol": symbol,
    }
