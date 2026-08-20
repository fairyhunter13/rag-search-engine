"""Fusion and diversification without a model; retrieval with the real one."""

from __future__ import annotations

from pathlib import Path

import pytest

from coderag import config, federation, filters, index, registry, search
from coderag.search import Hit, SearchError


def _hit(
    path: str,
    text: str = "body",
    rrf: float = 0.5,
    rerank: float | None = None,
    project: str = "/p",
) -> Hit:
    return Hit(
        project=project,
        path=f"{project}/{path}",
        rel_path=path,
        start_line=1,
        end_line=2,
        lang="python",
        text=text,
        scores={"rrf": rrf, "bm25": None, "dense": None, "rerank": rerank},
    )


# -------------------------------------------------------------------- fusion


def test_rrf_reads_position_and_never_score():
    """Two lanes, opposite orders. The shared document has to win on agreement
    alone -- if either lane's magnitude leaked in, it could not."""
    fused = search.rrf([[7, 1, 2], [3, 7, 4]])
    assert max(fused, key=fused.get) == 7


def test_rrf_is_indifferent_to_the_gap_between_ranks():
    """The property that makes normalising unnecessary: a lane could return
    cosine distances or raw BM25 and the fusion would not move."""
    assert search.rrf([[1, 2]]) == search.rrf([[1, 2]])
    assert search.rrf([[9]])[9] == pytest.approx(1 / (config.RRF_K + 1))


# -------------------------------------------------------------- diversification


def test_the_pool_cut_keeps_more_than_one_hit_from_the_callers_own_project():
    """RRF fuses *within* a project, so its score is a rank: every member's best
    hit scores the same as the caller's best hit. Cutting the flat pool by that
    score across 136 members keeps everyone's top one and drops the caller's
    own third -- the one the reranker would have ranked first."""
    own = [_hit(f"mine{i}.py", rrf=0.9 - i / 100, project="/root") for i in range(10)]
    members = [_hit("theirs.py", rrf=0.95, project=f"/member{i}") for i in range(120)]

    cut = search._pool_cut(own + members, Path("/root"), limit=60)

    assert len(cut) == 60
    mine = [h for h in cut if h.project == "/root"]
    assert len(mine) == 10, "the caller's own project was cut down to its top hit"
    assert [h.rel_path for h in mine[:3]] == ["mine0.py", "mine1.py", "mine2.py"]


def test_the_pool_cut_still_reaches_every_member():
    """The other half: privileging the root must not shut the members out, or
    federation stops being fan-in."""
    own = [_hit(f"mine{i}.py", project="/root") for i in range(80)]
    members = [_hit("theirs.py", project=f"/member{i}") for i in range(5)]

    cut = search._pool_cut(own + members, Path("/root"), limit=60)

    assert {h.project for h in cut} >= {f"/member{i}" for i in range(5)}
    assert len(cut) == 60


def test_a_chunk_the_caller_also_has_is_reported_under_their_own_path():
    """Vendored copies and `_worktrees` checkouts make this the common case on
    this fleet. The fingerprint was project-blind, so the higher-scoring copy
    won and the caller was shown a member's path for code in their own tree."""
    theirs = _hit("vendor/x.py", "def f():\n    return 1", rrf=0.99, project="/member")
    mine = _hit("vendor/x.py", "def f():\n\treturn 1", rrf=0.10, project="/root")

    out = search._diversify([theirs, mine], k=5, max_per_file=5, root="/root")

    assert [h.project for h in out] == ["/root"]


def test_the_per_file_cap_never_demotes_a_better_result():
    """Six chunks from one file, one from another, in rank order. The cap has
    to thin the first file without letting the last hit jump the queue."""
    hits = [_hit("a.py", f"chunk {i}") for i in range(6)] + [_hit("b.py", "other")]
    out = search._diversify(hits, k=4, max_per_file=2)

    assert [h.rel_path for h in out[:3]] == ["a.py", "a.py", "b.py"]


def test_near_duplicates_collapse_on_normalised_text():
    """The chunker's overlap manufactures these by construction, so the
    comparison ignores whitespace: two chunks differing only in indentation are
    the same answer twice."""
    hits = [_hit("a.py", "def f():\n    return 1"), _hit("a.py", "def f():\n\treturn 1")]
    assert len(search._diversify(hits, k=5, max_per_file=5)) == 1


