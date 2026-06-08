# Reranker Implementation Plan

## Current State — Problems

The reranker is functional but has 5 significant problems:

1. **All tiers use the same model** — `Xenova/ms-marco-MiniLM-L-6-v2` for budget/balanced/premium. Premium users get budget-quality reranking.
2. **No IOBinding** — embedder has a fast `_try_embed_iobinding` path; reranker does plain `reranker.rerank()` with unnecessary host↔device tensor copies.
3. **Min-max score normalization is fragile** — if all docs score between 5.01 and 5.09, normalization spreads noise across [0,1] and rank-1 is meaningless.
4. **Single model cache** — switching tiers reloads the model with a full `gc.collect()` + CUDA sync (~3–5s penalty).
5. **No reranker warmup** — `_warmup_models()` pre-loads embedder but not reranker. First search query pays 2–5s cold-start penalty.

---

## Phase R1 — Tier-Specific Reranker Models

**Goal:** Assign a distinct cross-encoder model to each tier, differentiated by size, speed, and code awareness.

| Tier | Model | Params | VRAM | Strength |
|---|---|---|---|---|
| budget | `Xenova/ms-marco-MiniLM-L-6-v2` | 22M | ~85MB | Fast, web passage ranking |
| balanced | `jinaai/jina-reranker-v1-turbo-en` | 37M | ~150MB | Code-aware, 2× better than MiniLM |
| premium | `jinaai/jina-reranker-v2-base-multilingual` | 278M | ~560MB | Best-in-class code + multilingual |

**Steps:**
1. Update `TIER_MODELS` in `embeddings.py`:
   ```python
   TIER_MODELS = {
       "premium":  {
           "embed":  "jinaai/jina-embeddings-v2-base-code",
           "rerank": "jinaai/jina-reranker-v2-base-multilingual",
       },
       "balanced": {
           "embed":  "jinaai/jina-embeddings-v2-base-en",
           "rerank": "jinaai/jina-reranker-v1-turbo-en",
       },
       "budget":   {
           "embed":  "jinaai/jina-embeddings-v2-small-en",
           "rerank": "Xenova/ms-marco-MiniLM-L-6-v2",
       },
   }
   ```
2. Update `models_for_tier()` in `cli.py` to match
3. VRAM budget with all three simultaneously: 85 + 150 + 560 = ~795MB — safe on RTX 5080

**Risk:** Low. All models supported by fastembed, download from HuggingFace on first use.

---

## Phase R2 — Multi-Model LRU Reranker Cache

**Goal:** Replace single `_cached_reranker` pair with an LRU cache holding up to `RERANKER_CACHE_SIZE` (default 2) loaded rerankers. Eliminates 3–5s reload penalty when switching tiers.

**Steps:**
1. Replace single-model globals:
   ```python
   # Before
   _cached_reranker: object | None = None
   _cached_reranker_model: str | None = None

   # After
   _reranker_cache: dict[str, object] = {}   # model_name → reranker instance
   _reranker_lru: list[str] = []             # index 0 = most recently used
   _reranker_cache_lock = threading.Lock()
   RERANKER_CACHE_SIZE = int(os.environ.get("OPENCODE_RERANKER_CACHE_SIZE", "2"))
   ```
2. `_reranker(model)` logic:
   - Cache hit: move model to front of LRU list, return cached instance
   - Cache miss + cache full: evict tail entry (`del old; gc.collect(); gc.collect(); _cuda_sync_and_empty_cache()`)
   - Cache miss: load model, add to front of LRU list
3. `cleanup_models()`: iterate all cache entries, evict each with full CUDA sync
4. All reads/writes inside `_reranker_cache_lock`

**VRAM math with `RERANKER_CACHE_SIZE=2`:** budget + premium = 85 + 560 = 645MB — safe alongside 6 embed workers (~3.3GB for jina-code × 6).

**Risk:** Low.

---

## Phase R3 — Explicit Batch Size Control

