"""Tests for model warmup and pre-loading functionality.

These tests verify that the server correctly pre-warms models on startup
to avoid first-request latency.
"""

import time


class TestOnnxThreadScaling:
    """Test ONNX thread count scaling based on CPU count."""

    def test_thread_env_vars_are_set(self):
        """Verify OMP_NUM_THREADS and MKL_NUM_THREADS are set after import."""
        import os
        from opencode_embedder import embeddings

        # These should be set by _limit_onnx_threads() at import time
        assert "OMP_NUM_THREADS" in os.environ
        assert "MKL_NUM_THREADS" in os.environ

    def test_thread_count_matches_cpu_scaling(self):
        """Verify thread count follows the CPU-based scaling rules."""
        import os
        from opencode_embedder import embeddings

        cpus = embeddings._cpus
        threads = int(os.environ.get("OMP_NUM_THREADS", "1"))

        # Scaling rules:
        #   ≤4 CPUs:  1 thread
        #   5-16 CPUs: 2 threads
        #   >16 CPUs: 4 threads
        if cpus <= 4:
            assert threads == 1, f"Expected 1 thread for {cpus} CPUs, got {threads}"
        elif cpus <= 16:
            assert threads == 2, f"Expected 2 threads for {cpus} CPUs, got {threads}"
        else:
            assert threads == 4, f"Expected 4 threads for {cpus} CPUs, got {threads}"

    def test_hf_hub_disable_xet_is_set(self):
        """Verify HF_HUB_DISABLE_XET is set to prevent xet protocol issues."""
        import os
        from opencode_embedder import embeddings

        assert os.environ.get("HF_HUB_DISABLE_XET") == "1"


class TestEmbedderCaching:
    """Test that the embedder model is properly cached."""

    def test_embedder_is_cached(self):
        """Verify the same embedder instance is returned on repeated calls."""
        from opencode_embedder.embeddings import _embedder, cleanup_models

        # Clean up first
        cleanup_models()

        model = "jinaai/jina-embeddings-v2-small-en"

        # First call loads the model
        embedder1 = _embedder(model)
        # Second call should return the same instance
        embedder2 = _embedder(model)

        assert embedder1 is embedder2, "Embedder should be cached"

    def test_different_model_creates_new_embedder(self):
        """Verify different model names create different embedders."""
        import pytest
        from opencode_embedder.embeddings import _embedder, cleanup_models

        cleanup_models()

        model1 = "jinaai/jina-embeddings-v2-small-en"
        model2 = "BAAI/bge-small-en-v1.5"

        embedder1 = _embedder(model1)
        try:
            embedder2 = _embedder(model2)
        except Exception:
            # model2 may be incompatible with the active provider (e.g. TensorRT
            # requires shape inference on bge models). Skip in that case.
            pytest.skip("model2 incompatible with active ONNX provider")

        # Different models should have different cached embedders
        # (the second call replaces the cache)
        assert embedder1 is not embedder2

    def test_cleanup_clears_cache(self):
        """Verify cleanup_models clears the cached embedder."""
        from opencode_embedder.embeddings import (
            _embedder,
            cleanup_models,
            _cached_embedder,
        )

        model = "jinaai/jina-embeddings-v2-small-en"
        _embedder(model)

        cleanup_models()

        from opencode_embedder import embeddings

        assert embeddings._cached_embedder is None
        assert embeddings._cached_embedder_model is None


class TestChunkerCaching:
    """Test that chunkers are properly cached."""

    def test_code_chunker_is_cached(self):
        """Verify tree-sitter CodeChunker is cached per language."""
        from opencode_embedder import chunker

        # Clear cache
        chunker.cleanup_chunkers()

        # First call creates the chunker
        ts_chunker1 = chunker._get_code_chunker("python")
        # Second call should return the same instance
        ts_chunker2 = chunker._get_code_chunker("python")

        assert ts_chunker1 is ts_chunker2, "CodeChunker should be cached"

    def test_different_languages_have_different_chunkers(self):
        """Verify different languages get different CodeChunker instances."""
        from opencode_embedder import chunker

        chunker.cleanup_chunkers()

        py_chunker = chunker._get_code_chunker("python")
        ts_chunker = chunker._get_code_chunker("typescript")

        assert py_chunker is not ts_chunker

    def test_semantic_chunker_is_cached(self):
        """Verify SemanticChunker is cached (singleton)."""
        from opencode_embedder import chunker

        chunker.cleanup_chunkers()

        # First call creates the chunker
        sem1 = chunker._get_semantic_chunker()
        # Second call should return the same instance
        sem2 = chunker._get_semantic_chunker()

        assert sem1 is sem2, "SemanticChunker should be cached"

    def test_token_chunker_is_cached(self):
        """Verify TokenChunker is cached (singleton)."""
        from opencode_embedder import chunker

        chunker.cleanup_chunkers()

        tok1 = chunker._get_token_chunker()
        tok2 = chunker._get_token_chunker()

        assert tok1 is tok2, "TokenChunker should be cached"

    def test_cleanup_clears_all_chunkers(self):
        """Verify cleanup_chunkers clears all cached chunkers."""
        from opencode_embedder import chunker

        # Load some chunkers
        chunker._get_code_chunker("python")
        chunker._get_token_chunker()

        # Clean up
        chunker.cleanup_chunkers()

        assert len(chunker._code_chunkers) == 0
        assert chunker._token_chunker is None


