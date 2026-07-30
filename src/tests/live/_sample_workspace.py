"""Session-scoped sample workspace builder for the live test suite.

Materializes committed fixture trees into a temporary registry workspace,
indexes with GPU, labels the communities the way the daemon now does, then
tears down cleanly.

The labelling step used to replay four committed enrichment.json goldens —
frozen DeepSeek narrations, so the suite never called the API. Tier 3's
deletion removed both the narrator and the goldens, and structural labelling
is not a stand-in for them: it is what the daemon writes now, so building the
workspace with `_label_project` makes the fixture match production instead of
preserving a shape nothing produces any more.

Usage:
    from tests.live._sample_workspace import SampleWorkspace, build_sample_workspace, teardown_sample_workspace
"""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from rag_search.core.config import ProjectEntry
from rag_search.core.registry import remove_project, upsert_project

_REPO_ROOT = Path(__file__).parents[3]
_FIXTURES = _REPO_ROOT / "src" / "tests" / "fixtures" / "sample_projects"
_SHOP_SRC = _FIXTURES / "shop-federation"
_LEDGER_SRC = _FIXTURES / "ledger-standalone"
_MEMBERS = ["cart-svc", "checkout-svc", "promo-svc"]
_SAFE_BASE = Path.home() / ".local" / "share" / "rse-test-dirs"


@dataclass
class SampleWorkspace:
    base: Path
    fed_root: str
    cart: str
    checkout: str
    promo: str
    ledger: str

    @property
    def member_paths(self) -> list[str]:
        return [self.cart, self.checkout, self.promo]


def _copy_fixtures(base: Path) -> tuple[Path, Path]:
    fed_base = base / "shop-federation"
    shutil.copytree(_SHOP_SRC, fed_base)
    ledger_dir = base / "ledger-standalone"
    shutil.copytree(_LEDGER_SRC, ledger_dir)
    return fed_base, ledger_dir


def _register(fed_base: Path, ledger_dir: Path) -> tuple[str, list[str], str]:
    member_paths = [str(fed_base / m) for m in _MEMBERS]
    fed_root = str(fed_base)
    upsert_project(ProjectEntry(path=fed_root, enabled=True, federation=member_paths))
    for m in member_paths:
        upsert_project(ProjectEntry(path=m, enabled=True))
    ledger = str(ledger_dir)
    upsert_project(ProjectEntry(path=ledger, enabled=True))
    return fed_root, member_paths, ledger


def label_member(member_path: str) -> None:
    """Re-label a sample member's communities (idempotent self-heal).

    Replaces replay_member_golden, which re-applied a frozen DeepSeek narration after an
    in-process re-index cleared it. _label_project only touches communities whose summary is
    NULL or empty, so calling it twice is free.
    """
    from rag_search.daemon.sweeps import _label_project
    _label_project(member_path)


def _index_members(paths: list[str]) -> None:
    from rag_search.daemon.sweeps import _index_project
    for p in paths:
        _index_project(p)


# _replay_golden and its _live_sig helper went with the goldens. They existed to re-key a frozen
# narration onto whatever community ids Leiden happened to produce this run — matching on the
# member signature when the ids drifted. Structural labelling reads the live graph directly, so
# there is nothing left to re-key.


def assert_under_test_base(path: str) -> None:
    """Refuse to touch anything outside the suite's own base. Checked here, not by the caller.

    Not hypothetical. A red demo of `purge_rows_under` ran a deliberately-broken predicate
    in-process — through the session fixture that calls it against the *real* registry — and deleted
    198 fleet rows and 138 stores, an hour of GPU embedding each, with no backup and no filesystem
    snapshot to go back to. The predicate was wrong for about thirty seconds and the loss is
    permanent.

    So the destructive step re-derives authority from the path itself instead of trusting the loop
    that selected it, and *raises* rather than skipping: a caller that has gone wrong must fail
    loudly, not quietly do less than it believes it is doing. What makes "under the base" sufficient
    authorisation is `test_no_real_project_in_tests.py`, the standing invariant that nothing real
    may live there.
    """
    p = str(path)
    if p != str(_SAFE_BASE) and not p.startswith(str(_SAFE_BASE) + os.sep):
        raise AssertionError(
            f"refusing to purge {p!r}: it is outside the test base {_SAFE_BASE}. Every registry row "
            "and index store this suite may delete lives under that base; a path from anywhere else "
            "belongs to a real project whose store costs an hour of GPU to rebuild. Fix the caller "
            "that selected this path — do not relax this check."
        )


