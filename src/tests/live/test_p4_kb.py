"""P4 kb/ tests: answer_cache — the whole of what `kb/` is after tier 3 (all fast).

Two of this file's three subjects were tier 3 and both are gone. `build_wiki` wrote the
community wiki out of DeepSeek-narrated summaries; `detect_patterns` was the framework
labeller behind `overview(what="patterns")`, the last synchronous cloud round trip on a
query path. Neither property survives the module it tested — there is no wiki to render
and no framework labeller to call. `answer_cache` is deterministic TTL caching with no
LLM in it, which is why it stayed in tier 2 while the rest of the package left.
"""
import pytest

pytestmark = pytest.mark.live


def test_answer_cache_set_get_invalidate(tmp_path):
    from rag_search.kb.answer_cache import get, invalidate
    from rag_search.kb.answer_cache import set as cache_set
    cd = tmp_path / "ac"
    cache_set(cd, "key1", "value1", ttl_s=3600)
    assert get(cd, "key1") == "value1"
    assert get(cd, "missing") is None
    invalidate(cd)
    assert get(cd, "key1") is None


def test_answer_cache_expired_returns_none(tmp_path):

    from rag_search.kb.answer_cache import get
    from rag_search.kb.answer_cache import set as cache_set
    cd = tmp_path / "ac2"
    cd.mkdir()
    cache_set(cd, "k", "v", ttl_s=-1)  # already expired
    assert get(cd, "k") is None
