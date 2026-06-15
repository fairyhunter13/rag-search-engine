"""overview() tool implementation — isolated to keep mcp.py within 150 lines."""
from __future__ import annotations

import json
import re
from pathlib import Path


def _find_import_cycles(conn) -> list[list[str]]:  # type: ignore[no-untyped-def]
    """Tarjan SCC on the file-level call graph; returns SCCs of size ≥ 2."""
    rows = conn.execute(
        "SELECT DISTINCT s1.file,s2.file FROM edges e "
        "JOIN symbols s1 ON e.caller_sid=s1.sid "
        "JOIN symbols s2 ON e.callee_sid=s2.sid "
        "WHERE s1.file!=s2.file AND s1.file IS NOT NULL AND s2.file IS NOT NULL LIMIT 20000"
    ).fetchall()
    adj: dict[str, list[str]] = {}
    for a, b in rows:
        adj.setdefault(a, []).append(b)
    idx: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stk: set[str] = set()
    stk: list[str] = []
    cnt = [0]
    cycles: list[list[str]] = []

    def sc(v: str) -> None:
        idx[v] = low[v] = cnt[0]
        cnt[0] += 1
        stk.append(v)
        on_stk.add(v)
        for w in adj.get(v, []):
            if w not in idx:
                sc(w)
                low[v] = min(low[v], low[w])
            elif w in on_stk:
                low[v] = min(low[v], idx[w])
        if low[v] == idx[v]:
            scc: list[str] = []
            while True:
                w = stk.pop()
                on_stk.discard(w)
                scc.append(w)
                if w == v:
                    break
            if len(scc) >= 2:
                cycles.append(scc[:5])

    try:
        for v in list(adj):
            if v not in idx:
                sc(v)
    except RecursionError:
        pass
    return cycles[:20]


def _detect_services(root: str) -> list[dict]:
    """Detect gRPC services via .proto `service` blocks and Go Register*Server calls."""
    rp = Path(root)
    names: set[str] = set()
    for f in rp.rglob("*.proto"):
        try:
            for m in re.finditer(r"^service\s+(\w+)\s*\{", f.read_text(), re.MULTILINE):
                names.add(m.group(1))
        except OSError:
            pass
    for f in rp.rglob("*.go"):
        try:
            for m in re.finditer(r"\bRegister(\w+)Server\b", f.read_text()):
                names.add(m.group(1))
        except OSError:
            pass
    if not names:
        return []
    return [{"name": rp.name, "path": root, "services": sorted(names)}]


_VALID = {
    "structure", "projects", "metrics", "patterns", "communities",
    "architecture_domains", "hierarchy", "status", "import_cycles",
    "surprising_connections", "feature_map", "business_rules",
    "process_flows", "suggested_questions", "service_mesh",
}


