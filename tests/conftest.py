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
from pathlib import Path

import pytest
from mcp_types import ListRootsResult, Root

from coderag import config


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
    from live import enabled_count

    before = enabled_count()
    yield
    after = enabled_count()
    assert after == before, (
        f"{request.module.__name__} left {after - before} row(s) enabled in the real "
        "registry; disable what you register, in a teardown that runs on failure too"
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
