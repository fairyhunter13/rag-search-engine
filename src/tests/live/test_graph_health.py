"""Graph-health guards — symbol-hollow detection and clear-on-reindex invariants.

Tests:
  GH1: GraphStore.clear() wipes symbols/edges/communities
  GH2: symbol_hollow flag fires on edge-free graph (communities>0, edges=0)
  GH3: overview(status) includes symbol_hollow field on healthy projects
  GH4: _graph_needs_full_index: symbols with zero communities (no graph built yet) force a
       full re-index; an empty graph and a clustered graph do not. This is narrower than symbol_hollow (GH2): a graph with
       communities>0 but edges=0 does NOT re-trigger automatically, by design — the
       extraction gap that produces a hollow graph (e.g. an embedded-script SFC format
       whose call sites tree-sitter can't see) would reproduce on every re-index, so
       reconcile does not spin retrying it on unchanged source. symbol_hollow is a
       diagnostic signal for operators, not an automatic self-heal trigger. See the
       2026-07-09 root-federation audit for the confirmed root cause and rationale.
  GH5: a full re-derive subtracts what the source no longer has (symbols of a deleted file,
       its coverage row, and an edge whose call site went while both endpoints survived).
       Behavioural: it replaced a source-inspection guard that asserted `gs.clear()` was
       called before a rebuild, which pinned the mechanism that turned out to be the bug.
  GH6: structural community labels stay distinct and deterministic
  GH8: no reader ever observes an empty graph while a full re-derive runs. Numbered after
       GH7 because GH6 was already taken by the label guard `community.py` cites by name.
  GH7: H1 — a federation root's symbol-arm exemption is evidence-based, not categorical. A root
       holding no code of its own stays exempt (it is empty by design); one that attempted code
       files and got zero symbols back is hollow like any member. The edge arm keeps its blanket
       exemption, because HR4's synthesis-only L3 rows really do carry no edges.
"""
from __future__ import annotations

import json

import pytest

from tests.live._sample_workspace import SampleWorkspace

pytestmark = pytest.mark.live


def test_graph_store_clear_wipes_tables(safe_tmp_path):
    """GH1: GraphStore.clear() must delete symbols, edges, and communities."""
    from rag_search.graph.community import detect_communities
    from rag_search.graph.extractor import extract_symbols, symbol_id
    from rag_search.graph.store import GraphStore

    gdb = safe_tmp_path / "graph.db"
    gs = GraphStore(gdb)
    try:
        fpath = safe_tmp_path / "auth.py"
        fpath.write_text("def authenticate(token): pass\ndef validate(t): return bool(t)\n")
        for sym in extract_symbols(fpath, fpath.read_text(), "python"):
            gs.upsert_symbol(symbol_id(str(fpath), sym.name, sym.start_line),
                             sym.name, sym.qualified_name, sym.kind,
                             str(fpath), sym.start_line, sym.end_line, sym.language)
        gs.commit()
        detect_communities(gs)
        assert gs.symbol_count() > 0
        assert gs.community_count() > 0
        gs.clear()
        assert gs.symbol_count() == 0, "clear() must delete all symbols"
        assert gs.edge_count() == 0, "clear() must delete all edges"
        assert gs.community_count() == 0, "clear() must delete all communities"
    finally:
        gs.close()


def test_symbol_hollow_flag_fires_on_edge_free_graph(safe_tmp_path):
    """GH2: communities>0 but edges=0 must set symbol_hollow=True in overview(status)."""
    import asyncio

    from rag_search.core.config import ProjectEntry, project_graph_db
    from rag_search.core.registry import remove_project, upsert_project
    from rag_search.graph.store import GraphStore
    from rag_search.server.mcp import overview as overview_tool

    proj = str(safe_tmp_path)
    upsert_project(ProjectEntry(path=proj, enabled=True))
    try:
        gs = GraphStore(project_graph_db(proj))
        try:
            gs._con.execute(
                "INSERT INTO symbols(sid,name,qualified_name,kind,file,start_line,end_line,language)"
                " VALUES ('s1','foo','pkg.foo','function','main.go',1,5,'go')"
            )
            gs._con.execute(
                "INSERT INTO communities(id,level,title,summary,member_count)"
                " VALUES (1,1,'Core','summarized',1)"
            )
            gs._con.execute("UPDATE symbols SET community_id=1 WHERE sid='s1'")
            gs.commit()
        finally:
            gs.close()

        result = json.loads(asyncio.run(overview_tool(proj, "status")))
        assert result.get("symbol_hollow") is True, f"edge-free graph must have symbol_hollow=True; got {result}"
        member = next((m for m in result.get("members", []) if m["path"] == proj), None)
        assert member is not None and member.get("symbol_hollow") is True
        assert member.get("edges") == 0
    finally:
        remove_project(proj)