def handle_overview(project_path: str, what: str) -> str:
    from opencode_search.core.registry import list_projects

    if what not in _VALID:
        return json.dumps({"error": f"unknown what={what!r}", "valid": sorted(_VALID)})
    if what == "projects":
        return json.dumps({"projects": [
            {"path": p.path, "enabled": p.enabled, "indexed_at": p.indexed_at}
            for p in list_projects()
        ]})
    if what == "metrics":
        from opencode_search.server.routes_ops import _metrics
        return json.dumps(_metrics)
    if not project_path:
        ps = [p for p in list_projects() if p.enabled]
        project_path = ps[0].path if ps else ""
    if what == "patterns" and project_path:
        from pathlib import Path

        from opencode_search.kb.patterns import detect_patterns
        return json.dumps(detect_patterns(Path(project_path)))
    if project_path:
        from opencode_search.core.config import project_graph_db
        from opencode_search.graph.store import GraphStore

        gdb = project_graph_db(project_path)
        if gdb.exists():
            gs = GraphStore(gdb)
            try:
                c = gs.conn
                if what == "communities":
                    rows = c.execute("SELECT id,title,level FROM communities ORDER BY level,id LIMIT 50").fetchall()
                    return json.dumps({"communities": [{"id": r[0], "title": r[1], "level": r[2]} for r in rows]})
                if what in ("architecture_domains", "hierarchy"):
                    f = "WHERE level>=2" if what == "architecture_domains" else ""
                    rows = c.execute(f"SELECT id,title,level FROM communities {f} ORDER BY level,id LIMIT 200").fetchall()
                    return json.dumps({what: [{"id": r[0], "title": r[1], "level": r[2]} for r in rows]})
                if what == "status":
                    from opencode_search.core.config import project_vector_db
                    from opencode_search.core.registry import get_project
                    e = get_project(project_path)
                    total = gs.community_count()
                    summarized = c.execute(
                        "SELECT COUNT(*) FROM communities WHERE summary IS NOT NULL AND summary != ''"
                    ).fetchone()[0]
                    pct = round(summarized / total * 100, 1) if total else 0.0
                    kb_state = ("indexing" if not project_vector_db(project_path).exists() else
                                "ready" if pct >= 95 else
                                "enriching" if pct > 0 else "searchable")
                    return json.dumps({"path": project_path, "indexed_at": e.indexed_at if e else None,
                                       "file_count": e.file_count if e else 0,
                                       "symbols": gs.symbol_count(), "communities": total,
                                       "kb_state": kb_state, "enriched_pct": pct})
                if what == "import_cycles":
                    cycs = _find_import_cycles(c)
                    cnt = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
                    return json.dumps({"cycles": cycs, "cycle_count": len(cycs),
                                       "has_cycles": bool(cycs), "edge_count": cnt})
                if what == "surprising_connections":
                    try:
                        rows = c.execute(
                            "SELECT s.name,t.name FROM edges e "
                            "JOIN symbols s ON e.caller_sid=s.sid JOIN symbols t ON e.callee_sid=t.sid "
                            "WHERE s.community_id != t.community_id LIMIT 20"
                        ).fetchall()
                    except Exception:
                        rows = c.execute(
                            "SELECT s.name,t.name FROM edges e "
                            "JOIN nodes s ON e.from_id=s.id JOIN nodes t ON e.to_id=t.id "
                            "WHERE s.community_id != t.community_id LIMIT 20"
                        ).fetchall()
                    return json.dumps({"connections": [{"src": r[0], "tgt": r[1]} for r in rows]})
                if what == "feature_map":
                    rows = c.execute("SELECT id,title,semantic_type FROM communities WHERE semantic_type IS NOT NULL").fetchall()
                    return json.dumps({"features": [{"id": r[0], "title": r[1], "type": r[2]} for r in rows]})
                if what == "business_rules":
                    rows = c.execute("SELECT id,title FROM communities WHERE semantic_type IN ('rule','constraint','validation')").fetchall()
                    return json.dumps({"rules": [{"id": r[0], "title": r[1]} for r in rows]})
                if what == "process_flows":
                    rows = c.execute("SELECT id,title FROM communities WHERE semantic_type IN ('workflow','process','flow')").fetchall()
                    return json.dumps({"flows": [{"id": r[0], "title": r[1]} for r in rows]})
                if what == "suggested_questions":
                    rows = c.execute(
                        "SELECT title FROM communities WHERE title IS NOT NULL ORDER BY member_count DESC LIMIT 5"
                    ).fetchall()
                    qs = [f"How does {r[0]} work?" for r in rows if r[0]]
                    if not qs:
                        qs = ["What is the overall architecture?", "What are the main modules?"]
                    return json.dumps({"questions": qs})
                if what == "service_mesh":
                    return json.dumps({"services": _detect_services(project_path)})
                fc = c.execute("SELECT COUNT(DISTINCT file) FROM symbols WHERE file IS NOT NULL").fetchone()[0]
                return json.dumps({"path": project_path, "symbols": gs.symbol_count(),
                                   "communities": gs.community_count(), "files_with_symbols": fc})
            finally:
                gs.close()
    return json.dumps({"what": what, "status": "no project available"})
