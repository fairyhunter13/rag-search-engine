# Federation & Search-Engine Architecture — Part 1: Core

> Source-of-truth is `src/rag_search/`. Last reconciled 2026-07-28. **2026-07-28**: **tier 3 —
> the entire generative KB — was deleted.** `kb/bpre*.py`, `kb/wiki.py`, `kb/docgen.py`,
> `kb/okf.py`, `kb/valueflow.py`, `kb/patterns.py`, `kb/resolve_rerank.py`, `graph/enrich.py`,
> `graph/llm.py`, `vendor/okf/` and the `vendor/docgen` submodule are gone; `kb/answer_cache.py`
> is all that survives under `kb/`. **`DEEPSEEK_API_KEY` has no reader left anywhere in the repo**
> and `claude -p` on dashboard chat is the only generative call in the system. Every entry below
> this one is a **dated record** of what was true when written — the resolution ladder, the
> enrichment pipeline and the DeepSeek token economy they describe no longer exist, and the
> sections that described them in the present tense have been rewritten rather than deleted, so
> the shape of what left stays legible. **2026-07-09**: removed
> `kb/llm_escalation.py` (`escalate()`) as confirmed dead code — never called in production;
> `kb/bpre.py::_llm_link_resolve` is and always was the real, live, correctly-token-accounted
> Tier-2 SEA-select implementation (see `docs/audits/2026-07-09-deep-conformance-audit.md`).
> HR16/HR18 and the ladder pattern in `model.yaml` now cite `_llm_link_resolve` directly; the
> now-meaningless `/api/metrics` `llm_cache` key (fed only by the removed module) was dropped in
> favor of the already-complete `llm_tokens.bpre_link.*` telemetry. **2026-06-25**: Phase 1 — edge-free degeneracy exemption (D, HR20 `ec>0` guard on all three clauses); dashboard chat = Haiku-only (F, no DeepSeek fallback); architecture doc-sync + engineering principles register (§1a, HR27–HR29). **2026-06-23**: Phase 4A–F token-economy closes six LLM leaks (A=tail-classify guard `narrated=1`, B=batch L2 narration `enrich_communities_l2_batch`, C=full `llm_token_stats` instrumentation `classify/l2/bpre/l3.*`, D=narrated backfill `semantic_type IS NOT NULL`, E=batch BPRE narrative `_generate_narratives_batch` ≤20/call, F=L3 incremental self-heal via per-theme `child_sig` (Enzyme-IVM), 1800s window removed (`2380d45`)); cAST structural-path header prepended to every chunk (`chunk_file(project_root=...)`, arXiv 2506.15655); 3 stale Leiden refs corrected to k-core (HR24, shipped 2026-06-23). **2026-06-21**: H1–H3 universal symbol backbone (`tree-sitter-language-pack==1.9.1`, `process()` API, 306 langs, deleted `_TS_LANG`/`_DEF_KINDS`/`_CALL_NODE`/`_EXT_LANG`); G0–G5 5-tier resolution ladder (`kb/valueflow.py` Tier-1.5, `kb/resolve_rerank.py` Tier-1.75, `kb/llm_escalation.py` Tier-2, `deepseek-v4-flash` pin); HR15 Category B updated; HR16–HR19 added; §7a expanded. **2026-06-20**: BPRE Phase D + HR14; codex removed → haiku-only HR10; direct-DeepSeek classifier HR11; `think=False` HR12; regex→tree-sitter HR15.
> Continued in [federation-ops-and-invariants.md](federation-ops-and-invariants.md).

## 1. Purpose & scope

rag-search is a local, GPU-only semantic code-search and **deterministic structure** engine. It
indexes one or more project trees and serves five MCP tools (`search`, `ask`, `graph`, `overview`,
`index`) plus an HTTP dashboard from a single daemon at `127.0.0.1:8765`.

It was *"a code-search and KB engine"* until 2026-07-28. The KB half — the part that spent cloud
LLM tokens to narrate what the graph already knew — is deleted. What remains is retrieval (cAST
chunking → GPU embedder → vector store → GPU reranker) and structure (tree-sitter symbols, call
edges, fastgreedy communities with **templated** labels). Both halves are deterministic; neither
calls a generative model.

