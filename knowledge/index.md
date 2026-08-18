---
okf_version: "0.2"
---

This bundle is the home for durable knowledge about this repo. It replaces the signpost that stood
here until 2026-08-18; the reversal and its cost are in
[docs/decisions/2026-08-18-the-bundle-becomes-the-home.md](../docs/decisions/2026-08-18-the-bundle-becomes-the-home.md).

Two things did **not** move, and writing here does not excuse you from them:

* **`HR#` rows and the §14 coverage map** stay in
  [docs/architecture/federation-ops-and-invariants.md](../docs/architecture/federation-ops-and-invariants.md).
  They are read by `test_coverage_map_traceability.py`, which fails when a row stops resolving.
  `okf check` cannot know an invariant lost its guard. A new invariant gets its row *and* a concept
  here; each concept names the ids it holds.
* **Decision records** stay in [docs/decisions/](../docs/decisions/), one per dated slug, indexed by
  its own README and gated by `test_decisions_index_is_complete_and_resolves`. A concept here cites
  the record; it does not replace it.

## Component

* [core: where state lives and which projects exist](components/core.md) - env knobs, on-disk paths, the registry, per-project config and GPU device selection.
* [daemon: when work happens](components/daemon.md) - the watcher, the graph lane, the sweeps, and the two threads that park forever on purpose.
* [embed: the only module that touches CUDA inference](components/embed.md) - the process-wide GPU lock and the per-thread accounting around it.
* [graph: symbols, edges, and communities](components/graph.md) - tree-sitter extraction, the call graph, the partition, and `EXTRACTOR_REV`.
* [index: discovery, chunking, and the vector store](components/index-package.md) - the write path, and what decides whether stored vectors are still valid.
* [query: answering a question](components/query.md) - the read path, its two lanes, its fan-out, and its scope vocabulary.
* [server: how the outside world talks to the engine](components/server.md) - one Starlette app carrying the MCP surface and the dashboard API.

## Constraint

* [A federation is a query-time union, never a merged index](constraints/a-federation-is-a-query-time-union.md) - HR4, HR5 and federation invariants #1–#4, #6–#8.
* [Every parse is bounded out of process](constraints/every-parse-is-bounded-out-of-process.md) - HR39: no working in-process cancellation, so the worker is the unit that can be killed.
* [Inference runs on the GPU or it fails](constraints/inference-runs-on-the-gpu-or-it-fails.md) - HR6, HR26, HR41: CPU fallback is fatal, and VRAM comes back on demand.
* [No generative model runs inside the package](constraints/no-generative-model-runs-inside-the-package.md) - HR9, HR10, HR12, HR31: the two lanes and the line between them.
* [One predicate decides what is indexed](constraints/one-predicate-decides-what-is-indexed.md) - HR28, HR29, HR35: every enumerator routes through `_should_drop`.
* [Retrieval is recall then rerank](constraints/retrieval-is-recall-then-rerank.md) - HR7, HR8, HR30: fusion on rank, ordering by rerank score, exactly four tools.
* [Structure is read, never classified](constraints/structure-is-read-never-classified.md) - HR3, HR15, HR20, HR24, HR25: no regex or keyword table decides what user code means.
* [The daemon lives inside one core](constraints/the-daemon-lives-inside-one-core.md) - HR40: a cgroup ceiling under the cooperative fixes, proven by throttle counters.
* [The graph lane wakes only on code drift](constraints/the-graph-lane-wakes-only-on-code-drift.md) - HR1, HR2, HR32, HR38: the watcher is the only steady-state trigger.
* [The tracked tree is publishable and device-neutral](constraints/the-tracked-tree-is-publishable.md) - HR34: machine-specific values arrive from the environment, never from a tracked file.
* [The watcher is one generator in one thread](constraints/the-watcher-is-one-generator-in-one-thread.md) - HR33, HR37: one `watchfiles.watch()` over every root, restarted through an acknowledged handshake.

## Defect

* [A clean exit meant the reload never came back](defects/a-clean-exit-meant-the-reload-never-came-back.md) - one exit code carried two intents, and `Restart=on-failure` believed it.
* [A guard test named its own modules](defects/a-guard-test-named-its-own-modules.md) - a hand-maintained scan list turned the guard into a collection error.
* [An embedded script block was opaque to extraction](defects/an-embedded-script-block-was-opaque-to-extraction.md) - Vue and Svelte files indexed with zero symbols, indistinguishable from a sparse repo.
* [The incremental path stamped no freshness](defects/the-incremental-path-stamped-no-freshness.md) - a field only the slow path wrote, under a name that read as currency.
* [The watcher attributed events by string prefix](defects/the-watcher-attributed-events-by-string-prefix.md) - sibling roots could receive each other's file events.

## Interface

* [The HTTP route layer](interfaces/the-http-route-layer.md) - the registration order, the route table, and the four behaviours the URLs do not reveal.
* [The MCP tool surface](interfaces/the-mcp-tool-surface.md) - four tools, their signatures, and what each one refuses.

## Runbook

* [Moving the pipeline stamp](runbooks/moving-the-pipeline-stamp.md) - bumping `EXTRACTOR_REV` or `ALGO_VERSION` without stamping a store that ran old code.
* [Restoring the registry](runbooks/restoring-the-registry.md) - the one file that cannot be re-derived, and why a restore goes row by row.
* [Running CI on the self-hosted runner](runbooks/running-ci-on-the-self-hosted-runner.md) - no checkout, an owner-gated trigger list, and how a run proves the bytes it tested.
* [Running the live suite](runbooks/running-the-live-suite.md) - the markers, the three hazards, and what mass onnxruntime failures actually mean.
