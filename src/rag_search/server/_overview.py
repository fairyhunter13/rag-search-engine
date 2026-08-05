"""overview() tool implementation — isolated to keep mcp.py within 150 lines."""
from __future__ import annotations

import json


def _find_import_cycles(conn) -> list[list[str]]:  # type: ignore[no-untyped-def]
    """Tarjan SCC on the file-level import graph; returns SCCs of size ≥ 2.

    Reads `file_imports`, which is what the name has always claimed. Until e11 there was no
    import table, so this ran on the file-level projection of the *call* graph — a related but
    different relation, and a strictly weaker answer to the question asked: a call cycle is
    resolvable by moving one function, an import cycle is a module-structure defect. The two
    also disagree in both directions, since a file can import another without calling into it
    (the §8 measurement: 0% of one member's import pairs were induced by calls) and a call can
    cross files that import each other transitively rather than directly.

    Falls back to nothing rather than to the call graph when the table is empty: an empty
    answer is honest about a store predating e11, and silently substituting a different
    relation is the failure this change exists to end.
    """
    rows = conn.execute(
        "SELECT src_file,dst_file FROM file_imports "
        "WHERE src_file!=dst_file LIMIT 20000"
    ).fetchall()
    adj: dict[str, list[str]] = {}
    for a, b in rows:
        adj.setdefault(a, []).append(b)
    idx: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stk: set[str] = set()
    stk: list[str] = []
    cnt = 0
    cycles: list[list[str]] = []

    # D6, applied to Tarjan: an explicit stack, because the recursive form recursed once per
    # *node* and its driver caught `RecursionError` and returned `cycles[:20]` anyway — a cycle
    # list truncated at whatever depth CPython gave up on, served as if it were complete. The
    # caller cannot tell those apart, and the one project most worth asking about cycles is the
    # one with the deepest import chain. Depth here is bounded by the call graph of the repo
    # under analysis, which this module does not control; 1000 files in a chain is not exotic.
    #
    # Frames carry an iterator rather than an index because a node's successor list is only ever
    # walked forwards, once. `next(it, None)` is unambiguous: the query filters both endpoints
    # `IS NOT NULL`, so no successor can be None.
    for root in adj:
        if root in idx:
            continue
        idx[root] = low[root] = cnt
        cnt += 1
        stk.append(root)
        on_stk.add(root)
        work: list[tuple[str, object]] = [(root, iter(adj[root]))]
        while work:
            v, it = work[-1]
            w = next(it, None)  # type: ignore[call-overload]
            if w is not None:
                if w not in idx:
                    idx[w] = low[w] = cnt
                    cnt += 1
                    stk.append(w)
                    on_stk.add(w)
                    work.append((w, iter(adj.get(w, ()))))
                elif w in on_stk:
                    low[v] = min(low[v], idx[w])
                continue
            # Successors exhausted: this is where the recursion used to return, so the
            # parent's `low[v] = min(low[v], low[w])` update happens here instead.
            work.pop()
            if work:
                par = work[-1][0]
                low[par] = min(low[par], low[v])
            if low[v] == idx[v]:
                scc: list[str] = []
                while True:
                    x = stk.pop()
                    on_stk.discard(x)
                    scc.append(x)
                    if x == v:
                        break
                if len(scc) >= 2:
                    cycles.append(scc[:5])
    return cycles[:20]


_VALID = {
    "structure", "projects", "metrics", "communities",
    "status", "import_cycles",
    "surprising_connections", "validate",
}


