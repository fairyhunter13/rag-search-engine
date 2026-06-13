"""GPU-only code embedding via FastEmbed-GPU + ONNX Runtime CUDA EP."""
from __future__ import annotations

import os
import threading

import numpy as np

from opencode_search.core.config import (
    EMBED_DEVICE,
    EMBED_MODEL,
    ONNX_ARENA_MB,
    RERANK_MODEL,
    THERMAL_MAX_C,
)
from opencode_search.core.gpu import assert_cuda_available, gpu_temp_c

os.environ.setdefault("ORT_MAX_MEM_LIMIT_MB", str(ONNX_ARENA_MB))

# Prevents concurrent GPU inference races (embed + rerank on same device).
_GPU_INFER_LOCK = threading.Lock()


class Embedder:
    def __init__(self, model: str = EMBED_MODEL, device: str = EMBED_DEVICE):
        if "cpu" in device.lower():
            raise RuntimeError("CPU embedding is forbidden — use device='cuda'.")
        assert_cuda_available()
        self._model_name = model
        self._model = None

    def _init(self) -> None:
        from fastembed import TextEmbedding
        self._model = TextEmbedding(
            model_name=self._model_name,
            providers=["CUDAExecutionProvider"],
            max_length=512,
        )

    def warmup(self) -> None:
        if self._model is None:
            self._init()
        list(self._model.embed(["warmup"], batch_size=1))

    def embed(self, texts: list[str], batch_size: int = 8) -> np.ndarray:
        """Embed on GPU; returns normalized float16 array of shape (n, 768)."""
        if self._model is None:
            self._init()
        temp = gpu_temp_c()
        if temp > THERMAL_MAX_C:
            raise RuntimeError(f"GPU too hot ({temp:.0f}°C > {THERMAL_MAX_C}°C).")
        raw = np.array(list(self._model.embed(texts, batch_size=batch_size)), dtype=np.float32)
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (raw / norms).astype(np.float16)

    @property
    def dim(self) -> int:
        if self._model is None:
            self._init()
        meta = self._model._get_model_description(self._model_name)
        return int(meta.get("dim", 768))


class Reranker:
    """Cross-encoder reranker (jina-reranker-v1-turbo-en) on GPU."""

    def __init__(self, model: str = RERANK_MODEL) -> None:
        assert_cuda_available()
        self._model_name = model
        self._model = None

    def _init(self) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        self._model = TextCrossEncoder(
            model_name=self._model_name,
            providers=["CUDAExecutionProvider"],
        )

    def rerank(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        if self._model is None:
            try:
                self._init()
            except Exception:
                return [1.0] * len(passages)
        with _GPU_INFER_LOCK:
            scores = list(self._model.rerank(query, passages))
        return [float(s) for s in scores]


_default: Embedder | None = None


def get_embedder() -> Embedder:
    global _default
    if _default is None:
        _default = Embedder()
    return _default
