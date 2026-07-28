"""GPU-only code embedding via FastEmbed-GPU + ONNX Runtime CUDA EP."""
from __future__ import annotations

import logging
import threading

import numpy as np

from rag_search.core.config import (
    EMBED_DEVICE,
    EMBED_MAX_TOKENS,
    EMBED_MODEL,
    RERANK_MODEL,
)
from rag_search.core.gpu import (
    GPU_EP_NAMES,
    assert_gpu_available,
    select_gpu_providers,
)

_log = logging.getLogger(__name__)

# Serialises every GPU-resident ONNX operation in this process: both inference paths AND
# session construction/warm-up. onnxruntime#26610 — several InferenceSession objects on one
# device, driven from different threads, abort or SEGV at *instantiation* as well as at Run(),
# and Run() releases the GIL so daemon threads genuinely overlap on the card. This lock is what
# 9fab658 added for the real cause of the SEGVs; the clean-room rewrite kept it only on the
# rerank path and put a temperature-polling sleep where it had been. RLock, not Lock,
# because embed() holds it across a lazy _init() that takes it again.
_GPU_INFER_LOCK = threading.RLock()

# Query-embed call count, the mirror of query.search._rerank_stats. A federated search must
# embed the query once no matter how many members it spans, and this is the only signal that
# says so: N members embedding one short string separately are fast enough that latency hides it.
_embed_stats: dict = {"calls": 0, "texts": 0}


def embed_stats() -> dict:
    """A copy of the process-lifetime embed counters."""
    return dict(_embed_stats)


class Embedder:
    def __init__(self, model: str = EMBED_MODEL, device: str = EMBED_DEVICE):
        if "cpu" in device.lower():
            raise RuntimeError("CPU embedding is forbidden — use device='cuda'.")
        assert_gpu_available()
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
        if not getattr(ort.SessionOptions, "_rse_no_pattern", False):
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
                self_so.log_severity_level = 3  # suppress benign VerifyEachNodeIsAssignedToAnEp (ORT places shape ops on CPU by design)

            ort.SessionOptions.__init__ = _no_pattern_init  # type: ignore[method-assign]
            ort.SessionOptions._rse_no_pattern = True  # type: ignore[attr-defined]

        self._model = TextEmbedding(
            model_name=self._model_name,
            providers=select_gpu_providers(),
            max_length=EMBED_MAX_TOKENS,
        )
        # FastEmbed reads model_max_length=8192 from tokenizer_config.json and
        # silently ignores the max_length kwarg above.  Force it here so no batch
        # ever exceeds EMBED_MAX_TOKENS — 8192-token sequences cause FusedMatMul
        # to request 24 GB workspace on a 16 GB GPU.  This is a backstop, not the
        # normal path: the chunker targets the same budget, so a chunk that
        # reaches here should already fit.
        self._model.model.tokenizer.enable_truncation(max_length=EMBED_MAX_TOKENS)
        providers = self._model.model.model.get_providers()
        if not providers or providers[0] not in GPU_EP_NAMES:
            raise RuntimeError(f"Embedder not bound to a GPU EP (providers={providers}). CPU inference is forbidden.")

    def warmup(self) -> None:
        with _GPU_INFER_LOCK:
            if self._model is None:
                self._init()
            list(self._model.embed(["warmup"], batch_size=1))

    def embed(self, texts: list[str], batch_size: int = 8) -> np.ndarray:
        """Embed on GPU; returns normalized float32 array of shape (n, 768).

        Deliberately not float16. VectorStore stores FLOAT[768] and upcasts on the way in,
        so narrowing here bought no space at all — it only added quantisation error to every
        vector on the way to a float32 column. The cost of dropping it is transient: a bulk
        index holds n x 768 x 4 bytes, ~390 MB rather than ~195 MB on the largest repo here.
        """
        with _GPU_INFER_LOCK:
            if self._model is None:
                self._init()
            _embed_stats["calls"] += 1
            _embed_stats["texts"] += len(texts)
            raw = np.array(list(self._model.embed(texts, batch_size=batch_size)), dtype=np.float32)
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return raw / norms

    @property
    def dim(self) -> int:
        with _GPU_INFER_LOCK:
            if self._model is None:
                self._init()
        meta = self._model._get_model_description(self._model_name)
        return int(meta.get("dim", 768))


# Rerankers fastembed has no built-in description for. Its registry is a curated list, not a
# capability boundary — `add_custom_model` serves any HF repo carrying an ONNX export, which
# gte-reranker-modernbert-base does (`onnx/model.onnx`, Apache-2.0). Keeping the table explicit
# rather than registering whatever RSE_RERANK_MODEL names means a typo still fails loudly
# instead of becoming a download attempt against a repo nobody vetted.
_CUSTOM_RERANKERS = {
    "Alibaba-NLP/gte-reranker-modernbert-base": (0.6, "apache-2.0"),
}


def _register_custom_reranker(model: str) -> None:
    """Teach fastembed a reranker it does not ship, idempotently. No-op for built-ins."""
    from fastembed.common.model_description import ModelSource
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    if any(m["model"] == model for m in TextCrossEncoder.list_supported_models()):
        return
    if model not in _CUSTOM_RERANKERS:
        raise RuntimeError(
            f"RSE_RERANK_MODEL={model!r} is neither a fastembed built-in nor a vetted custom "
            f"reranker (known: {sorted(_CUSTOM_RERANKERS)})"
        )
    size_gb, lic = _CUSTOM_RERANKERS[model]
    TextCrossEncoder.add_custom_model(
        model, ModelSource(hf=model), size_in_gb=size_gb, license=lic
    )


def _unpin_tokenizer_padding(tokenizer) -> bool:
    """Repad to batch-longest when a repo pins a fixed padding length. True if it changed.

    fastembed only calls `enable_padding` when the tokenizer has none, so a `tokenizer.json`
    shipping `padding.strategy.Fixed` is honoured verbatim — every input is inflated to that
    length no matter how short it is. gte-reranker-modernbert-base pins 8000, which makes one
    three-passage rerank ask for 3 x 12 heads x 8000^2 x 4 B = 9.2 GB and die in the allocator,
    while its own truncation is capped at 512. Fixed padding is never right for a reranker:
    below the cap it is dead compute, at the cap it is fatal.
    """
    pad = tokenizer.padding
    if not pad or pad.get("length") is None:
        return False
    tokenizer.enable_padding(pad_id=pad["pad_id"], pad_token=pad["pad_token"])
    return True


class Reranker:
    """Cross-encoder reranker on GPU. Model from RSE_RERANK_MODEL; see _CUSTOM_RERANKERS."""

    def __init__(self, model: str = RERANK_MODEL) -> None:
        assert_gpu_available()
        self._model_name = model
        self._model = None

    def _init(self) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        _register_custom_reranker(self._model_name)
        self._model = TextCrossEncoder(
            model_name=self._model_name,
            providers=select_gpu_providers(),
        )
        _unpin_tokenizer_padding(self._model.model.tokenizer)
        providers = self._model.model.model.get_providers()
        if not providers or providers[0] not in GPU_EP_NAMES:
            raise RuntimeError(f"Reranker not bound to a GPU EP (providers={providers}). CPU inference is forbidden.")

    def rerank(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        with _GPU_INFER_LOCK:
            if self._model is None:
                self._init()
            scores = list(self._model.rerank(query, passages))
        return [float(s) for s in scores]


_default: Embedder | None = None


def get_embedder() -> Embedder:
    global _default
    if _default is None:
        _default = Embedder()
        _default.warmup()
    return _default
