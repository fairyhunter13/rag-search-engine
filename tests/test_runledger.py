"""The daemon's own work, and the questions no row could answer before.

Every arm here fails against the code before this ledger: `_drain` threw away
what `index_project` returned, and `_dispatch` dropped an event in three bare
`continue`s.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coderag import index, registry, runledger


def _project(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "a.py").write_text("def handler():\n    return 1\n")
    registry.claim(path, direct=True)
    return registry.resolve(path)


def test_a_pass_carries_its_phase_timings(tmp_path):
    """`index_project` computed these and returned them to a caller that
    discarded the whole dict."""
    project = _project(tmp_path / "mine")
    stage = index.index_project(project)["stage"]
    for field in ("open_ms", "walk_ms", "write_ms", "files", "chunks"):
        assert field in stage, f"{field} missing from {stage}"


def test_a_kind_filter_does_not_return_another_kinds_rows(tmp_path):
    """Three kinds share one file so one question reads them together. A
    reader asking for one of them must still get one of them."""
    runledger.record("index", {"project": "/a"})
    runledger.record("watch", {"raw": 3})
    assert [r["project"] for r in runledger.read(kind="index")] == ["/a"]
    assert [r["raw"] for r in runledger.read(kind="watch")] == [3]


def test_only_the_failures_when_asked(tmp_path):
    runledger.record("index", {"project": "/a"})
    runledger.record("index", {"project": "/b", "error": "OSError: nope"})
    rows = runledger.read(errors_only=True, kind="index")
    assert [r["project"] for r in rows] == ["/b"]


def test_a_rotation_does_not_hide_the_rows_it_moved(monkeypatch):
    monkeypatch.setattr(runledger, "MAX_BYTES", 1)
    runledger.record("index", {"project": "/a"})
    runledger.record("index", {"project": "/b"})
    assert {r["project"] for r in runledger.read(kind="index")} == {"/a", "/b"}


def test_a_broken_state_dir_never_fails_the_daemon(tmp_path, monkeypatch):
    """Best-effort by construction: the daemon must not fail on bookkeeping."""
    monkeypatch.setattr(runledger, "path", lambda: tmp_path / "f" / "g.jsonl")
    (tmp_path / "f").write_text("not a directory")
    runledger.record("index", {"project": "/a"})
