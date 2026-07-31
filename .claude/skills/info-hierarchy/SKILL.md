---
name: info-hierarchy
description: "RSE's DIKW doctrine ladder — how data climbs from data to wisdom, what each rung costs, and the compute-spend doctrine. Read when deciding where a new derived artifact belongs."
---

# Info Hierarchy

RSE's DIKW doctrine ladder — how data climbs to wisdom and what each rung costs.

> "Spend LLM tokens only to climb Information→Knowledge→Wisdom, and only at the
> nodes/queries actually read." — §1a P1
>
> **Rewritten 2026-07-28.** The generative tier that made that sentence a *budget* is deleted:
> RSE now spends **zero** LLM tokens climbing the ladder, at any rung. The doctrine survives as
> its stronger form — the climb is deterministic end to end, and the only generative call left in
> the system is `claude -p` answering a dashboard chat turn, which reads the ladder rather than
> building it. HR23's `llm_token_stats` accounting retired with the calls it accounted for.

## The ladder

```
WISDOM    §1a Principles (P0–P18) + §13b HRs — the governing laws.
          Derived from architecture decisions across all projects.
          Surfaced as: CLAUDE.md invariants, docs/world-model/model.yaml L1.
          Generation: human-authored + machine-verified (check_world_model.py).
          LLM cost: $0 (pre-built; checked at edit time, not query time).

KNOWLEDGE Community summaries + labels (L1, level=1 in graph.db).
          Derived from: symbols + edges → fastgreedy community detection → structural labelling.
          Surfaced as: overview(communities, query=...); ask() Architecture section (CLI/chat only).
          Generation: label_community_structural (graph/community.py) — templated from the
                      community's own members, deterministic. DeepSeek narration and the
                      wiki community_*.md surface left with tier 3 on 2026-07-28.
          LLM cost: $0. This rung used to be the only one that cost anything; now none do.

INFORMATION Symbols + call edges (graph.db symbols/edges tables).
            Derived from: tree-sitter parse of source files.
            Surfaced as: graph() callers/callees/impact, overview(import_cycles).
            Generation: extract_symbols() + detect_communities() — zero LLM, deterministic.
            LLM cost: $0 (structural parsing only).

DATA      Source code chunks + file tree.
          Derived from: iter_files() + chunk_file() with cAST structural-path header.
          Surfaced as: search() results; ask() Code section (CLI/chat only).
          Generation: index_project() → VectorStore (sqlite-vec, FLOAT[768]).
          LLM cost: $0 (embed-only, GPU).
```

## RSE's DIKW spend doctrine

1. **Data** (embed+index): GPU-only. Never generative. `index_project()`.
2. **Information** (symbols+edges): tree-sitter only. Never generative. `extract_symbols()`.
3. **Knowledge** (community summaries): structural labelling only, `$0`. `label_community_structural()` templates a summary from the community's own members. Never generative — the DeepSeek narration this rung used to describe was deleted 2026-07-28, and with it the significance gate and the reject-option abstention it needed.
4. **Wisdom** (invariants/principles): authored once, machine-checked. `check_world_model.py`.

## Extraction: one tier, and nothing below it (P6/HR15)

**Rewritten 2026-07-28, with the tier-3 deletion.** The **Information** step used to be a
confidence-gated *ladder*: tree-sitter structure resolved the majority, a GPU rerank pass resolved
residual ambiguity by structural context, and DeepSeek resolved whatever remained. The lower two
tiers lived entirely in `kb/bpre*.py` and `kb/resolve_rerank.py` and were deleted with tier 3, so
extraction is now **a single deterministic pass with no fallback beneath it**.

That makes the standing prohibition load-bearing rather than merely tidy: **no regex, static or
dynamic keyword list, or per-language mapping table may stand in for structural extraction.**
Surface-text name matching is unsound (false positives), and there is no longer an escalation tier
to catch what it gets wrong — a heuristic added here would be the *only* answer, not a first guess.
The ban is package-wide over `src/rag_search/` outside the four files where matching literal text
*is* the intrinsic mechanism, and it is enforced by `test_no_code_semantic_regex.py`.

What extraction cannot resolve is **recorded as unresolved**, never guessed and never silently
dropped. HR23's `llm_token_stats()` budget accounting retired alongside the calls it audited: with
no generative tier there is no token budget in this path to make auditable.

## Compute-spend doctrine (CPU / GPU / RAM)

Parallel to the LLM-spend ladder above, RSE applies a **compute-spend doctrine** governing when CPU, GPU, and RAM are consumed:

