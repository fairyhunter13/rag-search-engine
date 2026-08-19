"""ONNX Runtime on the GPU: one embedder, one reranker, one lock.

The prefixes are a precondition, not a tuning knob. A local A/B over 10 arms,
2 projects and 330 queries measured a candidate embedder at +0.008 recall@1
without them -- a tie -- and +0.062 with them. The entire margin was the
prefix. FastEmbed supplies none of this (`query_embed` and `passage_embed`
fall through to `embed` unchanged in 0.8.0), so the pipeline must, and an
unknown `side` raises rather than defaulting: embedding a query into the
document space is silent, plausible, and costs a full re-embed to discover.

`_GPU_INFER_LOCK` is the single GPU serializer and the *innermost* lock. It is
held around ORT `Run` and nothing else -- never around SQLite I/O, never while
holding the registry flock. Released between batches, so a user's query
interleaves with a running index build at batch granularity instead of waiting
for a whole project.
"""

from __future__ import annotations

import contextlib
import ctypes
import gc
import logging
import threading
import time

import numpy as np
import onnxruntime as ort
from huggingface_hub import hf_hub_download
from tokenizers import Tokenizer

from . import config, gpu

_GPU_INFER_LOCK = threading.RLock()
_embedder: Embedder | None = None
_reranker: Reranker | None = None
_lock = threading.Lock()

DOCUMENT, QUERY = "document", "query"
MIN_BATCH = 4
log = logging.getLogger(__name__)


def _is_oom(exc: Exception) -> bool:
    """Matched on the message because ORT raises a bare `Fail` for everything.

    Narrow on purpose: a shape or provider error retried at half the batch is
    the same error four times over, and the loop would report the fourth.
    """
    return "Failed to allocate memory" in str(exc)


_last_used = time.monotonic()


def _mark_used() -> None:
    global _last_used
    _last_used = time.monotonic()


def _session(repo: str, filename: str) -> ort.InferenceSession:
    gpu.preload()
    path = hf_hub_download(repo_id=repo, filename=filename)
    # The external-data sidecar, when the export has one. ORT loads it by
    # relative path from beside the graph, so it has to be fetched separately
    # or the session fails at Run with a missing-initializer error.
    with contextlib.suppress(Exception):
        hf_hub_download(repo_id=repo, filename=f"{filename}_data")
    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(path, opts, providers=gpu.providers())


def _tokenizer(repo: str, max_tokens: int) -> Tokenizer:
    tok = Tokenizer.from_file(hf_hub_download(repo_id=repo, filename="tokenizer.json"))
    tok.enable_truncation(max_length=max_tokens)
    tok.enable_padding()
    return tok


def _feed(session: ort.InferenceSession, encodings) -> dict[str, np.ndarray]:
    """Only the inputs this graph declares.

    Exports disagree on `token_type_ids`: passing one the graph does not
    declare is a hard ORT error, and omitting one it does declare is a silent
    accuracy loss on any model that uses segment embeddings.
    """
    ids = np.array([e.ids for e in encodings], dtype=np.int64)
    mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
    feed = {"input_ids": ids, "attention_mask": mask}
    declared = {i.name for i in session.get_inputs()}
    if "token_type_ids" in declared:
        feed["token_type_ids"] = np.array([e.type_ids for e in encodings], dtype=np.int64)
    return {k: v for k, v in feed.items() if k in declared}


