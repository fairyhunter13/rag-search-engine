"""The two chunker arms, and the stamp that stops a stale store reading current.

Both are opt-in and both change what gets embedded, which is the combination
that goes wrong quietly: the vectors are in the same space and the same shape,
so nothing downstream can tell that half the store was written by the other arm.
"""

import sqlite3

import pytest

from coderag import chunk, config, store

FENCED = """# Title

Intro paragraph with enough words to occupy a chunk on its own here.

```python
def a():
    return 1


def b():
    return 2
```

| col | col |
|---|---|
| 1 | 2 |
| 3 | 4 |

Closing paragraph.
"""


def _fences(text: str) -> int:
    return text.count("```")


def test_the_markdown_arm_is_off_unless_it_is_asked_for(monkeypatch):
    """Off is the shipped configuration. An arm that is quietly on is not an
    arm -- it is an undocumented default with a flag next to it."""
    assert config.CHUNK_MD_SPLITTER is False
    base = chunk.chunk_text(FENCED, rel_path="doc.md", size=40)
    monkeypatch.setattr(config, "CHUNK_MD_SPLITTER", True)
    assert chunk.chunk_text(FENCED, rel_path="doc.md", size=40) != base


def test_the_markdown_arm_stops_cutting_code_fences_in_half(monkeypatch):
    """The measured gain, on the shape it was measured on. A chunk holding an
    odd number of fence markers is one whose code block is severed -- it renders
    as prose downstream and embeds as neither.
    """
    monkeypatch.setattr(config, "CHUNK_MD_SPLITTER", True)
    chunks = chunk.chunk_text(FENCED, rel_path="doc.md", size=40)
    assert not [c for c in chunks if _fences(c.text) % 2]


def test_the_markdown_arm_never_touches_a_code_file(monkeypatch):
    """It dispatches on lang, so a `.py` file is chunked identically either way.
    A flag that silently reaches 75% of the corpus is not the arm it claims."""
    src = "import os\n\n\ndef a():\n    return 1\n\n\ndef b():\n    return 2\n"
    monkeypatch.setattr(config, "CHUNK_MD_SPLITTER", False)
    off = chunk.chunk_text(src, rel_path="x.py", size=10)
    monkeypatch.setattr(config, "CHUNK_MD_SPLITTER", True)
    assert chunk.chunk_text(src, rel_path="x.py", size=10) == off


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row  # what store.connect sets; get_meta indexes by name
    connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    yield connection
    connection.close()


def test_a_store_written_by_another_chunker_arm_is_refused(conn, monkeypatch):
    store.stamp(conn)
    assert store.incompatible(conn) is None
    monkeypatch.setattr(config, "CHUNK_MD_SPLITTER", True)
    assert "chunk_md_splitter" in (store.incompatible(conn) or "")


def test_bumping_the_algo_version_forces_a_rebuild(conn, monkeypatch):
    """The gap this closes. `ProjectConfig.signature()` versions excludes, and
    nothing versioned the header -- so changing what `scope_header` emits
    rewrote every embedded string while every store on disk still read as
    current. Stale in the one direction nothing reports.
    """
    store.stamp(conn)
    monkeypatch.setattr(config, "CHUNK_ALGO", config.CHUNK_ALGO + 1)
    assert "chunk_algo" in (store.incompatible(conn) or "")
