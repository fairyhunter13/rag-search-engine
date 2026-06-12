"""Live tests for the persistent answer cache (handlers/_answer_cache.py).

All tests require a running daemon at :8765 and at least one indexed project with
communities.  No mocks.  GPU-only inference (no CPU fallback).

Test coverage:
  - test_answer_cache_roundtrip        — save/load/invalidate/stale-sig cycle
  - test_answer_cache_hit_is_fast      — cache hit returns cached:true in <3s
  - test_answer_cache_nearest_card     — semantic-near query returns a precomputed card
  - test_warm_answer_cache_populates   — _warm_answer_cache creates entries for suggested_questions
"""
from __future__ import annotations

import asyncio
import time

import pytest

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _save_load_module():
    from opencode_search.handlers._answer_cache import (
        invalidate_answers,
        load_answer,
        make_answer_key,
        save_answer,
    )
    return save_answer, load_answer, invalidate_answers, make_answer_key


# ---------------------------------------------------------------------------
# T2.1 correctness — roundtrip, invalidate, stale-sig bypass
# ---------------------------------------------------------------------------

class TestAnswerCacheRoundtrip:
    """save_answer → load_answer roundtrip; invalidate drops it; stale sig misses."""

    def test_save_and_load(self, project):
        save_answer, load_answer, _invalidate, _ = _save_load_module()

        payload = {"answer": "test answer", "sources": [], "communities": []}
        save_answer(project, "feature", "test roundtrip query", payload)

        hit = load_answer(project, "feature", "test roundtrip query")
        assert hit is not None, "load_answer returned None after save_answer"
        assert hit.get("answer") == "test answer"
        assert hit.get("_graph_sig") is not None
        assert hit.get("_cached_at") is not None

    def test_invalidate_drops_entry(self, project):
        save_answer, load_answer, invalidate_answers, _ = _save_load_module()

        payload = {"answer": "invalidation test", "sources": []}
        save_answer(project, "global", "invalidation query", payload)
        assert load_answer(project, "global", "invalidation query") is not None

        invalidate_answers(project)
        hit = load_answer(project, "global", "invalidation query")
        assert hit is None, "load_answer should return None after invalidate_answers()"

    def test_stale_graph_sig_returns_none(self, project):
        """An entry with a mismatched _graph_sig is treated as a miss."""
        import json
        import time as _t

        from opencode_search.handlers._answer_cache import (
            _ANSWER_CACHE,
            _entry_path,
            load_answer,
        )

        scope, query = "feature", "stale sig test query"
        path = _entry_path(project, scope, query)
        path.parent.mkdir(parents=True, exist_ok=True)
        stale_entry = {
            "answer": "stale",
            "sources": [],
            "_graph_sig": "__intentionally_wrong_sig__",
            "_cached_at": _t.time(),
            "_scope": scope,
            "_query": " ".join(query.lower().split()),
        }
        path.write_text(json.dumps(stale_entry), encoding="utf-8")

        # Purge in-process cache so disk read is attempted
        from opencode_search.handlers._answer_cache import make_answer_key
        key = make_answer_key(project, scope, query)
        _ANSWER_CACHE.pop(key, None)

        hit = load_answer(project, scope, query)
        assert hit is None, "load_answer should bypass an entry with a stale _graph_sig"

        # Cleanup
        import contextlib
        with contextlib.suppress(Exception):
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# T2.3 correctness — cache hit is fast
# ---------------------------------------------------------------------------

class TestAnswerCacheHitIsFast:
    """A second handle_ask_feature call on a warmed query returns cached:true in <3s."""

    @pytest.mark.slow
    def test_second_call_is_cached_and_fast(self, project):
        """Warm the cache with one live call; second call must return cached:true <3s."""
        from opencode_search.handlers._answer_cache import invalidate_answers

        query = "how does this project work overall"

        # Drop any stale cache for this project first
        invalidate_answers(project)

        async def _run():
            from opencode_search.handlers._feature import handle_ask_feature
            # First call — cold miss; synthesizes and writes to cache
            result1 = await handle_ask_feature(query=query, project_path=project, top_k=5)
            assert "error" not in result1 or result1.get("cached") is not None, (
                f"First handle_ask_feature call errored: {result1}"
            )

            # Second call — should hit cache
            t0 = time.monotonic()
            result2 = await handle_ask_feature(
                query=query, project_path=project, top_k=5, use_cache=True
            )
            elapsed = time.monotonic() - t0

            cached = result2.get("cached")
            assert cached in (True, "nearest"), (
                f"Expected cached:true or cached:'nearest' on second call, got cached={cached!r}. "
                f"elapsed={elapsed:.2f}s result={str(result2)[:200]}"
            )
            assert elapsed < 3.0, (
                f"Cache hit took {elapsed:.2f}s — expected <3s. "
                f"cached={cached!r}"
            )

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# T2.3 nearest-card path
# ---------------------------------------------------------------------------

class TestAnswerCacheNearestCard:
    """A semantically-near (not exact) query returns a precomputed card."""

    @pytest.mark.slow
    def test_near_query_returns_card(self, project):
        """Warm with a canonical query; a paraphrased variant should get a nearest hit."""
        from opencode_search.config import DEFAULT_DIMS, DEFAULT_EMBED_MODEL
        from opencode_search.embeddings import embed_query
        from opencode_search.handlers._answer_cache import (
            invalidate_answers,
            nearest_answer,
            save_answer,
        )

        # Manually warm a specific card with its embedding
        canonical_query = "what is the overall system architecture"
        payload = {
            "answer": "This is a microservices system handling campaign management.",
            "sources": [],
            "communities": [],
        }
        emb = embed_query(canonical_query, model=DEFAULT_EMBED_MODEL, dimensions=DEFAULT_DIMS)
        save_answer(project, "global", canonical_query, payload, embedding=emb)

        # A paraphrased near query
        near_query = "describe the high-level architecture of this system"
        hit = nearest_answer(project, "global", near_query, threshold=0.70)

        assert hit is not None, (
            f"nearest_answer returned None for semantically-near query '{near_query}'. "
            f"Precomputed canonical query was: '{canonical_query}'"
        )
        assert hit.get("answer") == payload["answer"]

        # Cleanup
        invalidate_answers(project)


# ---------------------------------------------------------------------------
# T2.2 warmer populates cache
# ---------------------------------------------------------------------------

class TestWarmAnswerCachePopulates:
    """_warm_answer_cache creates cache entries for the top suggested_questions."""

    @pytest.mark.slow
    def test_warm_creates_entries(self, project):
        """After _warm_answer_cache runs, load_answer returns a hit for at least one
        suggested_question entry."""
        from opencode_search.daemon import _warm_answer_cache
        from opencode_search.handlers._answer_cache import (
            invalidate_answers,
            load_answer,
        )

        # Start clean
        invalidate_answers(project)

        # Run the warmer
        asyncio.run(_warm_answer_cache(project))

        # Check that at least one suggested_question got cached
        from opencode_search.handlers._graph import handle_suggest_questions
        questions_result = asyncio.run(handle_suggest_questions(project_path=project))
        questions = questions_result.get("questions", [])

        if not questions:
            pytest.skip("No suggested_questions for this project — warmer has nothing to fill")

        hits = 0
        for q in questions[:7]:
            text = q.get("question", "")
            if text and load_answer(project, "feature", text) is not None:
                hits += 1

        assert hits > 0, (
            f"_warm_answer_cache ran but load_answer returned None for all "
            f"{min(7, len(questions))} suggested_questions. "
            f"First question: {questions[0].get('question', '')!r}"
        )
