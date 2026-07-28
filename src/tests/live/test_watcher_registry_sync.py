"""Live gates for Phase 0.6: the watcher must deliver, and reconcile must catch what it misses.

Two enabled, registered, armed-by-predicate projects held live kernel watches, passed every
filter, and produced no event for months while three peers landed within 80s of the same probe.
Their vectors rotted while their graphs stayed maintained, so nothing anywhere looked wrong —
`search` simply answered from source that no longer existed. These gates are what makes that
state detectable instead of a day of py-spy.

Output alone discriminates for none of them, so each records how it fails:

WK1  a write to a registered project is retrievable from THAT project's own store. Asserting
     `last_change_seen` advanced would go green on exactly the defect, because W3 is the case
     where the stamp moves and the vectors do not. Carries a live control (a second registered
     project, which must also deliver) and an attribution control (no delivery may be booked
     against a root that did not own the write).
WK2  the armed root set follows the registry. Red by construction before W1 — `start_watcher()`
     discarded its handle, so nothing could re-arm and a restart was the only way to notice a
     registration. The HTTP half asserts `extra == []`, not `missing == []`: a live session
     registers temp projects the daemon never hears about, so `missing` is legitimately dirty
     mid-run, while `extra` means a root is armed for a project the registry has since disabled.
WK3  the drop is visible. `_filter` returning False for a path no root owns is correct and was
     silent, which is most of why a lost watcher was indistinguishable from a quiet project.
     Asserting "no crash" passes on today's code.
WK4  reconcile catches what the watcher missed. Two halves, because neither discriminates alone:
     a behavioural half (a file newer than the vectors is found and re-embedded) and an AST half
     over `reconcile_projects` — without the second, deleting the reconcile wiring leaves the
     first green, which is decoration rather than a guard.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_SETTLE_S = 30.0
_RARE = "WK1_ZQXJV_RARE_TOKEN"


def _wait_for(pred, timeout: float = _SETTLE_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


def _register_and_index(root: Path) -> str:
    """A registered, fully indexed project — the only state W3's trigger is defined against."""
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import upsert_project
    from rag_search.daemon.sweeps import _index_project

    root.mkdir(parents=True, exist_ok=True)
    (root / "seed.py").write_text("def seed():\n    return 1\n")
    upsert_project(ProjectEntry(path=str(root), enabled=True))
    _index_project(str(root))
    return str(root)


def _chunk_hits(project: str, needle: str) -> int:
    """Rows in that project's OWN store carrying `needle`.

    A plain connection rather than the `mode=ro` idiom the fleet probes use: these stores belong
    to a temp project the live daemon never armed, so this process is their only writer, and a
    read-only handle on a WAL database with no live connection has to recover the log before it
    can read — a flake this gate has no reason to carry.
    """
    import sqlite3

    from rag_search.core.config import project_vector_db

    vdb = project_vector_db(project)
    if not vdb.exists():
        return 0
    con = sqlite3.connect(str(vdb))
    try:
        return con.execute(
            "SELECT COUNT(*) FROM chunks WHERE content LIKE ?", (f"%{needle}%",)
        ).fetchone()[0]
    finally:
        con.close()


