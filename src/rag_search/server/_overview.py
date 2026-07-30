"""overview() tool implementation — isolated to keep mcp.py within 150 lines."""
from __future__ import annotations

import json


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


_VALID = {
    "structure", "projects", "metrics", "communities",
    "status", "import_cycles",
    "surprising_connections", "validate",
}


def _extraction_totals(rows: list[dict]) -> dict:
    """Roll per-(language, rung) rows up into the dark-set headline numbers.

    `dark_files` is the number this whole apparatus exists to drive down, and it is reported
    beside the breakdown rather than alone — a single coverage percentage is what made the gap
    unactionable, because five different causes all landed in it.
    """
    files = sum(r["files"] for r in rows)
    with_syms = sum(r["files"] for r in rows if r["symbols"])
    return {"by_language_rung": rows, "files": files,
            "files_with_symbols": with_syms,
            "dark_files": files - with_syms,
            "anon_dropped": sum(r["anon"] for r in rows),
            "files_with_errors": sum(r["errors"] for r in rows),
            "coverage_pct": round(100.0 * with_syms / files, 2) if files else None}


def _extraction_block(project_path, projects) -> dict:  # type: ignore[no-untyped-def]
    """Per-(language, rung) extraction coverage, plus the dark-set totals it exists to expose.

    Served from inside the daemon rather than by a caller opening `graph.db` itself, and that
    is a correctness requirement, not a convenience: the stores are `journal_mode=WAL` and the
    daemon is their writer, so an external read-only connection can be handed the
    pre-checkpoint snapshot of the main DB while committed rows still sit in the `-wal`.
    Measured 2026-07-30 — the identical `COUNT(*) … LIKE '%.svelte'` returned **0**, then **78**
    after the checkpoint, against a `graph.db` whose mtime never moved. A wrong answer that is
    stable and reproducible is the failure mode here, so the reader must be the writer.

    Stores are opened and closed one at a time: the comprehension that opened all of them at
    once leaked ~150 descriptors on a 157-member federation when it hit EMFILE partway.
    """
    from rag_search.core.config import project_graph_db
    from rag_search.daemon.federation import expand_federation
    from rag_search.graph.store import GraphStore

    if not project_path:
        project_path, _err = _require_project(projects)
    if not project_path:
        return {}
    agg: dict[tuple[str, str], dict] = {}
    for _p in expand_federation(project_path):
        _db = project_graph_db(_p)
        if not _db.exists():
            continue
        _gs = GraphStore(_db)
        try:
            for row in _gs.extraction_summary():
                key = (row.get("language") or "unknown", row.get("rung") or "unknown")
                acc = agg.setdefault(key, {"language": key[0], "rung": key[1],
                                           "files": 0, "symbols": 0, "anon": 0, "errors": 0})
                for _f in ("files", "symbols", "anon", "errors"):
                    acc[_f] += row.get(_f) or 0
        finally:
            _gs.close()
    return _extraction_totals(sorted(agg.values(), key=lambda r: r["files"], reverse=True))


def _require_project(projects) -> tuple[str, str | None]:  # type: ignore[no-untyped-def]
    """Resolve an omitted project for a project-scoped overview. One enabled project → use it;
    several → fail loud with candidates rather than silently answering about projects[0]."""
    ps = [p for p in projects if p.enabled]
    if len(ps) == 1:
        return ps[0].path, None
    if not ps:
        return "", None  # downstream reports 'no project available'
    return "", json.dumps({
        "error": "project_path required — multiple projects indexed; none could be inferred. "
                 "Pass project_path.",
        "candidates": [p.path for p in ps[:12]],
    })


