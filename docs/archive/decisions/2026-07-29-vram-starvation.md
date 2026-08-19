# Giving the GPU back before idle

**2026-07-29** · HR41 · guard: `test_gpu_budget.py` GB1–GB2

HR40 bounds CPU with a kernel quota; nothing bounded VRAM.

ORT's BFC arena only ever grows — it keeps the high-water mark of the largest batch a session has
served until the `InferenceSession` is destroyed, and `arena_extend_strategy=kSameAsRequested`
bounds how *eagerly* it grows, not whether it ever shrinks.

The release lives in `daemon/server.py::release_models()` with three callers — the idle tick,
`_shutdown_exit`, and `POST /api/gpu/release` — precisely because keeping the only copy behind the
300 s idle check made it unreachable when it mattered: **a daemon you are actively working against
never goes idle for 300 s**, so its high-water mark stands for the whole session.

## Measured

12.2 GB of a 16 GB card still held at `active_clients: 0`, 3.5 GB free. That starved the live
suite (~8.4 GB for its own in-process embedder + reranker) into **60 failures inside onnxruntime**
— `CUBLAS failure 3` / `BFCArena` errors naming neither the GPU nor the daemon. Restarting the
daemon turned those same 60 failures into 623 passed with nothing else changed.

## The operational rule

**If the live suite fails en masse with ORT allocation errors, check free VRAM before you read the
diff.** The suite now reclaims the GPU in a session fixture and asserts headroom up front
(`RSE_TEST_MIN_VRAM_MB`), so a shortfall says so instead of looking like broken code.

**The transferable lesson:** a reclamation path reachable only from an idle timer is unreachable
exactly when contention makes it necessary. Give every "release the resource" path a manual
caller.
