"""P1 GPU smoke: embed on GPU, store in sqlite-vec, search with exact recall."""
import tempfile
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.live



def test_no_cpu_fallback(cuda_ep):
    import onnxruntime as ort
    assert "CUDAExecutionProvider" in ort.get_available_providers()


def test_embedder_bound_to_gpu(embedder):
    """P32.3: verify the real ONNX session bound to a GPU EP (not just compiled in), not CPU."""
    from rag_search.core.gpu import GPU_EP_NAMES
    providers = embedder._model.model.model.get_providers()
    assert providers[0] in GPU_EP_NAMES, f"Embedder not on GPU: {providers}"
    assert providers[0] != "CPUExecutionProvider", f"Embedder bound to CPU: {providers}"
    # On CUDA GPU host DISABLE_TENSORRT=1, primary must be CUDA.
    assert providers[0] == "CUDAExecutionProvider", f"Expected CUDA primary on this host: {providers}"


def test_reranker_bound_to_gpu(embedder):
    """P32.4: Reranker ONNX session must have a GPU EP as primary (position-0)."""
    from rag_search.core.gpu import GPU_EP_NAMES
    from rag_search.embed.embedder import Reranker
    r = Reranker()
    r._init()
    try:
        providers = r._model.model.model.get_providers()
        assert providers[0] in GPU_EP_NAMES, f"Reranker not on GPU: {providers}"
        assert providers[0] != "CPUExecutionProvider", f"Reranker bound to CPU: {providers}"
        assert providers[0] == "CUDAExecutionProvider", f"Expected CUDA primary on this host: {providers}"
    finally:
        del r


def test_reranker_padding_is_batch_longest(embedder):
    """RP1: no reranker may pad to a fixed length, and the repad must really do the work.

    fastembed calls `enable_padding` only when the tokenizer has none, so a `tokenizer.json`
    shipping `padding.strategy.Fixed` survives into inference and inflates every input to that
    length. gte-reranker-modernbert-base pins 8000: one three-passage rerank then asks the
    allocator for 9.2 GB and dies. The first assertion is what production must hold; the second
    re-pins the live tokenizer and drives the real function over it, so this cannot pass by the
    model simply never having had fixed padding ([[feedback_guard_tests_must_discriminate]]).
    """
    from rag_search.embed.embedder import Reranker, _unpin_tokenizer_padding
    r = Reranker()
    r._init()
    try:
        tok = r._model.model.tokenizer
        assert tok.padding.get("length") is None, (
            f"reranker pads every input to {tok.padding['length']} tokens — dead compute below "
            "the truncation cap, and an allocator failure at it"
        )
        tok.enable_padding(length=8000, pad_id=tok.padding["pad_id"],
                           pad_token=tok.padding["pad_token"])
        assert _unpin_tokenizer_padding(tok) is True, "repad reported no change on fixed padding"
        assert tok.padding.get("length") is None, "repad left the fixed length in place"
        assert _unpin_tokenizer_padding(tok) is False, "repad must be idempotent"
    finally:
        del r


def test_embed_returns_float32(embedder):
    """float32 out, because VectorStore's FLOAT[768] column upcasts anyway — narrowing to
    float16 first saved nothing and only added quantisation error."""
    texts = ["def hello():", "class Foo:", "import os"]
    vecs = embedder.embed(texts)
    assert vecs.dtype == np.float32
    assert vecs.shape == (3, 768)


def test_embed_vectors_normalized(embedder):
    vecs = embedder.embed(["hello world"]).astype(np.float32)
    norm = float(np.linalg.norm(vecs[0]))
    assert abs(norm - 1.0) < 1e-4, f"not unit-normalized: norm={norm}"


def test_vector_store_insert_search(embedder):
    from rag_search.index.store import VectorStore

    texts = [
        "def embed(texts): ...",
        "class Config: pass",
        "SELECT * FROM users",
        "import numpy as np",
        "const x = 1;",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        store = VectorStore(Path(tmp) / "test.db", dim=768)
        vecs = embedder.embed(texts)
        for i, (text, vec) in enumerate(zip(texts, vecs, strict=True)):
            store.insert(i, f"file_{i}.py", 1, 5, "python", text, vec)
        store.flush()
        assert store.count() == len(texts)

        q = embedder.embed(["embed function for texts"])[0].astype(np.float32)
        results = store.search(q, top_k=3)
        assert len(results) == 3
        assert results[0]["path"] == "file_0.py", f"embed text should rank first: {results}"
        store.close()


def test_vector_store_exact_recall(embedder):
    """sqlite-vec flat search must have recall=1.0 on self-queries."""
    from rag_search.index.store import VectorStore

    texts = [f"code snippet number {i}" for i in range(100)]
    with tempfile.TemporaryDirectory() as tmp:
        store = VectorStore(Path(tmp) / "recall.db", dim=768)
        vecs = embedder.embed(texts, batch_size=8)
        for i, (text, vec) in enumerate(zip(texts, vecs, strict=True)):
            store.insert(i, f"f{i}.py", 1, 1, "python", text, vec)
        store.flush()

        hits = 0
        for i in range(10):
            q = vecs[i].astype(np.float32)
            results = store.search(q, top_k=1)
            if results and results[0]["chunk_id"] == i:
                hits += 1
        assert hits == 10, f"Recall@1 should be 1.0, got {hits}/10"
        store.close()