def _mean_pool(hidden: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Masked mean over tokens, then L2 normalise.

    The mask is not optional: padding tokens carry real activations, so a plain
    mean makes a chunk's vector depend on how much padding its batch happened
    to need -- the same text embeds differently in two different batches.
    """
    weights = mask[..., None].astype(np.float32)
    summed = (hidden * weights).sum(axis=1)
    counts = np.clip(weights.sum(axis=1), 1e-9, None)
    vectors = summed / counts
    if config.EMBED_TRUNCATE_DIMS:
        # Renormalising after the cut is the whole technique -- a truncated
        # unit vector is not a unit vector, and cosine over it silently ranks
        # by how much norm each chunk happened to keep.
        vectors = vectors[:, : config.EMBED_TRUNCATE_DIMS]
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.clip(norms, 1e-12, None)


class Embedder:
    def __init__(self, repo: str = "", filename: str = ""):
        self.repo = repo or config.EMBED_MODEL
        self.session = _session(self.repo, filename or config.EMBED_ONNX_FILE)
        gpu.verify_session(self.session, f"embedder {self.repo}")
        self.tokenizer = _tokenizer(self.repo, config.EMBED_MAX_TOKENS)
        self.batch = gpu.adaptive_batch(per_item_bytes=config.EMBED_MAX_TOKENS * 4 * 1024)

    def embed(self, texts: list[str], *, side: str) -> np.ndarray:
        if side not in (DOCUMENT, QUERY):
            raise ValueError(f"side must be {DOCUMENT!r} or {QUERY!r}, got {side!r}")
        if not texts:
            return np.zeros((0, config.EMBED_DIMS), dtype=np.float32)

        prefix = config.DOCUMENT_PREFIX if side == DOCUMENT else config.QUERY_PREFIX
        out: list[np.ndarray] = []
        start = 0
        while start < len(texts):
            window = [prefix + t for t in texts[start : start + self.batch]]
            try:
                out.append(self._forward(window))
            except Exception as exc:  # ORT's Fail derives from Exception, not RuntimeError
                if not _is_oom(exc) or self.batch <= MIN_BATCH:
                    raise
                # The batch is sized from free VRAM against a per-item estimate,
                # and that estimate is a guess about a model this code has never
                # seen: nomic's peak is one MLP intermediate per live layer, and
                # the guess was ~10x under it. Halving on the allocator's own
                # answer is the only sizing that stays right for the next model.
                self.batch = max(MIN_BATCH, self.batch // 2)
                log.warning("embed batch OOM; retrying at %d", self.batch)
                continue
            start += len(window)
        return np.concatenate(out).astype(np.float32)

    def _forward(self, window: list[str]) -> np.ndarray:
        encodings = self.tokenizer.encode_batch(window)
        feed = _feed(self.session, encodings)
        with _GPU_INFER_LOCK:
            _mark_used()
            hidden = np.asarray(self.session.run(None, feed)[0], dtype=np.float32)
        return _mean_pool(hidden, feed["attention_mask"])


class Reranker:
    """Cross-encoder over (query, chunk) pairs.

    Verified on the GPU exactly as the embedder is: this is the larger model,
    and it is the one that quietly landed on CPU back when only the embedder
    was checked.
    """

    def __init__(self, repo: str = "", filename: str = ""):
        self.repo = repo or config.RERANK_MODEL
        self.session = _session(self.repo, filename or config.RERANK_ONNX_FILE)
        gpu.verify_session(self.session, f"reranker {self.repo}")
        self.tokenizer = _tokenizer(self.repo, config.RERANK_MAX_TOKENS)

    def score(self, query: str, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros(0, dtype=np.float32)
        out = []
        for start in range(0, len(texts), config.RERANK_BATCH):
            window = texts[start : start + config.RERANK_BATCH]
            encodings = self.tokenizer.encode_batch([(query, t) for t in window])
            feed = _feed(self.session, encodings)
            with _GPU_INFER_LOCK:
                _mark_used()
                logits = self.session.run(None, feed)[0]
            out.append(np.asarray(logits, dtype=np.float32).reshape(len(window), -1)[:, -1])
        return np.concatenate(out)


def get_embedder() -> Embedder:
    global _embedder
    with _lock:
        if _embedder is None:
            _embedder = Embedder()
        return _embedder


def get_reranker() -> Reranker:
    global _reranker
    with _lock:
        if _reranker is None:
            _reranker = Reranker()
        return _reranker


def loaded() -> bool:
    return _embedder is not None or _reranker is not None


def idle_seconds() -> float:
    """How long since the GPU last ran anything.

    Kept here rather than on the server because the thing being unloaded is
    what knows when it was last used: a request-level clock counts a search
    that never reached a model, and misses an hour-long index build entirely.
    """
    return time.monotonic() - _last_used


def release_models() -> None:
    """Drop both models and hand the memory back.

    Measured: an idle daemon holding 12.2 GB with 3.5 GB free on a 16 GB card.
    The ONNX BFC arena never shrinks, so nothing here is automatic -- dropping
    the references frees the CUDA allocations, and `malloc_trim` returns the
    host-side arena that `gc.collect()` leaves sitting in glibc's free lists.

    Measured on this box: 2,322 MiB held, 2,082 MiB returned. The ~240 MiB
    residue is the CUDA context itself, which only process exit frees -- so a
    daemon that has ever loaded a model never returns to zero, and that is the
    floor, not a leak.

    **Callers must not hold a session across this.** Nulling the singletons
    frees nothing while any local still references the model; the first attempt
    at this measurement reclaimed 0 MiB for exactly that reason. Take the
    model, use it, drop it -- never stash one on an object that outlives a
    request.

    A plain public function on purpose. Its reachability matters more than the
    unload policy: the previous engine had this function and nothing that
    called it, which is exactly the 12.2 GB.
    """
    global _embedder, _reranker
    with _lock:
        _embedder = None
        _reranker = None
    gc.collect()
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL("libc.so.6").malloc_trim(0)