def purge_project(path: str) -> None:
    """Deregister a test project AND delete the index dir it created.

    `remove_project` only drops the registry row. The store lives at
    INDEX_ROOT/<slug>-<sha16 of the absolute path>, and every run builds its workspace under a
    fresh `tempfile.mkdtemp`, so the moment the row is gone the previous run's dirs are
    unreachable by name forever — one day's runs left 19 dirs / 62 MB. `sweeps.maintenance()`
    does vacuum them, but only after a daemon restart and only when sweeps are not paused, so
    relying on it made three mechanisms lean on each other with none of them owned by the run
    that made the mess. The sweep stays as the backstop for dirs whose name nobody remembers.
    """
    from rag_search.core.config import index_dir
    assert_under_test_base(path)
    with contextlib.suppress(Exception):
        remove_project(path)
    shutil.rmtree(index_dir(path), ignore_errors=True)


def purge_index_dirs_under(base: Path) -> None:
    """Delete the index dir of `base` and of every real directory beneath it.

    Complements `purge_project`, which needs a registry row to know the path. Most tests remove
    their own rows in a `finally` before the fixture that owns the tree tears down, so by then
    the row — the only handle on the `<slug>-<sha16>` name — is gone. Walking the tree instead
    finds those stores while the paths still exist. Call before deleting the tree.

    `followlinks=False` and the prefix check are load-bearing, not defensive: the sample
    workspace is a *symlink* federation, and following one would compute the index dir of a real
    project outside the workspace and delete a live store — the fleet-deleting shape that
    `clean-orphans` already learned once.
    """
    import os as _os

    from rag_search.core.config import index_dir
    assert_under_test_base(base)
    base_str = str(base)
    if not base.is_dir():
        return
    candidates = [base_str]
    for dirpath, dirnames, _ in _os.walk(base_str, followlinks=False):
        dirnames[:] = [d for d in dirnames if not Path(dirpath, d).is_symlink()]
        candidates.extend(str(Path(dirpath) / d) for d in dirnames)
    for path in candidates:
        if path != base_str and not path.startswith(base_str + "/"):
            continue  # unreachable by construction; the store deleted here is unrecoverable
        d = index_dir(path)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


def purge_rows_under(base: Path) -> list[str]:
    """Drop every registry row pointing into `base`, with the store each one owns.

    Called at both ends of the session, because the daemon puts rows back: federation discovery
    upserts every member it finds under a registered root, so a row this suite deleted at 14:00 can
    exist again at 14:05. `purge_unowned_index_dirs_created_since` spares anything a row owns, so
    five stores per run survived it — the row was restored by the time the diff ran, and the daemon
    dropped it again minutes later once reconcile noticed the tree was gone, leaving a dir nothing
    owns and nobody remembers. A row under the suite's own base is never a legitimate owner:
    `test_no_real_project_in_tests.py` is the invariant that nothing real may live there.
    """
    import os as _os

    from rag_search.core.registry import list_projects
    removed: list[str] = []
    base_str = str(base)
    for e in list_projects():
        if e.path == base_str or e.path.startswith(base_str + _os.sep):
            purge_project(e.path)  # row *and* store: the row is the only handle on the dir name
            removed.append(e.path)
    return removed


