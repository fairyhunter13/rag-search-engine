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
        import onnxruntime as ort
        from fastembed import TextEmbedding

        # FastEmbed filters extra_session_options by EXPOSED_SESSION_OPTIONS before
        # passing to _load_onnx_model — enable_mem_pattern is silently dropped.
        # Patch ort.SessionOptions.__init__ directly so every session in this process
        # gets enable_mem_pattern=False, stopping BFC arena from pre-allocating 24GB
        # (exceeds the 16GB GPU) on the first FusedMatMul call.
        if not getattr(ort.SessionOptions, "_ocs_no_pattern", False):
            _orig_so_init = ort.SessionOptions.__init__

            def _no_pattern_init(self_so: ort.SessionOptions) -> None:
                _orig_so_init(self_so)
                self_so.enable_mem_pattern = False
                self_so.enable_cpu_mem_arena = False
                # GPU-only: 1 CPU thread per session is sufficient; no spinning saves idle CPU.
                self_so.intra_op_num_threads = 1
                self_so.inter_op_num_threads = 1
                self_so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                self_so.add_session_config_entry("session.intra_op.allow_spinning", "0")
                self_so.add_session_config_entry("session.inter_op.allow_spinning", "0")

            ort.SessionOptions.__init__ = _no_pattern_init  # type: ignore[method-assign]
            ort.SessionOptions._ocs_no_pattern = True  # type: ignore[attr-defined]

        self._model = TextEmbedding(
            model_name=self._model_name,
            providers=[("CUDAExecutionProvider", _CUDA_PROVIDER_OPTIONS)],
            max_length=512,
        )
        # FastEmbed reads model_max_length=8192 from tokenizer_config.json and
        # silently ignores the max_length=512 kwarg above.  Force it here so
        # no batch ever produces sequences longer than 512 tokens — 8192-token
        # sequences cause FusedMatMul to request 24 GB workspace on a 16 GB GPU.
        self._model.model.tokenizer.enable_truncation(max_length=512)
        providers = self._model.model.model.get_providers()
        if not providers or providers[0] != "CUDAExecutionProvider":
            raise RuntimeError(f"Embedder not using CUDAExecutionProvider as primary EP (providers={providers}). CPU inference is forbidden.")

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
        providers = self._model.model.model.get_providers()
        if not providers or providers[0] != "CUDAExecutionProvider":
            raise RuntimeError(f"Reranker not using CUDAExecutionProvider as primary EP (providers={providers}). CPU inference is forbidden.")

    def rerank(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        if self._model is None:
            self._init()
        with _GPU_INFER_LOCK:
            scores = list(self._model.rerank(query, passages))
        return [float(s) for s in scores]


_default: Embedder | None = None


def get_embedder() -> Embedder:
    global _default
    if _default is None:
        _default = Embedder()
        _default.warmup()
    return _default