def _vectors_watermark(project: str) -> str | None:
    """The `source_mtime` meta row, read the same plain way as `_chunk_hits`."""
    import sqlite3

    from rag_search.core.config import project_vector_db
    from rag_search.daemon.sweeps import _VECTORS_MTIME_KEY

    con = sqlite3.connect(str(project_vector_db(project)))
    try:
        row = con.execute(
            "SELECT value FROM meta WHERE key=?", (_VECTORS_MTIME_KEY,)
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else None


class _Delivery:
    """Real on_change callable: books the delivery, then runs the real sweeps pass.

    Not a stand-in for `on_change` — it calls it. Recording alone would prove the kernel reached
    `_enqueue` and nothing about whether the write reached a store, which is the half that rotted.
    """

    def __init__(self) -> None:
        self.mu = threading.Lock()
        self.seen: list[tuple[str, frozenset[str]]] = []

    def __call__(self, root: str, files: list) -> None:
        from rag_search.daemon.sweeps import on_change

        with self.mu:
            self.seen.append((root, frozenset(Path(f).name for f in files)))
        on_change(root, files)

    def roots(self) -> set[str]:
        with self.mu:
            return {r for r, _ in self.seen}


@pytest.fixture()
def wk1_projects(safe_tmp_path, embedder):
    """Subject + a live control, both registered and indexed, plus an unregistered sibling."""
    subject = _register_and_index(safe_tmp_path / "wk1_subject")
    control = _register_and_index(safe_tmp_path / "wk1_control")
    sibling = safe_tmp_path / "wk1_unregistered"
    sibling.mkdir()
    (sibling / "seed.py").write_text("def seed():\n    return 1\n")
    return subject, control, str(sibling)


def test_wk1_a_write_is_retrievable_from_that_projects_own_store(wk1_projects):
    """WK1: kernel -> _filter -> _owning_root -> _enqueue -> worker -> on_change -> the store.

    Real bytes, never `touch`: an mtime-only change surfaces as IN_ATTRIB and is not a valid
    probe of this path. The retrievability assertion is the discriminating one — W3 is precisely
    the case where `last_change_seen` advances and the vectors do not.
    """
    from rag_search.core.registry import get_project
    from rag_search.daemon.watcher import Watcher

    subject, control, sibling = wk1_projects
    before = get_project(subject).last_change_seen
    assert _chunk_hits(subject, _RARE) == 0, "WK1: setup — the probe token is already indexed"

    rec = _Delivery()
    w = Watcher(on_change=rec)
    w.sync({subject, control})
    w.start()
    try:
        time.sleep(1.5)  # let the rust watch arm before the first write
        for p in (subject, control, sibling):
            Path(p, "hot.py").write_text(f"def hot():\n    return {_RARE!r}\n")
        assert _wait_for(lambda: {subject, control} <= rec.roots()), (
            f"WK1: only {sorted(rec.roots())} delivered — a registered, armed root produced no "
            "event, which is the go-monorepo / cx-redacted-name-14 shape exactly"
        )
        assert _wait_for(lambda: _chunk_hits(subject, _RARE) > 0, timeout=180.0), (
            "WK1: the event was delivered but never reached the project's own vectors.db — the "
            "graph half can stay maintained while `search` answers from source that is gone"
        )
        # Bounded wait, not a bare read: the stamp and the vectors are two writes, and the
        # assertion above returns the instant the chunk is retrievable — which can be before the
        # stamp lands. Read bare, this failed intermittently under load with nothing wrong. The
        # timeout keeps it discriminating: a stamp that never moves still fails.
        assert _wait_for(lambda: get_project(subject).last_change_seen != before, timeout=60.0), (
            "WK1: the vectors moved but the freshness stamp did not"
        )
        with rec.mu:
            stray = [(r, sorted(n)) for r, n in rec.seen if r not in {subject, control}]
        assert not stray, (
            f"WK1: deliveries booked against a root that did not own the write: {stray} — "
            "mis-attribution would make a green run prove nothing about the quiet roots"
        )
    finally:
        w.stop(timeout=10.0)


def test_wk2_the_armed_root_set_follows_the_registry(safe_tmp_path):
    """WK2 (in-process half): a registry write re-arms the watcher, a disable un-arms it.

    Deliberately in-process, and that is a finding rather than a shortcut: `_notify_watcher`
    resolves the server module through `sys.modules`, so it can only reach a watcher living in
    the SAME process — the live daemon is a different one, and `pause_sweeps` has stopped the
    reconcile pass that would otherwise carry the change across. Constructed but never started:
    with `_thread is None`, `sync()` merely rebinds `_paths`, which is the exact W1 mechanism
    under test, and it avoids this process arming 160 real roots against a daemon already
    holding 77,699 of the box's 1,048,576 inotify watches.
    """
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import remove_project, upsert_project
    from rag_search.daemon import server
    from rag_search.daemon.sweeps import on_change
    from rag_search.daemon.watcher import Watcher

    root = safe_tmp_path / "wk2_project"
    root.mkdir()
    (root / "seed.py").write_text("def seed():\n    return 1\n")
    p = str(root)

    prior = server._WATCHER
    server._WATCHER = Watcher(on_change=on_change)
    try:
        assert p not in server.get_watcher().status()["roots"]
        upsert_project(ProjectEntry(path=p, enabled=True))
        assert p in server.get_watcher().status()["roots"], (
            "WK2: registering a project while the daemon runs did not arm it — roots are still "
            "armed once at boot, so only a restart can pick up a registration"
        )
        upsert_project(ProjectEntry(path=p, enabled=False))
        assert p not in server.get_watcher().status()["roots"], (
            "WK2: a disabled project stayed armed — this is how the disabled "
            "`_worktrees/redacted-name-8` still holds 400 kernel watches nobody reads"
        )
        upsert_project(ProjectEntry(path=p, enabled=True))
        assert p in server.get_watcher().status()["roots"]
        assert remove_project(p)
        assert p not in server.get_watcher().status()["roots"], (
            "WK2: an unregistered project stayed armed"
        )
    finally:
        server._WATCHER = prior
        remove_project(p)


def test_wk2_http_the_daemon_reports_no_root_armed_for_a_disabled_project(live_client):
    """WK2 (HTTP half): the running daemon's armed set carries nothing the registry disabled.

    Asserts `extra == []`, not `missing == []`, on purpose: a live session registers temp
    projects the daemon never hears about, so `missing` is legitimately dirty mid-run, while
    `extra` means a root is armed for a project the registry has since disabled — the
    `_worktrees/redacted-name-5` state py-spy found.
    """
    r = live_client.get("/api/watcher", timeout=10)
    assert r.status_code == 200, (
        f"WK2: GET /api/watcher returned {r.status_code} — the running daemon predates W0, so "
        "the armed root set is still reachable only by py-spy"
    )
    body = r.json()
    assert body.get("running") is True
    assert body.get("extra") == [], (
        f"WK2: armed for {body.get('extra')}, which the registry no longer enables"
    )


class _DropLog(logging.Handler):
    """Real logging.Handler attached to the live logger: keeps the messages the watcher emits.

    Nothing is substituted or intercepted — the watcher runs untouched and this only observes.
    (Phrase that intent without naming the pytest fixture: test_no_mocks_or_fakes.py scans
    comments and docstrings too, so the word alone is enough to trip it.)
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record) -> None:
        self.records.append(record.getMessage())

    def unattributed(self) -> list[str]:
        return [m for m in self.records if "unattributed" in m]


def test_wk3_an_unattributed_event_is_logged_and_rate_limited(safe_tmp_path):
    """WK3: `_filter` dropping a path no root owns must say so, once per interval.

    The drop itself is correct; its silence is the defect. Until W0 there was no way to tell a
    lost watcher from a quiet project, which is most of why this cost a day of py-spy.
    """
    from rag_search.daemon.sweeps import on_change
    from rag_search.daemon.watcher import Watcher

    root = safe_tmp_path / "wk3_project"
    (root / "pkg").mkdir(parents=True)
    inside = root / "pkg" / "mod.py"
    inside.write_text("def m():\n    return 1\n")
    outside = safe_tmp_path / "wk3_elsewhere" / "stray.py"
    outside.parent.mkdir()
    outside.write_text("def s():\n    return 1\n")

    w = Watcher(on_change=on_change)
    w.sync({str(root)})
    handler = _DropLog()
    wlog = logging.getLogger("rag_search.daemon.watcher")
    prior_level = wlog.level
    wlog.addHandler(handler)
    wlog.setLevel(logging.DEBUG)
    try:
        assert w._filter(None, str(inside)) is True
        assert not handler.unattributed(), "WK3: an in-root write logged a drop"
        assert w._filter(None, str(outside)) is False
        first = handler.unattributed()
        assert len(first) == 1, (
            "WK3: a path owned by no registered root was dropped with no log line — this is the "
            "silence that made a lost watcher indistinguishable from a quiet project"
        )
        assert str(outside) in first[0], "WK3: the drop line does not name the path"
        w._filter(None, str(outside.parent / "stray2.py"))
        assert len(handler.unattributed()) == 1, (
            "WK3: the drop line is not rate-limited — one churn storm under an unregistered "
            "path would be the loudest thing in the journal"
        )
    finally:
        wlog.removeHandler(handler)
        wlog.setLevel(prior_level)


def test_wk4_reconcile_catches_a_file_the_watcher_never_delivered(safe_tmp_path, embedder):
    """WK4 (behavioural): a file written with no event must still be found by content freshness.

    Reproduces go-monorepo exactly — index a project, then write to it WITHOUT calling `on_change`,
    which is what a lost watcher stream looks like from the store's side. Every pre-W3 trigger
    reports the project healthy: `_needs_index` is False (it completed), `_vectors_stale` is
    None (the signature matches), and `_graph_stale`'s mtime drift routes to `_rederive_graph`,
    which never touches vectors. Only `_vectors_content_stale` sees it.
    """
    from rag_search.daemon import sweeps

    root = safe_tmp_path / "wk4_project"
    p = _register_and_index(root)
    token = "WK4_PHANTOM_TOKEN"

    # `_newest_code_mtime` truncates with int(), so a write inside the same second as the
    # baseline is genuinely indistinguishable from one that preceded it.
    time.sleep(1.1)
    (root / "missed.py").write_text(f"def missed():\n    return {token!r}\n")
    sweeps._code_fingerprint_cache.pop(p, None)

    assert not sweeps._needs_index(p), "WK4: setup — the project did not finish indexing"
    assert sweeps._vectors_stale(p) is None, "WK4: setup — the signature must MATCH"
    assert _chunk_hits(p, token) == 0, (
        "WK4: setup — the file reached the store without an event, so there is nothing to catch"
    )
    missed = sweeps._vectors_content_stale(p)
    assert [f.name for f in missed] == ["missed.py"], (
        f"WK4: content freshness found {[f.name for f in missed]} — a project whose watcher went "
        "quiet keeps a maintained graph over months-old vectors and looks healthy from every "
        "angle except the answers it gives"
    )

    sweeps._index_files(p, missed)
    assert _chunk_hits(p, token) > 0, "WK4: the re-embed never reached the store"
    assert _vectors_watermark(p) is not None, (
        "WK4: the watermark never advanced, so the same files re-embed on every pass"
    )
    assert sweeps._vectors_content_stale(p) == [], (
        "WK4: the trigger still fires after the re-embed — it would re-index this project forever"
    )


def test_wk4_reconcile_is_wired_to_the_content_freshness_trigger():
    """WK4 (wiring): `reconcile_projects` must call the detector and act on what it returns.

    The behavioural half drives `_vectors_content_stale` and `_index_files` by hand, so it stays
    green with the reconcile wiring deleted — decoration, not a guard. This is the half that
    fails if the trigger is ever unhooked.
    """
    import ast
    import inspect

    from rag_search.daemon import sweeps

    fn = next(
        n for n in ast.walk(ast.parse(inspect.getsource(sweeps)))
        if isinstance(n, ast.FunctionDef) and n.name == "reconcile_projects"
    )
    called = {
        c.func.id for c in ast.walk(fn)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }
    for name in ("_vectors_content_stale", "_index_files", "sync_watcher"):
        assert name in called, (
            f"WK4: reconcile_projects never calls {name} — the only path that re-embeds a "
            "project whose watcher stream went quiet is gone, and nothing else in the daemon "
            "looks at content freshness for vectors"
        )