**Federation** treats a *root* project that contains **symlinks to external sub-repos** as
one **logical repository**, while storing and indexing each linked sub-repo ("member") as
an independent unit.

## 1a. Engineering principles (doctrine)

The governing principle is **P0: most efficient + most effective, for *everything* in RSE**. Every component, lane, and algorithm is chosen and tuned for maximum efficiency *and* effectiveness; all principles below are corollaries (§9b per-workload engine assignment, no cross-lane bleed; HR6, HR9, HR10, HR12, HR26).

1. ~~**DeepSeek = least/minimum token usage (DIKW token economy).**~~ *Retired 2026-07-28 with tier 3.* It governed the cloud generative lane — significance-gated head, prefix caching, tail abstention, child-reuse roll-ups, HR22–HR24's budget accounting. That lane no longer exists, so the principle has no subject; its **stronger** successor is stated in `docs/info-hierarchy.md`: the DIKW climb is now deterministic at every rung and costs `$0`, not "the fewest possible tokens". The id is kept rather than reused, per `docs/world-model/README.md`.
2. **tree-sitter, and nothing beneath it — no dynamic or static mapping, no keyword, no regex.** The only code that classifies *what the user's code means* uses tree-sitter (structural facts). **The "then LLM" half retired 2026-07-28**: the semantic/linkage tier that used to catch what tree-sitter could not statically reach lived entirely in `kb/bpre*.py` + `kb/resolve_rerank.py` and left with tier 3. That makes the prohibition load-bearing rather than tidy — a heuristic added here would be the *only* answer, not a first guess. No regex, no static/dynamic keyword list, no mapping table may substitute for structural analysis of user code, **package-wide over `src/rag_search/`** outside the four intrinsic-mechanism files (P6, §7a; HR14/HR15 survive, HR16–HR19's ladder does not).
3. **GPU-only inference; CPU fallback fatal; maximize GPU, minimize CPU & RAM** (HR6, HR26, HR32, P16). Idle target: < 1 % CPU, constant RAM floor (models unload after `RSE_MODEL_IDLE_UNLOAD_S`). The heavy pass — graph re-derive + structural community labelling — runs only on real source-fingerprint drift (`_code_source_fingerprint` gate in `on_change`; the `_last_enriched_sig` gate it replaced went with the enrich/wiki/BPRE cascade on 2026-07-28, and the gate survived because the re-derive it protects is the expensive half that did). File-watching is event-driven via `watchfiles`/Rust `notify` — one thread + one inotify instance for all roots, `watch_filter` ignore-aware via the same HR35 resolver as the drift gate (P17, HR33, HR37); polling, if ever needed, is the Rust library's own `force_polling` path, never a hand-rolled loop.
4. **No local generative LLM, and since 2026-07-28 no cloud one either except chat** — `claude -p` at claude-haiku-4-5 on the dashboard chat route is the *whole* generative surface, and `routes_chat.py` is its only caller. The cloud DeepSeek KB lane and the doc-tooling lane both left with tier 3 (HR12, P1/P14).
5. **Determinism + idempotence** — byte-identical reruns, now unconditionally rather than "with LLM off", because there is no LLM left to turn off. Community labelling is gated on `summary IS NULL` and never re-labels settled rows (HR3, HR20, HR21, HR24, HR25; **HR11, HR13 and HR23 governed the deleted narration — the DeepSeek `semantic_type` classifier, the wiki artifact, and the DIKW token budget — and retired with it**. HR24 stays: fastgreedy detection was always LLM-free).
6. **Federation = query-time union; MCP read-path is retrieval-only.** Every MCP action (`search`/`ask`/`graph`/`overview`) returns **root + all federated members combined** (query-time union; no cross-repo edges). The MCP query lane runs **no generative LLM inference** — only GPU **embedding** (+ cross-encoder rerank) for retrieval. **Since 2026-07-28 that is true of the write path too**: there is no enrichment-time generative spend to pre-build, so the read-path serves what deterministic indexing produced. The invariant used to be "no generation *here*"; it is now "no generation *anywhere* but dashboard chat" (HR4; §9b Lane A; read-only-MCP invariant). Federated readiness = **worst-of-members** (HR7); one absolute path = one index dir, per-project content-addressed stores (HR5).
7. **Self-healing** — event-driven (watcher) + reconcile re-derive on algo/source drift (HR1, HR2, HR25, §10).
8. **`docs/` is ordinary source + universal config** — nothing generates a `docs/` tree any more (docgen deleted 2026-07-28); `docs/` is walked, chunked and embedded like any other directory, and `.rse-index.yaml` is honored by every enumerator (HR28, HR29; HR27 retired).
9. **Two-stage retrieval; rerank is the relevance authority.** Query = hybrid recall (bi-encoder `sqlite-vec` + FTS5 BM25, fused by RRF) → cross-encoder rerank (`gte-reranker-modernbert-base`, GPU); results ordered by `rerank_score`, **never the bare retrieval score**; **both** AXIS A (code chunks) and AXIS B (community/architecture context) are reranked; reranking runs **at query time only**, never at index/KB-build time (HR8; inv#10, inv#11).
10. **Public-repo hygiene, now whole-tree.** RSE emits no artifacts at all since 2026-07-28 — wiki `community_*.md`/`domain_*.md`, `federation.md`, BPMN and citations all left with tier 3 — so the artifact-scoped rule (P7/HR13) is **retired** and its whole-repo widening (P18) is the entire principle: every tracked file must be safe to publish, with no secrets, no real device paths and no company/device names. The mechanism the old rule protected still exists and still matters: `symbols.file` stores **absolute** paths, so anything that ever renders one must strip it to root-relative first.
11. **Engineering doctrine** — every line of code is a liability (prefer no change → deletion → smallest sufficient diff); correctness before speed; live suite uses no mocks (real embedder + GPU). Machine-verified Concept→Spec→Impl→Test traceability closes the V&V loop (HR30).

## 1b. World model & governance/spec WM *(updated Phase 1 2026-06-26)*

RSE's world model is a **governance/spec WM** (see `docs/world-model/` + `docs/reference/world-model.md`):
- **State** = codebase + invariants/laws (P0–P18 in §1a + `model.yaml`; P7/P12/P13/P15 retired 2026-07-28, ids kept so numbering is never reused)
- **Action** = a diff/change
- **Guard** = does the diff satisfy the preconditions?
- **Planner/validator** = `scripts/check_world_model.py` (GPU-free; emits CONFORMS/AT_RISK)

The old `kb/world_model.py` Requirements Traceability Matrix (`overview(what='world_model')`, HR30) was **deleted** (WS-B 2026-06-26) along with `FEATURES.md`. The governance/spec WM in `docs/world-model/model.yaml` (L1–L4 layers) **replaces** it as the normative source of truth. `scripts/check_world_model.py` provides the executable conformance check.

RTM: §1a principles → §13b HRs → §14 test map (three layers). L3 traceability is machine-verified by `test_world_model_traceability.py` (asserts every `model.yaml` L3_specs `test:` resolves to a live `def test_…`). `test_feature_proof.py` guards non-import of deleted modules.

## 2. Vocabulary

| Term | Meaning |
|---|---|
| **Root** | A registered project whose tree contains symlinks pointing outside itself. |
| **Member** | An external repo reached via a symlink under a root; stored as its own project. |
| **Logical repo** | Union of root + all its members. |
| **Index dir** | `INDEX_ROOT/{slug}-{sha256(path)[:16]}` holding `vectors.db` and `graph.db`. The `wiki/` subdirectory left with tier 3 on 2026-07-28 — nothing writes it and nothing reads it. |

## 3. System context

- **Daemon** (`daemon/server.py`): uvicorn app = `mcp.streamable_http_app()` (FastMCP) +
  dashboard routes. Boots `assert_cuda_available()` first — **CPU fallback is fatal**.
- **Background** (`_start_background`): Scheduler → synchronous `register_all_members()` →
  `start_watcher()` → one-shot `reconcile_projects` thread.
- **Registry** (`core/registry.py`): `~/.local/share/rag-search/projects.json`,
  atomically written under `fcntl` lock. Each row: `ProjectEntry` with
  `path, enabled, indexed_at, file_count, chunk_count, federation: list[str], …`.
- **Vector store**: sqlite-vec flat `vec0`, `FLOAT[768]`, exact recall.
- **Graph store**: SQLite `symbols / edges (caller_sid, callee_sid) / communities`.
- **Enrichment LLM**: ***none — retired 2026-07-28 with tier 3.*** There is no enrichment lane and no cloud generative client in the repo; `DEEPSEEK_API_KEY` has no reader, so a keyless box is now the *normal* configuration rather than a crash. The dashboard chat LLM (**claude-haiku-4-5** via Claude Code CLI only, **no fallback engine** — emits SSE error when CLI unavailable) is **dashboard-chat only**, is the system's whole generative surface, and is never called by MCP tools.

## 4. Federation discovery (`daemon/federation.py:discover_members`)

```
root = Path(root_path).resolve()
os.walk(root, followlinks=False)       # any depth; do NOT follow links while walking
  prune dirs in IGNORED_DIRS
  for each dir that IS a symlink:
     target = dir.resolve()
     skip if target == root or target.is_relative_to(root)   # cycle guard
     if _looks_like_repo(target):      # iter_files(target) yields ≥1 file
         members.append(str(target))
     dirs.remove(dir)                  # never descend into the symlink
```

- **Any-depth** scan (commit 2796ae6): nested symlinks found, not just direct children.
- **Cycle guard**: links resolving back inside root are ignored.
- Returns **resolved absolute paths** so a member has a canonical identity.

## 5. Registration model

- `index_members(root)`: discover → upsert new members as `enabled=True` → write
  `root.federation = [all members]`. Returns newly-registered count.
- `register_all_members()`: `index_members` for every enabled project; idempotent.
- `expand_federation(path)`: `[path] + entry.federation` — the canonical "whole logical
  repo" primitive used by cascade-remove and read-path aggregation.

**Members are first-class independent projects.** Each has its own index dir and is
independently searchable. The root merely *references* members in `federation`.

## 6. Storage & isolation

Content-addressed: `INDEX_ROOT/{slug}-{sha256(path)[:16]}`. No cross-project DB sharing.
**Orphan vacuum** (`sweeps.maintenance`, @6 h): any `INDEX_ROOT` subdirectory not in the
registry is `rmtree`'d.

## 7. Indexing pipeline (`sweeps._index_project`)

1. **Chunk + embed** → `vectors.db` (`index/indexer.index_project`). Each chunk receives a deterministic structural-path header `# <repo-relative-path>` prepended to its content (cAST, arXiv 2506.15655; `chunk_file(project_root=...)`).
2. **Symbol extraction** (tree-sitter) over `iter_files(root, federation_mode=True)`.
3. **Call-edge resolution** (second pass): cross-file edges only.
4. **Community detection** (L1): fastgreedy modularity (`igraph.community_fastgreedy`, deterministic; edgeless symbols grouped by directory). Stamps `meta[algo_version]` + `meta[source_sig]` so `reconcile_projects` auto-repairs drift.
5. Stamp `indexed_at`, `file_count`, `chunk_count` in registry.

`federation_mode=True` prunes symlink dirs/files pointing **outside** the root — the
no-inlining invariant. Without it a root's file_count balloons ~12× by double-counting
linked trees.

## 7a. Code-semantic classification doctrine — tree-sitter, and nothing beneath it (P6, HR14/HR15)

The *only* code that classifies *what the user's code means* uses **tree-sitter** (structural
facts). No regex, no static/dynamic keyword list, no mapping table may substitute for structural
analysis of user code.