# X2's rung says `language_mismatch`, which names a cause it cannot actually observe: the check
# only sees that a file's bytes did not parse and that nothing extracted. Measured 2026-07-31
# across 118 graphs, gherkin fails 138 of 147 files — and reading one shows ordinary, valid
# English Gherkin. The bytes match the extension; the pack's grammar cannot parse its own
# language. So the rung blames the file for an upstream grammar weakness, and the two causes were
# untriageable.
#
# They are separable without touching extraction, and that is the point: a *file* that is genuinely
# misnamed is rare within its language, while a *grammar* that cannot parse its language fails
# nearly all of it. The rate is already in `extraction_summary()`'s (language, rung) rows, so this
# costs no schema change, no EXTRACTOR_REV bump, and no 111-graph re-derive.
#
# Both bounds are measured, not guessed. Fleet rates split 93.9 % (gherkin) against a top
# wrong-extension rate of 9.5 % (nginx), then vimdoc 5.2 %, php 2.0 %, scss 1.9 %, css 1.1 % — an
# 84-point gap with nothing in it, which is why one ratio suffices. The floor exists because the
# only other languages above the ratio are prolog at 1 file and batch at 2: a rate computed from
# one file is not evidence, and `insufficient_sample` is the honest answer where the rung name
# asserted a cause outright.
_WEAK_GRAMMAR_RATE = 0.5
_WEAK_GRAMMAR_MIN_FILES = 10


def _mismatch_diagnosis(rows: list[dict]) -> list[dict]:
    """Per-language verdict on `language_mismatch`: weak grammar, wrong extension, or too few."""
    total: dict[str, int] = {}
    bad: dict[str, int] = {}
    for r in rows:
        lang = r.get("language") or "unknown"
        total[lang] = total.get(lang, 0) + (r.get("files") or 0)
        if r.get("rung") == "language_mismatch":
            bad[lang] = bad.get(lang, 0) + (r.get("files") or 0)
    out = []
    for lang, n in bad.items():
        seen = total.get(lang, 0)
        rate = n / seen if seen else 0.0
        if seen < _WEAK_GRAMMAR_MIN_FILES:
            verdict = "insufficient_sample"
        elif rate >= _WEAK_GRAMMAR_RATE:
            verdict = "weak_grammar"
        else:
            verdict = "wrong_extension"
        out.append({"language": lang, "mismatch_files": n, "files": seen,
                    "mismatch_pct": round(100.0 * rate, 2), "verdict": verdict})
    return sorted(out, key=lambda r: r["mismatch_files"], reverse=True)


def _grammar_ceilings(rows: list[dict]) -> list[dict]:
    """Per-language `grammar_has_no_queries`: dark files rungs 4-5 can never reach, by construction.

    Rung 4 (`highlights`) and rung 5 (`embedded`) both start by fetching the grammar's own
    `highlights.scm` / `injections.scm` and return `{}` when the text is empty
    (`extractor._highlight_captures`, `_iter_script_blocks`). A language whose pack ships neither
    has a *ceiling*, not a coverage bug: no amount of ladder work reaches those files, and reading
    their dark count as a gap is how effort gets spent where it cannot pay.

    Measured 2026-07-31 against the installed pack: groovy, php, vimdoc and jinja2 ship neither
    query — php being 20,014 fleet files at 33.4 % dark. Reported beside `by_language_rung` rather
    than folded into it, because a ceiling and a miss are different claims and the whole point of
    the rung column was to stop merging causes into one number.

    `_mismatch_diagnosis`'s `weak_grammar` is deliberately left alone: that verdict is about a
    grammar failing to *parse* its language (gherkin, 93.9 %), which is a different failure from a
    grammar that parses fine and merely ships no queries.
    """
    langs: dict[str, dict] = {}
    for r in rows:
        acc = langs.setdefault(r.get("language") or "unknown",
                               {"language": r.get("language") or "unknown",
                                "files": 0, "files_with_symbols": 0})
        acc["files"] += r.get("files") or 0
        acc["files_with_symbols"] += r.get("files_with_symbols") or 0
    out = []
    for lang, acc in langs.items():
        try:
            from tree_sitter_language_pack import (
                get_highlights_query,
                get_injections_query,
                has_language,
            )
            # `has_language` first, and not merely as a guard: without it the `no_language`
            # bucket — 11,791 files filed under `unknown` — reads back as a grammar that ships
            # no queries, which is the opposite claim. There is no grammar to have a ceiling.
            # (`get_language` would answer this too, but by raising `DownloadError`; asking a
            # membership question with an exception handler is how the real errors get swallowed.)
            if not has_language(lang) or get_highlights_query(lang) or get_injections_query(lang):
                continue
        except Exception:
            continue
        out.append({"language": lang, "files": acc["files"],
                    "dark_files": acc["files"] - acc["files_with_symbols"],
                    "ceiling": "grammar_has_no_queries"})
    return sorted(out, key=lambda r: r["dark_files"], reverse=True)