def test_overview_status_includes_symbol_hollow_field(sample_workspace: SampleWorkspace):
    """GH3: healthy standalone project (edges>0) must have symbol_hollow=False in overview(status).

    Uses sample promo-svc (7 L1 communities, edges present) — not a federation root from
    its own perspective, so hollow-member propagation does not apply.
    """
    import asyncio

    from rag_search.server.mcp import overview as overview_tool

    result = json.loads(asyncio.run(overview_tool(sample_workspace.promo, "status")))
    assert "symbol_hollow" in result, f"symbol_hollow missing; keys={list(result)}"
    assert result["symbol_hollow"] is False, (
        f"promo-svc with edges must not be hollow; got {result}"
    )


def test_reconcile_triggers_reindex_when_communities_empty(tmp_path):
    """GH4: an unclustered graph is re-indexed; a clustered or symbol-free one is not.

    This asserted the substring "community_count() == 0" was present in
    `reconcile_projects`'s own source. The decision has since moved into
    `_graph_needs_full_index`, and the guard went red on the move without anything being
    broken — a source-text assertion tracks where code lives, not what it does. Worse, it
    could not see the defect that actually shipped in that predicate: keying on the
    community count *alone* made a symbol-free project (config/docs trees) re-index on every
    pass forever. All three states are now exercised.
    """
    from rag_search.daemon.sweeps import _graph_needs_full_index
    from rag_search.graph.store import GraphStore

    gs = GraphStore(tmp_path / "graph.db")
    try:
        assert _graph_needs_full_index(gs) is False, (
            "GH4: an empty graph has nothing to cluster — re-indexing it can never satisfy "
            "the gate, which is the permanent-burn bug this predicate was rewritten to fix"
        )
        gs.upsert_symbol("s1", "fn", "fn", "function", "a.py", 1, 2, "python")
        gs.commit()
        assert _graph_needs_full_index(gs) is True, (
            "GH4: symbols with zero communities means the clustering pass never ran — "
            "reconcile must force a full re-index"
        )
        gs.upsert_community(0, level=1, title="grp", summary="ok", member_count=1)
        gs.commit()
        assert _graph_needs_full_index(gs) is False, (
            "GH4: a clustered graph must not re-index on every pass"
        )
    finally:
        gs.close()


def test_gh5_full_rederive_subtracts_what_the_source_no_longer_has(safe_tmp_path):
    """GH5: a full re-derive drops symbols, edges and coverage rows the source lost.

    This asserted `"gs.clear()" in _index_project` until 2026-07-31 — the mechanism, not the
    property. The mechanism was wrong: `clear()` commits, so it published an empty graph for the
    whole extraction. The property is what mattered and it is what is checked here, so the gate
    survives the fix that removed the thing it used to name.

    All three subtractions are distinct failures. A dropped *file* leaves symbols with no source;
    a dropped *symbol* inside a surviving file leaves a row `upsert_symbol` can never overwrite,
    because it is keyed on a `sid` the new pass no longer emits; a dropped *call* between two
    symbols that both still exist at the same lines leaves an edge whose endpoints are perfectly
    valid — the one case `purge_dangling_edges` cannot see, and the reason `prune_edges_to` exists.
    """
    from rag_search.core.config import project_graph_db
    from rag_search.daemon.sweeps import _rederive_graph
    from rag_search.graph.store import GraphStore

    proj = str(safe_tmp_path)
    (safe_tmp_path / "keep.py").write_text(
        "def caller():\n    return callee()\n\n\ndef callee():\n    return 1\n")
    (safe_tmp_path / "gone.py").write_text("def vanishes():\n    return 0\n")
    _rederive_graph(proj)
    gs = GraphStore(project_graph_db(proj))
    try:
        before = {r[0] for r in gs._con.execute("SELECT name FROM symbols")}
        edges_before = gs.edge_count()
    finally:
        gs.close()
    assert {"caller", "callee", "vanishes"} <= before, f"GH5 setup: only extracted {before}"
    assert edges_before > 0, "GH5 setup: caller->callee edge was never written"

    # The file goes; `callee` survives at its exact line (so its sid is unchanged) but is no
    # longer called — the edge must still be retracted.
    (safe_tmp_path / "gone.py").unlink()
    (safe_tmp_path / "keep.py").write_text(
        "def caller():\n    return 2\n\n\ndef callee():\n    return 1\n")
    _rederive_graph(proj)

    gs = GraphStore(project_graph_db(proj))
    try:
        after = {r[0] for r in gs._con.execute("SELECT name FROM symbols")}
        files = {r[0] for r in gs._con.execute("SELECT file FROM file_extraction")}
        edges_after = gs.edge_count()
    finally:
        gs.close()
    assert "vanishes" not in after, f"GH5: symbol of a deleted file survived: {sorted(after)}"
    assert {"caller", "callee"} <= after, f"GH5: re-derive lost live symbols: {sorted(after)}"
    assert not any(f.endswith("gone.py") for f in files), (
        f"GH5: file_extraction kept a row for a deleted file — coverage denominator is inflated: "
        f"{sorted(files)}")
    assert edges_after == 0, (
        f"GH5: {edges_after} edge(s) survived after the only call was deleted; both endpoints "
        f"still exist, so purge_dangling_edges cannot catch this — prune_edges_to must")


