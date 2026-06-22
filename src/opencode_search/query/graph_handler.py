"""Graph query relations: definition, callers, callees, impact, impact_narrative."""
from __future__ import annotations

from opencode_search.graph.store import GraphStore

_SYM_KEYS = ("sid", "name", "qualified_name", "kind", "file", "start_line", "end_line", "language")


def _lookup_sids(store: GraphStore, symbol: str) -> list[str]:
    rows = store._con.execute(
        "SELECT sid FROM symbols WHERE name=? OR qualified_name=?",
        (symbol, symbol),
    ).fetchall()
    return [r[0] for r in rows]


def _lookup_sid(store: GraphStore, symbol: str) -> str | None:
    sids = _lookup_sids(store, symbol)
    return sids[0] if sids else None


def definition(symbol: str, store: GraphStore) -> list[dict]:
    rows = store._con.execute(
        "SELECT sid,name,qualified_name,kind,file,start_line,end_line,language "
        "FROM symbols WHERE name=? OR qualified_name=? LIMIT 10",
        (symbol, symbol),
    ).fetchall()
    return [dict(zip(_SYM_KEYS, r, strict=True)) for r in rows]


def callers(symbol: str, store: GraphStore) -> list[dict]:
    sids = _lookup_sids(store, symbol)
    if not sids:
        return []
    ph = ",".join("?" * len(sids))
    rows = store._con.execute(
        f"SELECT DISTINCT s.name,s.file,s.start_line FROM edges e "
        f"JOIN symbols s ON e.caller_sid=s.sid WHERE e.callee_sid IN ({ph}) LIMIT 50",
        sids,
    ).fetchall()
    return [{"name": r[0], "file": r[1], "start_line": r[2]} for r in rows]


def callees(symbol: str, store: GraphStore) -> list[dict]:
    sids = _lookup_sids(store, symbol)
    if not sids:
        return []
    ph = ",".join("?" * len(sids))
    rows = store._con.execute(
        f"SELECT DISTINCT s.name,s.file,s.start_line FROM edges e "
        f"JOIN symbols s ON e.callee_sid=s.sid WHERE e.caller_sid IN ({ph}) LIMIT 50",
        sids,
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


def path_between(src: str, tgt: str, store: GraphStore, *, max_depth: int = 5) -> list[dict]:
    """BFS shortest call-path from src to tgt (callee direction)."""
    ss, ts = _lookup_sid(store, src), _lookup_sid(store, tgt)
    if not ss or not ts:
        return []
    prev: dict[str, str | None] = {ss: None}
    depth: dict[str, int] = {ss: 0}
    queue = [ss]
    while queue:
        sid = queue.pop(0)
        if depth[sid] >= max_depth:
            continue
        for (nxt,) in store._con.execute("SELECT callee_sid FROM edges WHERE caller_sid=?", (sid,)).fetchall():
            if nxt in prev:
                continue
            prev[nxt] = sid
            depth[nxt] = depth[sid] + 1
            if nxt == ts:
                cur: str | None = ts
                path: list[str] = []
                while cur:
                    path.append(cur)
                    cur = prev[cur]
                sids = list(reversed(path))
                by_sid = {r[0]: r for r in store._con.execute(
                    f"SELECT sid,name,file FROM symbols WHERE sid IN ({','.join('?'*len(sids))})", sids
                ).fetchall()}
                return [{"name": by_sid[s][1], "file": by_sid[s][2]} for s in sids if s in by_sid]
            queue.append(nxt)
    return []


def semantic_trace(src: str, tgt: str, store: GraphStore) -> str:
    """LLM narrative trace from src symbol to tgt symbol via the call path."""
    from opencode_search.graph.llm import deepseek_chat, deepseek_key
    path = path_between(src, tgt, store)
    if not path:
        return f"No call path found from '{src}' to '{tgt}' within depth 5."
    if not deepseek_key():
        return "Narrative requires DEEPSEEK_API_KEY (no local generative LLM)."
    steps = " → ".join(p["name"] for p in path)
    return deepseek_chat(f"Explain how '{src}' leads to '{tgt}'. Call path: {steps}. Describe each step's purpose.")


def impact_narrative(symbol: str, store: GraphStore) -> str:
    """LLM blast-radius summary for a symbol."""
    from opencode_search.graph.llm import deepseek_chat, deepseek_key
    affected = impact(symbol, store)
    if not affected:
        return f"No callers found for '{symbol}' — low blast radius."
    if not deepseek_key():
        return "Narrative requires DEEPSEEK_API_KEY (no local generative LLM)."
    names = ", ".join(r["name"] for r in affected[:20])
    return deepseek_chat(
        f"Summarize blast radius of changing '{symbol}'. "
        f"Affected symbols: {names}. "
        f"Reply in 2-3 sentences with risk level (low/medium/high) and affected domains."
    )
