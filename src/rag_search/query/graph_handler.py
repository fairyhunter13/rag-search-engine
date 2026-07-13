"""Graph query relations: definition, callers, callees, impact, impact_narrative."""
from __future__ import annotations

from rag_search.graph.store import GraphStore

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


def run_graph(
    symbol: str,
    project_path: str = "",
    relation: str = "definition",
    to_symbol: str = "",
) -> str:
    """Sync federation fan-out graph query. Shared by MCP + CLI. DB-reads only, no LLM."""
    import json as _json

    from rag_search.core.config import project_graph_db
    from rag_search.core.registry import list_projects
    from rag_search.daemon.federation import expand_federation, federated_map

    if project_path:
        from rag_search.core.registry import resolve_registered_root
        project_path = resolve_registered_root(project_path)
    if not project_path:
        projects = [p for p in list_projects() if p.enabled]
        if not projects:
            return _json.dumps({"error": "No indexed projects found."})
        project_path = projects[0].path

    def _dump(payload: dict) -> str:
        """Every response after resolution discloses which project answered it (no silent fallback)."""
        return _json.dumps({**payload, "resolved_project": project_path})

    if not any(project_graph_db(p).exists() for p in expand_federation(project_path)):
        return _dump({"error": f"Not indexed: {project_path}"})
    _union = {
        "callers": callers, "callees": callees,
        "impact": impact, "definition": definition,
    }
    if relation in _union:
        _fn = _union[relation]
        matches = [
            m
            for _, ms in federated_map(project_path, lambda gs: _fn(symbol, gs))
            for m in ms
        ]
        return _dump({"matches": matches})
    if relation == "impact_narrative":
        affected = [
            m
            for _, ms in federated_map(project_path, lambda gs: impact(symbol, gs))
            for m in ms
        ]
        if not affected:
            return _dump({
                "symbol": symbol, "risk": "low", "affected_count": 0,
                "summary": f"No callers found for '{symbol}' — low blast radius.",
            })
        names = [r["name"] for r in affected[:20]]
        risk = "high" if len(affected) > 10 else "medium" if len(affected) > 3 else "low"
        return _dump({
            "symbol": symbol, "risk": risk, "affected_count": len(affected),
            "affected": names,
            "summary": (
                f"Changing '{symbol}' affects {len(affected)} symbol(s): "
                f"{', '.join(names[:5])}{'...' if len(names) > 5 else ''}."
            ),
        })
    _note = "call paths are per-member; cross-repo paths are not represented"
    if not to_symbol:
        return _dump({"error": f"relation='{relation}' requires to_symbol"})
    for _, path in federated_map(
        project_path, lambda gs: path_between(symbol, to_symbol, gs)
    ):
        if path:
            steps = " → ".join(p["name"] for p in path)
            return _dump({
                "from": symbol, "to": to_symbol, "path": path, "note": _note,
                "summary": f"{symbol} → {to_symbol} via {len(path)} step(s): {steps}",
            })
    return _dump({
        "from": symbol, "to": to_symbol, "path": [], "note": _note,
        "summary": (
            f"No call path found from '{symbol}' to '{to_symbol}'."
            if to_symbol
            else f"relation='{relation}' requires to_symbol"
        ),
    })
