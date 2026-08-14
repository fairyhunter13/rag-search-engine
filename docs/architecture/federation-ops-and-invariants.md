# Federation & Search-Engine Architecture — Part 2: Ops, Transport & Invariants

> Continued from [federation-and-search-engine.md](federation-and-search-engine.md).

## 10. Event-driven lifecycle

- **Watcher** (`daemon/watcher.py`): a single `watchfiles.watch()` (Rust `notify`) generator
  in one `rse-watcher` thread covers **all** watched roots — one inotify instance total, not
  one per project; Rust-side debounce/step coalesces bursts, and `watch_filter` reuses the
  same `is_ignored_path` resolver the drift gate uses so ignored-dir churn never reaches
  `on_change`. No hand-rolled poll fallback — polling (if ever needed) is the Rust library's
  own `force_polling` path. Watches **all enabled projects** — members included — because
  `register_all_members()` runs before `start_watcher()`; a project added while the loop is
  already running relaunches the watch generator (restart Event) and blocks the caller on a
  bounded restart-ack Event so the new root is actually armed before `watch()` returns.
- **`on_change(path, files)`** (`sweeps`): incremental `_index_files` on changed files (or
  full `_index_project` if `files` is empty), then — **only if `_code_source_fingerprint`
  actually moved** (HR38) — a **45 s-debounced** (`_LANE_DEBOUNCE_S`) hand-off of the changed
  paths to the graph lane, which re-derives the symbol graph and calls `_label_project`.
- **`reconcile_projects()`**: startup + `index()`-triggered one-shot. Calls
  `register_all_members()`, then for every enabled project with no chunks (`_needs_index`)
  or missing community summaries (`_needs_labels`): `_index_project` and/or `_label_project`
  — the latter alone when only labels are missing, so a settled project is not re-indexed.
  **The root-pass is gone**: `build_federation_hierarchy(root)` + `reconstruct_processes(root)`
  were the federation-wiki and BPRE passes and left with tier 3 on 2026-07-28, taking the
  Enzyme-IVM `child_sig` machinery with them. Globally pausable via `_PAUSED` (tests set this
  via the `pause_sweeps` autouse fixture) — and `_PAUSED` *abandons* the walk rather than
  suspending it, which is why the live suite may not run during a migration.
- **`index(path, enabled=True)`** (MCP): rejects forbidden roots (`is_forbidden_root` →
  `/tmp`, `~/.cache`), upserts enabled, spawns a `reconcile_projects` thread. Registering a
  root therefore automatically discovers, indexes, labels, and starts watching its members.

## 11. Removal & consistency

- **`index(path, enabled=False)`** (MCP): `expand_federation(path)` → `remove_project` +
  `rmtree(index_dir)` for each path. Removing a root cascades to its members; response
  reports `members_removed`.
  When the expansion returns more than the path asked for, the first call returns
  `status: "confirm_required"` with the member list and each store's size and deletes
  **nothing**; `confirm_members=True` performs the cascade. The asymmetry is the reason:
  membership rows are rediscovered by the next federation walk, embeddings are not, so the
  recoverable half self-heals and the expensive half is GPU time proportional to the members.
  A project with no members removes in one call — the gate is on fan-out, not on deletion.
- **Orphan vacuum** (§6 of part 1) is the backstop that reconciles storage to the registry
  if anything is left behind.

## 12. MCP transport architecture

Two transports serve the same 4 tools:

- **HTTP** — `mcp.streamable_http_app()` at `:8765/mcp`. One shared daemon, one model
  copy, no per-session process. **Preferred transport** (commit c48ba25).
- **stdio bridge** — `daemon bridge-stdio`: full in-process engine per client session
  (~1 GB). Retained as fallback; idle self-exit after `RSE_BRIDGE_IDLE_S` (default
  600 s).

### 12.1 Config source-of-truth

`scripts/integrations/canonical.py` + `scripts/configure_integrations.py` write MCP
entries into each discovered client config (claude profile(s) + hermes; OpenCode is
no longer configured). Canonical URL: `http://127.0.0.1:8765/mcp`.

| Client family | Format |
|---|---|
| Claude `settings.json` | `{"type":"http","url":"http://127.0.0.1:8765/mcp"}` |
| hermes `config.yaml` | `url: http://127.0.0.1:8765/mcp` (drop command/args/env) |

Dropping `env` is safe: `RSE_ALLOW_INDEX_OUTSIDE_CWD` is unreferenced in `src/`; the
only LLM vars left are the four `RSE_QUERY_LLM_*` ones (`core/config.py:45-48`), they
match daemon defaults, and their single reader is `routes_chat.py`'s `claude -p` call
— **no MCP tool reaches an LLM at all since 2026-07-28**, `overview(what="patterns")`
having been the last one and having left with tier 3.

## 13. Invariants the engine MUST uphold

## 13a. Hard pipeline requirements — the contract

The diagram and HR table (§13b) are the normative write-path spec. A change
that violates them is an architecture regression; §14 maps each HR to the live
test that proves it.

```
┌──────────── QUERY PATH (synchronous, read) ────────────┐
│ MCP tools: search·graph·overview·index (HTTP :8765)     │
│ routes → mcp.py / _overview.py                          │
│ FEDERATION FAN-OUT  federation.py                        │
│   expand_federation(root) = [root] + symlink members     │
│   federated_map(fn) → fn on each member's OWN stores     │
│ union lists · worst-of index_state · per-member stores   │
└────────────────────────────────────────────────────────┘

┌──────────── BACKGROUND PATH (async, write) ────────────┐
│ _start_background: scheduler(6h/idle/watchdog)           │
│   NO kb_sweep, NO periodic reconcile                     │
│ reconcile_projects() — once at startup (thread)          │
│ start_watcher() — watchfiles, all enabled projects, 1     │
│   thread + 1 inotify instance, Rust-side debounce         │
│   on_change(project, files)  ← ONLY steady-state trigger │
│     ├─ _index_files()   → incremental VECTOR re-embed    │
│     └─ graph lane (45s/project debounce, HR38 sig gate)  │
│ _index_project = chunk+embed → symbols → edges → L1      │
│ _label_project = structural L1 labels (template, no LLM) │
│   → prune orphan communities, then label summary IS NULL │
└────────────────────────────────────────────────────────┘
index_state: indexing → degraded → ready
             (no vec)   (degenerate partition)  (HR20 clean)
```

**Redrawn 2026-07-28.** The write path used to have a second half — `_enrich_project`'s
cloud-DeepSeek L1/L2 narration, `classify semantic_type`, `build_wiki` and
`build_federated_index` — and it is deleted, not disabled. The pass that replaces it,
`_label_project`, is deterministic and offline, which is why the state ladder lost its
`searchable`/`enriching` rungs: those measured *how far narration had got*. What still
discriminates is whether the index exists and whether the partition is degenerate (HR20).
The field is `index_state`; the old name `kb_state` is gone from `src/` with tier 3.

## 13b. Hard requirements (HR)