**Goal:** Replace fastembed's internal batching with explicit controlled batching sized to GPU VRAM.

**Steps:**
1. Add `_get_rerank_batch_size()`:
   ```python
   def _get_rerank_batch_size() -> int:
       if _LOW_MEMORY_MODE: return 8
       vram_mb = _get_gpu_vram_mb()
       if vram_mb is None: return 16
       vram_gb = vram_mb / 1024
       if vram_gb < 8:  return 8
       if vram_gb < 16: return 16
       return 32   # RTX 5080: 32 pairs per batch
   ```
   Memory estimate: jina-reranker-v2 with batch=32, 512 tokens → ~600MB activation; safe alongside embed workers.

2. Add `_rerank_batched(reranker, query, docs, batch_size)`:
   ```python
   def _rerank_batched(reranker, query: str, docs: list[str], batch_size: int) -> list[float]:
       all_scores = []
       for i in range(0, len(docs), batch_size):
           batch = docs[i : i + batch_size]
           all_scores.extend(reranker.rerank(query, batch))
       return all_scores
   ```

3. Replace `scores = list(reranker.rerank(query, docs))` in `rerank()` with `_rerank_batched(reranker, query, docs, get_rerank_batch_size())`

**Risk:** Low.

---

## Phase R4 — IOBinding for Cross-Encoder

**Goal:** Add `_rerank_iobinding()` mirroring the embedder's `_try_embed_iobinding()`. Avoids redundant CPU↔GPU tensor copies for each batch.

**Steps:**
1. Implement `_rerank_iobinding(session, tokenizer, query, docs, batch_size, device)`:
   ```python
   def _rerank_iobinding(session, tokenizer, query, docs, batch_size, device="cuda"):
       import onnxruntime as ort
       all_scores = []
       pairs = [[query, doc] for doc in docs]
       try:
           for i in range(0, len(pairs), batch_size):
               batch = pairs[i : i + batch_size]
               encoded = tokenizer.encode_batch(batch)
               input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
               attn_mask  = np.array([e.attention_mask for e in encoded], dtype=np.int64)

               binding = session.io_binding()
               ids_gpu  = ort.OrtValue.ortvalue_from_numpy(input_ids, device)
               mask_gpu = ort.OrtValue.ortvalue_from_numpy(attn_mask, device)
               binding.bind_ortvalue_input("input_ids", ids_gpu)
               binding.bind_ortvalue_input("attention_mask", mask_gpu)
               for out in session.get_outputs():
                   binding.bind_output(out.name, device)   # logits stay on GPU

               session.run_with_iobinding(binding)
               logits = binding.get_outputs()[0].numpy()   # single GPU→CPU copy
               if logits.ndim > 1:
                   logits = logits.squeeze(-1)
               all_scores.extend(logits.tolist())
               del ids_gpu, mask_gpu, binding
           return all_scores
       except Exception as e:
           log.debug("reranker IOBinding failed: %s", e)
           return None
   ```

2. Add `_rerank_iobinding_confirmed` flag (same pattern as `_io_binding_confirmed` for embedder); set it in `_verify_onnx_session_provider` for reranker model

3. In `rerank()`: try IOBinding first if `_rerank_iobinding_confirmed`; fall back to `_rerank_batched()`

4. Extract ONNX session and tokenizer: `session = reranker_obj.model.model; tokenizer = reranker_obj.model.tokenizer`

5. Add input name probe at model load time to detect binding names (handles `token_type_ids` for some models):
   ```python
   _rerank_input_names = [inp.name for inp in session.get_inputs()]
   # Expect: ["input_ids", "attention_mask"] or ["input_ids", "attention_mask", "token_type_ids"]
   ```

**Risk:** Medium. Input names vary by model. Add probe at load time; fall back to standard batch path if names don't match `["input_ids", "attention_mask"]`.

---

## Phase R5 — Sigmoid Score Calibration

**Goal:** Replace fragile min-max normalization with sigmoid calibration that preserves absolute meaning of cross-encoder logit scores.