def test_gh8_a_rederive_is_never_observed_as_an_empty_graph(safe_tmp_path):
    """GH8: no reader ever sees an empty graph while a full re-derive runs.

    The regression this exists for is silent by construction: `clear()` committed the wipe, so a
    concurrent `graph()` or `overview(what="communities")` answered "no symbols" — correctly
    reporting what it read, and wrong about the repository. It cost CI run 30619058883, where
    `test_pipeline_all_stages_rse_repo` read this repo's own store mid-re-derive and got 0.

    One-sided on purpose: it fails only when a sample actually reads 0, and a re-derive too quick
    to sample simply yields fewer observations. That trades detection rate for never failing
    spuriously on a fast box — the alternative is a floor on sample count, which is a timing
    assertion in disguise and the exact shape `test_watcher_graph_rederive` had to abandon.
    """
    import contextlib
    import sqlite3
    import threading
    import time

    from rag_search.core.config import project_graph_db
    from rag_search.daemon.sweeps import _rederive_graph

    proj = str(safe_tmp_path)
    for i in range(40):
        (safe_tmp_path / f"m{i}.py").write_text("\n".join(
            f"def f{i}_{j}():\n    return h{i}_{j}()\n\n\ndef h{i}_{j}():\n    return {j}\n"
            for j in range(4)))
    _rederive_graph(proj)                      # populate, so there is something to wipe

    db = project_graph_db(proj)
    # `check_same_thread=False` is load-bearing, not boilerplate: the poller runs on another
    # thread, and the ProgrammingError sqlite3 would otherwise raise there is a `sqlite3.Error` —
    # it would be swallowed below, leaving `samples` empty and this test green without ever
    # having looked at the database.
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, check_same_thread=False)
    try:
        assert con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] > 0, "GH8 setup: empty"
        samples: list[int] = []
        stop = threading.Event()

        def poll() -> None:
            while not stop.is_set():
                # A reader erroring is a different defect, not this one — swallow it and let the
                # `assert samples` below catch the case where *every* read failed.
                with contextlib.suppress(sqlite3.Error):
                    samples.append(con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0])
                time.sleep(0.002)          # sample densely, but not with a whole core

        watcher = threading.Thread(target=poll, daemon=True)
        watcher.start()
        try:
            _rederive_graph(proj)
        finally:
            stop.set()
            watcher.join(timeout=30)
        assert samples, "GH8: the poller read nothing — the assertion below would be vacuous"
        assert 0 not in samples, (
            f"GH8: a concurrent reader saw an empty symbols table during a re-derive "
            f"({samples.count(0)} of {len(samples)} samples read 0) — the rebuild published a "
            f"hole instead of writing through it")
    finally:
        con.close()


_LABEL_SHAPE = [
    # (names, files) — the ccw shape in miniature: one dominant token across most communities,
    # distinguished only by a second token and by where the members live.
    (["test_login", "test_logout", "test_session"], ["src/auth/a.py"] * 3),
    (["test_render", "test_paint", "test_draw"], ["src/ui/b.py"] * 3),
    (["test_index", "test_reindex", "test_purge"], ["src/index/c.py"] * 3),
    (["test_embed", "test_rerank"], ["src/embed/d.py"] * 2),
    (["test_login", "test_logout"], ["src/api/e.py"] * 2),
    (["run_sweep", "run_reconcile"], ["src/daemon/f.py"] * 2),
]