class TestWarmupPerformance:
    """Test that warmup improves first-request performance."""

    def test_cached_embedder_is_fast(self):
        """Verify embedding after warmup is fast (no model loading)."""
        from opencode_embedder.embeddings import embed_passages, _embedder, get_active_provider

        model = "jinaai/jina-embeddings-v2-small-en"

        # Warmup: load the model
        _embedder(model)

        # For MIGraphX, we need to run once first to compile for this input shape
        # MIGraphX compiles models per unique input shape, not just once
        texts = ["test text for embedding"]
        embed_passages(texts, model=model, dimensions=512)  # Compile/warmup run

        # Measure embedding time with cached model (same input shape)
        start = time.perf_counter()
        embed_passages(texts, model=model, dimensions=512)
        elapsed = time.perf_counter() - start

        # Should be fast since model is loaded and (for MIGraphX) compiled
        # MIGraphX in-memory cache is fast, but we allow more time for GPU providers
        provider = get_active_provider()
        max_time = 5.0 if provider == "migraphx" else 1.0
        assert elapsed < max_time, (
            f"Embedding with cached model took too long: {elapsed:.2f}s (provider={provider})"
        )

    def test_cached_chunker_is_fast(self):
        """Verify chunking after warmup is fast (no grammar loading)."""
        from pathlib import Path
        from opencode_embedder import chunker

        # Warmup: load the Python chunker
        chunker._get_code_chunker("python")

        # Measure chunking time with cached chunker
        content = "def hello():\n    return 'world'\n" * 100
        start = time.perf_counter()
        chunks = chunker.chunk_file(content, Path("test.py"))
        elapsed = time.perf_counter() - start

        # Should be fast (< 100ms) since chunker is already loaded
        assert elapsed < 0.1, f"Chunking with cached chunker took too long: {elapsed:.3f}s"
        assert len(chunks) >= 1


class TestServerWarmup:
    """Test the server's warmup functionality."""

    def test_warmup_embedder_loads_model(self):
        """Verify _warmup_embedder loads the budget tier embedding model."""
        from opencode_embedder.embeddings import cleanup_models, _cached_embedder
        from opencode_embedder.server import ModelServer

        cleanup_models()

        server = ModelServer()
        server._warmup_embedder()

        from opencode_embedder import embeddings

        assert embeddings._cached_embedder is not None
        assert embeddings._cached_embedder_model == "jinaai/jina-embeddings-v2-small-en"

    def test_warmup_chunkers_loads_languages(self):
        """Verify _warmup_chunkers loads common language chunkers."""
        from opencode_embedder import chunker
        from opencode_embedder.server import ModelServer

        chunker.cleanup_chunkers()

        server = ModelServer()
        server._warmup_chunkers()

        # Should have loaded multiple languages
        assert len(chunker._code_chunkers) >= 5

        # Should include common languages
        expected_langs = ["python", "typescript", "javascript", "rust", "go"]
        for lang in expected_langs:
            ts_lang = chunker._LANG_TO_TREESITTER.get(lang)
            if ts_lang:
                assert ts_lang in chunker._code_chunkers, f"Missing chunker for {lang}"

    def test_warmup_chunkers_loads_semantic_chunker(self):
        """Verify _warmup_chunkers loads the SemanticChunker."""
        from opencode_embedder import chunker
        from opencode_embedder.server import ModelServer

        chunker.cleanup_chunkers()

        server = ModelServer()
        server._warmup_chunkers()

        # SemanticChunker should be loaded
        assert chunker._semantic_chunker is not None


class TestEmbedPassagesOutput:
    """Test embed_passages function output format."""

    def test_returns_list_of_vectors(self):
        """Verify embed_passages returns list of float vectors."""
        from opencode_embedder.embeddings import embed_passages

        texts = ["hello", "world"]
        model = "jinaai/jina-embeddings-v2-small-en"

        vectors = embed_passages(texts, model=model, dimensions=512)

        assert isinstance(vectors, list)
        assert len(vectors) == 2
        for vec in vectors:
            assert isinstance(vec, list)
            assert len(vec) == 512
            assert all(isinstance(v, float) for v in vec)

    def test_vectors_are_normalized(self):
        """Verify output vectors are unit normalized."""
        import math
        from opencode_embedder.embeddings import embed_passages

        texts = ["test normalization"]
        model = "jinaai/jina-embeddings-v2-small-en"

        vectors = embed_passages(texts, model=model, dimensions=512)

        for vec in vectors:
            norm = math.sqrt(sum(x * x for x in vec))
            assert abs(norm - 1.0) < 0.01, f"Vector not normalized: {norm}"

    def test_handles_long_text(self):
        """Verify long text is handled (truncated to MAX_TOKENS)."""
        from opencode_embedder.embeddings import embed_passages

        # Very long text (should be truncated)
        long_text = "word " * 10000  # ~10000 tokens
        model = "jinaai/jina-embeddings-v2-small-en"

        vectors = embed_passages([long_text], model=model, dimensions=512)

        assert len(vectors) == 1
        assert len(vectors[0]) == 512

    def test_handles_batch_of_texts(self):
        """Verify batching works correctly for multiple texts."""
        from opencode_embedder.embeddings import embed_passages

        texts = [f"text number {i}" for i in range(50)]
        model = "jinaai/jina-embeddings-v2-small-en"

        vectors = embed_passages(texts, model=model, dimensions=512)

        assert len(vectors) == 50
        for vec in vectors:
            assert len(vec) == 512