**Why min-max is wrong:**
- Docs scoring `[5.01, 5.02, 5.03, 5.04, 5.05]` → min-max gives `[0.0, 0.25, 0.5, 0.75, 1.0]` — completely artificial spread
- Sigmoid gives `[0.9933, 0.9934, 0.9934, 0.9934, 0.9934]` — correctly shows all are high-confidence matches

**Steps:**
1. Add `_calibrate_scores(logits, temperature)`:
   ```python
   def _calibrate_scores(logits, temperature: float = 1.0) -> np.ndarray:
       arr = np.asarray(logits, dtype=np.float32)
       return 1.0 / (1.0 + np.exp(-arr / temperature))
   ```

2. Temperature defaults per model:
   ```python
   RERANK_TEMPERATURE: dict[str, float] = {
       "Xenova/ms-marco-MiniLM-L-6-v2":              1.0,
       "jinaai/jina-reranker-v1-turbo-en":            1.0,
       "jinaai/jina-reranker-v2-base-multilingual":   1.0,
   }
   ```
   Override via `OPENCODE_RERANK_TEMPERATURE` env var.

3. Replace normalization block in `rerank()`:
   ```python
   temperature = float(os.environ.get(
       "OPENCODE_RERANK_TEMPERATURE",
       str(RERANK_TEMPERATURE.get(model, 1.0))
   ))
   normed = _calibrate_scores(scores, temperature)
   order = np.argsort(normed)[::-1][:top_k]
   return [(int(i), float(normed[i])) for i in order]
   ```

4. Keep min-max as opt-in via `OPENCODE_RERANK_NORMALIZE=minmax` for backward compatibility

**Risk:** Low. Sigmoid is monotonic — ordering is preserved. Scores change numerically but ranking is unchanged.

---

## Phase R6 — Two-Stage Reranking in Search Pipeline

**Goal:** Implement two-stage strategy in `search.py`. All ML calls are now direct in-process.

**Architecture:**
```
Stage 1 (per-project, parallelized):
hybrid retrieval (vector + FTS) → top 20 candidates
cross-encoder rerank (always) → keep top 15

Stage 2 (global, federated):
gather stage-1 outputs from all N projects → up to N×15 candidates
cross-encoder global rerank (always) → final top 10
```

**Constants:**
```python
STAGE1_VECTOR_K = 20        # hybrid retrieval candidates per project
STAGE1_RERANK_K = 15        # keep after per-project rerank
GLOBAL_RERANK_MAX = 100     # cap before global rerank (prevents VRAM spike)
FINAL_TOP_K = 10            # final results returned
OPENCODE_RERANK_CONCURRENCY = 1  # limit concurrent rerank calls (VRAM safety)
```

**Steps:**
1. Stage 1 per project:
   - Hybrid retrieval: top `STAGE1_VECTOR_K` (optionally oversampled)
   - Per-project cross-encoder rerank (always), keep top `STAGE1_RERANK_K`
   - Use `OPENCODE_RERANK_CONCURRENCY` to cap concurrent rerank calls (VRAM safety)

2. Federated stage:
   - Merge per-project outputs
   - Deduplicate by chunk identity/path
   - Global cross-encoder rerank (always), return top `FINAL_TOP_K`

**Risk:** Low — direct translation of Rust `search.rs` logic.

---

## Phase R7 — Reranker Warmup

**Goal:** Add reranker warmup to startup. Eliminates 2–5s cold-start on first search.

**Steps:**
1. Add `_warmup_reranker()`:
   ```python
   def _warmup_reranker(self) -> None:
       model = TIER_MODELS["budget"]["rerank"]
       try:
           rerank("warmup", ["warmup document a", "warmup document b"],
                  model=model, top_k=2)
           log.info("reranker model loaded: %s", model)
       except Exception as e:
           log.warning("reranker warmup failed %s: %s", model, e)
   ```