def test_community_labels_discriminate():
    """GH6: structurally-labelled communities must not collapse onto one token.

    `_label_from_names` returned the single most frequent snake_case token capitalised, so every
    community whose members are mostly `test_*` was called `Test`. Measured on real fleet graphs
    before the fix, over each community's full member list:

        project                 communities   distinct titles   worst collision
        claude-code-workflows        65        37 (57%)         'Test' x 22
        rag-search-engine            53        38 (72%)         'Test' x 14

    After: 61/65 (94%) and 51/53 (96%), worst collisions 4 and 3. That 57% is what makes this a
    defect rather than a tidiness complaint — `overview(what="communities")` is the whole
    architecture axis since `ask` was retired into it, and it was naming 22 of ccw's 65 domains
    identically. The candidate pool in `ask._candidate_summaries` is the other casualty; RR8 had
    to move its dedup off the title precisely because of this collision.

    The pair, so neither half passes alone:

    - **Discrimination.** Six communities, five dominated by `test`, must yield >=5 distinct
      labels (>=80%, the ratio the fix measures at 94% on ccw). The old one-token label scores
      2 here — `Test` and `Run` — so this is red before the fix, not decoration
      ([[feedback_guard_tests_must_discriminate]]).
    - **Determinism.** Labels are stored, and one that varies run to run re-derives the fleet
      forever. This is the half that rules out the tempting "append a hash" fix, which would
      score 100% on discrimination and be worthless.

    Cases 1 and 5 are the load-bearing pair: identical member names, different directory. They
    separate only if the label uses where the members live — which is why the directory is in
    the label rather than just a second token.

    Touches no model; structural labelling never did, which is what let it survive tier 3.
    """
    from rag_search.graph.community import _label_from_names

    labels = [_label_from_names(names, files) for names, files in _LABEL_SHAPE]
    distinct = len(set(labels))
    assert distinct >= 5, (
        f"GH6: {distinct} distinct labels from {len(_LABEL_SHAPE)} distinct communities — "
        f"structural labels are collapsing onto a shared token: {labels}"
    )
    assert labels[0] != labels[4], (
        "GH6: two communities with identical member names in different directories got the same "
        f"label ({labels[0]!r}) — the label ignores where the members live"
    )
    again = [_label_from_names(names, files) for names, files in _LABEL_SHAPE]
    assert labels == again, "GH6: labelling must be deterministic; it is stored and diffed"
    assert all(lbl for lbl in labels), f"GH6: no community may go unlabelled: {labels}"


def _fedroot_status(proj: str, files: list[tuple[str, str]]) -> dict:
    """Register `proj` as a federation root, give its store communities but no symbols, and ask.

    `files` is `(name, language)` — each recorded as an *attempted* extraction that produced
    nothing, which is the state H1 has to be able to read. The federation list points at a member
    path that is deliberately not registered: `expand_federation` still returns it, and the root's
    own entry in `members` is what both arms below assert on, exactly as GH2 does.
    """
    import asyncio

    from rag_search.core.config import ProjectEntry, project_graph_db
    from rag_search.core.registry import upsert_project
    from rag_search.graph.store import GraphStore
    from rag_search.server.mcp import overview as overview_tool

    upsert_project(ProjectEntry(path=proj, enabled=True, federation=[proj + "/member"]))
    gs = GraphStore(project_graph_db(proj))
    try:
        gs.upsert_community(1, level=1, title="Core", summary="synthesis", member_count=1)
        for name, lang in files:
            gs.record_extraction(name, lang, "generic", 0, 0, 0)
        gs.commit()
    finally:
        gs.close()
    result = json.loads(asyncio.run(overview_tool(proj, "status")))
    return next((m for m in result.get("members", []) if m["path"] == proj), {})


def test_gh7a_a_root_with_no_code_of_its_own_is_not_hollow(safe_tmp_path):
    """GH7a — the half of the old exemption that was right, kept.

    A federation root is usually a container directory: a README, a licence, and the member trees
    below it. Zero symbols there is the correct answer, not a fault, so flagging it would make the
    signal useless by firing on every root in the fleet. The check must read "no code was
    attempted", not "the store is a root".
    """
    from rag_search.core.registry import remove_project

    proj = str(safe_tmp_path)
    try:
        member = _fedroot_status(proj, [("README.md", "markdown"), ("LICENSE", "text")])
        assert member, "GH7a: the root did not appear among its own federation members"
        assert member.get("symbols") == 0, f"GH7a: fixture is not symbol-free: {member}"
        assert member.get("symbol_hollow") is False, (
            "GH7a: a root whose only files are docs was flagged hollow — every container "
            f"directory in the fleet would alarm: {member}")
    finally:
        remove_project(proj)


def test_gh7b_a_root_that_extracted_none_of_its_own_code_is_hollow(safe_tmp_path):
    """GH7b — the half that was wrong, fixed. Red before H1.

    Same store, same zero symbols, same communities; the only difference is that the attempted
    files are code. Under the old categorical `and not _is_fedroot` this was reported healthy —
    so the one store in the fleet where "0 symbols" is genuinely ambiguous was the one store that
    could never say so, and a root whose whole extraction lane had broken looked exactly like a
    root that is a directory of members. `file_extraction` is what makes the two separable, which
    is why this guard could not have been written before W1.
    """
    from rag_search.core.registry import remove_project

    proj = str(safe_tmp_path)
    try:
        member = _fedroot_status(proj, [("main.go", "go"), ("util.go", "go")])
        assert member, "GH7b: the root did not appear among its own federation members"
        assert member.get("symbol_hollow") is True, (
            "GH7b: a federation root attempted two Go files, extracted nothing from either, and "
            f"still reported healthy — this is the categorical exemption H1 replaced: {member}")
    finally:
        remove_project(proj)