def _extraction_totals(rows: list[dict]) -> dict:
    """Roll per-(language, rung) rows up into the dark-set headline numbers.

    `dark_files` is the number this whole apparatus exists to drive down, and it is reported
    beside the breakdown rather than alone — a single coverage percentage is what made the gap
    unactionable, because five different causes all landed in it.

    It is summed from a per-group *file* count, never from `if r["symbols"]`. That predicate was
    the original, and it credited every file in a group as covered as soon as one file in it
    carried a symbol — so the number built to expose the dark set was hiding part of it. Found
    2026-07-30 by cross-checking this block against a direct `mode=ro` read of the same store:
    111 files claimed against 100 real, 57.81 % against 52.08 %. The disagreement looked at first
    like the WAL staleness hazard this block exists to avoid, which is the trap — a second
    plausible cause for the same symptom is how the real one goes unfixed.

    `symbols` and `by_rung` were added 2026-07-31 for a related reason: the rows carried a
    per-group symbol total and this rollup discarded it, so the only way to ask "which rung is
    the ladder actually paying for" was to open every `graph.db` by hand — which is exactly the
    external read AU1 forbids. Measured that way across 118 graphs: `generic` is 15,106 files
    (33 % of the fleet) and **43 symbols in total**, 0.2 % of its files yielding anything. It is
    a failure rung whose name says "parsed, nothing found" and whose *occupancy* reads like
    success, so any per-rung reading taken from `files` alone rates the fleet ~84 % productive
    where per-file yield says 48 %. Reporting symbols beside files per rung is what makes those
    two numbers impossible to confuse; the ladder itself is unchanged, so no graph is re-derived.
    """
    files = sum(r["files"] for r in rows)
    with_syms = sum(r.get("files_with_symbols") or 0 for r in rows)
    by_rung: dict[str, dict] = {}
    for r in rows:
        acc = by_rung.setdefault(r.get("rung") or "unknown",
                                 {"rung": r.get("rung") or "unknown", "files": 0,
                                  "symbols": 0, "files_with_symbols": 0})
        for f in ("files", "symbols", "files_with_symbols"):
            acc[f] += r.get(f) or 0
    for acc in by_rung.values():
        acc["dark_files"] = acc["files"] - acc["files_with_symbols"]
        acc["coverage_pct"] = (round(100.0 * acc["files_with_symbols"] / acc["files"], 2)
                               if acc["files"] else None)
    return {"by_language_rung": rows,
            "by_rung": sorted(by_rung.values(), key=lambda r: r["files"], reverse=True),
            "mismatch_diagnosis": _mismatch_diagnosis(rows),
            "grammar_ceilings": _grammar_ceilings(rows),
            "files": files,
            "symbols": sum(r.get("symbols") or 0 for r in rows),
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

    _err = None
    if not project_path:
        project_path, _err = _require_project(projects)
    if not project_path:
        # Never a bare `{}`. "I could not decide which project you meant" and "the ladder
        # recorded nothing" are different answers, and returning the same empty dict for both is
        # how a metrics block lies quietly — the defect class this whole block exists to end.
        # Measured 2026-07-30: with 152 enabled projects this branch fires on *every* unscoped
        # call, so `overview(what="metrics")` reported `extraction: {}` fleet-wide while each
        # store held rows. That read as "the instrument sees nothing", which is the one thing it
        # must never say when it simply was not asked.
        return json.loads(_err) if _err else {"error": "no project available"}
    agg: dict[tuple[str, str], dict] = {}
    for _p in expand_federation(project_path):
        _db = project_graph_db(_p)
        if not _db.exists():
            continue
        _gs = GraphStore(_db)
        try:
            for row in _gs.extraction_summary():
                key = (row.get("language") or "unknown", row.get("rung") or "unknown")
                acc = agg.setdefault(key, {"language": key[0], "rung": key[1], "files": 0,
                                           "symbols": 0, "files_with_symbols": 0,
                                           "anon": 0, "errors": 0})
                # `files_with_symbols` is in this list, not derived after the loop: a federation
                # root sums per-member rows, and a member contributing zero to a group that
                # another member fills is exactly the case the group-level predicate got wrong.
                for _f in ("files", "symbols", "files_with_symbols", "anon", "errors"):
                    acc[_f] += row.get(_f) or 0
        finally:
            _gs.close()
    return _extraction_totals(sorted(agg.values(), key=lambda r: r["files"], reverse=True))


def _fleet_pipeline_block() -> dict:
    """`_pipeline_block` over the whole registered fleet, independent of any `project_path`.

    This lives beside `extraction` rather than inside it, and that placement is the whole point.
    `pipeline_version` answers a fleet question — *has the re-derive converged?* — but it was
    computed at the tail of `_extraction_block`, which is project-scoped and early-returns when
    no project can be inferred. With 150 projects enabled `_require_project` refuses on **every**
    unscoped call, so the key was absent from exactly the call an operator makes to ask the
    fleet-wide question, and present-but-scoped-to-one-federation on the other. Measured
    2026-08-05: unscoped had no `pipeline_version` at all; scoped reported `stores: 1`.

    Two individually-correct changes a day apart combined into that silence — EL13 (2026-07-30)
    made the unscoped case return an `error` dict instead of a bare `{}`, and `_pipeline_block`
    (2026-07-31) was added underneath that new early return. AU5 kept passing throughout because
    it calls `_pipeline_block` with a hand-built dict and never traverses `handle_overview`.

    Always fleet-wide, never scope-dependent: one key with two meanings is a number that gets
    misread, and a stale count over a one-member federation was never what anyone wanted.

    Cost, measured 2026-08-05 over 150 stores: **89 ms** total, 0.6 ms/store — one indexed `meta`
    lookup each. (A bare `sqlite3.connect` does the same walk in 24 ms; the difference is
    `GraphStore`'s own setup, and it is the price of reading through the writer rather than
    around it. Quoted here so a later reader does not "optimise" it back to a raw handle.)
    Stores are opened and closed one at a time, as `_extraction_block` does and for
    the reason `_each_store` documents: each WAL connection is three descriptors, and holding a
    federation open at once is the 2026-07-29 descriptor wedge. In-process `GraphStore`, never an
    external `mode=ro` handle — the daemon is these stores' writer, and an outside reader can be
    served a pre-checkpoint snapshot (AU1).
    """
    from rag_search.core.config import project_graph_db
    from rag_search.core.registry import list_projects
    from rag_search.daemon.federation import searchable_stores
    from rag_search.graph.store import GraphStore

    stamps: dict[str, int] = {}
    for _p in searchable_stores(list_projects()):
        _db = project_graph_db(_p)
        if not _db.exists():
            continue
        _gs = GraphStore(_db)
        try:
            _stamp = _gs.get_meta("algo_version") or "(unstamped)"
        finally:
            _gs.close()
        stamps[_stamp] = stamps.get(_stamp, 0) + 1
    return _pipeline_block(stamps)


def _pipeline_block(stamps: dict[str, int]) -> dict:
    """Which pipeline revision each store's symbols were actually produced by.

    The self-healing design is sound and the re-derive is driven by comparing a store's stored
    `meta.algo_version` against `sweeps._pipeline_algo_version()` — but nothing reported the
    comparison, so a fleet that had stopped converging looked identical to one that had. Measured
    2026-07-31 by opening 144 stores by hand: **128 were stale**, 51 of them four extractor
    revisions behind, and the only way to learn that was the external `mode=ro` read AU1 forbids.
    That is the gap this closes; the walk fix that makes the number move lives in
    `sweeps.reconcile_order` / `reconcile_projects`.

    Pure arithmetic over a `{stamp: count}` tally — the walk that produces the tally is
    `_fleet_pipeline_block`. Kept separate so the counting rules stay unit-testable against a
    hand-built dict (AU5) while reachability is guarded at the surface (AU6); AU5 alone passing
    while the block was unreachable is the reason both exist.

    `stale_stores` is the number to watch: it must fall pass over pass. It counts `(unstamped)`
    too — a store with no `meta` row has never completed a derive, which is staler, not exempt.
    """
    from rag_search.daemon.sweeps import _pipeline_algo_version
    current = _pipeline_algo_version()
    return {"current": current,
            "stores": sum(stamps.values()),
            "stores_current": stamps.get(current, 0),
            "stale_stores": sum(n for s, n in stamps.items() if s != current),
            "by_stamp": sorted(({"algo_version": s, "stores": n, "current": s == current}
                                for s, n in stamps.items()),
                               key=lambda r: r["stores"], reverse=True)}


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
        # `pipeline_version` is a sibling of `extraction`, not a member of it: it is fleet-scoped
        # and must survive the project-scoped refusal `extraction` returns on an unscoped call.
        return json.dumps({**_snapshot(),
                           "extraction": _extraction_block(project_path, list_projects()),
                           "pipeline_version": _fleet_pipeline_block()})
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
        from contextlib import ExitStack, closing

        from rag_search.core.config import project_graph_db
        from rag_search.daemon.federation import expand_federation
        from rag_search.graph.store import GraphStore
        from rag_search.query.search import _FANOUT_WORKERS

        _paths = [p for p in expand_federation(project_path) if project_graph_db(p).exists()]
        if not _paths:
            return json.dumps({"what": what, "status": "no project available"})

        def _each_store():  # type: ignore[no-untyped-def]
            """(path, GraphStore) pairs, with at most `_FANOUT_WORKERS` open at any moment.

            The predecessor opened the whole federation before touching any of it. Each SQLite
            WAL connection is three descriptors (db + -wal + -shm), so on the largest workspace's 157
            graph-bearing members that is a peak of ~471 for the length of the request, and a
            federated `search` — which fans out over the same members — can be doing it at the
            same time. That is the descriptor shortage behind the 2026-07-29 wedge; capping the
            peak is what keeps it from being reachable at all, rather than merely survivable.

            The leak half of that incident is fixed structurally here. It came from a
            comprehension binding its list only after the last element, so an exception partway
            through orphaned every store already opened; `enter_context` takes ownership of each
            one the moment it exists, and unwinds the ones it holds on any exit.

            `closing` rather than `with GraphStore(...)`: the class exposes `close()` and no
            `__enter__`/`__exit__` — the same reason, and the same stdlib adapter, as the
            federated search path in query/search.py.

            Batch size is `search.py`'s `_FANOUT_WORKERS`, not a second constant: it is the same
            federation fanned out over the same members, and two knobs for one property drift.
            """
            for _i in range(0, len(_paths), _FANOUT_WORKERS):
                _chunk = _paths[_i:_i + _FANOUT_WORKERS]
                with ExitStack() as _es:
                    yield from zip(_chunk, [
                        _es.enter_context(closing(GraphStore(project_graph_db(_p))))
                        for _p in _chunk
                    ], strict=False)

        if what == "communities":
            # Carries `summary` and `member_count`, and ranks by `query` when one is given.
            # This is the architecture axis the `ask` tool used to reach: `ask` re-ran a whole
            # federated chunk search to get here, then returned it as a 3000-char prose blob.
            # Current practice is the opposite — consolidate into a parameterised tool, and
            # return structured rows the caller can act on rather than assembled context.
            # The cap is global, not per store. `LIMIT 50` inside the loop bounds each
            # federation *member* — the largest workspace has 194, so the payload would have been up to
            # 9,700 rows and the rerank below would have scored every one of them. Sorting
            # after the concatenation is required for the same reason: each store returns its
            # own descending run, and concatenated descending runs are not descending.
            _crows: list = []
            for _, gs in _each_store():
                _crows.extend(gs.conn.execute(
                    "SELECT id,title,level,summary,member_count FROM communities "
                    "WHERE level>=1 ORDER BY member_count DESC LIMIT 50").fetchall())
            rows = sorted(_crows, key=lambda r: r[4] or 0, reverse=True)[:50]
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
            for p, gs in _each_store():
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
                # H1. The exemption used to be `and not _is_fedroot` across *both* arms, while
                # the comment above it justified only the edge arm. So a federation root that
                # held its own code and extracted none of it reported healthy — the one store
                # in the fleet where "0 symbols" is ambiguous was the one store that could
                # never say so. The edge arm keeps its exemption, because HR4 really does mean
                # a root's L3 synthesis rows carry no edges. The symbol arm now asks for
                # evidence instead: a root with no code files of its own is empty by design and
                # stays exempt; one that attempted code files and got nothing back is hollow
                # like any member. That question is only answerable since `file_extraction`.
                _code_files = gs.code_files_extracted()[0] if _is_fedroot else 0
                _hollow = ((s == 0 and cm > 0 and (not _is_fedroot or _code_files > 0))
                           or (ec == 0 and cm > 0 and not _is_fedroot))
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
            # One pass, both figures: a second walk would re-open every store to read a count.
            _cycs: list = []
            cnt = 0
            for _, gs in _each_store():
                _cycs.extend(_find_import_cycles(gs.conn))
                cnt += gs.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            cycs = _cycs[:20]
            return json.dumps({"cycles": cycs, "cycle_count": len(cycs), "has_cycles": bool(cycs),
                                "edge_count": cnt, "resolved_project": project_path})
        if what == "surprising_connections":
            rows = []
            for _, gs in _each_store():
                rows.extend(gs.conn.execute(
                    "SELECT s.name,t.name FROM edges e "
                    "JOIN symbols s ON e.caller_sid=s.sid JOIN symbols t ON e.callee_sid=t.sid "
                    "WHERE s.community_id != t.community_id LIMIT 20"
                ).fetchall())
            return json.dumps({"connections": [{"src": r[0], "tgt": r[1]} for r in rows[:20]],
                                "resolved_project": project_path})
        # `suggested_questions` stood here. It rendered f"How does {title} work?" over the top
        # 5 communities by member_count, and `_label_from_names` gives ccw 22 communities
        # called `Test` — so the dashboard offered "How does Test work?" five times. It
        # existed to seed a chat box, which no longer prompts for questions.
        # default: structure
        # One pass for all three totals; three comprehensions stood here and each re-opened the
        # whole federation.
        fc = _sym = _com = 0
        for _, gs in _each_store():
            fc += gs.conn.execute(
                "SELECT COUNT(DISTINCT file) FROM symbols WHERE file IS NOT NULL").fetchone()[0]
            _sym += gs.symbol_count()
            _com += gs.community_count()
        return json.dumps({"path": project_path, "symbols": _sym,
                           "communities": _com, "files_with_symbols": fc,
                           "resolved_project": project_path})
    return json.dumps({"what": what, "status": "no project available"})
