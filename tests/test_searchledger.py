"""The stage record, and the two questions it exists to answer.

Every arm here fails against the code before the ledger: `search` returned
`took_ms` and a project count, and nothing in between was written down.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coderag import registry, searchledger, tools


def _project(path: Path, *, indexed: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    registry.claim(path, direct=True)
    if indexed:
        registry.update(path, indexed_at=1_700_000_000.0, file_count=1, chunk_count=1)
    return registry.resolve(path)


@pytest.fixture
def pin():
    from mcp_types import ListRootsResult, Root

    def make(*paths: Path) -> ListRootsResult:
        return ListRootsResult(roots=[Root(uri=p.as_uri(), name=p.name) for p in paths])

    return make


def test_a_failed_search_writes_a_row(tmp_path, pin):
    """The one call a reader most wants to find was the one leaving no trace:
    the error path returned a string and wrote nothing anywhere."""
    mine = _project(tmp_path / "mine")
    out = tools.search_code("x", pin(mine), root=str(tmp_path / "nope"), mode="lexical")
    assert "error" in out

    rows = searchledger.read()
    assert len(rows) == 1, rows
    assert "not indexed" in rows[0]["error"]


def test_the_error_carries_the_trace_that_finds_its_row(tmp_path, pin):
    """A trace nobody can quote back is a trace nobody can look up."""
    mine = _project(tmp_path / "mine")
    out = tools.search_code("x", pin(mine), root=str(tmp_path / "nope"), mode="lexical")
    quoted = out["error"].split("[trace ")[1].rstrip("]")
    assert [r for r in searchledger.read() if r["trace"] == quoted]


def test_a_row_carries_both_sides_of_the_cut(tmp_path, pin):
    """`pool` against `cut` is the pair that names a starved round-robin. One
    number without the other is what made the last one take an offline replay."""
    mine = _project(tmp_path / "mine")
    tools.search_code("x", pin(mine), root=str(mine), mode="lexical")
    row = searchledger.read()[0]
    for field in ("unit", "pool", "pool_projects", "filtered", "cut", "cut_projects",
                  "returned", "retrieve_ms", "rerank_ms", "took_ms"):
        assert field in row, f"{field} missing from {row}"


def test_the_reply_never_carries_the_stages(tmp_path, pin):
    """Evidence, not an answer. A model reading pool sizes spends context on a
    number it cannot act on."""
    mine = _project(tmp_path / "mine")
    out = tools.search_code("x", pin(mine), root=str(mine), mode="lexical")
    assert "trace" not in out


def test_a_rotation_does_not_hide_the_rows_it_moved(monkeypatch):
    """`read` walks both generations. A rotation the moment before a question is
    asked would otherwise answer it with an empty file."""
    monkeypatch.setattr(searchledger, "MAX_BYTES", 1)
    searchledger.record({"trace": "aaaa", "root": "/one"})
    searchledger.record({"trace": "bbbb", "root": "/two"})
    assert {r["trace"] for r in searchledger.read()} == {"aaaa", "bbbb"}


def test_a_broken_state_dir_never_fails_the_search(tmp_path, monkeypatch):
    """Best-effort by construction: a search must not fail on its bookkeeping."""
    monkeypatch.setattr(searchledger, "path", lambda: tmp_path / "f" / "g.jsonl")
    (tmp_path / "f").write_text("not a directory")
    searchledger.record({"trace": "aaaa"})