**Rewritten 2026-07-28.** This section used to read *"tree-sitter **or LLM** only"*, and the
semantic/linkage half — everything that caught what tree-sitter could not statically reach — lived
entirely in `kb/bpre*.py` + `kb/resolve_rerank.py`. It left with tier 3. Two consequences:

1. **The scope is now package-wide.** The doctrine is enforced over all of `src/rag_search/`
   rather than over a named Category-A list, because the list was mostly `kb/` modules that no
   longer exist. Exemptions are the four intrinsic-mechanism files below and nothing else.
2. **The prohibition is load-bearing rather than tidy.** With no escalation tier beneath it, a
   heuristic added here would be the *only* answer a caller ever gets, not a first guess that a
   later tier corrects. HR16–HR19's ladder is retired; HR14/HR15 survive in this narrowed form.

- **Exempt by name — the four intrinsic-mechanism files**, and nothing else
  (`test_no_code_semantic_regex.py::_CATEGORY_B_ALLOWLIST`): (a) `graph/extractor.py` — the
  generic call-node detector (node-kind ∋ `"call"`/`"invocation"`), the `_MEMBER_KINDS`
  frozenset — `_BRANCH_NODE_KINDS` left with tier 3 the same day, since it existed only to
  give BPRE a branch depth per call site — and the
  `tree_sitter_language_pack.process()` + `StructureKind`/`SymbolKind` label-space. (b)
  `index/discover.py` — the file-extension → language bootstrap. (c) `core/registry.py` and
  (d) `core/config.py` — path-slug plumbing (`re.sub`), no user-code semantics at all. *(The
  former category-B entry for LLM output-vocabulary tables in `graph/enrich.py`/`kb/wiki.py` is
  gone: both modules are deleted, so there is no LLM output to name.)*
