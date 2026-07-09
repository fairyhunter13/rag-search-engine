"""overview() tool implementation — isolated to keep mcp.py within 150 lines."""
from __future__ import annotations

import json
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
    """Detect gRPC services by mining registrar names from generated *.pb.go (tree-sitter)."""
    from rag_search.kb.bpre_ast import federation_discover
    names = set(federation_discover([root]).registrars.values())
    if not names:
        return []
    return [{"name": Path(root).name, "path": root, "services": sorted(names)}]


_VALID = {
    "structure", "projects", "metrics", "patterns", "communities",
    "status", "import_cycles",
    "surprising_connections", "feature_map", "business_rules",
    "process_flows", "suggested_questions", "service_mesh", "validate",
}


def handle_overview(project_path: str, what: str) -> str:
    from rag_search.core.registry import list_projects

    if what not in _VALID:
        return json.dumps({"error": f"unknown what={what!r}", "valid": sorted(_VALID)})
    if what == "projects":
        return json.dumps({"projects": [
            {"path": p.path, "enabled": p.enabled, "indexed_at": p.indexed_at,
             "last_change_seen": p.last_change_seen}
            for p in list_projects()
        ]})
    if what == "metrics":
        from rag_search.server.routes_ops import _snapshot
        return json.dumps(_snapshot())
    if what == "validate":
        if not project_path:
            _ps = [p for p in list_projects() if p.enabled]
            project_path = _ps[0].path if _ps else ""
        from rag_search.index.validate import validate_index
        return json.dumps({**validate_index(project_path), "resolved_project": project_path})
    if not project_path:
        ps = [p for p in list_projects() if p.enabled]
        project_path = ps[0].path if ps else ""
    if what == "patterns" and project_path:
        from pathlib import Path

        from rag_search.kb.patterns import detect_patterns
        return json.dumps({**detect_patterns(Path(project_path)), "resolved_project": project_path})
    if project_path:
        from rag_search.core.config import project_graph_db
        from rag_search.daemon.federation import expand_federation
        from rag_search.graph.store import GraphStore

        if what == "service_mesh":
            return json.dumps({"services": [s for p in expand_federation(project_path) for s in _detect_services(p)],
                                "resolved_project": project_path})
        _paths = [p for p in expand_federation(project_path) if project_graph_db(p).exists()]
        if not _paths:
            return json.dumps({"what": what, "status": "no project available"})
        _gstores = [GraphStore(project_graph_db(p)) for p in _paths]
        try:
            if what == "communities":
                rows = [r for gs in _gstores for r in gs.conn.execute("SELECT id,title,level FROM communities WHERE level>=1 ORDER BY level,id LIMIT 50").fetchall()]
                return json.dumps({"communities": [{"id": r[0], "title": r[1], "level": r[2]} for r in rows],
                                    "resolved_project": project_path})
            if what == "status":
                from rag_search.core.config import project_vector_db
                from rag_search.graph.quality import partition_quality
                # §2a: one registry read; reuse for all per-member get_project() calls below.
                _by_path = {e_.path: e_ for e_ in list_projects()}
                e = _by_path.get(project_path)
                tot_sym, tot_comm, tot_fc = 0, 0, 0
                members_info: list = []
                worst_state = "ready"
                _rank = {"indexing": 0, "searchable": 1, "enriching": 2, "ready": 3}
                root_pct = (100.0, 100.0)
                for i, (p, gs) in enumerate(zip(_paths, _gstores, strict=False)):
                    c = gs.conn
                    l1t = c.execute("SELECT COUNT(*) FROM communities WHERE level=1").fetchone()[0]
                    l1s = c.execute("SELECT COUNT(*) FROM communities WHERE level=1 AND summary IS NOT NULL AND summary!=''").fetchone()[0]
                    l1p = round(l1s / l1t * 100, 1) if l1t else 100.0
                    _pct = l1p
                    ep = _by_path.get(p)  # §2a: cached lookup, not a fresh file read
                    _ks = ("indexing" if (ep is None or ep.indexed_at is None
                                          or not project_vector_db(p).exists()) else
                           "ready" if _pct >= 95 else "enriching" if l1p > 0 else "searchable")
                    s, cm = gs.symbol_count(), gs.community_count()
                    ec = gs.edge_count()
                    tot_sym += s
                    tot_comm += cm
                    tot_fc += ep.file_count if ep else 0
                    # Federation roots legitimately have 0 edges (HR4: synthesis L3 rows only).
                    _is_fedroot = bool(ep and ep.federation)
                    _hollow = ((s == 0 and cm > 0) or (ec == 0 and cm > 0)) and not _is_fedroot
                    # §2b: read cached partition-quality verdict from meta; recompute only on miss/mismatch.
                    _pq_sig = f"{s}:{ec}:{cm}"
                    _pq_raw = gs.get_meta("partition_quality")
                    if _pq_raw:
                        _pq_cached = json.loads(_pq_raw)
                        hq = _pq_cached["q"] if _pq_cached.get("sig") == _pq_sig else partition_quality(gs)
                    else:
                        hq = partition_quality(gs)
                    # Degenerate partition demotes kb_state below ready (Gate kb_state, user choice).
                    if hq.get("degenerate") and _ks == "ready":
                        _ks = "searchable"
                    members_info.append({"path": p, "kb_state": _ks, "symbols": s,
                                         "communities": cm, "edges": ec,
                                         "symbol_hollow": _hollow,
                                         "hierarchy_quality": hq})
                    if _rank.get(_ks, 3) < _rank.get(worst_state, 3):
                        worst_state = _ks
                    if i == 0:
                        root_pct = (_pct, l1p)
                from pathlib import Path as _P

                from rag_search.core.index_config import _CONFIG_NAMES, effective_config
                _ecfg = effective_config(project_path)
                _pp = _P(project_path).resolve()
                _has_own = any((_pp / n).is_file() for n in _CONFIG_NAMES)
                _is_member = any(str(_pp) in (ep_.federation or []) for ep_ in _by_path.values())  # §2a
                _cfg_src = "own" if _has_own else "inherited" if _is_member else "default"
                _any_hollow = any(m.get("symbol_hollow") for m in members_info)
                _any_degenerate = any(m.get("hierarchy_quality", {}).get("degenerate") for m in members_info)
                return json.dumps({"path": project_path, "indexed_at": e.indexed_at if e else None,
                                   "last_change_seen": e.last_change_seen if e else None,
                                   "file_count": e.file_count if e else 0, "total_file_count": tot_fc,
                                   "symbols": tot_sym, "communities": tot_comm,
                                   "kb_state": worst_state, "enriched_pct": root_pct[0],
                                   "l1_enriched_pct": root_pct[1],
                                   "symbol_hollow": _any_hollow,
                                   "hierarchy_quality": {"degenerate": _any_degenerate},
                                   "members": members_info,
                                   "config": {"exclude": _ecfg.exclude,
                                              "use_default_ignores": _ecfg.use_default_ignores,
                                              "max_pending_files": _ecfg.max_pending_files,
                                              "source": _cfg_src},
                                   "resolved_project": project_path})
            if what == "import_cycles":
                cycs = [cy for gs in _gstores for cy in _find_import_cycles(gs.conn)][:20]
                cnt = sum(gs.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] for gs in _gstores)
                return json.dumps({"cycles": cycs, "cycle_count": len(cycs), "has_cycles": bool(cycs),
                                    "edge_count": cnt, "resolved_project": project_path})
            if what == "surprising_connections":
                rows = [r for gs in _gstores for r in gs.conn.execute(
                    "SELECT s.name,t.name FROM edges e "
                    "JOIN symbols s ON e.caller_sid=s.sid JOIN symbols t ON e.callee_sid=t.sid "
                    "WHERE s.community_id != t.community_id LIMIT 20"
                ).fetchall()]
                return json.dumps({"connections": [{"src": r[0], "tgt": r[1]} for r in rows[:20]],
                                    "resolved_project": project_path})
            if what == "feature_map":
                rows = [r for gs in _gstores for r in gs.conn.execute(
                    "SELECT id,title,semantic_type FROM communities "
                    "WHERE semantic_type IS NOT NULL AND semantic_type != '' AND level=1"
                ).fetchall()]
                return json.dumps({"features": [{"id": r[0], "title": r[1], "type": r[2]} for r in rows],
                                    "resolved_project": project_path})
            if what == "business_rules":
                rows = [r for gs in _gstores for r in gs.conn.execute(
                    "SELECT id,title,summary,member_count FROM communities "
                    "WHERE semantic_type='business_rule' ORDER BY member_count DESC"
                ).fetchall()]
                return json.dumps({"rules": [
                    {"id": r[0], "title": r[1], "summary": r[2] or "", "member_count": r[3] or 0}
                    for r in rows
                ], "resolved_project": project_path})
            if what == "process_flows":
                from rag_search.core.config import root_process_db
                pdb = root_process_db(project_path)
                if pdb.exists():
                    import sqlite3 as _sq
                    pcon = _sq.connect(str(pdb))
                    try:
                        procs = pcon.execute(
                            "SELECT p.id, p.name, p.entry_service, p.services_json, p.step_count, "
                            "pa.mermaid FROM processes p LEFT JOIN process_artifacts pa "
                            "ON pa.process_id=p.id ORDER BY p.step_count DESC"
                        ).fetchall()
                    finally:
                        pcon.close()
                    return json.dumps({"source": "reconstructed", "flows": [
                        {"id": r[0], "name": r[1], "entry_service": r[2],
                         "services": json.loads(r[3] or "[]"),
                         "step_count": r[4], "mermaid": r[5] or ""}
                        for r in procs
                    ], "resolved_project": project_path})
                rows = [r for gs in _gstores for r in gs.conn.execute(
                    "SELECT id,title,summary,member_count FROM communities "
                    "WHERE semantic_type='business_process' ORDER BY member_count DESC"
                ).fetchall()]
                return json.dumps({"source": "communities", "flows": [
                    {"id": r[0], "title": r[1], "summary": r[2] or "", "member_count": r[3] or 0}
                    for r in rows
                ], "resolved_project": project_path})
            if what == "suggested_questions":
                rows = [r for gs in _gstores for r in gs.conn.execute(
                    "SELECT title FROM communities WHERE title IS NOT NULL AND level>=1 ORDER BY member_count DESC LIMIT 5"
                ).fetchall()]
                qs = list(dict.fromkeys(f"How does {r[0]} work?" for r in rows if r[0]))[:5]
                if not qs:
                    qs = ["What is the overall architecture?", "What are the main modules?"]
                return json.dumps({"questions": qs, "resolved_project": project_path})
            # default: structure
            fc = sum(gs.conn.execute("SELECT COUNT(DISTINCT file) FROM symbols WHERE file IS NOT NULL").fetchone()[0] for gs in _gstores)
            return json.dumps({"path": project_path, "symbols": sum(gs.symbol_count() for gs in _gstores),
                               "communities": sum(gs.community_count() for gs in _gstores), "files_with_symbols": fc,
                               "resolved_project": project_path})
        finally:
            for gs in _gstores:
                gs.close()
    return json.dumps({"what": what, "status": "no project available"})
