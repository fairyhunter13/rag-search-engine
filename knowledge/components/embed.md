---
type: Component
resource: src/rag_search/embed/
title: "embed: the only module that touches CUDA inference"
description: One file, 286 lines, holding the process-wide GPU lock every embed and rerank passes through, plus the per-thread accounting that a dict keyed by thread id would get wrong.
tags: [embed, gpu, cuda, rlock, threading-local]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# embed: the only module that touches CUDA inference

`embedder.py` is the whole package. Every other package that needs a vector calls `get_embedder()`;
none of them opens a session. Two residents exist, the embedder and the cross-encoder reranker, and
[inference runs on the GPU or it fails](../constraints/inference-runs-on-the-gpu-or-it-fails.md)
covers why a CPU binding is fatal rather than slow. There is no generative model here — see
[no generative model runs inside the package](../constraints/no-generative-model-runs-inside-the-package.md).

## `_GPU_INFER_LOCK` is an RLock on purpose

ORT's `Run()` releases the GIL, so daemon threads genuinely overlap on the card, and that overlap
produced SEGVs. The lock serialises every embed and every rerank in the process — four call sites,
all of them.

It is an `RLock` rather than a `Lock` because `embed()` holds it across a lazy `_init()` that takes
it again. Downgrading it to `Lock` deadlocks on the first cold call, which is exactly the call a
smoke test skips.

The federation fan-out in `query/search.py` runs eight threads and must never contend with this
lock: the GPU is touched once before the loop and once after, never inside it. That ordering is the
only reason the fan-out is safe.

## Per-thread counts, not a dict keyed by thread id

Embedding is not confined to the caller's thread — `mcp.index()` hands a fleet-wide reconcile to a
daemon thread and the watcher's dispatch workers reach `_index_project` on theirs — so a delta taken
around one search is a delta around whatever else was in flight. The per-call-site counter therefore
lives in a `threading.local()`.

A dict keyed by thread id is the wrong structure and the failure is silent: ids are reused after a
thread exits, so a fresh thread inherits its predecessor's count and the dict leaks an entry per
thread. The process-wide counter kept beside it answers a different question and cannot substitute.

## What must not import this

`index/bounded_parse.py` workers are CPU-only and must never import this module — a spawn worker
that pulls in the embedder pays a model load per parse. Guarded, not documented:
`test_bounded_parse_workers_never_import_embedder` in `test_no_unbounded_parse.py`. Overlap of the
inference regions is pinned by `test_tk6a_gpu_inference_regions_never_overlap` in
`test_gpu_autodetect.py`.