- **The former Category A is empty**, which is why the guard now checks the *whole* package
  against the wide pattern set (`compile|finditer|findall|search|match|fullmatch|sub|subn`) that
  only Category A used to face — the tree-wide sweep previously screened `compile`/`finditer`
  alone. Measured at the deletion: zero hits outside the four exempt files.
- **Embedded-`<script>` sub-parsing (F2, 2026-07-09)**: Vue/Svelte/Astro/HTML host grammars all
  parse `<script>...</script>` as one opaque `raw_text` leaf — the grammar never descends into the
  embedded JS/TS as call/function/class nodes. `graph/extractor.py::_iter_script_blocks` locates
  that leaf plus its `lang="ts"|"js"` attribute (pure
  node-kind/attribute reads, no vocabulary) and sub-parses it with the js/ts grammar, remapping line
  numbers by the block's start row. This closes the symbol/call-graph blind
  spot for Vue/Svelte SFCs; `.html` files never reach this function (`is_code_language("html")`
  is `False`), so the generic detector only ever fires on real SFC files. Guarded by
  `test_embedded_script_extraction.py`. *(The BPRE twin, `kb/bpre_ast.py::_script_blocks`, left
  with tier 3 on 2026-07-28; the extractor's copy is the survivor and was always the one the
  symbol graph read.)*
