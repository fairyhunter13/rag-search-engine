"""Live proof gates: the watcher must re-derive the code graph, incrementally (no mocks).

on_change rebuilt vectors and re-narrated the existing skeleton but never re-extracted symbols
or edges, so a function written after the last full index was searchable and yet invisible to
`graph` and to the structural half of `ask`. Both repos in daily use on this box were provably
stale by the engine's own `source_sig` criterion when this was found.

Output alone barely discriminates here: a full `_rederive_graph` hung off on_change produces the
same rows as an incremental pass for an edit the watcher reported, and the vector index updates
either way. WG6 therefore changes a file *without* reporting it, and WG5 asserts subtraction plus
survival of an untouched file's edges — the things an upsert-only, a clear()-and-rebuild, or a
walk-the-whole-repo implementation cannot satisfy.

WG1  a new symbol must reach graph.db through on_change alone
WG2  docs-only churn must NOT re-extract (HR38 must survive)
WG3  a federation root must NOT re-extract (HR4 carve-out)
WG4  on_change must not deadlock on _KB_HEAVY_LOCK when it takes the extraction branch
WG5  a deleted symbol disappears; an untouched file keeps its symbols AND its cross-file edges
WG6  a changed file the watcher did NOT report must not be parsed (no whole-repo walk)
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.live


def _open(proj: str):
    from rag_search.core.config import project_graph_db
    from rag_search.graph.store import GraphStore
    return GraphStore(project_graph_db(proj))


def _syms(proj: str) -> set[str]:
    gs = _open(proj)
    try:
        return {r[0] for r in gs._con.execute("SELECT name FROM symbols")}
    finally:
        gs.close()


def _named_edges(proj: str) -> set[tuple[str, str]]:
    gs = _open(proj)
    try:
        # The store sets a sqlite3.Row factory, which never compares equal to a plain tuple.
        return {(r[0], r[1]) for r in gs._con.execute(
            "SELECT c.name, e.name FROM edges JOIN symbols c ON c.sid=edges.caller_sid "
            "JOIN symbols e ON e.sid=edges.callee_sid"
        )}
    finally:
        gs.close()


def _meta(proj: str, key: str) -> str | None:
    gs = _open(proj)
    try:
        return gs.get_meta(key)
    finally:
        gs.close()


def _fire(proj: str, files: list) -> None:
    """Drive the real on_change with the debounce and the reuse stamp cleared.

    Clearing `_last_enriched_sig` deliberately makes WG2 harder: without it a docs-only change
    would return at the HR38 gate and the test would pass without ever reaching the new code.
    """
    from rag_search.daemon import sweeps
    sweeps._last_kb_enrich.pop(proj, None)
    sweeps._last_enriched_sig.pop(proj, None)
    sweeps._last_index_fail.pop(proj, None)
    sweeps.on_change(proj, [str(f) for f in files])


def test_wg1_new_symbol_reaches_the_graph(safe_tmp_path):
    """WG1: a function written after the last full index must be in graph.db after on_change.

    Red against the unmodified on_change, which only re-embedded vectors.
    """
    from rag_search.daemon.sweeps import _index_project

    proj = str(safe_tmp_path)
    src = safe_tmp_path / "mod.py"
    src.write_text("def wg_seed():\n    return 1\n")
    _index_project(proj)
    assert "wg_seed" in _syms(proj), "WG1: baseline index must contain the seed symbol"

    time.sleep(1.1)  # the code sig truncates mtimes to whole seconds
    src.write_text("def wg_seed():\n    return 1\n\n\ndef wg_zzq_added():\n    return 2\n")
    _fire(proj, [src])
    assert "wg_zzq_added" in _syms(proj), (
        "WG1: on_change must re-extract the changed file — the graph is stale without it"
    )


def test_wg2_docs_only_churn_does_not_re_extract(safe_tmp_path):
    """WG2: HR38 must survive — non-code churn must not wake tree-sitter extraction."""
    from rag_search.daemon.sweeps import _index_project

    proj = str(safe_tmp_path)
    (safe_tmp_path / "mod.py").write_text("def wg_seed():\n    return 1\n")
    _index_project(proj)
    before = (_syms(proj), _meta(proj, "source_sig"), _meta(proj, "incremental_since_full"))

    time.sleep(1.1)
    notes = safe_tmp_path / "notes.md"
    notes.write_text("# just documentation\n")
    _fire(proj, [notes])
    assert _syms(proj) == before[0], "WG2: a docs edit must not change the symbol table"
    assert _meta(proj, "source_sig") == before[1], "WG2: the code sig must not have moved"
    assert _meta(proj, "incremental_since_full") == before[2], (
        "WG2: a docs edit must not consume an incremental pass"
    )


def _forget_sig(proj: str) -> None:
    """Drop the fingerprint memo, exactly as on_change does before it judges drift.

    Rewriting a file in place leaves the root dir mtime alone, and that mtime is the memo's
    coarse pre-gate — without this a direct _graph_needs_update call reads a stale sig and
    reports "fresh" for a project that just changed.
    """
    from rag_search.daemon import sweeps
    sweeps._code_fingerprint_cache.pop(proj, None)
    sweeps._fingerprint_cache.pop(proj, None)


def test_wg3_federation_root_is_not_the_watchers_job(safe_tmp_path):
    """WG3: HR4 — a federation root has 0 own communities by design, so it never re-extracts.

    Paired against a plain project under identical drift: without the control assertion this
    would also pass against a _graph_needs_update that simply always answered False.
    """
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import upsert_project
    from rag_search.daemon.sweeps import _graph_needs_update, _index_project

    root, member = safe_tmp_path / "root", safe_tmp_path / "member"
    root.mkdir()
    member.mkdir()
    (root / "main.py").write_text("def wg_root():\n    return 1\n")
    (member / "svc.py").write_text("def wg_member():\n    return 1\n")
    _index_project(str(root))
    _index_project(str(member))
    upsert_project(ProjectEntry(path=str(root), enabled=True, federation=[str(member)]))
    upsert_project(ProjectEntry(path=str(member), enabled=True))

    time.sleep(1.1)
    (root / "main.py").write_text("def wg_root():\n    return 2\n")
    (member / "svc.py").write_text("def wg_member():\n    return 2\n")
    _forget_sig(str(root))
    _forget_sig(str(member))
    assert _graph_needs_update(str(member)) is True, (
        "WG3 control: real code drift on a plain project must need an update"
    )
    assert _graph_needs_update(str(root)) is False, (
        "WG3: a federation root must be left to reconcile (HR4 carve-out)"
    )


def test_wg4_extraction_branch_does_not_deadlock(safe_tmp_path):
    """WG4: on_change takes _KB_HEAVY_LOCK for extraction, and _enrich_project takes it again.

    It is a plain threading.Lock, so holding it across the _enrich_project call hangs the
    watcher thread outright — no error, no log line, no more indexing ever. A watchdog is the
    only way to observe that: a deadlocked on_change never returns to fail an assertion.
    """
    import threading

    from rag_search.daemon.sweeps import _index_project

    proj = str(safe_tmp_path)
    src = safe_tmp_path / "mod.py"
    src.write_text("def wg_seed():\n    return 1\n")
    _index_project(proj)
    time.sleep(1.1)
    src.write_text("def wg_seed():\n    return 1\n\n\ndef wg_second():\n    return 2\n")

    done = threading.Event()

    def _run() -> None:
        try:
            _fire(proj, [src])
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    assert done.wait(timeout=180), "WG4: on_change never returned — _KB_HEAVY_LOCK deadlock"
    assert "wg_second" in _syms(proj), (
        "WG4: the run must actually have taken the lock-holding extraction branch"
    )


def test_wg5_deleted_symbol_goes_and_untouched_edges_survive(safe_tmp_path):
    """WG5: subtraction, and the boundary of "incremental".

    Three implementations pass a "the new symbol is there" test and fail here: upsert-only
    (wg_doomed lingers), clear()-and-rebuild-changed-files (b.py loses its symbols), and
    deleting both directions of a changed file's edges (b.py -> a.py silently vanishes even
    though nothing about b.py changed).
    """
    from rag_search.daemon.sweeps import _index_project

    proj = str(safe_tmp_path)
    a, b = safe_tmp_path / "a.py", safe_tmp_path / "b.py"
    a.write_text("def wg_alpha():\n    return 1\n\n\ndef wg_doomed():\n    return 2\n")
    b.write_text("from a import wg_alpha\n\n\ndef wg_beta():\n    return wg_alpha()\n")
    _index_project(proj)
    assert {"wg_alpha", "wg_doomed", "wg_beta"} <= _syms(proj), "WG5 baseline: symbols missing"
    assert ("wg_beta", "wg_alpha") in _named_edges(proj), "WG5 baseline: cross-file edge missing"

    time.sleep(1.1)
    a.write_text("def wg_alpha():\n    return 1\n")  # wg_doomed deleted; wg_alpha unmoved
    _fire(proj, [a])
    syms, edges = _syms(proj), _named_edges(proj)
    assert "wg_doomed" not in syms, "WG5: upsert never subtracts — the row must be deleted"
    assert {"wg_alpha", "wg_beta"} <= syms, "WG5: an untouched file must keep its symbols"
    assert ("wg_beta", "wg_alpha") in edges, (
        "WG5: b.py was not re-scanned, so its edge into a.py must survive the pass"
    )


def test_wg6_only_the_handed_over_files_are_re_extracted(safe_tmp_path):
    """WG6: the pass must parse the watcher's files, not walk the repo again.

    Extraction is single-worker-bound (HR40) and scales with repo size, not edit size — measured
    on this box, a full re-derive costs 134s on a 7.7k-file repo and 188s on a 17k-file one,
    which a 45s watcher debounce cannot absorb. Both paths produce identical rows for a
    single-file edit, so an `only=None` slipping back in is invisible to every other gate here.

    The first version of this asserted a timing ratio (measured floor 6.6x on an idle box). It
    failed at 2.6x inside the full live suite — both numbers inflate under load, but not
    proportionally, because detect_communities is a fixed cost *both* passes pay. The ghost file
    below tests the same property deterministically: it is changed on disk but deliberately not
    handed to the pass, so its new symbol can only appear if the repo was walked.
    """
    from rag_search.daemon.sweeps import _rederive_graph, _update_graph_files

    proj = str(safe_tmp_path)
    for i in range(200):
        (safe_tmp_path / f"m{i}.py").write_text(f"def wg_f{i}():\n    return {i}\n")
    _rederive_graph(proj)

    time.sleep(1.1)
    target, ghost = safe_tmp_path / "m7.py", safe_tmp_path / "m50.py"
    target.write_text("def wg_f7():\n    return 7\n\n\ndef wg_f7_extra():\n    return 0\n")
    ghost.write_text("def wg_f50():\n    return 50\n\n\ndef wg_ghost_unhandled():\n    return 0\n")
    _forget_sig(proj)
    _update_graph_files(proj, [target])  # ghost deliberately withheld

    syms = _syms(proj)
    assert "wg_f7_extra" in syms, "WG6: the handed-over file must have been re-extracted"
    assert "wg_ghost_unhandled" not in syms, (
        "WG6: a file the watcher did not report was parsed anyway — the pass is walking the "
        "whole repo, which costs ~190s on a 17k-file repo inside a 45s debounce window"
    )
