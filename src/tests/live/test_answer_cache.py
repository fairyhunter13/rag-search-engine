"""answer_cache: deterministic TTL caching for assembled `ask` context (all fast).

Two of this file's three subjects were tier 3 and both are gone. `build_wiki` wrote the
community wiki out of DeepSeek-narrated summaries; `detect_patterns` was the framework
labeller behind `overview(what="patterns")`, the last synchronous cloud round trip on a
query path. Neither property survives the module it tested — there is no wiki to render
and no framework labeller to call.

`answer_cache` was the only survivor, so `kb/` was a package holding one module that had
nothing to do with the knowledge base; it now lives in `query/` beside its two callers.
"""
import pytest

pytestmark = pytest.mark.live


def test_answer_cache_set_get_invalidate(tmp_path):
    from rag_search.query.answer_cache import get, invalidate
    from rag_search.query.answer_cache import set as cache_set
    cd = tmp_path / "ac"
    cache_set(cd, "key1", "value1", ttl_s=3600)
    assert get(cd, "key1") == "value1"
    assert get(cd, "missing") is None
    invalidate(cd)
    assert get(cd, "key1") is None


def test_answer_cache_expired_returns_none(tmp_path):

    from rag_search.query.answer_cache import get
    from rag_search.query.answer_cache import set as cache_set
    cd = tmp_path / "ac2"
    cd.mkdir()
    cache_set(cd, "k", "v", ttl_s=-1)  # already expired
    assert get(cd, "k") is None