- **Spend compute only to re-climb the DIKW ladder on real source drift.** The heavy pass (community re-detection + structural labelling + `index_docs`) runs only when `_source_fingerprint` detects that indexed source files actually changed. It was a much heavier cascade until 2026-07-28 — enrich, wiki and BPRE all hung off this same gate — which is why the gate is scoped as carefully as HR35–HR38 below describe. Metadata-only events (file close/open, CHMOD) and changes to non-indexed files are filtered at the watcher boundary and again by the source-drift gate in `on_change`.
- **Idle ⇒ near-zero CPU + constant RAM floor.** With no queries and no source drift the daemon holds < 1 % CPU. The existing `_idle_unload` path (300 s default) nulls the ONNX session references, calls `gc.collect` + `malloc_trim`, and releases the ORT CUDA arena — the only reliable way to return GPU memory to the OS. Models reload on demand at the next real query or edit.
- **GPU is the sole inference engine — maximized, never idle-spun.** Embedding and reranking run exclusively on CUDA; CPU fallback is fatal. The GPU is not used during idle periods; it warms up only for actual embed/rerank operations triggered by real queries or real source changes.
- **File-watching uses kernel notifications, not CPU polling.** `watchfiles` (Rust `notify`) is the watching mechanism — push events from the OS kernel, not a polling loop, delivered through a **single** background thread + inotify instance covering every watched root. The poll fallback lives inside the Rust library (`force_polling`, NFS/SMB/WSL) — there is no hand-rolled Python poll loop to maintain.
- **The watcher trigger surface must be ignore-aware too, not only the gates downstream of it (HR37, 2026-07-01 evening).** A drift gate that correctly ignores tool-cache churn is not enough if the *watcher* still receives, buffers, and dispatches every raw event from that churn before the gate ever gets a say. `daemon/watcher.py`'s `watch_filter` reuses the same HR35 `is_ignored_path` resolver as the drift gate, and Rust-side debounce/step coalesces bursts before they cross into Python — closing a 4th, distinct idle-CPU cause where a per-root watchdog Observer (one inotify instance + thread *per project*, ~278 threads on a 139-member fleet) delivered every raw ignored-dir event individually into an unbounded Python buffer, pinning one dispatch thread even after the HR35/HR36 gates were already reporting "no drift" correctly.
- **The drift gate's input must itself be trustworthy (HR35, 2026-07-01).** `_source_fingerprint` walks via `iter_files`/`is_ignored_path` (`index/discover.py`), which apply one shared discovery decision order: RSE `.rse-index.yaml` `exclude` (drop) > RSE `include` (force-keep) > default hidden-dir/`IGNORED_DIRS` policy (drop) > `.gitignore` (drop, supplementary, cached per-mtime) > keep. Gitignored/hidden tool-cache dirs (`.svelte-kit`, `.playwright-mcp`, `.astro`, `.turbo`, `.vite`, etc.) never enter the fingerprint, so a live dev-server/tool-cache rewriting those paths cannot spuriously flip the sig and re-trigger the heavy cascade — root-caused after a live `vite dev` + Playwright-MCP session pinned a CPU core via an every-~5min false-positive BPRE/enrich rebuild.
- **Every compute-spend gate must be scoped to what it actually consumes, not just what's easiest to hash (HR36, 2026-07-01).** Generalizes HR35: a drift gate reusing a *coarser* signature than its actual dependency surface will spend compute on irrelevant churn — root-caused as a 3rd, distinct idle-CPU cause (2026-07-01) when docs/config/image edits and `.claude/*.js` tool-cache churn on a 170-member federation root kept flipping an all-files stamp faster than the ~5min federation rebuild it triggered could complete, pinning a CPU core continuously even after HR35 shipped. The stamps that carried this rule were BPRE's (`bpre_source_sig`, `_member_scan_sig`, `_bpre_code_sig` in `kb/bpre.py`) and they were deleted with tier 3 on 2026-07-28; the rule itself did **not** leave with them, because HR38's `_code_source_fingerprint` — which is the surviving gate — exists precisely to satisfy it. Two mechanics worth keeping when writing the next one: route the file set through the same HR35 `_should_drop` resolver as `iter_files` rather than re-deriving it, and write the stamp from the signature computed at the *start* of the rebuild, so it never chases a moving target.
- **The `on_change` cascade gate itself must be code-scoped (HR38, 2026-07-01).** HR36 made the downstream reuse stamp code-only, but `sweeps.py`'s `on_change` cascade gate and `_graph_stale`/`source_sig` meta-stamp were still comparing the coarser all-files `_source_fingerprint` — so non-code churn (docs/config/image edits) could still spuriously wake the heavy pass and force a graph re-derive even though the downstream stamp would then correctly no-op on reuse. Both are now repointed to `_code_source_fingerprint` (an `is_code_language` filter), unifying the stamps on one code-only definition of "source changed." **This is the gate that outlived tier 3** — the cascade it guards is smaller since 2026-07-28, but the gate is unchanged and is now the only one of its family left. The vector-index/doc-search reindex step (`_index_files`/`_index_project`) runs *before* this gate and is unaffected — doc search freshness doesn't depend on it.
- **A parse that cannot be bounded in-process must be bounded out-of-process, not skipped (HR39, 2026-07-01).** Three independent tests confirmed py-tree-sitter 0.25's `progress_callback` never fires during a stuck parse (and `tree_sitter_language_pack`'s bundled parser exposes no callback at all), so a pathological grammar — `cobol` fed non-cobol bytes, proven this session — pins a CPU core forever with no in-process way to interrupt it. `index/bounded_parse.py` runs every tree-sitter parse call-site — since 2026-07-28 that is `graph/extractor.py` alone, `kb/bpre_ast.py` having left with tier 3 — inside a persistent **spawn**-context worker pool (never `fork` — the daemon holds CUDA + threads); a worker that exceeds its deadline is killed and respawned, the timeout is counted (`parse_timeout_count` in `overview(what="metrics")`) and logged by path-hash only (P18/HR34), and extraction continues with the next file. This is what lets RSE support every one of the 299 tree-sitter code grammars with **no exception, no skip** — the alternative (silently excluding cobol) was rejected.
- **The CPU budget itself must be kernel-enforced, not just cooperatively gated (HR40, 2026-07-01).** Every fix above (HR32/HR35/HR36/HR37/HR38/HR39) stops the daemon from *spuriously* spending compute — but none of them physically bound what happens if a gate is ever wrong again, or under genuinely heavy real work. HR40 adds two independent, layered guarantees on top: an **idle tier** — `daemon/cpu_budget.py` self-measures the daemon's own cgroup-v2 `cpu.stat` usage delta (exposed via `/healthz` and `/api/metrics`), live-gated by an automated test asserting < 1% of one core over a quiescent window — and an **active tier** — `CPUQuota=100%` (with `CPUAccounting=yes` set explicitly, since `CPUQuota=` alone doesn't imply it — systemd issue #9647) on the daemon's systemd unit, a **cgroup-v2 kernel ceiling** the daemon's entire service cgroup physically cannot exceed, covering `bounded_parse.py`'s spawn-context workers too since they are children of the same cgroup (`RSE_BOUNDED_PARSE_WORKERS` dropped `2→1` accordingly — two workers would only time-slice one capped core). The proof isn't merely "usage stayed low": `cpu.stat`'s `nr_throttled`/`throttled_usec` climbing under sustained real load is the canonical evidence the cap is *physically biting*, cross-checked by a hermetic `systemd-run --user --scope` self-test independent of the daemon's own unit that proves the `cpu` controller is genuinely delegated on this host at all.

