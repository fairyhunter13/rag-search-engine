"""P1 GPU smoke: embed on GPU, store in sqlite-vec, search with exact recall."""
import tempfile
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.live


def test_assert_ollama_gpu_raises_when_not_resident():
    """assert_ollama_gpu must raise for a model that is not loaded (not resident)."""
    from opencode_search.core.gpu import assert_ollama_gpu
    with pytest.raises(RuntimeError, match=r"not resident|Cannot reach"):
        assert_ollama_gpu("nonexistent-model-ocs-x:0")


@pytest.mark.slow
def test_ollama_llm_gpu_resident():
    """chat() returns a real answer AND residency assertion passes (full GPU offload)."""
    from opencode_search.core.config import LLM_BASE_URL, LLM_MODEL
    from opencode_search.core.gpu import assert_ollama_gpu
    from opencode_search.graph.llm import chat
    out = chat("Reply with only the word OK.")
    assert out.strip(), "chat() returned empty string"
    assert_ollama_gpu(LLM_MODEL, LLM_BASE_URL)


def test_no_cpu_fallback(cuda_ep):
    import onnxruntime as ort
    assert "CUDAExecutionProvider" in ort.get_available_providers()


def test_embedder_bound_to_cuda(embedder):
    """P32.3: verify the real ONNX session bound to CUDA EP, not just that it's compiled in."""
    providers = embedder._model.model.model.get_providers()
    assert providers[0] == "CUDAExecutionProvider", f"Embedder not on GPU: {providers}"


def test_embed_returns_float16(embedder):
    texts = ["def hello():", "class Foo:", "import os"]
    vecs = embedder.embed(texts)
    assert vecs.dtype == np.float16
    assert vecs.shape == (3, 768)


def test_embed_vectors_normalized(embedder):
    vecs = embedder.embed(["hello world"]).astype(np.float32)
    norm = float(np.linalg.norm(vecs[0]))
    assert abs(norm - 1.0) < 1e-4, f"not unit-normalized: norm={norm}"


def test_vector_store_insert_search(embedder):
    from opencode_search.index.store import VectorStore

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
    from opencode_search.index.store import VectorStore

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
