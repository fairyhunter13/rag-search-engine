---
type: Constraint
resource: src/rag_search/core/gpu.py
title: Inference runs on the GPU or it fails
description: HR6, HR26 and HR41 are one lane read three times — CPU fallback is fatal, provider selection is a ranked whitelist with no CPU entry, and VRAM comes back on demand rather than only after idle.
tags: [gpu, onnxruntime, cuda, vram, hr6, hr26, hr41]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# Inference runs on the GPU or it fails

Three `HR#` rows describe one thing from three angles. Read together they say: the GPU is not the
fast path, it is the only path, and the two costs of that are provider selection and arena memory.

## CPU is not a degraded mode, it is a bug (HR6)

Every embedding and every rerank runs on CUDA. A CPU binding does not degrade throughput, it
silently changes what the index *means* — two stores embedded on different devices are not
comparable, and nothing downstream would say so. So the failure is made loud:
`assert_gpu_available()` runs in both `Embedder.__init__` and `Reranker.__init__`, and the ORT
session sets `session.disable_cpu_ep_fallback=1` so the runtime cannot quietly rebind underneath.
A binding guard then asserts `session.get_providers()[0]` is in `GPU_EP_NAMES`, because the two
earlier checks both pass on a session that ORT downgraded after construction.

There is no generative model in this lane and never was — see
[the package opens no LLM](no-generative-model-runs-inside-the-package.md).

## Selection is a whitelist, so "available" cannot mean CPU (HR26)

`_GPU_EP_ORDER` lists the execution providers in preference order and **excludes CPU by
construction** rather than filtering it out later:

```
NvTensorRTRTX → Tensorrt → CUDA → MIGraphX → ROCM → DirectML
```

`rank_gpu_providers(available, *, disable_tensorrt)` is pure — deterministic, no GPU needed — which
is what makes the ladder testable on a machine that has none. `select_gpu_providers()` ranks the
*real* `ort.get_available_providers()` and **raises `RuntimeError` on an empty result**. An empty
list is the CPU-only machine, and raising is the whole point.

`select_gpu_device()` sets `CUDA_DEVICE_ORDER=PCI_BUS_ID` **before CUDA initialises** — after
initialisation it is ignored, and the device the code picks stops matching the device the operator
named. Selection is max free VRAM, tie-broken by compute capability then lowest index, overridable
with `RSE_GPU_DEVICE`.

## The arena only grows, so the release path cannot live behind the idle check (HR41)

ONNX Runtime's BFC arena holds the high-water mark of the largest batch a session ever served and
returns it only when the `InferenceSession` is destroyed. `arena_extend_strategy=kSameAsRequested`
bounds how eagerly it grows; it does not make it shrink.

The original design released models on the 300 s idle tick. That is unreachable exactly when it
matters: **a daemon being actively worked against never goes idle.** Measured on a 16 GB card, the
daemon held 12.2 GB at `active_clients=0`, which starved a live suite loading its own ~8.4 GB of
embedder and reranker into 60 failures inside onnxruntime — none of which named the GPU or the
daemon. Restarting the daemon turned those 60 into 623 passed with nothing else changed.

So `release_models()` has three callers: the idle tick, `_shutdown_exit`, and
`POST /api/gpu/release`. Releasing must never become a silent CPU downgrade — the next caller
rebuilds lazily on a GPU EP or dies, which is HR6 again.

This is the failure mode behind the "mass ORT failures ⇒ check free VRAM before you read the diff"
instruction in `CLAUDE.md`, and it is why
[a live suite runs alone](../runbooks/running-the-live-suite.md) matters operationally.

## Sources

The normative rows are HR6, HR26 and HR41 in
[§13b](../../docs/architecture/federation-ops-and-invariants.md); the guards are named in §14 and
listed here so this concept can be checked without leaving it:

| Row | Guard | File |
|---|---|---|
| HR6 | `test_no_cpu_fallback`, `test_embedder_bound_to_gpu` | `test_smoke.py` |
| HR26 | `test_rank_gpu_providers_ladder_order`, `test_select_gpu_providers_non_empty_and_no_cpu`, `test_select_gpu_providers_fatal_on_cpu_only` | `test_gpu_autodetect.py` |
| HR41 | `test_gb1_gpu_release_actually_returns_vram` | `test_gpu_budget.py` |

Incident record: [VRAM starvation](../../docs/decisions/2026-07-29-vram-starvation.md).