- ~~**Resolution ladder** (HR16–HR19)~~ — ***retired 2026-07-28 with tier 3.*** It was the
  cross-service edge resolver for BPRE process reconstruction: three confidence tiers
  (1.0 `EXTRACTED` structural extraction → 0.8 `RERANKED` HTTP route match, literal or
  value-flow-resolved or GPU-rerank residue → 0.7 `_llm` cloud select), spread across
  `kb/bpre*.py`, `kb/valueflow.py` and `kb/resolve_rerank.py`. Every module in it is deleted and
  `process_graph.db` is no longer written, so there is nothing left to stamp a confidence on.
  Recorded here rather than removed because §1a item 2's ban on heuristics only reads as
  load-bearing once you can see that the tier beneath it is gone. `graph/extractor.py`'s
  parse-error/empty-structure fallback is still the deterministic `_generic_walk` (`H1` in the
  coverage table above), and it never called an LLM.
- **Guard test**: `test_no_code_semantic_regex.py`, rewritten 2026-07-28. It now checks the whole
  of `src/rag_search/` against the wide pattern set, exempting only the four files named above.
  The named `_SEMANTIC_HEURISTIC_DEBT` registry went with tier 3 — it had been **empty since
  2026-07-01**, so its guard iterated nothing and could not fail, and every entry it ever held
  named a `kb/bpre_spec.py` table.
