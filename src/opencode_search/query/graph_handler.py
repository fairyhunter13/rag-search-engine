"""Graph query relations: definition, callers, callees, impact, impact_narrative."""
from __future__ import annotations

from opencode_search.graph.store import GraphStore

_SYM_KEYS = ("sid", "name", "qualified_name", "kind", "file", "start_line", "end_line", "language")


def _lookup_sid(store: GraphStore, symbol: str) -> str | None:
    row = store._con.execute(
        "SELECT sid FROM symbols WHERE name=? OR qualified_name=? LIMIT 1",
        (symbol, symbol),
    ).fetchone()
    return row[0] if row else None


def definition(symbol: str, store: GraphStore) -> list[dict]:
    rows = store._con.execute(
        "SELECT sid,name,qualified_name,kind,file,start_line,end_line,language "
        "FROM symbols WHERE name=? OR qualified_name=? LIMIT 10",
        (symbol, symbol),
    ).fetchall()
    return [dict(zip(_SYM_KEYS, r, strict=True)) for r in rows]


def callers(symbol: str, store: GraphStore) -> list[dict]:
    sid = _lookup_sid(store, symbol)
    if sid is None:
        return []
    rows = store._con.execute(
        "SELECT s.name,s.file,s.start_line FROM edges e "
        "JOIN symbols s ON e.caller_sid=s.sid WHERE e.callee_sid=? LIMIT 50",
        (sid,),
    ).fetchall()
    return [{"name": r[0], "file": r[1], "start_line": r[2]} for r in rows]


def callees(symbol: str, store: GraphStore) -> list[dict]:
    sid = _lookup_sid(store, symbol)
    if sid is None:
        return []
    rows = store._con.execute(
        "SELECT s.name,s.file,s.start_line FROM edges e "
        "JOIN symbols s ON e.callee_sid=s.sid WHERE e.caller_sid=? LIMIT 50",
        (sid,),
    ).fetchall()
    return [{"name": r[0], "file": r[1], "start_line": r[2]} for r in rows]


def impact(symbol: str, store: GraphStore, *, max_depth: int = 5) -> list[dict]:
    """BFS over callers to find transitive impact set (what would break)."""
    start = _lookup_sid(store, symbol)
    if start is None:
        return []
    visited: dict[str, int] = {}
    queue = [(start, 0)]
    while queue:
        sid, depth = queue.pop(0)
        if sid in visited or depth > max_depth:
            continue
        visited[sid] = depth
        for (csid,) in store._con.execute(
            "SELECT caller_sid FROM edges WHERE callee_sid=?", (sid,)
        ).fetchall():
            if csid not in visited:
                queue.append((csid, depth + 1))
    impact_sids = [s for s in visited if s != start]
    if not impact_sids:
        return []
    placeholders = ",".join("?" * len(impact_sids))
    rows = store._con.execute(
        f"SELECT name,file,start_line FROM symbols WHERE sid IN ({placeholders})",
        impact_sids,
    ).fetchall()
    return [{"name": r[0], "file": r[1], "start_line": r[2]} for r in rows]


def impact_narrative(symbol: str, store: GraphStore) -> str:
    """LLM blast-radius summary for a symbol."""
    from opencode_search.graph.llm import chat
    affected = impact(symbol, store)
    if not affected:
        return f"No callers found for '{symbol}' — low blast radius."
    names = ", ".join(r["name"] for r in affected[:20])
    return chat(
        f"Summarize blast radius of changing '{symbol}'. "
        f"Affected symbols: {names}. "
        f"Reply in 2-3 sentences with risk level (low/medium/high) and affected domains."
    )