| # | Hard requirement |
|---|---|
| **HR1** | **Watcher is the steady-state indexing trigger**; `on_change` incremental-embeds changed files. Full `_index_project` (graph+k-core) runs only at first index / reconcile. |
| **HR2** | **The graph lane is triggered by the same watcher event as HR1**, on its own 45 s/project debounce (`_LANE_DEBOUNCE_S`), gated by HR38's `_code_source_fingerprint`. Event-driven only — no sweep timer, no periodic reconcile timer. Its payload is symbol re-derive + `_label_project`. |
| **HR3** | **Re-running `detect_communities` / `_label_project` MUST NOT wipe existing summaries.** `ready` stays `ready` across re-index (`summary=None` in `upsert_community`, never `""`). |
| **HR4** | **Federation = query-time union**; each member has its own stores; no cross-repo edges. Fan-out via `expand_federation`/`federated_map`. (≡ inv#1–#4.) |
| **HR5** | **One absolute path = one index dir.** Two distinct clones → two indexes. (≡ inv#2, #8.) |
| **HR6** | **GPU-only for embeddings + reranking** (FastEmbed/ONNX/CUDA); any CPU fallback raises fatally. No local generative LLM — the GPU lane is embeddings + reranking ONLY. |
| **HR7** | **The field is `index_state` and it has three reachable values**: `indexing → degraded → ready` (`server/_overview.py::_rank = {"indexing": 0, "degraded": 1, "ready": 2}`). `ready` iff vectors exist and HR20's partition-quality gate says the partition is not degenerate. There is deliberately no fill-rate rung: structural labelling fills every summary deterministically in one pass, so such a rung could only ever read 0 % or 100 % and would discriminate nothing. Federated entity = worst-of members. |
| **HR8** | **Two-stage retrieval at query time**: Stage 1 = hybrid recall — bi-encoder vector search (`sqlite-vec`) and FTS5 BM25 (`chunks_fts`), fused by RRF; Stage 2 = cross-encoder rerank (`gte-reranker-modernbert-base`, GPU). Both AXIS A (code chunks) and AXIS B (community summaries) are reranked; AXIS B has no lexical lane. Results ordered by `rerank_score`, never the bare retrieval score. Reranking never runs at index/KB-build time. |
| **HR9** | **MCP query actions (`search`/`graph`/`overview`) perform ONLY embedding + reranking** — no generative LLM (cloud or local). Generation is delegated to the calling agent (June 2026 MCP best practice). *(Until 2026-07-29 this row also covered `ask` → `compose_answer`; `ask` left the MCP surface, and `compose_answer` is now reached only from the CLI and dashboard chat — guarded by `test_run_ask_is_llm_free`.)* |
| **HR10** | **Dashboard chat (`POST /api/chat_stream`) uses claude-haiku-4-5 only** (Claude Code CLI, `claude -p --model`); **no DeepSeek fallback** — emits an SSE `{"type":"error"}` + `{"type":"done"}` when the `claude` CLI is unavailable. Since 2026-07-28 this is not merely the *preferred* lane, it is **the only generative lane in the system**: tier 3 took DeepSeek out of the repo, so the clause that used to read "DeepSeek is KB-enrichment-exclusive (HR12)" now has nothing to exclude. **Codex support removed**. Dashboard-only: not wired to any MCP tool. Account selection goes through `core/claude_profiles.py`, the one module allowed to open a URL (a *usage* read — DK2 asserts it holds no completion call). |
| ~~**HR11**~~ | ***Retired 2026-07-28 — the classifier is deleted.*** Was: `semantic_type` assigned by direct cloud-DeepSeek classification over title+summary in batches of 20, new/unclassified communities only. `graph/enrich.py::_classify_batch` and the whole classify lane left with tier 3, and the **R2 purge dropped the `semantic_type` column itself** later the same day, together with `narrated`, `kind` and `path` — a fleet census over all 160 graphs found no writer and no reader for any of the four. `test_abstention.py` (AB1–AB8, SG1) was deleted whole rather than re-pointed. |
| **HR12** | **No generative LLM of any kind runs inside `src/rag_search/`** — not local, not cloud. Nothing in the package opens an LLM URL, so a box with no provider key configured is the normal configuration, not a fatal one. The GPU lane stays embeddings + reranking ONLY. Guard: DK2 in `test_inference_lanes.py` — a tree-wide scan for completion endpoints, an allowlist of exactly one URL-opening module (`core/claude_profiles.py`), and a `DEEPSEEK` reader scan. |
| ~~**HR13**~~ | ***Retired 2026-07-28 — there is no wiki.*** `kb/wiki.py`, `build_wiki` and `build_federated_index` are deleted, and with them `index.md`, `community_{id}.md` and the root `federation.md`. HR4 is unaffected: the federated index was presentation-only aggregation and created no cross-repo edges, so removing it removes a renderer, not a boundary. Was: The **wiki is a per-member artifact** built by `build_wiki(store, dir)`: `index.md` (type-grouped ToC), one deterministic `community_{id}.md` per L1 (reused DeepSeek summary as prose, member table with **project-root-relative** source citations, call-graph mermaid drawn from real `edges`). Community pages and diagrams use **no LLM** (deterministic → byte-identical reruns (wiki prose is template-based; no DeepSeek required)); L1 narration calls **cloud DeepSeek** during `_enrich_project`. L2 domain pages (`domain_{id}.md`) **deleted** (WS-B 2026-06-26). A federated root additionally gets `federation.md` via `build_federated_index` — **presentation-only** aggregation of each member's own graph.db (key business communities, semantic_type rollup); it creates/reads **no cross-repo edges** (HR4 preserved). Citations are root-relative so the absolute device path never leaks (public repo). |
| ~~**HR14**~~ | ***Retired 2026-07-28 — BPRE is deleted, all three tiers.*** `kb/bpre.py`, `kb/bpre_ast.py`, `kb/bpre_generic.py`, `reconstruct_processes`, `process_graph.db`, the `_regen_owning_*` cascade, `overview(what='process_flows')` and `GET /api/process/bpmn` are all gone. It was 1,874 lines and the single largest cost centre in the repo — four of the five recorded CPU/latency incidents name it. Was: **BPRE process graph is a root-level artifact**: `process_graph.db` lives in `root_process_db(root_path)` (the federation root's index dir), never in any per-member `graph.db` (HR4 preserved). `reconstruct_processes(root_path)` runs only for roots with ≥2 members. **Three-tier extraction model** (BPRE's own process-reconstruction tier numbering — distinct from HR16's cross-service-edge resolution-ladder tiers) — **Tier 1 (tree-sitter, `kb/bpre_ast.py`)**: two-pass structural scan reusing `graph/extractor.py` — Pass A discovers the gRPC/proto API surface by mining generated `*.pb.go` (real constructor + registrar names; no hardcoded patterns, no regex); Pass B detects call sites per-file against that surface (gRPC clients/servers, proto imports, pub/sub publish/consume, HTTP routes/clients, status enums). **Tier 2 (LLM linkage, ON when `DEEPSEEK_API_KEY` present)**: DeepSeek resolves config-driven links (JSON-topic IDs, client hosts) tree-sitter cannot statically reach; emitted with `confidence < 1.0` + `_llm` kind suffix; key absent ⇒ tree-sitter-unreachable edges are **absent**, never heuristically approximated. **Tier 3 (cloud DeepSeek, built and active)**: D5 rule-text extraction + D6 process narrative. D4 process traces are **handler-anchored and deduped**: each process is keyed on an entry handler's reachable symbol set (BFS over intra-service call graph from the handler), not any-edge service adjacency — no duplicate chains, no spurious transitivity, no test-file entry points. The Tier-1 pass is **GPU-free** and byte-identical on repeated runs (without a DeepSeek key, Tier-2 linkage is suppressed ⇒ F1/F2 preserved). Surfaces: `overview(what='process_flows')` returns reconstructed processes when db exists; `GET /api/process/bpmn` exports XSD-valid XML; the dashboard Processes view renders sequenceDiagrams via Mermaid. |
| **HR15** | **Tree-sitter only.** Nothing in the package may classify what the user's code means by regex, keyword list or mapping table, and there is no LLM to fall back on either. **Category B — intrinsic mechanism, exempt by name** in `_CATEGORY_B_ALLOWLIST`: `core.registry` (tier-suffix strip) and `core.config` (project-name slug). Every other module in the package is checked against the wide pattern set (`compile\|finditer\|findall\|search\|match\|fullmatch\|sub\|subn`); measured zero hits outside the two. An exemption for a module that does not use the thing exempted is a hole, so the allowlist carries its own dead-entry check. Guards: `test_no_code_semantic_regex.py::test_no_code_semantic_regex_outside_allowlist` and `::test_category_b_allowlist_has_no_dead_entries`. |
| ~~**HR16**~~ | ***Retired 2026-07-28 — the whole resolution ladder was BPRE's.*** All three tiers are deleted: Tier 1's gRPC/pubsub proto-registry match, Tier 1.5's `kb/valueflow.py`, Tier 1.75's `kb/resolve_rerank.py`, Tier 2's `_llm_link_resolve`. Note what this does **not** retire: `graph/extractor.py`'s pack-native `process()` extraction survives, and it is now the whole structure story. The ladder's own successor question — *should a call edge bind across a language boundary?* — is live and unanswered; today's resolver binds by bare name, and 6.0 % of the fleet's 18.3 M edges cross a language. Was: **Resolution ladder** (2026-06-21, `84aa9cf`; **corrected 2026-07-09**, see `docs/audits/2026-07-09-deep-conformance-audit.md` Part D): language-agnostic by construction; each tier sees only the prior tier's residue; the paid LLM only selects from symbolically-admitted candidates. The shipped ladder is **3 tiers**, 1.0→0.8→0.7, full stop. **Tier 1** (`graph/extractor.py` + `kb/bpre.py::_resolve_grpc_edges`/`_resolve_pubsub_edges`): pack-native `process()` — ANY language; generic call-node detection; gRPC/pubsub proto-registry match; conf 1.0 `EXTRACTED`. **Tier 1.5** (`kb/valueflow.py` + `kb/bpre.py`): deterministic intra-procedural value-flow/constant-propagation (non-literal keys: const/var/:=/`Sprintf`→value; insert→dispatch into maps/DI/topics) + generic cascade; residue emitted not dropped; no regex, no framework vocab; **feeds Tier 1 / Tier 1.75 and stamps no confidence of its own**. **Tier 1.75** (`kb/resolve_rerank.py::_resolve_http_edges`/`rerank_residue`): GPU-local embed→cross-encoder rerank using structural/type context — free (model already warm), ~60× cheaper than LLM localization; single candidate always binds; conf 0.8 `RERANKED` (shared with literal/value-flow-resolved HTTP matches; distinguished only by `kind` suffix, e.g. `http` vs `http_reranked`, never by confidence); gate: RSE default. **Tier 2** (`kb/bpre.py::_llm_link_resolve`): SEA-style LLM — selects from the symbolically-admitted candidate set, never authors; callee verified ∈ index; no self-consistency, no strict-schema; conf 0.7 `_llm`; ON when `DEEPSEEK_API_KEY` present. `graph/extractor.py`'s only parse-error/empty-structure fallback is the deterministic `_generic_walk`. **Invariants**: zero per-language/per-framework vocab and no `import re` in the resolution path (generic AST node-kind primitives + embeddings only); without a DeepSeek key, reconstruction is GPU-free, byte-identical, and deterministic. **Pack facts**: `tree-sitter-language-pack==1.9.1` / `tree-sitter==0.25.2`, 306 canonical + alias ≈ 312 langs, typed `process()`, broad-but-not-universal structure coverage. |
| ~~**HR17**~~ | ***Retired 2026-07-28 — `kb/valueflow.py` is deleted.*** Was: **Deterministic Tier-1.5: intra-procedural value-flow** (`kb/valueflow.py`, 2026-06-21; **corrected 2026-07-09** — the "FQN join, conf 0.9 `RESOLVED`" clause below was aspirational and never shipped; see HR16). Handles the *dynamic-mapping* case: non-literal call arguments (const/var assignment, `:=` short-var-decl, `Sprintf`, field lookup) are resolved through per-file `{identifier → string_value}` def-use maps built by a single AST pass. Language coverage: Go (`const_spec`/`var_spec`/`short_var_declaration`), Python (`assignment`), JS/TS (`variable_declarator`), Java/Kotlin (`local_variable_declaration`/`property_declaration`). `resolve_first_arg` follows literal fast-path → def-use identifier → selector field; returns `None` for true dynamics (call-result, reflection) → falls to GPU-rank. Cross-language gRPC/pubsub edges are resolved by proto/registry match in Tier 1 (conf 1.0) — there is no separate FQN-join tier and no 0.9 stamp anywhere in the shipped code. Residue is emitted as candidates (not dropped) for Tier-1.75 disambiguation. **Strictly no regex, no framework vocab**: structural AST-node-kind primitives only. Feasibility: YASA (arXiv:2601.17390) build-free data-flow at 31.8 KLOC/min. |
| ~~**HR18**~~ | ***Retired 2026-07-28 — `kb/resolve_rerank.py` and `_llm_link_resolve` are deleted, and so is the `RSE_DEEPSEEK_MODEL` pin.*** One thing here is worth carrying rather than burying: Tier 1.75 used the **already-warm local reranker** to shrink the cloud bill before Tier 2 ever fired. That instinct — spend the GPU model you are already paying for before you spend a token — is why the reranker survives R0 while every LLM lane above it did not. Was: **GPU Tier-1.75 + SEA-style verified LLM + V4 Flash** (`kb/resolve_rerank.py` + `kb/bpre.py::_llm_link_resolve` + `graph/llm.py`, 2026-06-21; consolidated 2026-07-09). Tier-1.75: `rerank_candidates(query_context, candidates, *, margin=0.05)` — GPU cross-encoder (already warm); single candidate always binds; multi-candidate binds when gap ≥ margin; falls through to Tier-2 when gap < margin. `rerank_residue` applies Tier-1.75 across residue items; resolved carry conf=0.8. SweRank (arXiv:2505.07849): retrieve-and-rerank beats Claude-3.5 localization at ~60× lower cost; SACL (arXiv:2506.20081): structural/type context (not bare names) improves reranker precision. Tier-2 (`_llm_link_resolve`): SEA-style (arXiv:2408.04344) — LLM selects from the admitted set, never authors; callee verified ∈ index; `stable_prefix` cache (DeepSeek 384K context, `json_object` non-thinking); no self-consistency, no strict-schema enforcement. Model: `deepseek-v4-flash` (pinned; `deepseek-chat` alias deprecates 2026-07-24); override via `RSE_DEEPSEEK_MODEL`. Token spend accounted at `/api/metrics` under `llm_tokens.bpre_link.*` (`calls`/`completion_tokens`/`prompt_cache_hit_tokens`/`prompt_cache_miss_tokens`). **2026-07-09**: the parallel `kb/llm_escalation.py` (`escalate()`) reusable-helper implementation of this same tier was removed as confirmed dead code — it was built and unit-tested (SEA invariant, cache stats, cap/warning) but never actually called from `_llm_link_edges`/`_llm_link_resolve` or anywhere else in production; `_llm_link_resolve`'s own inline implementation is and always was the real, live Tier-2, and is the one this row now documents. See `docs/audits/2026-07-09-deep-conformance-audit.md`. |
| ~~**HR19**~~ | ***Retired 2026-07-28 — it was the deferred complement to a subsystem that no longer exists.*** It never shipped, so nothing was deleted; its subject was BPRE's cross-service recall (HR14), and with BPRE gone the revisit trigger it named — "a measured cross-service recall miss on a live federation" — can no longer fire. Was: **DEFERRED — cross-service recall complement** (documented, not built). Static IDL/FQN-join = static SOTA with a finite recall floor. Next-phase lift: (a) test-time OpenTelemetry traces via servicegraph connector — confirms dynamic paths at ~0.95; (b) gRPC reflection at runtime — resolves service-to-endpoint bindings without generated code. Reconciliation: trace-confirmed-static / trace-only (dynamic-flag) / static-only. Anti-hallucination guard (arXiv:2512.12117): structural verification gate stays mandatory even with trace data. **Reaffirmed 2026-07-09** (research refresh): static microservice recovery has a measured ceiling (F1 0.86 single-tool / 0.91 ensemble, arXiv:2412.08352) that runtime tracing is the recognized complement for — but building HR19 adds a runtime-trace operational dependency to a deliberately static/CI-friendly engine. **Revisit trigger: a measured cross-service recall miss on a live federation, not a date** — see `docs/audits/2026-07-09-whole-engine-conformance-and-research.md` addendum. |
| **HR20** | **Composite partition-quality gate on `index_state`** (`graph/quality.py`, 2026-06-22; updated 2026-06-25; field renamed from `kb_state` 2026-07-28). `partition_quality(store)` is deterministic (igraph + SQL, zero LLM) and computes a composite score: `modularity_q` (igraph.modularity), `coverage` (intra-community edges / total), `singleton_ratio` (L1 with member_count==1 / n_L1). `degenerate=True` when `(edges>0 AND singleton_ratio ≥ 0.60) OR (edges>0 AND coverage < 0.20) OR (edges>0 AND n_l1≥2 AND modularity_q < 0.05)`. **Edge-free projects (ec=0) are exempt from the entire degeneracy gate** — all three clauses require `edges>0`; an edge-free repo structurally cannot form non-singleton communities via detection so no penalty applies. A degenerate partition demotes `index_state` from `'ready'` to **`'degraded'`** in `overview(status)` (`_overview.py:160`) — and this gate is now the *only* thing standing between `indexing` and `ready`, which is why the state ladder is three-valued rather than four. **Modularity Q alone is explicitly rejected** as the sole gate (exponentially many near-optimal partitions, degrades on sparse code graphs; CPM preferred for code — arXiv 2501.07025). Federation roots with synthesis L3 communities (0 call edges) are exempt from `symbol_hollow`. |
| ~~**HR21**~~ | **DELETED** (WS-B 2026-06-26): Federation-global L3 roll-up synthesis (`kb/federation_hierarchy.py`) removed; L3 rows purged from all graph.db; `build_federation_hierarchy` gone. |
| ~~**HR22**~~ | **DELETED** (WS-B 2026-06-26): Deterministic structural spine (`kb/structure.py`) removed; level=0 dir/file community rows purged; `build_structure_tree` gone. |
| ~~**HR23**~~ | ***Retired 2026-07-28 — a token economy with no tokens to spend.*** `graph/enrich.py` is deleted, and with it `compute_significance`'s head/tail split, `_enrich_one_batch`, `RSE_ENRICH_BUDGET_TOKENS`, the abstention-on-unrecognised-type rule, and every `llm_token_stats()` namespace (`enrich.*`, `classify.*`, `bpre.*`, `bpre_link.*`). **Two things it left behind, both worth naming rather than assuming.** (1) The **`communities.narrated` column outlived the doctrine by a few hours** — schema, two data migrations, and `upsert_community`'s `MAX(narrated, …)` clause — with nothing writing 1 to it and no selector reading it. "Leaving the column is cheaper than a migration" was the first call and it was wrong for the reason [[feedback_guard_tests_must_discriminate]] keeps naming: an inert column still has to be reasoned about, and DN4 was still asserting its integrity. The **R2 purge dropped it** along with `semantic_type`, `kind` and `path`, deleting both migrations, the `MAX(…)` clause, and `test_abstention.py` whole. (2) The doctrine's *shape* is what R0 acted on: spend nothing to climb Information, and the Information rung — fastgreedy community structure at 0 tokens, byte-reproducible — is the rung that survived, because it was the one that never needed the LLM. Was: **DIKW token economy** (`graph/enrich.py`, 2026-06-23; updated WS-B 2026-06-26). Spend LLM tokens only to climb Information→Knowledge→Wisdom, and only at the nodes/queries actually read. **Information** (L1 fastgreedy community structure) = 0 tokens, byte-reproducible. **Knowledge** (LLM on the significance-gated head only): `compute_significance()` classifies all unenriched L1 into head (`member_count≥8 OR ≥2 cross-community edges`) and tail; head is enriched in batches of 20 via `deepseek_extract(stable_prefix, dynamic_tail)` (prefix-cached), capped by `RSE_ENRICH_BUDGET_TOKENS`. `_enrich_one_batch` sets `narrated=1` on every successfully enriched community (idempotency guard). **Abstention on unrecognised types**: when DeepSeek returns a type not in `_TYPE_ORDER`, `semantic_type` is set to `NULL` (reject-option doctrine, NCwR arXiv 2412.03190) — never forced to `"utility"`. **Tail abstains**: `label_community_structural` sets `semantic_type=NULL, narrated=0`. **`narrated` column** (`communities.narrated INTEGER DEFAULT 0`): 0 = unnarrated (tail or not-yet-narrated); 1 = LLM-narrated (head or lazily-narrated). All semantic retrieval selectors filter `narrated=1`. `llm_token_stats()` (flat dotted-namespace keys) exposes the full token budget at `/api/metrics` and `overview(what='metrics')`. Active namespaces: `enrich.*` (L1 head batched narration), `classify.*` (semantic-type classification), `bpre.*` (BPRE process narrative batched), `bpre_link.*` (BPRE Tier-2 edge-linking `_llm_link_resolve`, wired 2026-07-01 — previously called `deepseek_extract` without accumulation, invisible to the budget). *(L2/L3 `l2.*`/`l3.*` namespaces deleted WS-B 2026-06-26.)* `classify.calls==0` invariant: the tail abstains, so classify only fires on narrated head rows. |
| **HR24** | **Flat-L1 community detection: fastgreedy modularity** (`graph/community.py`). `detect_communities` uses `igraph.community_fastgreedy().as_clustering()` (Clauset-Newman-Moore) on the symbol call-graph's undirected subgraph; edgeless symbols are grouped by directory, which avoids an N-singleton explosion. RNG seeded at module import for byte-identical runs (`random.seed(0)` + `igraph.set_random_number_generator`). All output rows are `level=1`. Version constant: `ALGO_VERSION = "fg1"`. **`leidenalg` must not be imported** in `community.py` — SC8a. **SC8b**: `detect_communities` is byte-identical on two independent runs from the same graph. **Flat reasoning-retrieval** (`query/ask.py`): `_community_summaries()` selects top-k L1 communities via GPU cross-encoder rerank, with no L2 `parent_id` drill — one selector, used by both `ask` scopes, which differ only in whether the code axis or this one is assembled first. Candidate predicate: `level=1 AND summary IS NOT NULL AND summary!=''`. An unrecognized scope errors with its valid set rather than falling through to plain chunk context. |
| **HR25** | **Self-healing graph pipeline** (`graph/store.py` meta table; `daemon/sweeps.py` M1-M2, 2026-06-23; updated WS-B 2026-06-26). **M1 — version + source stamps**: `graph.db` carries a `meta(key,value)` table (survives `GraphStore.clear()`). Two keys: `algo_version = f"{ALGO_VERSION}+{_code_fingerprint()}"` (bumped by editing `ALGO_VERSION`) and `source_sig` (SHA-1 of sorted `relpath:mtime` for `iter_files(root, federation_mode=True)` — stat-only, GPU-free). Both stamped by `_index_project` after `detect_communities` and by `_rederive_graph` after re-derive. **M2 — reconcile self-heals stale graphs**: `reconcile_projects` calls `_graph_stale(path, gs)` in the non-federation branch (`sweeps.py:496`); when `True`, calls `_rederive_graph(entry.path)` + **`_label_project`** (`:510-512`) — the labelling successor to `_enrich_project`, renamed 2026-07-28 when its LLM half was deleted. The chain is a three-way `if/elif`: full `_index_project` + label when there are no communities at all, re-derive + label when the graph is stale, and **label-only** when just the summaries are missing (`_needs_labels`, `:513`), so a settled project is never re-indexed to fix a label. `_rederive_graph` is GPU-free (tree-sitter + igraph): clears graph, re-extracts symbols+edges via `_extract_graph(gs, root)`, re-detects L1 communities, restamps both meta keys. Rides the existing 30-min `_reconcile_loop` — no new timers. **GPU-only invariant preserved**: `_rederive_graph` never calls `get_embedder` or `embed()`. *(M3 L2 coarseness guard + M4 edge-sparse L2 completeness + `_regen_owning_hierarchy` L3 heal deleted WS-B 2026-06-26.)* |
| **HR26** | **GPU execution provider auto-detect** (`core/gpu.py`, 2026-06-23). `_GPU_EP_ORDER` whitelist (CPU excluded): `NvTensorRTRTXExecutionProvider → TensorrtExecutionProvider → CUDAExecutionProvider → MIGraphXExecutionProvider → ROCMExecutionProvider → DmlExecutionProvider`. `rank_gpu_providers(available, *, disable_tensorrt)` is pure (deterministic, no GPU). `select_gpu_device()` (@lru_cache): sets `CUDA_DEVICE_ORDER=PCI_BUS_ID` before CUDA init, enumerates via `pynvml` (max free VRAM → tie-break compute capability → lowest index), honours `RSE_GPU_DEVICE`. `select_gpu_providers()`: preloads ORT NVIDIA DLLs, ranks real `ort.get_available_providers()`, **raises `RuntimeError` if empty** (CPU forbidden — fatal). Both `Embedder` and `Reranker` call `assert_gpu_available()` in `__init__` and bind via `select_gpu_providers()` in `_init()`; session `add_session_config_entry("session.disable_cpu_ep_fallback", "1")` prevents ORT silent CPU binding; binding guard asserts `session.get_providers()[0] ∈ GPU_EP_NAMES`. `assert_gpu_available` / `is_gpu_available` are the renamed backward-compat aliases. |
| ~~**HR27**~~ | ***Retired 2026-07-28 — doc generation is deleted.*** Recorded because the deletion order is the reusable part: the generated trees were swept off disk **before** the guard came out, so no fleet index inherited machine-written prose. |
| **HR28** | **`docs/` is ordinary source.** There is no *generated* docs concept: `iter_files` walks `docs/` with no opt-in flag, `index_project` embeds it like any other directory, and `scope="docs"` is purely a language predicate over `_TEXT_LANGS` (`scope="code"` excludes the same set). A docs write therefore **must be re-embedded**, and the mechanism is `on_change`'s index step (`_index_files`/`_index_project`), which runs *unconditionally and above* the code-only drift gate — that gate filters docs out by construction, so a docs edit always reads as "no drift" there. GG4 pins that ordering inside `on_change`, having once asserted it against a fingerprint function with no production caller. Guards: GG1–GG4 in `test_docs_index.py`. |
| **HR29** | **`.rse-index.yaml` honored by every project-source enumerator.** **Two enumerators exist, and they cannot disagree**: the bulk walk (`index/discover.py::iter_files`) and the incremental/watcher path (`daemon/sweeps.py::on_change` → `discover.py::is_ignored_path`). Both resolve through the single `_should_drop` predicate in `discover.py`, which is where it lives precisely so the watcher, the indexer and the drift signal cannot diverge; `effective_config`/`is_excluded` survive as its `.rse-index.yaml` tier. That shared predicate is the whole of HR29's surface — a narrower claim than a list of walks, and a stronger one, because it is structural rather than repeated. No enumerator may silently index a file that `effective_config` would exclude. (See §16.5 in Part 1.) |
| **HR30** | **MCP surface integrity** (2026-06-26, Phase 2c; guard re-pointed 2026-07-28). The MCP tool surface must be exactly `index + search + graph + overview` (`ask` retired 2026-07-29). Guard: `test_mcp_has_four_tools` in `test_server.py`, asserting set equality — a subset check cannot see an *extra* tool, so it would not enforce the "exactly". |
| **HR31** | **LLM lane map — lane separation.** **Two lanes, and they must not cross.** **GPU** (FastEmbed/ONNX/CUDA) = embed + cross-encoder rerank ONLY, CPU fallback fatal. **`claude -p`** (`claude-haiku-4-5`, headless) = dashboard chat only, with account selection via `core/claude_profiles.py`. That is the whole map, and `claude -p` has exactly one caller: nothing generative runs on the indexing path at all. **The remaining rule is one-directional** — nothing outside `routes_chat.py` may reach a generative model, and `routes_chat.py` may reach one only by spawning `claude -p`. Guard: `test_inference_lanes.py`, whose DK2 half proves a two-lane map by a tree-wide scan rather than by a crossing test. |
| **HR32** | **Idle-efficiency: source-drift gate + idle-unload + single-flight heavy work.** `on_change` in `daemon/sweeps.py` computes `_code_source_fingerprint(project_path)` after the index step (HR38) and **returns without waking the graph lane** when the sig equals `_last_labelled_sig[path]`. The sig is stamped only by a pass that completed (`_graph_lane_pass`); on exception the file batch is pushed back into `_pending_graph_files` and *not* stamped, so a failed pass retries rather than silently losing the drift. (`watchfiles`'s `Change` enum has only added/modified/deleted — there are no close/open metadata events to filter.) With no cascade the daemon reaches true idle → `_idle_unload` (`server.py`, `RSE_MODEL_IDLE_UNLOAD_S` = 300 s, checked on a 60 s scheduler tick) nulls the embedder/reranker, calls `gc.collect` + `malloc_trim(0)`, and returns the ORT CUDA arena to the OS — still the only reliable RSS release path. **The lock must not leave**: `_HEAVY_LOCK` serialises every CPU-bound graph pass — symbol extraction and `_label_project` — across the watcher's dispatch workers, the graph lane and reconcile, so at most one heavy pass runs at a time. It is never held around index/embed or a GPU query. **Target**: < 1 % CPU at idle; a restart's one-time settle bounded to ~one core; RSS drops to the Python+uvicorn+sqlite floor; GPU still serves embed/rerank on real queries. |
| **HR33** | **Notification-first watcher contract** (2026-07-01, superseded by the watchfiles rewrite, HR37, same evening). `Watcher.start()` runs one `watchfiles.watch()` generator in a single `rse-watcher` thread; fallback to polling, if ever needed (NFS/SMB/WSL), is the Rust `notify` crate's own `force_polling`, never a hand-rolled Python loop. `watch()`'s `Change` enum has no separate close/open metadata events to filter. fanotify requires root → user-service daemon stays on inotify (via the Rust library). |
| **HR37** | **Watcher trigger surface is ignore-aware and storm-proof, not just the drift gates downstream of it** (2026-07-01 evening). `daemon/watcher.py` was rewritten from a watchdog `Observer` (one inotify instance + emitter/buffer thread **per scheduled watch** — 139 members → ~278 threads, already over the default `fs.inotify.max_user_instances=128`) to a single `watchfiles.watch()` (Rust `notify`) generator covering **all** roots in one thread. `_filter(change, path)` finds the owning root via `_owning_root` (boundary-aware, longest-`Path.relative_to`-match over the registered root set — a raw `path.startswith(root)` was found and fixed 2026-07-09: it has no path-boundary check, so a root whose name is a string-prefix of a sibling's, e.g. `foo` vs `foo-bar`, could misattribute events depending on set iteration order, corrupting HR5 index isolation; regression-guarded by `test_wt5_prefix_sibling_roots_no_misattribution`) and reuses the HR35 `is_ignored_path` resolver — hidden-dir/gitignore/RSE-config-aware, same resolver the drift gate uses — so ignored-dir events are dropped by the `watch_filter` callable and never reach `on_change`. `debounce`/`step` (Rust-side) coalesce a burst into one batch per `for changes in watch(...)` iteration; the batch is grouped by owning root and dispatched as one `on_change(root, files)` call per root per batch, subsuming the old hand-rolled per-project debounce. Dynamic `watch(new_root)` while the loop is running can't add paths to an active `watch()` generator, so it sets a `_restart` `threading.Event`; `_StopOrRestart.is_set()` (`self._stop.is_set() or self._restart.is_set()`) makes the current generator return `'stop'` and the outer loop relaunches with the updated root set. `watch()` blocks on a bounded `_restart_ack` `threading.Event` (set by the loop immediately after it clears `_restart` and is about to re-enter `_watch()`) before returning to the caller — closing a race found during Phase-6 test-writing where a write immediately following a dynamic add could land in the teardown/rearm gap and be silently lost (`notify` does not retroactively see events from before a watch is armed). Root-caused 2026-07-01 evening as a 4th, distinct idle-CPU cause via live `py-spy dump`: the daemon's own watcher dispatch thread was pinned at ~104% CPU for the full 64-minute process life (RSS 2.45GB, an unbounded Python-side `InotifyBuffer`/`DelayedQueue`), *even though* the HR35/HR36 drift gates were already correctly reporting "no drift" for the same ignored-dir churn — the gates were right, but the machinery asking them, per raw kernel event, was itself the burn. |
| **HR35** | **Gitignore/hidden-dir-aware discovery with RSE-config precedence** (2026-07-01). `_code_source_fingerprint` (`daemon/sweeps.py`) and the watcher's `is_ignored_path` both route through one shared resolver, `index/discover.py::_should_drop`, applied in strict order per path segment: (1) RSE `.rse-index.yaml` `exclude` → **drop** (strongest, explicit intent); (2) RSE `include` (new force-keep globs) → **keep**, overriding everything below — this is what makes RSE config win over `.gitignore` on conflict; (3) default policy (`cfg.use_default_ignores`): `IGNORED_DIRS` membership or a hidden segment (`.`-prefixed, below the project root) → **drop**; (4) `.gitignore` (new, supplementary, gated by `cfg.respect_gitignore`, default `True`; `pathspec.PathSpec.from_lines("gitignore", ...)`, cached per-file keyed on mtime, root + nested chains honored) → **drop**; (5) else **keep**. Root-caused 2026-07-01: `iter_files` had no hidden-dir skip and no `.gitignore` support, so a live `vite dev` + Playwright-MCP session continuously rewriting git-ignored `wiki/.svelte-kit`/`.playwright-mcp` flipped `_source_fingerprint` on every write, making the HR32 drift gate perpetually report "drifted" and re-triggering the full BPRE/enrich cascade every ~5 min — pinning a CPU core indefinitely and blocking `_idle_unload`. `IGNORED_DIRS` also gained an explicit tool-cache belt (`.svelte-kit`, `.playwright-mcp`, `.astro`, `.turbo`, `.parcel-cache`, `.vite`, `.output`, `.vitest`) for clarity on non-git projects. The fix adds no idle cost: gitignore specs are compiled once per file and cached on mtime; hidden-dir/`IGNORED_DIRS` checks are pure string comparisons. Verified live on the originally-affected repo by direct causal isolation: `iter_files` yields zero `.svelte-kit`/`.playwright-mcp` entries, and every observed post-fix fingerprint change traced to a real, tracked, actively-edited source file — never to a gitignored/hidden path. |
| ~~**HR36**~~ | ***Retired 2026-07-28 — BPRE is deleted, and the half worth keeping is HR35's.*** `_bpre_code_sig`, `bpre_source_sig` and `_member_scan_sig` all left with tier 3; the only surviving mentions in `src/` are dated retirement notes (`daemon/federation.py:25`, `index/discover.py:198`, and two test docstrings). **What this row actually contributed and must not be lost with it**: the *unification* — that a second walker must not re-implement discovery with weaker rules. That is now HR35's `_should_drop`, which every enumerator routes through (HR29), and it is a structural guarantee rather than a per-subsystem repair. Was: BPRE's federation-wide reuse stamp (`bpre_source_sig`, `kb/bpre.py`) and per-member scan-cache key (`_member_scan_sig`) previously hashed `_source_fingerprint` — the same all-files sig HR35 corrected for the indexer — but via a *separate, weaker* walk (`_source_files` used raw `os.walk` + `IGNORED_DIRS`, no hidden-dir/gitignore awareness). Fix: `_source_files` now routes through the same HR35 `_should_drop` resolver as `iter_files`, filtered to `is_code_language(detect_language(p))` (preserving `_is_test_file` exclusion and nested-federation-member pruning); `_bpre_code_sig(member)` hashes only that code-file set, coarse-cached on the member root-dir mtime (mirrors `_source_fingerprint`'s cache pattern) and invalidated by `daemon/sweeps.py::on_change` alongside `_fingerprint_cache`. `bpre_source_sig` and `_member_scan_sig` are repointed to `_bpre_code_sig`; the stamp is written once from the sig computed at rebuild start (`_reconstruct_processes_locked`), removing the prior end-of-rebuild recompute that chased a moving target. Root-caused 2026-07-01 as a 3rd, distinct idle-CPU cause — found live *after* HR35 shipped: on a 170-member federation root, concurrent docs/config/image edits (`docs/*.yaml`, `docs/**/*.png`) and hidden `.claude/*.js` tool-cache churn (both outside BPRE's actual dependency surface but inside the all-files fingerprint) kept flipping `bpre_source_sig` faster than the ~5 min federation-wide BPRE rebuild it triggered could finish, producing back-to-back rebuilds and pinning a CPU core continuously. Adds no idle cost (same coarse-cache-plus-explicit-invalidation pattern as HR32/HR35); a real member code edit still flips the sig and rebuilds correctly. |
| **HR38** | **The graph lane's drift gate is code-only.** Non-code churn — docs, config, image edits — must not wake a graph re-derive. `_code_source_fingerprint(path)` walks `iter_files` filtered through `is_code_language(detect_language(f))` behind a coarse-mtime-gated cache, and backs `_graph_stale`'s `source_sig` comparison, both `set_meta("source_sig", ...)` stamp sites (`_rederive_graph`, `_index_project`) and `on_change`'s gate comparison against `_last_labelled_sig`. Its cache is invalidated alongside `_fingerprint_cache` at the top of `on_change`, so every real watcher event re-walks fresh. The vector-index step (`_index_files`/`_index_project`) runs *before* this gate and is untouched — doc-search freshness is unaffected; only the graph lane is code-gated, and since that lane is the whole of the daemon's heavy work the gate is the main thing standing between non-code churn and a busy GPU. |
| **HR39** | **All tree-sitter parses are bounded out-of-process.** py-tree-sitter 0.25's `progress_callback` never fires during a stuck parse — ignored for bytestring input, never invoked even with a read-callback source, both hung to a 12 s OS-kill — and `tree_sitter_language_pack.get_parser` returns a bundled parser with no callback mechanism at all, so a pathological grammar (`cobol` fed non-cobol bytes) pins a CPU core forever with no in-process cancellation available. `index/bounded_parse.py` holds a persistent **spawn**-context worker pool (never `fork` — the daemon holds CUDA and many threads); `run_bounded(func_ref, args, deadline_s)` runs the extraction function entirely inside the worker, so only picklable results — never a `Node` — cross the process boundary. On timeout the worker is `terminate()`'d and respawned, `parse_timeout_count` (exposed via `overview(what="metrics")`) increments, a path-hash (never the real path — HR34) is logged, and extraction continues: timed-out files are recorded, never silently skipped. Workers never import the embedder, so the GPU-only doctrine is unaffected, and the overhead is indexing-time only — query and idle paths never parse. Guard: `test_no_unbounded_parse.py` bans a direct `get_parser(...).parse(` anywhere outside `bounded_parse.py`, which is what keeps the invariant closed for call sites nobody has written yet. |
| **HR40** | **Two-tier CPU budget, kernel-enforced not merely cooperative** (2026-07-01). All prior idle-CPU fixes (HR32/HR35/HR36/HR37/HR38) are *cooperative* — they stop the daemon from spuriously doing work, but nothing physically bounded it if a gate were ever wrong again. HR40 closes that with two independent, layered guarantees. **Idle tier**: `daemon/cpu_budget.py` resolves the daemon's own cgroup-v2 directory via `/proc/self/cgroup` (unified hierarchy only) and reads `cpu.stat`'s `usage_usec`; `cpu_percent_core()` diffs successive samples into a fraction-of-one-core figure, exposed via `/healthz` (`cpu_percent_core`, `cpu_quota_cores`) and `/api/metrics` (`cpu` block: `percent_core`, `quota_cores`, `usage_nsec`, `nr_periods`, `nr_throttled`, `throttled_usec`). A live automated gate (`test_cb3_idle_cpu_under_one_percent_core`) measures the *running daemon's own* `usage_nsec` delta over a quiescent ≥20s window via that HTTP surface and asserts it stays under 1% of one core — measuring the daemon's real cgroup, not the uncapped pytest process. **Active tier**: `daemon/systemd.py::unit_text()` sets `CPUAccounting=yes` and `CPUQuota=100%` in `[Service]` (mirrored by a `cpu-budget.conf` drop-in for operator overrides) — `CPUAccounting=yes` is required explicitly because `CPUQuota=` alone does not imply it (systemd issue #9647). This is a **cgroup-v2 kernel ceiling**: the daemon's entire service cgroup, including `index/bounded_parse.py`'s spawn-context worker pool (its workers are children of the daemon's cgroup, not a separate one), physically cannot exceed one core — not cooperative throttling, kernel enforcement. `RSE_BOUNDED_PARSE_WORKERS` default dropped `2→1`: under a 1-core quota, two workers would only time-slice the same core with no throughput gain and extra context-switch/RSS overhead. **Proof, not just presence**: `cpu.stat`'s `nr_throttled`/`throttled_usec` climbing under sustained real load (driven via an HTTP-triggered MCP `index()` call against a synthetic multi-file workspace, which spawns `reconcile_projects` inside the daemon's own process/cgroup) is the canonical evidence the cap is *physically biting*, not merely that usage happened to stay low — this is what `test_cb4_active_work_capped_and_throttled` asserts, alongside an upper bound of ~1 core average. **Hermetic delegation self-test**: `test_cb6_systemd_scope_delegation_hermetic_proof` runs a fresh `systemd-run --user --scope -p CPUQuota=100%` 4-process CPU burn, independent of the RSE daemon/unit entirely, and asserts its own `cpu.stat.nr_throttled` rises — proving the `cpu` controller is genuinely delegated to the `--user` systemd manager on this host (the precondition every other CB test depends on), with a remediation hint (root `delegate.conf` drop-in) if it isn't. |
| **HR34** | **The tracked tree is publishable and device-neutral** (2026-07-09; consumability facet 2026-08-05; restated here 2026-08-14 when the register that used to hold it was deleted). No absolute `/home/<user>/`, `/root/`, `/Users/<user>/` or `C:\Users\<user>\` path in any tracked file; every machine-specific value in `core/config.py` — models, embed device, daemon host/port, GPU device — is produced by `os.environ.get(...)`, so a fresh clone needs zero source edits to retarget. The pre-RSE brand tokens are permanently banned outside a narrow external-product allowlist — the patterns live in `test_no_legacy_ose_opencode_tokens_reappear`, which is the one place allowed to spell them. Device-specific name bans (company, codename, device id) never ship here: the *mechanism* is `test_no_banned_device_names`, the *values* arrive via `RSE_NAME_BAN` and live in the private audit repo, because a list of the names you must not publish publishes them. A clone with nothing to ban declares `RSE_NAME_BAN=none`; an unset variable fails, since a guard that stands down when its input is missing reports the same green as a clean tree. The tree is governed, **not the history** — the 2026-08-04 sweep's own pre-sweep text stays reachable via `git log -p`, and rewriting ~92 % of commits to remove strings no longer in the tree was measured and declined. Also whether a stranger can *use* it: the declared license and the shipped `LICENSE` must agree, and the shipped MCP configs must advertise exactly the registered tools. Guards: `test_public_hygiene.py` (`test_no_absolute_home_paths`, `test_nb1_name_ban_variable_is_declared`, `test_the_repo_ships_the_license_its_metadata_declares`) and `test_e8b_shipped_mcp_configs_advertise_the_registered_tools` in `test_server.py`. |
| **HR41** | **The daemon hands VRAM back on demand, not only after 300 s idle** (2026-07-29; restated here 2026-08-14). ONNX Runtime's BFC arena only ever grows — it holds the high-water mark of the largest batch a session served and returns it to the driver only when the `InferenceSession` is destroyed; `arena_extend_strategy=kSameAsRequested` (`core/gpu.py`) bounds how eagerly it grows, not whether it shrinks. So the release path lives in `daemon/server.py::release_models()` with three callers — the idle tick, `_shutdown_exit`, and `POST /api/gpu/release` — rather than inlined behind the idle check, where it was unreachable exactly when needed: a daemon being actively worked against never reaches 300 s idle. Measured on a 16 GB card: 12.2 GB still held at `active_clients=0`, starving the live suite (which loads its own embedder and reranker on the same GPU, ~8.4 GB) into 60 failures inside onnxruntime that named neither the GPU nor the daemon. Restarting the daemon turned those 60 into 623 passed with nothing else changed. Releasing must never become a silent CPU downgrade: the next caller rebuilds lazily on a GPU EP or dies. Guard: `test_gb1_gpu_release_actually_returns_vram`. |

### 13c. Federation invariants

A table rather than a list, because §14 cites these by number and a markdown list renumbers
itself. The five that used to sit here as items 5 and 9–12 each carried a "≡ HR6/HR8/HR9/HR10"
marker in their own text and are dropped rather than restated; the gap at #5 is that.

| # | Federation invariant |
|---|---|
| **#1** | **No inlining** — external symlinked sub-repos are never indexed into the root (`federation_mode=True`); indexed only as independent members. |
| **#2** | **Members are first-class** — every member is an enabled, separately-searchable project with its own DBs. |
| **#3** | **`root.federation` is authoritative** and re-synced on every `index_members` call. |
| **#4** | **Logical-repo coverage** — `search(project_paths=[root])` and `overview(project_path=root)` expand through `expand_federation` to cover root + all members. |
| **#6** | **Forbidden roots** (`/tmp`, `~/.cache`) are never registered. |
| **#7** | **Idempotency** — discovery, registration, reconcile, labelling, and config repair all converge on reruns. |
| **#8** | **Registry↔storage consistency** — cascade-remove + orphan-vacuum keep `projects.json` and `INDEX_ROOT` in agreement. |

## 14. Test coverage map

Each §13 invariant has a corresponding live test that proves it without mocks.

**Re-verified 2026-07-28, name by name, against `git ls-files src/tests/live/` and the actual
`def test_…` / `class Test…` definitions.** The tier-3 deletion took eight test modules with it, and
this map had drifted further than that implies: a probe found **70 names here that no longer
resolve**. Every row below now either names a test that exists or is struck through with the date.
Two shapes recur and are worth telling apart: a row whose *invariant* retired (HR11, HR13, HR14,
HR16–HR19, HR23, HR36) is struck through, while a row whose invariant is live but whose *test file*
left (#6, #7, HR2, HR4, HR7, HR12, HR15, HR20, HR29, §16, both HR32 rows) is **re-pointed at its
successor** — the invariant never lapsed, only its proof moved. **Gated since 2026-08-14**:
`test_coverage_map_names_resolve` reads this table and fails if any name it cites in backticks is
not a live `def test_…`. Strikethrough is the escape hatch, and the only one — a proof that leaves
must be struck in the same edit, which is how the drift above became invisible for so long.

| Invariant | Test | File |
|---|---|---|
| #1 no-inlining | `test_inv1_no_inlining` | `test_federation_architecture.py` |
| #2 members first-class | `test_inv2_members_first_class` | `test_federation_architecture.py` |
| #3 federation authoritative | `test_inv3_federation_authoritative` | `test_federation_architecture.py` |
| #4 logical-repo coverage | `test_inv4_root_scoped_search_fanout` | `test_federation_logical_entity.py` |
| #6 forbidden root | `test_inv6_forbidden_root` + `test_index_tool_rejects_forbidden_root` *(re-pointed 2026-07-28 — ~~`test_upsert_project_rejects_forbidden_root`~~ left with `test_p22_kb_e2e.py`)* | `test_federation_architecture.py` / `test_server.py` |
| #7 idempotency | `test_p22_incremental_reindex_idempotent`, `test_p21_community_count_stable_on_redetect`, `test_needs_labels_clears_after_a_labelling_pass` *(re-pointed 2026-07-28 — labelling is the successor property, and it is held more strongly than enrichment held it)* | `test_daemon.py` |
| #8 cascade remove | `test_inv8_cascade_remove` | `test_federation_architecture.py` |
| HR1 watcher→index | `test_p34_watcher_updates_vector_index` | `test_daemon.py` |
| HR2 watcher→graph / event-driven *(was "watcher→KB", 2026-07-28)* | `test_watcher_labelling_e2e`, `test_on_change_wires_graph_labelling` | `test_daemon.py` |
| HR3 labelling idempotence *(was "enrichment", 2026-07-28)* | `test_detect_communities_idempotent` | `test_graph.py` |
| HR4 federation fan-out | `test_inv4_root_scoped_search_fanout` + `test_p20_index_members_discovers_federation_members` *(re-pointed 2026-07-28 — ~~`test_real_federation_fanout`~~ left with `test_p22_kb_e2e.py`; the surviving pair still covers both halves, discovery and query fan-out)* | `test_federation_logical_entity.py` / `test_daemon.py` |
| HR5 one path → one index | `test_inv2_members_first_class` + `test_inv8_cascade_remove` | `test_federation_architecture.py` |
| HR6 GPU-only | `test_no_cpu_fallback`, `test_embedder_bound_to_gpu` | `test_smoke.py` |
| HR7 index_state → ready *(renamed with the HR7 row, 2026-07-28)* | `test_overview_status_has_index_state`, `test_index_state_demoted_when_degenerate` | `test_server.py` / `test_hierarchy_quality.py` |
| HR8 rerank lift + both axes | `test_e1_rerank_reorders_search_results`, `test_e2_ask_context_is_rerank_ordered`, `test_e3_community_context_is_reranked`, `test_e4_rerank_lift_metric` | `test_server.py` |
| HR9 MCP embed+rerank only | `test_e5_mcp_query_path_no_generation` | `test_server.py` |
| HR10 dashboard chat haiku-only | `test_e6_dashboard_chat_haiku_only`, `test_chat_stream_sse_sends_done` | `test_server.py` / `test_query.py` |
| ~~HR11~~ | ***Retired 2026-07-28 — the DeepSeek classifier left with tier 3.*** `test_bpre.py` is deleted. | ~~`test_bpre.py`~~ |
| HR12 no generative LLM in `src/` *(payload widened 2026-07-28 — the old row gated "no idle LLM spin"; there is no LLM lane left to spin)* | `test_no_local_llm_tokens_anywhere_in_src`, `test_no_module_opens_a_generative_llm_endpoint`, `test_deepseek_api_key_has_no_reader`, `test_only_claude_profiles_opens_a_url` | `test_inference_lanes.py` |
| ~~HR13~~ | ***Retired 2026-07-28 — the wiki left with tier 3.*** `test_wiki_rich.py` is deleted. | ~~`test_wiki_rich.py`~~ |
| ~~HR14~~ | ***Retired 2026-07-28 — BPRE left with tier 3.*** `test_bpre_processes.py` and `test_bpre_ast.py` are deleted; the tree-sitter-only half of that row survives as HR39's guard test. | ~~`test_bpre_processes.py`, `test_bpre_ast.py`~~ |
| HR15 no-heuristic doctrine | `test_no_code_semantic_regex_outside_allowlist`, `test_category_b_allowlist_has_no_dead_entries`, `test_extractor_has_no_hardcoded_lang_dicts`, `test_discover_uses_pack_language_detection`, `test_no_skip_markers_in_live_suite` *(rewritten 2026-07-28 — Category A is empty now that `kb/` is gone, so the guard is one scan with a four-module allowlist instead of two category scans)* | `test_no_code_semantic_regex.py` |
| ~~HR16~~ | ***Retired 2026-07-28 — the resolution ladder was tier-3 machinery.*** `test_deterministic_resolution.py` is deleted. The surviving no-skip guard is `test_no_skip_markers_in_live_suite` under HR15. | ~~`test_deterministic_resolution.py`~~ |
| ~~HR17~~ | ***Retired 2026-07-28 — Tier-1.5 value-flow left with tier 3.*** `test_valueflow_dynamic.py` is deleted. | ~~`test_valueflow_dynamic.py`~~ |
| ~~HR18~~ | ***Retired 2026-07-28 — Tier-1.75/2 left with tier 3, and with them the last DeepSeek model assertion.*** `test_rerank_resolution.py` and `test_token_economy.py` are deleted. **Not to be confused with query-time reranking**, which is live and is HR8's. | ~~`test_rerank_resolution.py`, `test_token_economy.py`~~ |
| ~~HR19~~ | ***Retired 2026-07-28 — "deterministic gating" gated LLM edges, and there are none.*** The whole write path is deterministic now, which is invariant #9 rather than a gate. | ~~`test_deterministic_resolution.py`~~ |
| §16 config inheritance | `test_hh1_full_index_baseline`, `test_hh2_on_change_filters_excluded`, `test_hh3_code_walk_honours_exclude`, `test_hh4_code_walk_universal_discovery`, `test_hh5_is_code_language_contract` *(re-pointed 2026-07-28 — all three former tests left with `test_p22_kb_e2e.py`)* | `test_config_universality.py` |
| HR20 partition-quality gate | `test_partition_quality_on_sample` *(was `…_on_rse`)*, `test_edge_free_graph_not_degenerate` (DQ1), `test_degenerate_fires_on_all_singleton_graph`, `test_status_includes_hierarchy_quality`, `test_index_state_demoted_when_degenerate` | `test_hierarchy_quality.py` |
| ~~HR21 federation L3 roll-up~~ | **DELETED** (WS-B 2026-06-26) | ~~`test_hierarchy_quality.py`~~ |
| ~~HR21 e2e + HR20 metamorphic~~ | **DELETED** (WS-B 2026-06-26) | ~~`test_hierarchy_e2e.py`~~ |
| ~~HR22 structural spine~~ | **DELETED** (WS-B 2026-06-26) | ~~`test_structure_hierarchy.py`~~ |
| ~~HR23~~ | ***Retired 2026-07-28 — lazy narration was the DIKW layer's LLM half, and it left with tier 3.*** `test_lazy_wisdom.py` is deleted; `narrate_community_lazy` (which had no production caller even before R0) went with `graph/enrich.py`. **The SC8 pair it shared with HR24 is live and stays there** — the determinism it asserts is now the *only* labelling behaviour, not the cheap tier of two. | ~~`test_lazy_wisdom.py`~~ |
| HR24 flat-L1 community detection + flat tree-walk | `test_sc8_no_leidenalg_in_community`, `test_sc8_detect_communities_deterministic` *(the real names — this row said "SC8a/SC8b" for both, and the underlying algorithm is igraph `community_fastgreedy`, not Leiden and not k-core)*; RR1 `all` puts Code first; RR2 `architecture` puts Architecture first; RR3 an unknown scope errors with its valid set *(all three rewritten 2026-07-28: RR1 and RR2 both asserted only that "Architecture" appeared, which is true of every scope and of a deleted ordering branch, and RR3 asserted the absence of a string no assembly ever emitted — the ordering is now the assertion)*; **RR4 grounded community context** (`test_rr4_community_summaries_grounded`); RR5 adaptive MR; RR6 determinism MR; RR7 empty fallback | `test_schema_consistency.py`, `test_retrieval_routing.py` |
| HR25 self-healing graph pipeline | `test_algo_drift_triggers_rederive`, `test_source_drift_triggers_rederive`, `test_rederive_graph_has_no_embedder_call`, `test_graph_stale_fires_on_poisoned_version` | `test_self_heal_e2e.py`, `test_self_heal.py` |
| HR26 GPU provider autodetect | `test_rank_gpu_providers_ladder_order`, `test_select_gpu_providers_non_empty_and_no_cpu`, `test_select_gpu_providers_fatal_on_cpu_only` | `test_gpu_autodetect.py` |
| ~~HR27~~ | ***Retired 2026-07-28 — doc generation is deleted.*** | ~~`test_docgen_rootonly.py`~~ |
| HR28 docs are ordinary source | GG1 (discovered by default), GG2 (round-trip embed+search), GG3 (scope purity), GG4 (**a docs write moves the fingerprint** — inverted from the old churn guard) | `test_docs_index.py` |
| HR29 config universality — every enumerator | `test_hh1_full_index_baseline`, `test_hh2_on_change_filters_excluded`, `test_hh3_code_walk_honours_exclude`, `test_hh4_code_walk_universal_discovery`, `test_hh5_is_code_language_contract` *(re-pointed 2026-07-28 — HH3's "cross-surface" pair was the spine and BPRE walks; the surviving cross-surface claim is that every enumerator routes through HR35's `_should_drop`)* | `test_config_universality.py` |
| HR30 MCP surface integrity | `test_mcp_has_four_tools` | `test_server.py` |
| HR31 LLM lane map — lane separation | `test_rerank_passages_only_in_gpu_lane`, `test_embedder_never_requests_cpu_ep`, `test_chat_lane_is_haiku_only`, `test_chat_primary_model_is_haiku`; the one-directional half is `test_only_claude_profiles_opens_a_url` under HR12 *(mapped 2026-08-15 — restated into §13b on 2026-08-14 with its guard named inline, and inline is not this table)* | `test_inference_lanes.py` |
| HR32 idle-efficiency (source-drift gate + idle-unload) | `test_drift_gate_skips_labelling_when_sig_unchanged`, `test_drift_gate_triggers_labelling_when_sig_changes` *(renamed with the gate's payload, 2026-07-28)*; idle-unload: `test_p22_idle_unload_clears_embed_singleton`, `test_idle_unload_gc_and_malloc_trim_present`, `test_idle_unload_then_cuda_reload` | `test_idle_stability.py`, `test_daemon.py` |
| HR32 single-flight heavy work *(was "reconcile bulkification (Part D)", 2026-07-28)* | `test_heavy_lock_serializes_concurrent_passes` — **the one assertion of that row that must not leave**, renamed with `_KB_HEAVY_LOCK` → `_HEAVY_LOCK`. The other three (~~`test_reconcile_active_flag_lifecycle`~~, ~~`test_reconcile_bpre_root_pass_unconditional`~~, ~~`test_bulk_reconcile_suppresses_member_bpre_fanout`~~) are deleted: `_reconcile_active` and the BPRE fan-out they guarded no longer exist, and a suppression test for a cascade that cannot fire would pass on a dead path. | `test_idle_stability.py` |
| HR33 notification-first watcher contract | `test_watcher_prefers_inotify_over_poll` | `test_idle_stability.py` |
| HR37 watchfiles watcher: ignore-filter/coalescing/dynamic-add/root-boundary | `test_wt1_ignored_dir_churn_never_reaches_on_change`, `test_wt2_real_edit_fires_once`, `test_wt3_batch_coalescing_single_call_per_burst`, `test_wt4_dynamic_add_restart_delivers_new_root`, `test_wt5_prefix_sibling_roots_no_misattribution` | `test_idle_stability.py` |
| HR35 gitignore/hidden-dir-aware discovery with RSE-config precedence | `test_gitignore_respected_root_and_nested`, `test_hidden_dir_skip_tool_caches`, `test_include_overrides_gitignore_exclude_beats_include`, `test_respect_gitignore_false_disables_gitignore_only`, `test_drift_gate_quiescent_under_tool_cache_churn`, `test_is_ignored_path_agrees_with_iter_files` | `test_idle_stability.py` |
| ~~HR36~~ | ***Retired 2026-07-28 with the HR36 row itself.*** The BPS1–BPS4 quartet is deleted; the same four properties are asserted for the surviving cascade by HR38's FCG1–FCG4, which is why the retirement loses no coverage. | ~~`test_idle_stability.py`~~ |
| HR38 code-only labelling-cascade drift gate *(was "enrich-cascade … unified with HR36", 2026-07-28 — HR36 is retired, so this row is the whole gate rather than half of a pair)* | `test_fcg1_docs_wiki_churn_quiescent`, `test_fcg2_config_image_churn_quiescent`, `test_fcg3_real_code_drift_fires_cascade_once`, `test_fcg4_convergence_second_call_reuses` | `test_idle_stability.py` |
| HR39 bounded parse (spawn-context worker pool, timeout kill/respawn) | `test_pool_timeout_kills_and_respawns_only_that_slot`, `test_pool_healthy_after_timeout`, `test_sigkill_mid_task_recovers`, `test_cobol_grammar_parity_through_bounded_path`, `test_metrics_reports_timeout_count`; guard: `test_no_direct_parse_outside_worker_modules` | `test_bounded_parse.py`, `test_no_unbounded_parse.py` |
| HR40 two-tier CPU budget (idle <1% live gate + kernel CPUQuota active cap) | `test_cb1_unit_text_has_cpu_accounting_and_quota`, `test_cb2_daemon_cpu_quota_enforced`, `test_cb3_idle_cpu_under_one_percent_core`, `test_cb4_active_work_capped_and_throttled`, `test_cb5_parse_cpu_max_synthetic`, `test_cb5_parse_cpu_stat_synthetic`, `test_cb5_cpu_throttle_stat_shape`, `test_cb5_cpu_percent_core_non_negative`, `test_cb6_systemd_scope_delegation_hermetic_proof` | `test_cpu_budget.py` |
| HR34 the tracked tree is publishable and device-neutral | `test_no_absolute_home_paths`, `test_no_legacy_ose_opencode_tokens_reappear`, `test_no_banned_device_names`, `test_nb1_name_ban_variable_is_declared`, `test_the_repo_ships_the_license_its_metadata_declares`, `test_e8b_shipped_mcp_configs_advertise_the_registered_tools` *(mapped 2026-08-15, as HR31 — the whole public-hygiene family was proven and unmapped)* | `test_public_hygiene.py`, `test_server.py` |
| HR41 the daemon hands VRAM back on demand, not only after 300 s idle | `test_gb1_gpu_release_actually_returns_vram` | `test_gpu_budget.py` |
| Federation exclusion in force on the live daemon (§15.3) | `test_fe8_daemon_env_carries_federation_exclude`, `test_fe9_no_enabled_project_is_federation_excluded`, `test_fe10_armed_disabled_rows_are_covered_by_the_exclusion`, `test_fe11_the_gates_fire_when_the_exclusion_is_lost` (sufficiency); drop-in wiring: `test_systemd_dropins_target_deployed_unit` | `test_federation_exclude.py`, `test_daemon.py` |
| Sweep pause is a lease that expires (§15.4) | `test_pause_lease_expires_past_its_deadline_and_says_so`, `test_re_pause_re_arms_the_deadline_without_restamping_the_leak_signal`, `test_on_change_runs_again_once_the_pause_lease_has_expired`, `test_healthz_reports_how_long_sweeps_have_been_paused` (both fields); guard: `test_production_reads_go_through_the_leasing_accessor` | `test_daemon.py`, `test_server.py`, `test_no_raw_sweeps_toggle.py` |
| Heavy periodic jobs run on their interval, not at every start (§15.4) | `test_scheduler_defers_first_run_by_one_interval`, `test_maintenance_job_is_not_registered_to_run_at_start` | `test_daemon.py` |
| The suite deletes the index dirs it creates (§15.4, §15.5) | `test_co3_test_teardown_leaves_no_index_dir_behind` (row-driven and tree-walk paths), `test_co4_the_final_backstop_takes_only_new_unowned_dirs` (listing diff spares pre-existing and registered dirs), `test_co5_a_restored_row_under_the_test_base_does_not_protect_its_store` (**the residue's actual cause** — federation discovery restores member rows mid-run, and CO4's registry check then spared the very dirs it exists to take), `test_co6_purging_a_path_outside_the_test_base_is_refused` | `test_clean_orphans.py` |
| Deletion is authorised at the point of deletion, not by its caller (§15.5) | `test_co6_purging_a_path_outside_the_test_base_is_refused` — `purge_project`/`purge_index_dirs_under` re-derive authority from the path itself via `assert_under_test_base`, and **raise** rather than skip. Written after a red demo of `purge_rows_under` ran a deliberately-broken predicate in-process, through the session fixture that calls it against the *real* registry, deleting 198 fleet rows and 138 stores with no backup and no filesystem snapshot. Every guard above this one lived in the caller, which was the thing that was wrong. Both arms asserted: a guard that refuses everything protects the fleet and breaks the suite. | `test_clean_orphans.py` |
| A live run never overlaps another live run | `test_sc1_no_contender_is_reported_for_our_own_process_tree`, `test_sc2_a_real_pytest_process_in_this_checkout_is_reported` — the gate had already produced two false positives, including seeing its own process tree, so both directions are pinned | `test_suite_concurrency_gate.py` |
| Watcher activity is observable, so an idle measurement can validate its own window | `test_hl6_status_counts_completed_passes` (`dispatched` rises once per completed pass and only then); consumed by `test_cb3_idle_cpu_under_one_percent_core`, which discards contaminated windows instead of relaxing its 1% threshold. `pending`/`inflight` are instantaneous and answer "busy now"; they cannot answer "did anything happen while I wasn't looking", and keying on `dispatched` alone let a window falling entirely inside one long pass read as quiet at 2.9% of a core — hence all three, both ends. | `test_watcher_dispatch.py`, `test_cpu_budget.py` |
| This map is itself gated, in both directions | `test_coverage_map_names_resolve` — every unstruck name cited above resolves to a live `def test_…`; `test_every_defined_hr_id_is_mapped` — every unstruck id §13b defines has a row here, which is the claim the first line of this section makes and nothing checked until 2026-08-15, when HR31, HR34 and HR41 were found proven and unmapped | `test_coverage_map_traceability.py` |

## 15. Design rationale

The *engineering principles* that govern all architectural choices are recorded as a first-class doctrine register in the companion document's §1a — see [federation-and-search-engine.md §1a](federation-and-search-engine.md#1a-engineering-principles-doctrine).

- **Symlink-based federation** mirrors how developers compose multi-repo workspaces without
  a manifest format to maintain.
- **Members as independent projects** keeps the pipeline uniform; makes incremental updates
  and removals cheap; gives correct results for both whole-workspace and single-repo queries.
- **Per-project content-addressed storage** bounds blast radius; makes vacuum/removal
  trivial.
- **Event-driven + reconcile** is self-healing: stalled projects repaired at startup and on
  demand; edits flow incrementally through the debounced graph lane.
- **One daemon over HTTP** removes the per-session ~1 GB engine cost of the stdio bridge.

### 15.1 Duplicate mass across a federation: excluded, not deduplicated (2026-07-30)

A 193-member federation carried **2,203,331 chunks / 6.77 GB** of float32, and the great majority of
it was the same bytes over and over: vendored front-end and framework trees (ckeditor, jquery,
PHPExcel, CodeIgniter `system/`, minified bundles, `public/assets/**`) copied into member after
member. The designed fix was content-addressed sharing — a `file_aliases` table plus a per-federation
`shared.db`, so one embedding could serve every member holding that file, keeping all of it findable.

**It was not built.** Excluding the vendored trees in one member root's `.rse-index.yaml` reached the
same mass with no engine change at all, because a root's `effective_config` unions its excludes into
every member: a single file reached 135 members. Measured before committing to it — the exclusion
patterns matched **917,890 of 1,295,061 chunks (70.9%)**, and **98.6%** of that was byte-identical to
a copy in a sibling member. The fleet ended at **377,171 chunks**, below the sharing design's own
computed floor of 395,026, with the whole of `index/store.py` untouched.

What that trades away, stated plainly: excluded means **unfindable**, not deduplicated. A query for
a symbol defined only inside vendored third-party code now returns nothing, where the sharing design
would have returned one canonical hit. That was accepted deliberately — those trees are read as
documentation at their upstream source, not searched here — and it is the reason the exclusions are
scoped to vendored paths rather than to duplication in general.

Two rules fell out of the measurement and should govern the next such pass:

- **Exclude per pattern, on that pattern's own duplication.** `public/css/*` was dropped from the
  candidate list because 69% of its chunks were single-copy: it looked like bulk in aggregate while
  being mostly first-party CSS written once. A federation-wide "exclude what looks vendored" rule
  would have taken it.
- **First-party duplication stays.** 124,853 chunks are genuinely duplicated first-party code across
  members. Excluding them would hide code someone works on daily, and they are exactly the mass the
  sharing design is still the right answer for, if the cost ever justifies the table.

### 15.2 The text screen asks the bytes, not the extension (2026-07-30)

`_should_drop`'s last rule is git's binary test — a NUL byte in the first 8 kB. It originally ran
only for files `detect_language` could not name (`lang == "unknown"`), on two premises, both of
which measurement withdrew:

- **"A language the pack can name is text."** It is not. `.pkl` collides with Apple's **Pkl**
  configuration language, so five sklearn pickles (`\x80\x04\x95 … MinMaxScaler`) were named `pkl`,
  handed the 500 kB *code* size cap, chunked and embedded. At 719 bytes each that was five junk
  chunks; the cap is what made it a hole, because a 400 kB model artifact would have indexed in full.
- **"A named file should not pay the read."** The read costs **+3% of a warm discovery walk** (1,010
  files) and +44% of a cold one — and every file reaching this rule is about to be opened and read
  in full by the chunker anyway, so the marginal cost is one extra `open()` per kept file.

Widening it was checked against the whole fleet before shipping, not after: of **66,911** files
walked across 139 projects, exactly **7** newly drop out — the 5 pickles plus two UTF-16LE files
(a vendored `jscalendar` locale file, a member `README.md`) whose every second byte is
NUL. Zero collateral. The 7 files held 17 chunks, purged via `scripts/purge_unindexable.py`.

**The two UTF-16 files are dropped deliberately, and this is the interesting half.** They are real
text, not mojibake at the source: a Croatian i18n catalogue and a one-line README, both carrying a
`\xff\xfe` BOM. But the reader has no BOM handling, so what was *stored* for them was UTF-8-with-
replace over UTF-16 — junk. Absent from the index is more honest than wrong in it, and it matches
git, which also calls UTF-16 binary. A BOM carve-out was considered and rejected: it is a
hand-written list whose entire effect would be to keep those mojibake chunks searchable. Making the
reader BOM-aware is the real fix for them and is not built.

Note the counting asymmetry this exposed: a "stored text contains a NUL" query found **16** chunks
where the purge removed **17**. One `calendar-hr.js` chunk's slice landed on a NUL-free stretch of
the mojibake. Detecting bad content by sampling its own symptom undercounts it; the file-level
decision is the one to trust.

### 15.3 The exclusion that saved 541,718 chunks lived in one unversioned file (2026-07-30)

§15.1's worktree exclusion is enforced entirely by `is_federation_excluded()` reading
`RSE_FEDERATION_EXCLUDE` **from the daemon's process environment**, and that value existed in exactly
one place: `~/.config/systemd/user/rag-search-mcp-daemon.service.d/federation-exclude.conf`, untracked.
Four more live drop-ins (`indexing-throughput`, `reconcile-resync`, `self-heal`, `llm-haiku`) were in
the same state, and the one drop-in the repo *did* carry was filed under `rag-search.service.d` — a
unit name that has never been deployed, so systemd ignored it.

The regression path needs no code change and no operator mistake beyond losing the file: 58 registry
rows sit disabled with `indexed_at: null`, so `_needs_index()` is True for every one, and
`register_all_members()` runs synchronously at each daemon start and re-enables whatever discovery
finds. Reconcile then re-indexes 541,718 duplicate chunks under a one-core `CPUQuota`.

**Nothing went red, and FE1–FE7 are why.** Every one of them sets `RSE_FEDERATION_EXCLUDE` itself
before asserting, so they test the predicate and are blind to whether production is configured at
all — the same shape as a hand-written list with only an existence test behind it. Three additions
close it, and the third is the one that matters:

- **FE8** reads the daemon's env from `/proc/<MainPID>/environ`. Not `os.environ` (this pytest
  process is not production) and not `systemctl show -p Environment` (that is what systemd *would*
  set on the next start). Reading a shell's own env instead of the daemon's is precisely what
  produced a false "reconcile is about to re-index 541k deleted chunks" alarm the day before.
- **FE9** asserts no *enabled* row is excluded — the exclusion and discovery not contradicting.
- **FE10** asserts every *armed* row (disabled, never indexed, path still present) **is** covered by
  the value in force. The covering set is derived from live registry state, not written down here, so
  removing the worktree glob from the drop-in turns this red without anyone remembering to edit a
  list. **FE11** proves FE9/FE10 can go red — it feeds the same detectors the empty value (what the
  daemon would get if the file were deleted) plus injected rows for the four-way truth table, because
  the live drop-in is the only copy of the production value and is not an experiment to run on.

`systemd.UNIT_NAME` now names the deployed unit once, `scripts/systemd/<UNIT_NAME>.d/` holds all five
drop-ins, and `test_systemd_dropins_target_deployed_unit` derives the expected dir name from that
constant — the wrong-unit-name bug cannot come back silently. `install()` deliberately still writes
only the base unit: an `install()` from a checkout must not overwrite host-tuned values. `%h` *is*
expanded inside `Environment=` in a drop-in file (probed on a throwaway unit, 2026-07-30) though not
for `systemd-run -p Environment=`, which is what keeps the tracked copies free of absolute home paths
(HR34). The now-deleted `rag-search-failure-notify.service` went with the old name: a *system*
unit cannot be ordered after a *user* unit, it was never installed, and nothing referenced it.

### 15.4 Three mechanisms leaning on each other: the pause, the sweep, and the run (2026-07-30)

Orphan index dirs were being cleaned, but by accident. Untangling that took three changes that only
make sense together, because each one alone makes the situation worse.

**The pause had no way back.** `_PAUSED` was a bare global honoured by four early returns. Eleven
pause calls from separate live-test sessions — each *correctly* restoring the "already paused" it
found — left every sweep off for ~4 h, and the previous pass made that state observable
(`_PAUSED_SINCE`, `sweeps_paused_s`) without making it recoverable. Observability is not recovery: a
field only helps an operator who is already looking. The pause is now a lease. `set_paused` stamps
`_PAUSE_DEADLINE`, and `is_paused()` — the single read path, statically enforced — releases the pause
past that deadline and logs at WARNING how long it ran. The two timestamps take **opposite** rules on
a re-pause, which is the whole design: the deadline is re-armed (a caller that pauses again is alive,
and the live suite renews after every test from `_drain_graph_lane`), while `_PAUSED_SINCE` is never
restamped, because the fault it exposes is *repeated* pause calls and a moving stamp would have read
a few minutes throughout that 4 h. A killed session now decays in ≤30 min instead of indefinitely.

**A 6 h job was running ~20×/day.** `_Job._last_run` defaulted to `0.0` and was compared against
`time.monotonic()` — seconds since *boot* (~415,000 s on this host) — so every registered job was due
at process t=0. Measured: 35 orphan-vacuum bursts in 3 days, **34 of them within 0–1 s of a daemon
start**, exactly one at the real 21,580 s interval. That put an `rmtree` pass plus freelist checks on
278 SQLite files (VACUUMs measured at 8.6 s) in contention with member discovery and watcher sync on
a 1-core `CPUQuota`, at the busiest moment the daemon has. `register()` now stamps an absolute
`_next_run` deadline, with `run_at_start=True` for the cheap liveness ticks that genuinely want t=0.

**Nothing owned the mess.** The live suite created index dirs and deleted only the registry rows —
and the row is the only handle on the `<slug>-<sha16>` name, so once dropped the store is unreachable
by name forever (19 dirs / 62 MB from one day of runs). It looked bounded only because the orphan
sweep collected them, i.e. *because of* the two bugs above: a pause that eventually got resumed by
hand, and a heavy job firing at every restart. Fixing either one in isolation **lengthens** the
orphan lag. So the run that creates a store now deletes it — `purge_project` by row, and
`purge_index_dirs_under` by symlink-safe tree walk for the many tests that drop their own row in a
`finally` — and `maintenance()`'s sweep stays as the backstop for dirs whose name nobody remembers.

### 15.5 The daemon puts the rows back, and the guard belongs at the deletion (2026-07-30)

Five stores per run still survived all of the above, and the reason is a race nobody had modelled:
**the thing being cleaned up is concurrently restored by production.** `register_all_members()` upserts
every member it discovers under a registered root, so a row the suite deleted at 14:00 exists again at
14:05 — and the listing-diff backstop deliberately spares any dir a registry row owns, so a restored
row protected exactly the dirs it existed to take. Minutes later the daemon noticed the tree was gone,
dropped the row, and left a store nothing owned and no name could find. `purge_rows_under` now runs at
*both* ends of the session, and a row under the suite's own base is treated as never a legitimate
owner — `test_no_real_project_in_tests.py` is the standing invariant that makes that safe.

**Then the red demo of that function destroyed the fleet.** Per the sufficiency rule, a new guard must
be shown failing before it is trusted; the predicate was broken to `if True:` — and that broken version
was picked up *in-process* by the session-scoped autouse fixture that calls `purge_rows_under(_SAFE_BASE)`
against the **real** registry. It matched every row: 198 registry rows and 138 index stores deleted, an
hour of GPU embedding each, with no `projects.json` backup and no filesystem snapshot on ext4. Recovery
meant mining absolute paths out of 14 days of journal and the Claude profiles' project lists, filtered
to "still a git repo", then re-registering with `indexed_at` unset so reconcile rebuilt each store.
The federation member lists self-healed from symlink discovery at the next start; the embeddings did not.

Three things follow, and only the third is about tests. **Authority is re-derived at the point of
deletion**: `assert_under_test_base` is called by `purge_project` and `purge_index_dirs_under`, and it
raises rather than skipping, because every other guard on this path lives in the caller — which is the
component that was wrong. **A red demo runs in a subprocess**, never in-process, whenever the function
under test is one a fixture calls against production state; the counterfactual is worth nothing if
proving it can execute against the real registry. And **the mining recipe is the backup we actually
have**: the journal is the only durable record of which projects exist, which is an argument for
`projects.json` being versioned or snapshotted, not for trusting a careful hand.

### 15.6 What shipped against the blast radius, and how to use it (2026-07-30)

The recipe in 15.5 is a recovery, not a control. Four things now sit between a wrong premise and a
destroyed fleet, and they are deliberately of different kinds — a refusal only helps against the
wrong sweeps it can recognise, so the third assumes the refusal failed.

**The sweep refuses an answer that is not credible.** `orphan_dirs()` raises
`OrphanSweepRefusedError` when the registry holds zero rows while `INDEX_ROOT` holds stores — a
self-contradictory state, since stores are only ever written for a registered project — and when
orphans are both more than 5 and more than half the tree. Small deletions are always allowed: one
orphan beside one live store is 50% of the tree and completely routine. `--allow-bulk` lifts the
majority cap for an operator who has read the refusal; it deliberately does **not** lift the
empty-registry refusal, because with zero rows there is nothing left to check the decision against.
If you see this, restore `projects.json` first — the sweep is reporting a broken premise, not a
dirty disk.

**`projects.json` rotates on shrink.** `_mutate` compares the row count across the write and keeps
`.bak.1..5` only when the count *falls*. Rotating on every write was the original proposal and would
have been useless: `register_all_members()` reaches `_mutate` once per discovered member at every
daemon start, so a 5-deep ring is consumed by a single startup and every backup would post-date the
accident you wanted to undo. The ring only ever holds deletions, which is the only thing anyone
wants back.

**Deleting a store is a move, not an `rmtree`.** All three production sites — `maintenance()`'s
sweep, `clean-orphans --yes`, and the MCP federated remove — relocate to
`INDEX_ROOT/.trash/<YYYYmmddTHHMMSS>-<slug>-<sha16>`, expired after 7 days by the same 6-hourly
`maintenance()` that fills it. Seven days is chosen against how these failures get noticed: both
incidents surfaced when someone searched a repo and got nothing back, which is the next time that
repo is touched, not the next time anyone reads a log. `quarantine()` never falls back to deleting
when the rename fails — a quarantine that silently degrades to an `rmtree` is worse than none,
because the operator reading the code believes there is a week of undo and there is not.

To restore one, strip the timestamp prefix and move it back:

```bash
ls ~/.local/share/rag-search/indexes/.trash
mv ~/.local/share/rag-search/indexes/.trash/20260730T230028-cart-svc-cb164afc4c19a4df \
   ~/.local/share/rag-search/indexes/cart-svc-cb164afc4c19a4df
```

Then confirm a registry row still points at the project path, **with `indexed_at` set**. This is
deliberately the opposite of 15.5's recipe: `_needs_index` returns True when `indexed_at is None`,
when the store is missing, or when it holds zero chunks, so an unset stamp discards the embeddings
you just restored and re-earns them on the GPU. Unset it only when you *want* the rebuild. A second
quarantine of the same store inside one second gets a `-1`, `-2` uniquifier, because `rename` onto
an existing directory fails on Linux rather than merging.

**Three things would otherwise eat the trash**, and `.trash` satisfies both of the tests each of
them applies — it owns no registry row, and it appears mid-run. Two collapse into `orphan_dirs()`,
where the shared rule lives precisely so the two sweep sites cannot drift apart again as they once
did (`.name` versus `.resolve()`). `.trash` is excluded from `stores` rather than only from
`orphans`, or a quarantine dir counted in the denominator would make the majority cap *harder* to
trip exactly as the tree emptied. The third is the live suite's own
`purge_unowned_index_dirs_created_since`, and it is the one with no `--yes` in front of it.

**The walk order decides what a rebuild ever reaches.** `reconcile_projects` returns on
`is_paused()` and keeps no resume cursor, so each pass restarts at position 0 and only ever
completes a prefix — which makes ordering a question of reachability, not of politeness. It was
keyed on `last_change_seen`, which a never-indexed project does not have, so `or ""` sent every one
of them to the tail of a `reverse=True` walk. Measured during this rebuild: **157 of 210 enabled
rows held zero chunks, and 157 of 157 had an empty key.** Because a live suite holds the pause lease
for its whole run, running the tests was the thing preventing the rebuild the tests were waiting on.
`reconcile_order()` now puts never-embedded projects first — a project with no vectors returns
*nothing* for a search, while a stale one returns slightly old results — and falls back to recency
wherever that key does not discriminate, which is the ordering that fixed an earlier starvation
(a pass once ground through 198 projects for 7.6 h without reaching either repo edited that day)
and still has to hold in steady state.
