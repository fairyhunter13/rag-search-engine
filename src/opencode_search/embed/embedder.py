"""GPU-only code embedding via FastEmbed-GPU + ONNX Runtime CUDA EP."""
from __future__ import annotations

import threading

import numpy as np

from opencode_search.core.config import EMBED_DEVICE, EMBED_MODEL, RERANK_MODEL, THERMAL_MAX_C
from opencode_search.core.gpu import assert_cuda_available, gpu_temp_c

# Prevents concurrent GPU inference races (embed + rerank on same device).
_GPU_INFER_LOCK = threading.Lock()

# kSameAsRequested + enable_mem_pattern=False stop ORT BFC pre-allocating 24GB on a 16GB GPU.
# cuda_mem_limit was removed in ORT 1.26; arena strategy alone does NOT prevent the allocation —
# enable_mem_pattern=False on SessionOptions is the required companion fix.
_CUDA_PROVIDER_OPTIONS = {"arena_extend_strategy": "kSameAsRequested"}


class Embedder:
    def __init__(self, model: str = EMBED_MODEL, device: str = EMBED_DEVICE):
        if "cpu" in device.lower():
            raise RuntimeError("CPU embedding is forbidden — use device='cuda'.")
        assert_cuda_available()
        self._model_name = model
        self._model = None

    def _init(self) -> None:
        from fastembed import TextEmbedding
        from fastembed.common.onnx_model import OnnxModel

        # FastEmbed only exposes enable_cpu_mem_arena via extra_session_options.
        # Patch the class once to also handle enable_mem_pattern, which prevents
        # ORT BFC arena pre-allocating 24GB (OOM) on the first FusedMatMul call.
        if "enable_mem_pattern" not in OnnxModel.EXPOSED_SESSION_OPTIONS:
            OnnxModel.EXPOSED_SESSION_OPTIONS = (*OnnxModel.EXPOSED_SESSION_OPTIONS, "enable_mem_pattern")
            _orig = OnnxModel.add_extra_session_options.__func__

            @classmethod  # type: ignore[misc]
            def _patched(cls, so, opts):  # type: ignore[misc]
                if "enable_mem_pattern" in opts:
                    so.enable_mem_pattern = opts["enable_mem_pattern"]
                    opts = {k: v for k, v in opts.items() if k != "enable_mem_pattern"}
                _orig(cls, so, opts)

            OnnxModel.add_extra_session_options = _patched

        self._model = TextEmbedding(
            model_name=self._model_name,
            providers=[("CUDAExecutionProvider", _CUDA_PROVIDER_OPTIONS)],
            max_length=512,
            extra_session_options={"enable_mem_pattern": False, "enable_cpu_mem_arena": False},
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
