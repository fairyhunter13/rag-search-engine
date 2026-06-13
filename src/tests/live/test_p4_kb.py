"""P4 kb/ tests: hierarchy, wiki, answer_cache, patterns (all fast)."""
import pytest

pytestmark = pytest.mark.live


def test_hierarchy_no_cross_edges_is_ok(mini_stores):
    from opencode_search.graph.store import GraphStore
    from opencode_search.kb.hierarchy import build_hierarchy
    gs = GraphStore(mini_stores["gdb"])
    count = build_hierarchy(gs)
    gs.close()
    assert count >= 0  # 0 is valid when no cross-community call edges


def test_wiki_writes_pages(mini_stores, tmp_path):
    from opencode_search.graph.store import GraphStore
    from opencode_search.kb.wiki import build_wiki
    gs = GraphStore(mini_stores["gdb"])
    # Inject a pre-enriched community so wiki has something to write.
    gs.upsert_community(999, level=1, title="Auth module",
                        summary="Handles JWT authentication.", member_count=2)
    gs.commit()
    count = build_wiki(gs, tmp_path / "wiki")
    gs.close()
    assert count >= 1
    pages = list((tmp_path / "wiki").glob("*.md"))
    assert any("Auth module" in p.read_text() for p in pages)


def test_answer_cache_set_get_invalidate(tmp_path):
    from opencode_search.kb.answer_cache import get, invalidate
    from opencode_search.kb.answer_cache import set as cache_set
    cd = tmp_path / "ac"
    cache_set(cd, "key1", "value1", ttl_s=3600)
    assert get(cd, "key1") == "value1"
    assert get(cd, "missing") is None
    invalidate(cd)
    assert get(cd, "key1") is None


def test_answer_cache_expired_returns_none(tmp_path):

    from opencode_search.kb.answer_cache import get
    from opencode_search.kb.answer_cache import set as cache_set
    cd = tmp_path / "ac2"
    cd.mkdir()
    cache_set(cd, "k", "v", ttl_s=-1)  # already expired
    assert get(cd, "k") is None


def test_patterns_detects_python_files(mini_stores):
    from opencode_search.kb.patterns import detect_patterns
    result = detect_patterns(mini_stores["proj"])
    assert "python" in result["languages"]
    assert result["file_count"] >= 3
    assert isinstance(result["frameworks"], list)
    assert isinstance(result["dependencies"], list)
