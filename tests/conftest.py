"""Test isolation.

`isolated_state` is autouse and has no opt-out. A test that ran in-process
against the real registry is what destroyed the fleet's 236 rows once already,
and the version of that test looked entirely harmless -- it only *read*, until
a helper it called wrote back a pruned copy. Redirecting the paths for every
test costs nothing and removes the whole class.

It protects the in-process half only. A `live` test calls the running daemon,
and the daemon holds the real registry -- which `fleet_unchanged` is for.
"""

from __future__ import annotations

import subprocess
import warnings
from pathlib import Path

import pytest
from mcp_types import ListRootsResult, Root

from coderag import config

_LIVE_RAN = False


@pytest.fixture(scope="session", autouse=True)
def live_indexes_untouched():
    """The census `isolated_state` is supposed to make impossible, measured anyway.

    197 of 616 directories in the real index dir were suite residue -- `wroot0-*`,
    `stranger-*`, `billing-core-*` -- and every one arrived while a redirect that
    reads as airtight was in place. A redirect nothing counts is a claim; this is
    the count. `config.INDEX_DIR` is read here before any test's monkeypatch and
    again after they are all undone, so both reads see the real path.

    A `live` module talks to the daemon, which holds the real state, and leaving a
    store there is the documented disable-never-prune behaviour -- so a session
    that ran one warns where a pure in-process session fails.
    """
    real = config.INDEX_DIR

    def census() -> int:
        return len([p for p in real.iterdir() if p.is_dir()]) if real.is_dir() else 0

    before = census()
    yield
    grew = census() - before
    if grew <= 0:
        return
    if _LIVE_RAN:
        warnings.warn(
            f"the suite left {grew} index dir(s) in {real}; run `coderag doctor --prune`",
            stacklevel=1,
        )
        return
    pytest.fail(f"the in-process suite wrote {grew} index dir(s) into the live {real}")


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point every state path at a tmp dir, for every test in the suite."""
    state = tmp_path / "state"
    monkeypatch.setattr(config, "STATE_DIR", state)
    monkeypatch.setattr(config, "REGISTRY_PATH", state / "projects.json")
    monkeypatch.setattr(config, "REGISTRY_LOCK", state / "projects.lock")
    monkeypatch.setattr(config, "BACKUP_DIR", state / "backups")
    monkeypatch.setattr(config, "INDEX_DIR", state / "indexes")
    return state


@pytest.fixture(scope="module", autouse=True)
def fleet_unchanged(request):
    """A live module gives back every row it enabled, or this fails.

    Disabled, never pruned -- the suite's standing rule. What went unwatched is
    the row a test enabled and forgot: it survives every run, and reconcile
    retries it and logs a traceback at every start. Two rows did exactly that
    before this existed, and both were a single test that skipped the teardown
    its ten siblings had. Per module, so the red names the file.
    """
    marks = getattr(request.module, "pytestmark", [])
    marks = marks if isinstance(marks, list) else [marks]
    if not any(getattr(m, "name", "") == "live" for m in marks):
        yield
        return
    from live import fleet_state

    global _LIVE_RAN
    _LIVE_RAN = True
    before = fleet_state()
    yield
    after = fleet_state()
    # Stores first, and as a warning: unregistering a member leaves its store by
    # design -- disable-never-prune -- so a red here would be red after every
    # live run. Reported, never repaired: pruning a computed set is what wiped
    # the fleet registry twice, and `coderag doctor --prune` is a human's call.
    leaked = after["unclaimed_stores"] - before["unclaimed_stores"]
    if leaked > 0:
        warnings.warn(
            f"{request.module.__name__} left {leaked} unclaimed store(s); "
            "run `coderag doctor --prune` when the lane is idle",
            stacklevel=1,
        )
    rows = {k: (before[k], after[k]) for k in ("projects", "fleet_digest") if before[k] != after[k]}
    if rows:
        pytest.fail(
            f"{request.module.__name__} changed the real registry {rows}; disable what you "
            "register, in a teardown that runs on failure too"
        )


@pytest.fixture
def repo(tmp_path) -> Path:
    """A small git repo with a .gitignore, since discovery is git-aware."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def parseUserConfig(path):\n"
        '    """Read the user configuration file and merge profile overrides."""\n'
        "    return {}\n"
    )
    (root / "src" / "util.js").write_text("export function renderTable(rows) { return rows; }\n")
    (root / ".gitignore").write_text("ignored/\n*.log\n")
    (root / "ignored").mkdir()
    (root / "ignored" / "junk.py").write_text("# should never be indexed\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


@pytest.fixture
def pin():
    """The caller's workspace, which a tool call arrives with.

    `scope.Pinned` is filled by the framework from `roots/list` and is invisible
    to the model, so a direct call is the only place it has to be supplied by
    hand -- and supplying it is what keeps these tests honest about the boundary.
    """

    def build(*paths: Path) -> ListRootsResult:
        return ListRootsResult(roots=[Root(uri=f"file://{p}") for p in paths])

    return build
