"""Test isolation.

`isolated_state` is autouse and has no opt-out. A test that ran in-process
against the real registry is what destroyed the fleet's 236 rows once already,
and the version of that test looked entirely harmless -- it only *read*, until
a helper it called wrote back a pruned copy. Redirecting the paths for every
test costs nothing and removes the whole class.
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