def handle_overview(project_path: str, what: str, query: str = "") -> str:
    from rag_search.core.registry import list_projects

    if what not in _VALID:
        return json.dumps({"error": f"unknown what={what!r}", "valid": sorted(_VALID)})
    if project_path:
        from rag_search.core.registry import resolve_registered_root
        project_path = resolve_registered_root(project_path)
    if what == "projects":
        return json.dumps({"projects": [
            {"path": p.path, "enabled": p.enabled, "indexed_at": p.indexed_at,
             "last_change_seen": p.last_change_seen}
            for p in list_projects()
        ]})
    if what == "metrics":
        from rag_search.server.routes_ops import _snapshot
        return json.dumps({**_snapshot(),
                           "extraction": _extraction_block(project_path, list_projects())})
    if what == "validate":
        if not project_path:
            project_path, _verr = _require_project(list_projects())
            if _verr:
                return _verr
        from rag_search.index.validate import validate_index
        return json.dumps({**validate_index(project_path), "resolved_project": project_path})
    if not project_path:
        project_path, _err = _require_project(list_projects())
        if _err:
            return _err
    if project_path:
        from rag_search.core.config import project_graph_db
        from rag_search.daemon.federation import expand_federation
        from rag_search.graph.store import GraphStore

        _paths = [p for p in expand_federation(project_path) if project_graph_db(p).exists()]
        if not _paths:
            return json.dumps({"what": what, "status": "no project available"})
        _gstores: list[GraphStore] = []
        try:
            # Opened one at a time *inside* the `try`, not by a comprehension outside it. A
            # comprehension binds its whole list only after the last element, so an exception
            # partway through orphans every store it already opened — and each SQLite WAL
            # connection is three descriptors (db + -wal + -shm), so on inosoft-project's 157
            # graph-bearing members the first EMFILE leaked ~150 handles permanently. That is
            # what turned a transient descriptor shortage into a wedge only a restart cleared;
            # `finally` below could not help because `try` was never entered.
            for _p in _paths:
                _gstores.append(GraphStore(project_graph_db(_p)))
            if what == "communities":
                # Carries `summary` and `member_count`, and ranks by `query` when one is given.
                # This is the architecture axis the `ask` tool used to reach: `ask` re-ran a whole
                # federated chunk search to get here, then returned it as a 3000-char prose blob.
                # Current practice is the opposite — consolidate into a parameterised tool, and
                # return structured rows the caller can act on rather than assembled context.
                # The cap is global, not per store. `LIMIT 50` inside the loop bounds each
                # federation *member* — inosoft has 194, so the payload would have been up to
                # 9,700 rows and the rerank below would have scored every one of them. Sorting
                # after the concatenation is required for the same reason: each store returns its
                # own descending run, and concatenated descending runs are not descending.
                rows = sorted(
                    (r for gs in _gstores for r in gs.conn.execute(
                        "SELECT id,title,level,summary,member_count FROM communities "
                        "WHERE level>=1 ORDER BY member_count DESC LIMIT 50").fetchall()),
                    key=lambda r: r[4] or 0, reverse=True,
                )[:50]
                if query:
                    # Same cross-encoder `_community_summaries` uses, reached through the query
                    # layer because B2 (test_inference_lanes.py) allows no other layer to touch it.
                    from rag_search.query.ask import rank_community_rows
                    rows = rank_community_rows(query, rows)
                return json.dumps({"communities": [
                    {"id": r[0], "title": r[1], "level": r[2],
                     "summary": r[3], "member_count": r[4]} for r in rows],
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
                # Three reachable states. The old ladder had a fourth, `enriching`, keyed on the
                # level-1 summary fill rate — meaningless now that structural labelling fills every
                # summary deterministically, which would have pinned every project at a permanent
                # `ready`. What still discriminates is whether the index exists and whether the
                # partition is degenerate.
                _rank = {"indexing": 0, "degraded": 1, "ready": 2}
                for p, gs in zip(_paths, _gstores, strict=False):
                    ep = _by_path.get(p)  # §2a: cached lookup, not a fresh file read
                    _ks = ("indexing" if (ep is None or ep.indexed_at is None
                                          or not project_vector_db(p).exists()) else "ready")
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
                    # Degenerate partition demotes index_state below ready (HR20, user choice).
                    # The gate was written against the field's old name, `kb_state`; that name is
                    # gone from src/ with tier 3 and the partition check itself is tier 2 (igraph
                    # + SQL, no LLM), so only the label changed here.
                    if hq.get("degenerate") and _ks == "ready":
                        _ks = "degraded"
                    members_info.append({"path": p, "index_state": _ks, "symbols": s,
                                         "communities": cm, "edges": ec,
                                         "symbol_hollow": _hollow,
                                         "hierarchy_quality": hq})
                    if _rank.get(_ks, 2) < _rank.get(worst_state, 2):
                        worst_state = _ks
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
                                   "index_state": worst_state,
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
            # `suggested_questions` stood here. It rendered f"How does {title} work?" over the top
            # 5 communities by member_count, and `_label_from_names` gives ccw 22 communities
            # called `Test` — so the dashboard offered "How does Test work?" five times. It
            # existed to seed a chat box, which no longer prompts for questions.
            # default: structure
            fc = sum(gs.conn.execute("SELECT COUNT(DISTINCT file) FROM symbols WHERE file IS NOT NULL").fetchone()[0] for gs in _gstores)
            return json.dumps({"path": project_path, "symbols": sum(gs.symbol_count() for gs in _gstores),
                               "communities": sum(gs.community_count() for gs in _gstores), "files_with_symbols": fc,
                               "resolved_project": project_path})
        finally:
            for gs in _gstores:
                gs.close()
    return json.dumps({"what": what, "status": "no project available"})
