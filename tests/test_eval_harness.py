"""The eval harness's one invariant: a query must not be findable by identity.

`eval.py` produces every retrieval number this repo has, and had no test. The
failure it warns about in its own docstring is silent and flattering -- leave
the doc block in the indexed copy and every arm scores near 1.000, the ranking
collapses to string matching, and the run reads as a strong result.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval as harness
from coderag import filters

PY = '''"""Resolve a session token against the redis-backed store and refresh it."""

import redis


def get(key):
    return redis.get(key)
'''

MD = """# Session storage

Tokens are held in redis with a sliding expiry, refreshed on every read.

## Eviction

The eviction policy is LRU.
"""


def _project(tmp_path: Path) -> Path:
    (tmp_path / "session.py").write_text(PY, encoding="utf-8")
    (tmp_path / "storage.md").write_text(MD, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("kind", "positive", "lead"),
    [("code", "session.py", "Resolve a session token"), ("docs", "storage.md", "Session storage")],
)
def test_the_lead_block_is_the_query_and_is_gone_from_the_indexed_copy(
    tmp_path, kind, positive, lead
):
    """Both halves in one assertion, because either alone passes while broken.

    A query built but not stripped is the flattering failure; a block stripped
    but not queried leaves the corpus with no query set at all.
    """
    project = _project(tmp_path)
    queries = harness.build_queries(project, corpus=kind)
    assert [q.positive for q in queries] == [positive]
    assert queries[0].text.startswith(lead)

    dest = harness.materialize(project, tmp_path / "out", kind)
    body = " ".join((dest / positive).read_text(encoding="utf-8").split())
    assert lead not in body


def test_the_other_half_of_the_corpus_is_copied_whole(tmp_path):
    """The distractors have to stay what the engine really holds. Stripping the
    doc block from every file in both modes would quietly make the corpus
    easier for the mode that did not ask for it."""
    project = _project(tmp_path)
    dest = harness.materialize(project, tmp_path / "out", "docs")
    assert "Resolve a session token" in (dest / "session.py").read_text(encoding="utf-8")


def test_the_two_corpora_are_disjoint(tmp_path):
    """`docs` was not "the code set plus prose". A doc file in the code set
    supplies its H1 through the line-comment pattern, which is why the exclusion
    was there before this mode existed."""
    project = _project(tmp_path)
    code = {q.positive for q in harness.build_queries(project, corpus="code")}
    docs = {q.positive for q in harness.build_queries(project, corpus="docs")}
    assert code and docs and not code & docs
    assert all(filters.lang_of(rel) in filters.DOC_LANGS for rel in docs)
    assert not any(filters.lang_of(rel) in filters.DOC_LANGS for rel in code)


def test_a_heading_with_no_lead_paragraph_yields_no_query(tmp_path):
    """Rejected, not padded. Half the doc files in the sibling repo are a title
    followed straight by a sub-heading; a 3-word title is not a query, and
    accepting one would measure title-to-filename matching."""
    (tmp_path / "stub.md").write_text("# SQL Injection\n\n## When to use\n\nx\n", encoding="utf-8")
    assert harness.build_queries(tmp_path, corpus="docs") == []


def test_a_doc_file_opening_at_h2_still_yields_a_query(tmp_path):
    """The first heading, not the first H1. Requiring `#` selects for files
    whose author wrote one, which is a property of the author, not the text."""
    (tmp_path / "a.md").write_text(
        "## Federation\n\nA federation is discovery over registered roots, never a merged index.\n",
        encoding="utf-8",
    )
    assert harness.build_queries(tmp_path, corpus="docs")[0].text.startswith("Federation")


def test_ranks_ride_along_with_the_aggregates():
    """Two arms 0.02 apart in recall@10 differ by ~7 queries out of 300. The
    paired per-query outcome is what a bootstrap CI or McNemar needs, and it
    was computed and discarded."""
    scored = harness.score([1, None, 4])
    assert scored["queries"] == 3 and scored["recall@1"] == pytest.approx(1 / 3, abs=1e-4)
    assert scored[f"recall@{harness.K}"] == pytest.approx(2 / 3, abs=1e-4)