## Publishability & device-neutrality (P18, HR34)

RSE is a **public repo**. Parallel to the compute-spend and extraction doctrines above, every
tracked artifact — source, tests, docs, scripts — must be safe to
publish: no secrets, no real device paths, no company/project names. This began as a whole-repo
widening of P7/HR13, which banned absolute paths in generated wiki/OKF artifacts specifically;
**since 2026-07-28 the widening is all that is left** — RSE writes no artifacts, so P7 was retired
and P18 is now the whole rule rather than its outer ring.
Device/host portability is achieved the same way efficiency is achieved elsewhere in this doctrine —
by never hardcoding what should be resolved at the boundary: every machine-specific value (storage
paths, host, port, embed/rerank models, GPU device) is **env-driven with an XDG-style default**
(`core/config.py`), so the same tracked tree runs unmodified on any machine. Guarded by
`test_public_hygiene.py` (whole tracked-tree scan for `/home/`, `/root/`, `/Users/`, and Windows
`C:\Users\` literals, plus a structural check that `core/`/`daemon/` storage-path constants derive
from `os.environ.get(...)` rather than a hardcoded literal) and `test_no_real_project_in_tests.py`.
Device-specific name bans (real company/codename/device-id lists) are intentionally kept out of this
public tree and live only in the private `rse-live-audit` repo.

## Hierarchy removal (WS-B, 2026-06-26)

The former L2 (domain aggregations) and L3 (federation themes) layers between Knowledge and Wisdom have been **deleted**. They added 35,000+ graph.db rows per project at significant LLM cost but were not consumed by any query path that flat-L1 couldn't serve. Standalone docgen/OKF tools (WS-A/WS-C) now own deep hierarchy generation for any repo — they parse the repo directly, with no RSE graph.db input.

*(Amended 2026-07-28: docgen and OKF have both been deleted, so WS-A and WS-C no longer exist and RSE has no deep-hierarchy generator at all — flat L1 is the whole hierarchy, which is what WS-B concluded was sufficient in the first place. The WS-B paragraph above is a dated record of the June 2026 decision and is left as written.)*

## How to use

- **search/overview** (MCP), **ask** (CLI/chat) — consume Data+Information+Knowledge rungs.
- **overview(what='communities')** — Knowledge layer (structural labels, `$0`).
- **overview(what='import_cycles')** — Information layer (edges only, deterministic).
- **check_world_model.py** — enforces Wisdom layer against working-tree diffs.
- **gen_world_model_skills.py** — renders `.claude/skills/` from this file + `model.yaml`.

The `business_rules`, `process_flows`, `patterns`, `service_mesh` and `feature_map` variants were
deleted with tier 3 on 2026-07-28. `patterns` is worth naming specifically: it was the only
`overview` variant that made a synchronous cloud call on a query path, so removing it also removed
the last unmetered LLM round-trip in the system.

_Source: `docs/info-hierarchy.md` — generated by scripts/gen_world_model_skills.py._
