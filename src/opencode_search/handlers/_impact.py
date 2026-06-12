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

    from opencode_search.handlers._graph import _open_graph, handle_detect_impact

    # Get raw blast radius
    impact_data = await handle_detect_impact(
        symbol=symbol,
        project_path=project_path,
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
            "llm_used": False,
        }

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

    # Deterministic risk classification — no LLM.  LLM narrative moved to background.
    risk = "high" if impact_count > 20 else ("medium" if impact_count > 5 else "low")
    domain_list = list(affected_communities)[:10]
    domain_str = ", ".join(domain_list) if domain_list else "unknown domains"
    summary = (
        f"`{symbol}` has {impact_count} callers across {len(affected_communities)} "
        f"domain(s): {domain_str}."
    )

    return {
        "summary": summary,
        "action": "",
        "risk": risk,
        "impact_count": impact_count,
        "affected_domains": domain_list,
        "callers": callers[:20],
        "symbol": symbol,
        "llm_used": False,
    }
