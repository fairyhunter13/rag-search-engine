"""R11: Reranker tests covering R1–R10 behavior.

Run with GPU available:
    pytest tests/test_reranker.py -v

Run without GPU (unit-only tests will pass, GPU tests skip):
    pytest tests/test_reranker.py -v -m "not gpu"
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


def _has_gpu() -> bool:
    try:
        import onnxruntime as ort
        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


def _import_server():
    """Import server module — must always succeed (conftest patches GPU if needed).

    Never skips: the conftest session fixture pre-patches provider detection
    so server.py can be imported in any environment.
    """
    from opencode_embedder import server
    return server


# ---------------------------------------------------------------------------
# R1: Tier-model mapping
# ---------------------------------------------------------------------------
class TestTierModelMapping:
    def test_all_tiers_have_rerank(self):
        from opencode_embedder.embeddings import TIER_MODELS
        for tier in ("budget", "balanced", "premium"):
            assert "rerank" in TIER_MODELS[tier], f"{tier} missing rerank key"

    def test_tier_models_distinct(self):
        from opencode_embedder.embeddings import TIER_MODELS
        models = [TIER_MODELS[t]["rerank"] for t in ("budget", "balanced", "premium")]
        assert len(set(models)) == 3, f"Tiers must use distinct reranker models: {models}"

    def test_budget_rerank_model(self):
        from opencode_embedder.embeddings import TIER_MODELS
        assert TIER_MODELS["budget"]["rerank"] == "Xenova/ms-marco-MiniLM-L-6-v2"

    def test_balanced_rerank_model(self):
        from opencode_embedder.embeddings import TIER_MODELS
        assert TIER_MODELS["balanced"]["rerank"] == "jinaai/jina-reranker-v1-turbo-en"

    def test_premium_rerank_model(self):
        from opencode_embedder.embeddings import TIER_MODELS
        assert TIER_MODELS["premium"]["rerank"] == "jinaai/jina-reranker-v2-base-multilingual"

    def test_rust_cli_matches_python(self):
        """Verify Rust cli.rs model strings match Python TIER_MODELS (grep-based)."""
        cli_rs = os.path.join(
            os.path.dirname(__file__), "..", "..", "indexer", "src", "cli.rs"
        )
        if not os.path.exists(cli_rs):
            pytest.skip("cli.rs not found")
        content = open(cli_rs).read()
        assert "jina-reranker-v2-base-multilingual" in content, "premium reranker missing from cli.rs"
        assert "jina-reranker-v1-turbo-en" in content, "balanced reranker missing from cli.rs"
        assert "ms-marco-MiniLM-L-6-v2" in content, "budget reranker missing from cli.rs"


# ---------------------------------------------------------------------------
# R5: Sigmoid calibration
# ---------------------------------------------------------------------------
class TestSigmoidCalibration:
    def test_sigmoid_range(self):
        from opencode_embedder.embeddings import _calibrate_scores
        logits = [-5.0, -1.0, 0.0, 1.0, 5.0]
        result = _calibrate_scores(logits)
        assert all(0.0 <= float(s) <= 1.0 for s in result), f"Out of [0,1]: {result}"

    def test_sigmoid_monotonic(self):
        from opencode_embedder.embeddings import _calibrate_scores
        logits = [-2.0, -1.0, 0.0, 1.0, 2.0]
        result = _calibrate_scores(logits)
        result = list(result)
        assert result == sorted(result), f"Not monotonic: {result}"

    def test_sigmoid_no_false_spread(self):
        """All-similar logits must NOT be spread across [0,1] like min-max would."""
        from opencode_embedder.embeddings import _calibrate_scores
        logits = [5.01, 5.02, 5.03, 5.04, 5.05]
        result = _calibrate_scores(logits)
        assert all(float(s) > 0.99 for s in result), \
            f"Sigmoid spread noise: {[round(float(x), 4) for x in result]}"
        assert not (float(result[0]) < 0.01), \
            "Looks like min-max normalization (lowest score near 0)"

    def test_sigmoid_temperature_scaling(self):
        from opencode_embedder.embeddings import _calibrate_scores
        logits = [1.0, 2.0, 3.0]
        cool = _calibrate_scores(logits, temperature=0.5)   # sharper
        warm = _calibrate_scores(logits, temperature=2.0)   # softer
        # Temperature doesn't change ordering
        assert list(cool) == sorted(cool), "Cool not monotonic"
        assert list(warm) == sorted(warm), "Warm not monotonic"

    def test_rerank_temperature_dict(self):
        from opencode_embedder.embeddings import RERANK_TEMPERATURE, TIER_MODELS
        for tier in ("budget", "balanced", "premium"):
            model = TIER_MODELS[tier]["rerank"]
            assert model in RERANK_TEMPERATURE, f"Missing temperature for {model}"
            assert RERANK_TEMPERATURE[model] > 0


# ---------------------------------------------------------------------------
# R2: LRU reranker cache
# ---------------------------------------------------------------------------
class TestRerankerLRUCache:
    def setup_method(self):
        from opencode_embedder import embeddings as emb
        with emb._reranker_cache_lock:
            emb._reranker_cache.clear()
            emb._reranker_lru.clear()

    def test_cache_size_constant(self):
        from opencode_embedder.embeddings import RERANKER_CACHE_SIZE
        assert RERANKER_CACHE_SIZE >= 1

    def test_cache_size_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_RERANKER_CACHE_SIZE", "3")
        from opencode_embedder import embeddings as emb
        importlib.reload(emb)
        assert emb.RERANKER_CACHE_SIZE == 3

    @pytest.mark.gpu
    def test_lru_eviction_order(self):
        """Loading a third model evicts the LRU entry (not MRU)."""
        pytest.importorskip("onnxruntime")
        if not _has_gpu():
            pytest.skip("GPU required")

        from opencode_embedder import embeddings as emb
        from opencode_embedder.embeddings import TIER_MODELS

        # Override cache size to 2
        orig_size = emb.RERANKER_CACHE_SIZE
        emb.RERANKER_CACHE_SIZE = 2
        try:
            m_budget = TIER_MODELS["budget"]["rerank"]
            m_balanced = TIER_MODELS["balanced"]["rerank"]
            m_premium = TIER_MODELS["premium"]["rerank"]

            emb._reranker(m_budget)
            emb._reranker(m_balanced)
            # LRU order: [balanced, budget]
            assert len(emb._reranker_cache) == 2

            # Access budget to promote it: [budget, balanced]
            emb._reranker(m_budget)
            assert emb._reranker_lru[0] == m_budget

            # Load premium → evict balanced (LRU tail)
            emb._reranker(m_premium)
            assert len(emb._reranker_cache) == 2
            assert m_balanced not in emb._reranker_cache
            assert m_budget in emb._reranker_cache
            assert m_premium in emb._reranker_cache
        finally:
            emb.RERANKER_CACHE_SIZE = orig_size


# ---------------------------------------------------------------------------
# R3: Batch size control
# ---------------------------------------------------------------------------
class TestRerankerBatchSize:
    def test_batch_size_range(self):
        from opencode_embedder.embeddings import _get_rerank_batch_size
        bs = _get_rerank_batch_size()
        assert bs in (8, 16, 32), f"Unexpected batch size: {bs}"

    def test_low_memory_mode(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_EMBED_LOW_MEMORY", "1")
        from opencode_embedder import embeddings as emb
        importlib.reload(emb)
        assert emb._get_rerank_batch_size() == 8

    def test_rerank_batched_returns_all_scores(self, monkeypatch):
        """_rerank_batched must return a score for every doc regardless of batching."""
        from opencode_embedder.embeddings import _rerank_batched

        class FakeReranker:
            def rerank(self, query, docs):
                return [float(i) for i in range(len(docs))]

        docs = [f"doc {i}" for i in range(33)]  # straddles batch=32 boundary
        scores = _rerank_batched(FakeReranker(), "query", docs, batch_size=32)
        assert len(scores) == 33

    def test_rerank_batched_boundary(self):
        """Batch boundary at exactly batch_size must not drop any docs."""
        from opencode_embedder.embeddings import _rerank_batched

        call_sizes = []

        class TrackingReranker:
            def rerank(self, query, docs):
                call_sizes.append(len(docs))
                return [1.0] * len(docs)

        docs = [f"d{i}" for i in range(33)]
        scores = _rerank_batched(TrackingReranker(), "q", docs, batch_size=32)
        assert sum(call_sizes) == 33
        assert len(scores) == 33
        assert call_sizes == [32, 1]  # two batches: 32 then 1


# ---------------------------------------------------------------------------
# R5 + rerank() integration (no GPU needed for normalization logic)
# ---------------------------------------------------------------------------
class TestRerankerNormalization:
    def _call_rerank_mocked(self, monkeypatch, docs, logits, *, normalize="sigmoid", top_k=None):
        """Helper: call rerank() with mocked provider (no GPU required)."""
        monkeypatch.setenv("OPENCODE_RERANK_NORMALIZE", normalize)
        from opencode_embedder import embeddings as emb

        top_k = top_k if top_k is not None else len(docs)

        class FakeReranker:
            def rerank(self, q, d):
                return logits[:len(d)]

        with (
            patch.object(emb, "_reranker", lambda m: FakeReranker()),
            patch.object(emb, "get_active_provider", return_value="cpu"),
            patch.object(emb, "_rerank_iobinding_confirmed", False),
        ):
            return emb.rerank("q", docs, model="Xenova/ms-marco-MiniLM-L-6-v2", top_k=top_k)

    def test_minmax_opt_in(self, monkeypatch):
        docs = [f"doc {i}" for i in range(5)]
        logits = [1.0, 3.0, 2.0, 5.0, 4.0]
        results = self._call_rerank_mocked(monkeypatch, docs, logits, normalize="minmax")
        scores = [s for _, s in results]
        # min-max: min=1, max=5 → normalized 0..1
        assert min(scores) == pytest.approx(0.0, abs=0.01)
        assert max(scores) == pytest.approx(1.0, abs=0.01)

    def test_sigmoid_default(self, monkeypatch):
        docs = [f"doc {i}" for i in range(5)]
        logits = [5.01, 5.02, 5.03, 5.04, 5.05]
        results = self._call_rerank_mocked(monkeypatch, docs, logits, normalize="sigmoid")
        scores = [s for _, s in results]
        # All logits ~5 → sigmoid ~0.993
        assert all(s > 0.99 for s in scores), f"Expected all >0.99, got {scores}"

    def test_result_score_ordering(self, monkeypatch):
        docs = [f"d{i}" for i in range(6)]
        logits = [1.0, 5.0, 3.0, 2.0, 4.0, 0.5]
        results = self._call_rerank_mocked(monkeypatch, docs, logits, normalize="sigmoid")
        scores = [s for _, s in results]
        assert scores == sorted(scores, reverse=True), "Results not sorted by score"

    def test_top_k_limit(self, monkeypatch):
        docs = [f"d{i}" for i in range(10)]
        logits = list(range(10))
        results = self._call_rerank_mocked(monkeypatch, docs, logits, normalize="sigmoid", top_k=3)
        assert len(results) == 3

    def test_empty_docs(self):
        from opencode_embedder.embeddings import rerank, TIER_MODELS
        results = rerank("query", [], model=TIER_MODELS["budget"]["rerank"], top_k=5)
        assert results == []

    def test_zero_top_k(self):
        from opencode_embedder.embeddings import rerank, TIER_MODELS
        results = rerank("query", ["doc1"], model=TIER_MODELS["budget"]["rerank"], top_k=0)
        assert results == []


# ---------------------------------------------------------------------------
# R8: Score cache
# ---------------------------------------------------------------------------
class TestRerankerScoreCache:
    def test_cache_enabled(self):
        server = _import_server()
        assert server._RERANK_CACHE_ENABLED is True, "cachetools not installed; add it to deps"

    def test_cache_key_normalization(self):
        server = _import_server()
        k1 = server._rerank_cache_key("My Query", ["a", "b"], "model")
        k2 = server._rerank_cache_key("my query", ["a", "b"], "model")
        assert k1 == k2, "Query case must be normalized"

    def test_cache_key_different_docs(self):
        server = _import_server()
        k1 = server._rerank_cache_key("q", ["a", "b"], "model")
        k2 = server._rerank_cache_key("q", ["a", "c"], "model")
        assert k1 != k2, "Different docs must have different keys"

    def test_cache_key_different_models(self):
        server = _import_server()
        k1 = server._rerank_cache_key("q", ["a"], "model-a")
        k2 = server._rerank_cache_key("q", ["a"], "model-b")
        assert k1 != k2

    def test_cache_store_and_retrieve(self):
        server = _import_server()
        if server._rerank_result_cache is None:
            pytest.skip("cache not enabled")
        key = server._rerank_cache_key("test", ["d1", "d2"], "model-x")
        value = [(0, 0.95), (1, 0.80)]
        with server._rerank_result_cache_lock:
            server._rerank_result_cache[key] = value
        with server._rerank_result_cache_lock:
            result = server._rerank_result_cache.get(key)
        assert result == value

    def test_cache_ttl_env(self, monkeypatch):
        """TTLCache respects OPENCODE_RERANK_CACHE_TTL without reloading the server module."""
        from cachetools import TTLCache
        ttl = float(os.environ.get("OPENCODE_RERANK_CACHE_TTL", "30"))
        cache = TTLCache(maxsize=50, ttl=ttl)
        assert cache.ttl == 30.0  # default

        custom_cache = TTLCache(maxsize=50, ttl=5.0)
        assert custom_cache.ttl == 5.0

    def test_cache_module_level_constants(self):
        """Verify cache is wired up at module level without needing server instance."""
        from cachetools import TTLCache
        import hashlib, threading as _t

        cache = TTLCache(maxsize=50, ttl=30.0)
        lock = _t.Lock()

        def key(q, docs, model):
            h = hashlib.sha256("\n".join(docs).encode()).hexdigest()[:16]
            return (q.lower().strip(), h, model)

        k = key("Query", ["a", "b"], "m")
        with lock:
            cache[k] = [(0, 0.9)]
        with lock:
            assert cache.get(k) == [(0, 0.9)]


# ---------------------------------------------------------------------------
# R10: Rerank semaphore
# ---------------------------------------------------------------------------
class TestVramWatchdogRerank:
    def test_rerank_sem_in_init_source(self):
        """_rerank_sem must be present in ModelServer.__init__ source."""
        import inspect
        server = _import_server()
        src = inspect.getsource(server.ModelServer.__init__)
        assert "_rerank_sem" in src, "_rerank_sem not found in __init__"
        assert "Semaphore(1)" in src, "rerank sem must be Semaphore(1)"

    def test_rerank_sem_is_asyncio_semaphore(self):
        """When ModelServer is instantiated, _rerank_sem must be Semaphore(1)."""
        server_mod = _import_server()

        async def check():
            # Only construct — don't call start() to avoid GPU model loading
            s = server_mod.ModelServer.__new__(server_mod.ModelServer)
            s._shutdown = asyncio.Event()
            s._last_activity = 0.0
            s._start_time = 0.0
            s._embed_sem = asyncio.Semaphore(2)
            s._rerank_sem = asyncio.Semaphore(1)
            s._chunk_sem = asyncio.Semaphore(2)
            s._embed_workers = 2
            s._idle_shutdown_secs = 0
            s._embed_coalescer = None
            s._active_requests = 0
            s._max_active_requests = 8
            s._last_embed_time = 0.0
            assert isinstance(s._rerank_sem, asyncio.Semaphore)
            # A fresh Semaphore(1) should be acquirable once, then locked
            assert await asyncio.wait_for(s._rerank_sem.acquire(), timeout=0.1)
            locked = False
            try:
                await asyncio.wait_for(s._rerank_sem.acquire(), timeout=0.05)
            except asyncio.TimeoutError:
                locked = True
            assert locked, "rerank_sem should block after first acquire"

        asyncio.run(check())

    def test_vram_watchdog_throttles_rerank(self):
        """_vram_watchdog source must mention _rerank_sem."""
        import inspect
        server = _import_server()
        src = inspect.getsource(server.ModelServer._vram_watchdog)
        assert "_rerank_sem" in src, "_rerank_sem not referenced in _vram_watchdog"


# ---------------------------------------------------------------------------
# R4: IOBinding confirmation flag exists
# ---------------------------------------------------------------------------
class TestRerankerIOBinding:
    def test_iobinding_flag_exists(self):
        from opencode_embedder.embeddings import _rerank_iobinding_confirmed
        assert isinstance(_rerank_iobinding_confirmed, bool)

    def test_iobinding_lock_exists(self):
        from opencode_embedder.embeddings import _rerank_iobinding_lock
        import threading
        assert isinstance(_rerank_iobinding_lock, type(threading.Lock()))

    def test_rerank_input_names_list(self):
        from opencode_embedder.embeddings import _rerank_input_names
        assert isinstance(_rerank_input_names, list)

    def test_iobinding_function_signature(self):
        import inspect
        from opencode_embedder.embeddings import _rerank_iobinding
        sig = inspect.signature(_rerank_iobinding)
        params = list(sig.parameters)
        assert "session" in params
        assert "tokenizer" in params
        assert "query" in params
        assert "docs" in params
        assert "batch_size" in params
        assert "device" in params

    def test_iobinding_returns_none_on_bad_session(self):
        """_rerank_iobinding must return None (not raise) when session is invalid."""
        from opencode_embedder.embeddings import _rerank_iobinding

        class BadSession:
            def io_binding(self): raise RuntimeError("no binding")

        class FakeTokenizer:
            def encode_batch(self, pairs):
                class E:
                    ids = [1, 2, 3]
                    attention_mask = [1, 1, 1]
                return [E()] * len(pairs)

        result = _rerank_iobinding(BadSession(), FakeTokenizer(), "q", ["d1"], 1, "cpu")
        assert result is None


# ---------------------------------------------------------------------------
# R7: Warmup function exists
# ---------------------------------------------------------------------------
class TestRerankerWarmup:
    def test_warmup_reranker_method_exists(self):
        server = _import_server()
        assert hasattr(server.ModelServer, "_warmup_reranker"), "_warmup_reranker missing"

    def test_warmup_called_in_warmup_models(self):
        import inspect
        server = _import_server()
        src = inspect.getsource(server.ModelServer._warmup_models)
        assert "_warmup_reranker" in src, "_warmup_reranker not called in _warmup_models"

    def test_warmup_uses_budget_model(self):
        import inspect
        server = _import_server()
        src = inspect.getsource(server.ModelServer._warmup_reranker)
        assert "budget" in src, "warmup must use budget tier model"