def index_dir_names() -> set[str]:
    """Every dir name under INDEX_ROOT right now — the run's before-picture."""
    from rag_search.core.config import INDEX_ROOT
    return {d.name for d in INDEX_ROOT.iterdir() if d.is_dir()} if INDEX_ROOT.exists() else set()


def purge_unowned_index_dirs_created_since(before: set[str]) -> list[str]:
    """Delete index dirs that appeared during this run and that no registry row owns.

    The two paths above need the path: `purge_project` needs a registry row to derive the dir name,
    `purge_index_dirs_under` needs the source tree to still be walkable. Some stores are written
    after both are gone — a graph-lane pass joined by `_drain_graph_lane` recreates a store for a
    path deleted seconds earlier, which is why one measured run left a dir holding `graph.db` and no
    `vectors.db` at all. Nothing that knows the path is still alive at that point, so the last
    backstop has to work from the directory listing.

    Both conditions are load-bearing and neither is defensive. "Appeared since the snapshot" spares
    every pre-existing fleet store — 139 of them, each an hour of GPU re-embedding — and "no registry
    row" spares a fleet project indexed for the first time *during* the run. `clean-orphans` once
    matched a registry path against a dir name, which can never be equal, and so reported all 179
    dirs on this fleet as orphans; the same mistake here would run without a `--yes` to stop it.
    """
    from rag_search.core.config import INDEX_ROOT, index_dir
    from rag_search.core.registry import list_projects
    if not INDEX_ROOT.exists():
        return []
    owned = {index_dir(e.path).name for e in list_projects()}
    removed: list[str] = []
    for d in sorted(INDEX_ROOT.iterdir()):
        if not d.is_dir() or d.name in before or d.name in owned:
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d.name)
    return removed


def _deregister_under(base: Path) -> None:
    """Remove all registry entries whose path is under base (enabled or not), stores included."""
    from rag_search.core.registry import list_projects
    prefix = str(base) + "/"
    for e in list_projects():
        if e.path.startswith(prefix) or e.path == str(base):
            purge_project(e.path)


def _cleanup_stale_workspaces(keep: Path) -> None:
    """Remove leftover sample-ws-* dirs and their registry entries from crashed sessions."""
    for d in _SAFE_BASE.glob("sample-ws-*"):
        if d != keep:
            _deregister_under(d)
            purge_index_dirs_under(d)
            shutil.rmtree(d, ignore_errors=True)
    # Also clear any stale rse-test-dirs entries whose filesystem path no longer exists.
    from rag_search.core.registry import list_projects
    for e in list_projects():
        if str(_SAFE_BASE) in e.path and not Path(e.path).exists():
            purge_project(e.path)


def build_sample_workspace() -> SampleWorkspace:
    # The `no_deepseek()` context manager that used to wrap this whole body is gone with the client
    # it suppressed. Nothing here reaches a network at all now, so there is no lane left to close.
    _SAFE_BASE.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(dir=_SAFE_BASE, prefix="sample-ws-"))
    _cleanup_stale_workspaces(base)
    fed_base, ledger_dir = _copy_fixtures(base)
    fed_root, member_paths, ledger = _register(fed_base, ledger_dir)
    _index_members([fed_root, *member_paths, ledger])
    for p in [*member_paths, ledger]:
        label_member(p)
    # reconstruct_processes (BPRE) and build_federated_index (wiki) ran here; both left with tier 3.
    return SampleWorkspace(
        base=base,
        fed_root=fed_root,
        cart=member_paths[0],
        checkout=member_paths[1],
        promo=member_paths[2],
        ledger=ledger,
    )


def teardown_sample_workspace(ws: SampleWorkspace) -> None:
    for p in [ws.fed_root, *ws.member_paths, ws.ledger]:
        purge_project(p)
    purge_index_dirs_under(ws.base)  # stores for paths a test registered and deregistered itself
    shutil.rmtree(ws.base, ignore_errors=True)