2. Call in `_warmup_models()` after `_warmup_embedder()`:
   ```python
   await asyncio.to_thread(self._warmup_reranker)
   ```

3. Only warm up budget tier at startup (22M model, ~0.5s). Balanced/premium load on first use (~1–3s).

4. Set `_rerank_iobinding_confirmed` during warmup via `_verify_onnx_session_provider(reranker_obj, "reranker")`

**Risk:** Low.

---

## Phase R8 — Reranker Score Cache

**Goal:** Cache `(query, docs_hash, model)` → ranked results. Avoids re-running expensive cross-encoder on repeated (query, doc set) pairs.

**Steps:**
1. Add `TTLCache` in `search.py`:
   ```python
   from cachetools import TTLCache
   import hashlib

   _rerank_result_cache: TTLCache = TTLCache(
       maxsize=int(os.environ.get("OPENCODE_RERANK_CACHE_SIZE", "50")),
       ttl=float(os.environ.get("OPENCODE_RERANK_CACHE_TTL", "30")),
   )
   _rerank_result_cache_lock = threading.Lock()

   def _rerank_cache_key(query: str, docs: list[str], model: str) -> tuple:
       docs_hash = hashlib.sha256("\n".join(docs).encode()).hexdigest()[:16]
       return (query.lower().strip(), docs_hash, model)
   ```

2. Check cache before GPU inference; store after; no explicit invalidation (TTL handles staleness — when doc content changes, `docs_hash` changes naturally)

**Risk:** Low.

---

## Phase R9 — FP16 for Reranker

**Goal:** Apply FP16 session options to reranker (already partially applied; verify coverage).

**Steps:**
1. `_reranker()` already passes same session options as `_embedder()` — this is correct
2. FP16 is handled by:
   - CUDA: cuDNN picks FP16 algorithms when tensor cores available (`cudnn_conv_algo_search=EXHAUSTIVE`)
   - TensorRT: `trt_fp16_enable=True` already in `_gpu_provider_options()` — applies to reranker too
3. Verify in `_verify_onnx_session_provider(reranker_obj, "reranker")`: confirm CUDA/TRT active, log FP16 status
4. Add FP16 flag to `get_gpu_stats()` output

**Risk:** Low — session options already applied.

---

## Phase R10 — VRAM Watchdog Integration

**Goal:** Extend `_vram_watchdog` to also throttle reranking under VRAM pressure.

**Steps:**
1. Add `_rerank_sem = asyncio.Semaphore(1)` (reranking is sequential by design — concurrent rerank on same GPU causes VRAM spikes)
2. Wrap rerank call in `search.py`: `async with _rerank_sem:`
3. Extend `_vram_watchdog`: VRAM > 90% → also acquire slot from `_rerank_sem`; VRAM < 70% → release
4. Log throttle events at WARNING level

**Risk:** Low.

---

## Phase R11 — Reranker Tests

See `E2E_TESTING.md` section "Reranker Tests" for full test specifications.

Quick reference:
- Model tier mapping correctness
- GPU enforcement (no CPU fallback)
- IOBinding confirmation after warmup
- Sigmoid calibration produces [0,1] scores
- Monotonic ordering: rank 1 > rank 2 > ... > rank N
- Batch size 33 (tests boundary at 32)
- LRU cache eviction under VRAM pressure
- Two-stage integration (1 project vs 2 projects vs 6 projects)
- Score cache hit on repeated (query, docs)

---

## Implementation Order

Recommended sequence (highest impact first, lowest risk first):

```
R1 (tier models) → R5 (sigmoid) → R2 (LRU cache) → R7 (warmup) → R3 (batch size)
→ R6 (two-stage search) → R8 (score cache) → R4 (IOBinding) → R9 (FP16) → R10 (VRAM watchdog)
→ R11 (tests)
```

Start with R1+R5 (no infrastructure change, immediate quality improvement). Tackle R4 (IOBinding) last — needs per-model input name probe and fallback path.