- ~~**Token accounting (HR23)**~~ — ***retired 2026-07-28 with tier 3.*** `llm_token_stats()`,
  `_accumulate_llm_tokens` and the `bpre_link`/`enrich`/`classify` namespaces lived in
  `graph/llm.py`. `overview(what="metrics")` survives and no longer carries an LLM budget,
  because the system's only generative call is one `claude -p` per dashboard chat turn — bounded
  by user interaction, not by a sweep, so there is no budget to account for.

## 8. Community-labelling pass (`sweeps._label_project`)

**Renamed and reduced 2026-07-28.** This was `_enrich_project`, an eight-step pipeline whose middle
six steps were the generative KB. What survives is two steps, no network, no GPU:

1. Prune orphan L1 communities — rows with no symbols. This is what keeps the pass *terminating*:
   `label_community_structural` writes nothing for an empty community, so an orphan row would hold
   `_needs_labels` True forever.
2. Label every **flat L1** community whose `summary` is NULL or empty, via
   `graph/community.py::label_community_structural` — a deterministic template
   (`"N symbol(s) (kinds) from files. Primary: …"`), then a `Community-{id}` title fill-in.

*(Steps 2–6 of the old pipeline — LLM community narration, `semantic_type` classification,
`build_wiki`, `build_federated_index`, and `reconstruct_processes` (BPRE) — were deleted 2026-07-28
with tier 3. The 80 °C thermal guard that paced them went the same day and went **engine-wide**, not
just here: nothing in `src/` sleeps on temperature any more. The driver (~86 °C) and the hardware
(~88 °C) own throttling, and `gpu_temp_c()` survives as observability only. What *did* come back is
`_GPU_INFER_LOCK` around `Embedder.embed` — the guard that was actually load-bearing (onnxruntime
#26610: two ONNX sessions on one card from two threads abort at construction as well as at `Run()`).
Steps 7 — `run_docgen` — and 8 —
`index_docs` — were deleted the same day: nothing generates a `docs/` tree any more, and nothing
indexes one specially. `docs/` is ordinary source that `index_project` walks like any other
directory, and `search(scope="docs")` filters on language. See HR28 in Part 2.)*

The pass is **idempotent and gated on `summary IS NULL`**, so the daemon never re-labels settled
communities. It runs under `_HEAVY_LOCK`, single-flight across the watcher and reconcile threads,
and is never held around index/embed or GPU queries — search freshness is unaffected.

### 8a. LLM lanes within labelling — ***none, since 2026-07-28***

There is no generative call anywhere on the write path. The section this replaces described
summaries, symbol intents, `semantic_type` classification and wiki narrative all running through
cloud DeepSeek via `graph/llm.py`; that module and every caller are deleted, `DEEPSEEK_API_KEY` has
no reader, and **a keyless box is the normal configuration** rather than a crash at pass entry.
Determinism, which the old lane bought with `temperature=0`, is now structural: a template cannot
churn. Embedding and reranking are unchanged — they bind ONNX/CUDA and always did run regardless of
any key. The system's whole generative surface is one `claude -p` per dashboard chat turn (§9b).

## 9. Query / read path (`server/mcp.py`)

- **`search(query, scope, project_paths?)`**: when explicit paths are given, each resolved
  root is expanded through `expand_federation` (dedup), so a root-scoped query fans out
  across all members. No-path branch already covers members (they are enabled projects).
- **`ask(query, project_path?, scope)`**: gathers chunks from all `expand_federation` paths
  (each member's `VectorStore`, top_k per member), merges, then the GPU **cross-encoder
  re-ranks (Stage 2)** to global top-k by `rerank_score`, then `compose_answer` over the
  root's `GraphStore`. No LLM synthesis; persistent cache TTL 3600 s.
- **`graph`**: per-project call-graph queries (definition/callers/callees/impact/…).
- **`overview`**: `what=` views (`_overview.py::_VALID`, nine): structure, communities, status,
  projects, metrics, import_cycles, surprising_connections, suggested_questions, validate.
  *(`architecture_domains`, `hierarchy`, `world_model` deleted WS-B 2026-06-26. `patterns`,
  `feature_map`, `business_rules`, `process_flows` and `service_mesh` deleted 2026-07-28 with
  tier 3 — `patterns` was also the system's **only** generative call on a query path, a
  synchronous cloud round-trip inside an MCP tool, so its removal is what makes §1a item 6's
  read-path invariant unconditional.)*

## 9a. Reranking (Stage 2)

All MCP query paths run a **two-stage retrieval** pipeline (GPU; no CPU fallback):

- **AXIS A — code chunks**: *hybrid* retrieve, then cross-encoder rerank
  (`Alibaba-NLP/gte-reranker-modernbert-base`, fp16 ONNX) → sort by `rerank_score` → top_k.
  Federation: each member runs the above; union merged + re-sorted by `rerank_score`.
  Observability: `search()` records `rerank.queries` and `rerank.top1_changed` (the "lift"
  count where the cross-encoder moved a different chunk to position 1 vs the retrieval sort).
  Exposed via `GET /api/metrics` and `overview(what="metrics")`.
  *Hybrid, added 2026-07-28:* two lanes run per store — dense (`sqlite-vec` KNN) and lexical
  (FTS5 BM25 over `chunks_fts`, an external-content index on `chunks`) — fused by Reciprocal
  Rank Fusion (`Σ 1/(60 + rank)`) before either reaches the cross-encoder. Fusion is on **rank**
  because cosine and BM25 share no scale, no range and no polarity. The lexical lane is the only
  one that can retrieve a chunk for a name the embedder has never seen, and it is also what
  makes RRF scores comparable across federation members. No embeddings are involved, so the
  chunk shape and `embed_signature` are unchanged; `FTS_REV` versions the lexical index alone
  and a `fts_rev` meta key runs FTS5's `rebuild`/`optimize` backfill once per store
  (10.8 s + 1.8 s and +17 % on disk, measured on the fleet's largest store at 207 k chunks).
- **AXIS B — community/architecture context** (`_community_summaries`, both `ask` scopes):
  pool ≤50 L1 community summaries per store, then cross-encoder rerank → sort by `rerank_score`
  → top_k. Replaced former bi-encoder cosine (`s_vecs @ q_vec`) approach.
  *Updated 2026-07-28:* three near-duplicate selectors stood here (`_top_communities_semantic`,
  `_community_context`, `_tree_walk_context`); the unranked one fed the `feature`/`business`
  scopes the first N communities by rowid. All three collapsed into `_community_summaries`, and
  the scope surface collapsed with them — `all` and `architecture` are the two that remain, and
  they differ only in which axis is assembled first. Axis A takes no scope at all: one
  `search_federation` call serves every scope.
- Rerank scores (cross-encoder logits), RRF scores and vector scores are never blended across axes.
- Reranking runs **only** at query time; the index/KB-build pipeline never reranks.

## 9b. Inference lanes

| Lane | Surface | LLM(s) | Notes |
|------|---------|---------|-------|
| **A — MCP query** | `search`/`ask`/`graph`/`overview` via `/mcp` | embedding + reranking ONLY | No generation; delegated to the calling agent |
| **B — Dashboard chat** | `POST /api/chat_stream` | **claude-haiku-4-5** only (Claude Code CLI); emits SSE error event when CLI unavailable | The whole generative surface of the system; no fallback engine on this path (HR12) |

*(Lanes **D — KB enrichment** and **E — Doc-tooling** were both deleted 2026-07-28 with tier 3.
**HR31's four-lane map is now two lanes**, and that is the entire map. `claude -p` has exactly one
caller: lane B. Lane D was the cloud DeepSeek write-path sweep; nothing replaced it, because the
labelling that survives is a deterministic template.)*

**Lane invariant (HR12, HR31):**
- **LOCAL GPU lane** = embedding (FastEmbed/ONNX/CUDA, 768-dim) + cross-encoder reranking ONLY. CPU binding is fatal; any CPU fallback raises immediately. Non-generative by construction: a cross-encoder is a scorer.
- **CLOUD chat lane** = `claude -p` at claude-haiku-4-5, dashboard chat only, `routes_chat.py` its sole caller. Empty output is an error, not a fallback.

**Adding a third lane requires a row in this table first**, and the guard is
`test_inference_lanes.py`: no module in `src/rag_search/` may open a generative LLM URL, and
`DEEPSEEK_API_KEY` must have no reader. `core/claude_profiles.py` is allowlisted by name because it
opens `api.anthropic.com/api/oauth/usage` — a *usage* read for profile rotation, never a completion.

**No local generative LLM exists in the engine.** Ollama/qwen3 were decommissioned 2026-06-20. MCP query actions (`search`/`ask`/`graph`/`overview`) perform embedding + reranking ONLY — no generation (HR9). *(The 3-tier BPRE resolution ladder this paragraph used to close on — Tier-1.75 GPU rerank, Tier-2 cloud DeepSeek — retired 2026-07-28; see §7a. The GPU rerank lane survives, but it serves `search`/`ask`, not edge resolution.)*

## 16. Per-project config & federation inheritance

Each project may carry an optional `.rse-index.yaml` (or `.yml`) at its root.
`core/index_config.py` governs config loading and resolution.

### 16.1 `ProjectConfig` fields

| Field | Default | Meaning |
|---|---|---|
| `index.exclude` | `[]` | Glob patterns; matched against file path relative to root |
| `index.use_default_ignores` | `true` | Apply `IGNORED_DIRS` (node_modules, .git, …) |
| `watcher.max_pending_files` | `10 000` | Watcher queue cap before forced flush |

### 16.2 `effective_config(path)` — inheritance model

`iter_files(root)` resolves config via `effective_config(root)` instead of loading
`.rse-index.yaml` in isolation. Resolution rules:

1. **Standalone project** (no owning root in registry): `load_project_config(path)` — own file or defaults.
2. **Federation member** (path appears in some root's `federation` list):
   - `exclude` = **union** of root's globs + member's globs (order: root first).
   - `use_default_ignores`, `max_pending_files` = member's value when member has own config file, **else root's**.
3. Source label exposed in `overview(status).config.source`: `"own"` | `"inherited"` | `"default"`.

### 16.3 RSE config files are always indexed

`.rse-index.yaml` and `.rse-index.yml` **bypass** `exclude` patterns and
file-size limits in `iter_files()`. A user `exclude: ["*.yaml"]` rule never silently
drops the engine's own config from the index.

### 16.4 Config surfaced in `overview(status)`

`overview(what="status", project_path=…)` includes a `config` key:

```json
{
  "config": {
    "exclude": ["*.gen.py"],
    "use_default_ignores": true,
    "max_pending_files": 10000,
    "source": "inherited"
  }
}
```

`source` values: `"own"` (project has its own `.rse-index.yaml`),
`"inherited"` (federation member using root's config), `"default"` (standalone, no config file).

### 16.5 Config honored by every enumerator (HR29)

`.rse-index.yaml` is enforced not only in `iter_files` but in **every** project-source enumerator:

- **Bulk walk** (`index/discover.py::iter_files`): the indexer's own enumeration.
- **Incremental/watcher path** (`daemon/sweeps.py::on_change` → `discover.py::is_ignored_path`): the changed-file list is filtered before embedding.
- *(Structural spine `kb/structure.py` deleted WS-B 2026-06-26. The **BPRE walks** — `kb/bpre.py::_source_files`, `kb/bpre_ast.py::federation_discover` — and the **portal + docs walk** left with tier 3 on 2026-07-28; `index_docs`, which the latter fed, went with them.)*

**Two enumerators remain, and they cannot disagree**: both resolve through the single
`discover.py::_should_drop` predicate, which the generated-file drift fix (2026-07-17) moved
there so the watcher, the indexer and the drift signal could not disagree. That shared
predicate is now the whole of HR29's surface — a narrower claim than the four-bullet list above
it used to make, and a stronger one, because it is structural rather than repeated.

No enumerator may silently index a file that `effective_config` would exclude.