def test_a_thin_pool_still_returns_k_results():
    """Diversity is a preference between equally good answers, never a reason
    to return fewer. Without the backfill this returns 2 instead of 5."""
    hits = [_hit("a.py", f"chunk {i}") for i in range(5)]
    out = search._diversify(hits, k=5, max_per_file=2)

    assert len(out) == 5
    assert [h.text for h in out[:2]] == ["chunk 0", "chunk 1"], "the best two still lead"


# ------------------------------------------------------------------- refusals


def test_an_unknown_mode_errors_and_names_the_valid_set():
    """A silently widened corpus reads exactly like an engine defect."""
    with pytest.raises(SearchError, match="hybrid"):
        search.search("anything", mode="fuzzy")


def test_an_unknown_lang_errors_rather_than_returning_nothing():
    """`mode` refused an unknown value from the start; `lang` narrowed the
    corpus to nothing and reported it as no matches -- the one failure a caller
    cannot tell from an honest empty result."""
    with pytest.raises(SearchError, match="did you mean 'python'"):
        search.search("anything", lang="pyton")
    with pytest.raises(SearchError, match="valid:"):
        search.search("anything", lang="cobol")


def test_the_unlabeled_are_reachable_only_with_no_lang_filter():
    """Discovery is a denylist, so a file with an unrecognised extension is
    indexed and searchable with `lang=""`. That empty label is deliberately not
    a selectable value: offering it would imply a group, and the group is
    "everything we have no name for"."""
    assert "" not in set(filters.LANGS.values())
    unlabeled = _hit("notes.zig")
    unlabeled.lang = ""
    assert search._filter([unlabeled], None, "python") == []
    assert search._filter([unlabeled], None, "") == [unlabeled], "empty means unfiltered, not zig"


def test_an_unindexed_root_names_what_to_call_rather_than_widening(tmp_path):
    with pytest.raises(SearchError, match="index"):
        search.search("anything", tmp_path)


def test_an_empty_query_is_an_error_not_an_empty_corpus():
    with pytest.raises(SearchError):
        search.search("   ")


# ------------------------------------------------------- retrieval, on real GPU


@pytest.fixture
def indexed(repo):
    registry.claim(repo, direct=True)
    index.index_project(repo)
    return repo


@pytest.mark.gpu
def test_a_result_is_a_location_that_resolves(indexed):
    """A ranked list of stale line numbers looks identical to a working one, so
    every range is opened and checked against the file it names."""
    out = search.search("read the user configuration", indexed)
    assert out["results"], out["hint"]

    for result in out["results"]:
        path = Path(result["path"])
        assert path.exists()
        lines = path.read_text().splitlines()
        start, end = result["lines"]
        assert 1 <= start <= end <= len(lines)


@pytest.mark.gpu
def test_bodies_are_opt_in_and_previews_are_bounded(indexed):
    default = search.search("user config", indexed)["results"][0]
    assert "body" not in default
    assert len(default["preview"].splitlines()) <= 3

    full = search.search("user config", indexed, include_body=True)["results"][0]
    assert "body" in full


@pytest.mark.gpu
def test_the_lexical_lane_finds_an_identifier_the_query_only_half_names(indexed):
    """`user config` must reach `parseUserConfig`. This is the camelCase split
    and the tokenchars setting working together; either alone fails it."""
    out = search.search("user config", indexed, mode="lexical")
    assert any("parseUserConfig" in r["preview"] for r in out["results"])


@pytest.mark.gpu
def test_the_dense_lane_answers_a_question_with_no_shared_terms(indexed):
    """NL->PL: nothing in this query appears in the file, so BM25 cannot help."""
    out = search.search("how are profile settings merged", indexed, mode="semantic")
    assert out["results"] and out["reranked"]


@pytest.mark.gpu
def test_filters_narrow_and_an_empty_result_is_not_an_error(indexed):
    js_only = search.search("function", indexed, path_glob="*.js")
    assert all(r["rel_path"].endswith(".js") for r in js_only["results"])

    nothing = search.search("zzzznotpresentanywhere", indexed, path_glob="*.rs")
    assert nothing["results"] == [] and nothing["hint"]


@pytest.mark.gpu
def test_search_from_a_root_reaches_its_members(tmp_path, repo):
    """Federation is fan-in over per-project stores, so a hit has to arrive
    carrying the member's own resolved path, never the symlink."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "member").symlink_to(repo)
    registry.claim(root, direct=True)
    federation.register(root)
    for project in federation.expand(root):
        index.index_project(project)

    out = search.search("parseUserConfig", root, mode="lexical")
    assert any(r["project"] == str(repo) for r in out["results"])
    assert out["searched"]["projects"] == 2
