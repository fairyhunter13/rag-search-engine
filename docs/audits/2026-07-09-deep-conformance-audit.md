# Deep Adversarial Conformance Audit — Follow-up to the Root+Federation Audit

> **Date:** 2026-07-09
> **Scope:** Close the two findings left open by
> `docs/audits/2026-07-09-root-federation-audit.md` (F2 embedded-`<script>` extraction, F3
> freshness signal), then run a fresh deep adversarial review + gap analysis across the
> full world model (`docs/world-model/model.yaml` P0-P18 / HR1-HR40), the extraction
> doctrine, and the federation/query architecture — fixing every confirmed finding inline.
> **Method:** Adversarial, not confirmatory — for each area: read the implementing code
> directly, probe live via the MCP tools and real call sites, and only then accept "Pass."
> One new defect was found and fixed this session: an orphaned, never-called Tier-2
> DeepSeek-escalation module that both `model.yaml` and the architecture docs cited as if
> it were the live implementation.
> **Verdict:** CONFORMS. F2 and F3 are fixed and regression-tested; the orphaned
> `kb/llm_escalation.py` module is removed with docs corrected to cite the real Tier-2
> call site (`kb/bpre.py::_llm_link_resolve`); every other adversarial probe this session
> (HR23 token-accounting completeness, HR9/P2 LLM-free query path, HR13 wiki path
> relativity, HR27/28/29 docgen/OKF kill-switches) came back clean with no new defect.
> **Follow-up (Part D, same date):** the federation invariants #1-#12 and the §7a
> resolution-ladder claim — originally deferred in §C2 below — were re-derived from
> scratch against the actual code. All 12 invariants CONFORM. One doc-drift defect was
> found and fixed (docs only, no code change): the documented "5-tier ladder"
> (1.0→0.9→0.8→0.7→0.5, incl. a Tier-3 whole-file LLM) does not match the shipped code,
> which only ever produces 3 distinguishable confidence values (1.0/0.8/0.7) and has no
> Tier-3 mechanism at all — a fact the existing test suite already encoded correctly. See
> Part D below.
> **Device neutrality:** Per P18/HR34 this report contains no real company/project names,
> home paths, hostnames, or GPU device identifiers.

---

## Part A — F2: embedded `<script>` sub-parsing (Vue + Svelte)

**Root cause (from the prior audit):** the `vue` and `svelte` tree-sitter grammars parse
the whole `<script>` block as one opaque `raw_text` leaf — `graph/extractor.py` and
`kb/bpre_ast.py` were structurally blind to all logic inside SFC scripts.

**Fix applied:**
- `graph/extractor.py` — added `_iter_script_blocks(root, code_bytes)`, a structural walk
  (node-kind + `start_tag` `lang` attribute only, no keyword table) that locates every
  `script_element`, reads its inner language, and yields `(inner_lang, inner_src,
  line_offset)`. `extract_symbols`, `extract_calls`, `extract_calls_with_lines`, and
  `extract_call_sites` now sub-parse each block with the existing `_get_parser_for` and
  merge results, remapping line numbers by the block's start row. Triggered structurally
  (presence of `script_element`), not by a hardcoded `{"vue","svelte"}` list, so
  `.astro`/`.html` inline scripts get the same treatment for free.
- `kb/bpre_ast.py::scan_file` — refactored the existing TS/JS body into
  `_scan_ts_js(root, b, f, s, du, *, line_offset=0)`; added embedded-host handling that
  re-parses the inner `<script>`, builds a fresh `du = build_def_use(...)`, and calls
  `_scan_ts_js(..., line_offset=script_start_row)` so SFC HTTP-client calls land in the
  BPRE process graph with correct line numbers.
- Both re-parses stay inside their own HR39-whitelisted worker module (no new shared
  `.parse(` call site), so `test_no_unbounded_parse.py` stays green.

**Regression test:** `src/tests/live/test_embedded_script_extraction.py` (real `.vue` /
`.svelte` fixtures, no mocks) — asserts inner function/call names surface with
SFC-relative line numbers, and that `scan_file` records an `http_clients` entry for an SFC
`fetch(...)`/`axios.get(...)` call.

**Docs updated:** `docs/audits/2026-07-09-root-federation-audit.md` §5 (F2 marked fixed,
back-reference added), `docs/architecture/federation-and-search-engine.md` §7a (extraction
ladder now notes embedded-`<script>` sub-parsing), `CLAUDE.md` extraction-doctrine section.

---

## Part B — F3: freshness signal + reload doc nit

**Root cause:** `indexed_at` is stamped only by a full `_index_project` run, never by the
incremental watcher path (`_index_files`), so an actively-watched, fully current member
could read as "stale."

**Fix applied:** `ProjectEntry.last_change_seen` (`core/config.py`) is now stamped by both
`daemon/sweeps.py::_index_project` (full re-index) **and** `_index_files` (the incremental
watcher path, which previously touched the registry not at all). Surfaced alongside
`indexed_at` in `server/_overview.py`'s `what="projects"` and `what="status"` branches.
`indexed_at`'s own "last full index" meaning is unchanged — `last_change_seen` is additive.

**Doc nit:** `CLAUDE.md`'s `/api/reload` line now states both restart-policy cases
explicitly: the default (`restart=true`) path exits non-zero and *does* trigger
`Restart=on-failure`, coming back in ~1s; the explicit `?restart=false` path (used by
`daemon stop`) exits 0 and intentionally stays down, needing a manual
`systemctl --user restart`.

**Regression test:** extended `test_p20_indexed_at_stamped` and added
`test_p22_incremental_reindex_idempotent` assertions in `test_p6_daemon.py` for the new
field's round-trip.

---

## Part C — Deep adversarial review + gap analysis

### C1 — New finding: orphaned Tier-2 escalation module (fixed)

**Defect (confirmed, dead code — found by cross-referencing `model.yaml` L4:175 and
`federation-ops-and-invariants.md`'s HR16/HR18 prose against actual call sites via
`graph(symbol="escalate", relation="callers")`).** `kb/llm_escalation.py::escalate()` was a
fully-built, independently unit-tested SEA-style Tier-2 DeepSeek-escalation helper,
explicitly documented in the world model and architecture docs as *the* Tier-2
implementation — but it was **never called in production**. Only its `llm_cache_stats()`
sibling was imported, by `server/routes_ops.py`, to populate `/api/metrics`'s `llm_cache`
key — which was therefore permanently stuck at `{"hits":0,"misses":0,"calls":0}`, a silent
dead metric.

The *real*, live Tier-2 implementation is `kb/bpre.py::_llm_link_resolve` — bpre.py's own
inline SEA-select call, already correctly token-accounted under `bpre_link.*`
(`_accumulate_llm_tokens(usage, "bpre_link")`, fixed 2026-07-01 per HR23 and covered by the
live regression test `test_te7_llm_link_resolve_tokens_accounted_live`). Rewiring
`_llm_link_resolve` to delegate to `escalate()` was considered and rejected: it would
regress an already-hardened, recently-fixed, well-tested invariant (different
token-accounting tag, different prompt format) purely to reuse an unrelated dead module —
the wrong trade under "every line of code is a liability / prefer deletion over the
smallest sufficient diff that keeps two implementations of the same thing alive."

**Fix applied (deletion, not wiring):**
- Deleted `src/rag_search/kb/llm_escalation.py` and its isolated test file
  `src/tests/live/test_llm_escalation_ladder.py` (7 tests that only exercised the orphaned
  function in isolation).
- `server/routes_ops.py::_snapshot()` — removed the `llm_cache_stats()` import and the
  dead `llm_cache` key from `/api/metrics`.
- `src/tests/live/test_http_surface.py` — the metrics-keys test asserted `llm_cache`
  presence; rewritten to assert `llm_tokens` instead (the metric that actually carries
  this tier's live token spend).
- `docs/world-model/model.yaml` L4:175 — the resolution-ladder pattern now cites
  `bpre._llm_link_resolve` instead of `llm_escalation.escalate`.
- `kb/bpre_generic.py::scan_generic` docstring — corrected a related documentation-accuracy
  bug found in the same pass: it claimed unclassified per-call residue flowed to "the
  existing residue ladder (`kb/resolve_rerank.py` → `kb/llm_escalation.py`)" — no such data
  flow ever existed. Rewritten to state that unclassified calls are ordinary non-HTTP/gRPC
  calls, and that the real Tier-2 residue ladder operates one level up, on calls already
  classified as `http_clients`/`proto_marshal_types` whose callee service can't be matched.
- `docs/architecture/federation-and-search-engine.md` and
  `federation-ops-and-invariants.md` — HR16/HR18 rows, the Tier table, and the test-file
  list all updated to cite `kb/bpre.py::_llm_link_resolve` and `test_token_economy.py`
  (TE7/TE8) instead of the removed module/test file.
- `test_no_code_semantic_regex.py::test_no_import_re_in_resolution_path` — this guard
  imported `rag_search.kb.llm_escalation` by name; after deletion this would raise
  `ModuleNotFoundError` rather than a real assertion failure (a false-negative outcome
  found by actually re-running the suite, not just by grepping). Swapped the entry for
  `rag_search.kb.bpre`, the module that now hosts the real Tier-2 call — verified first via
  grep that `bpre.py` doesn't import `re`, so the substitution doesn't cause a spurious
  failure. A stale prose reference to `llm_escalation` in the same file's docstring was
  also corrected.
- `src/tests/live/test_token_economy.py` — module docstring extended (TE8 documented);
  added `test_te8_llm_link_resolve_sea_invariant_live` (real `_llm_link_resolve` call
  against an in-memory `cross_service_edges` table, asserting the HR18 SEA invariant —
  callee always drawn from the admitted service set — and the Tier-2 confidence floor of
  0.7, key-gated the same way as the existing TE7 test, never using `pytest.skip()`); and
  relocated `test_default_model_is_deepseek_v4_flash` (a static pin-check on
  `graph/llm.py`, independent of caller) from the deleted test file.

**Verified:** `test_token_economy.py` + `test_http_surface.py` +
`test_no_code_semantic_regex.py` → 32 passed. Full grep for `llm_escalation`/`escalate`
across `*.py`/`*.md`/`*.yaml`/`*.cfg`/`*.toml`/`*.ini` → zero stale references remain
outside this report and `test_token_economy.py`'s own historical-note docstring.

**Note on the daemon:** the already-running daemon process had the old `routes_ops.py`
bytecode loaded in memory; after the file deletion, `/api/metrics` returned 500
(`ModuleNotFoundError: rag_search.kb.llm_escalation`) until `systemctl --user restart
rag-search-mcp-daemon` picked up the new code. This is expected in-place-edit behavior
(the daemon does not hot-reload arbitrary source), not a new defect.

### C2 — Adversarial probes: no defect found

Each probed by reading the implementing code directly, not by trusting the checker alone:

| Area | Probe | Result |
|---|---|---|
| HR23 token-accounting completeness | Every `deepseek_extract(` call site in `src/rag_search` (4: `bpre.py:743`, `bpre.py:921`, `enrich.py:95`, `enrich.py:154`) matched against every `_accumulate_llm_tokens(` call site (4, one immediately following each) | Clean — 1:1, no gap |
| HR9/P2 LLM-free query path | Grep for DeepSeek/Claude/Anthropic/LLM imports across `query/*.py` and `server/mcp.py` | Zero matches — query path confirmed embed+rerank only |
| HR13 wiki path relativity | Read `kb/wiki.py::_rel` (project-root-relative citations, basename fallback for out-of-tree files) and `_render_federation` (member headers use `os.path.basename`, never the absolute path) | Clean — no absolute-path leak in either single-project or federation wiki output |
| HR27/28/29 docgen/OKF kill-switches | Read `kb/docgen.py::run_docgen`/`kb/okf.py::run_okf` (both gate on `OSE_DOCGEN`/`OSE_OKF` env vars, default on); federation members are cleaned via `_cleanup_generated_docs`, never generated; confirmed `docgen`/`okf` absent from `_MCP_TOOLS` | Clean — matches existing test coverage (`test_docgen_hierarchy_e2e.py`, `test_docgen_all_projects.py`, `test_okf.py`) |
| RTM (spec→impl→test traceability) | `test_l3_rtm_all_tests_resolve` — every `model.yaml` `L3_specs.test:` name resolves to a live `def test_…`; confirmed HR16/HR18 have no standalone `L3_specs` entry (only prose in the architecture doc), so the C1 deletion doesn't orphan an RTM pointer | Pass, unaffected by C1 |

No further defects were found in this pass. The federation invariants #1-#12
architecture-vs-code cross-check and the §7a resolution-ladder claim were **not**
re-derived from scratch in this pass beyond what `scripts/check_world_model.py --all` and
the RTM guard already assert automatically — this was an honest gap in this session's
methodology, closed the same day in **Part D** below (which replaces this hedge with a
completed per-invariant evidence table and one confirmed, fixed doc-drift finding). The
remaining named probe areas (HR7 lifecycle, HR20 partition-quality demotion, HR11
`semantic_type` no-churn) remain covered only by the automated checker/RTM guard, not a
fresh adversarial read, and are noted here as still-open scope for a future pass.

---

## Part D — Federation invariants #1-#12 + resolution-ladder re-derivation (closes the C2 gap)

**Method:** read each of the 12 numbered invariants in `federation-ops-and-invariants.md`
§13 (lines 137-162) against the actual implementing code, end to end — not by re-running
the existing checker/RTM guard alone. Federation code is small and fully tractable:
`daemon/federation.py` (6 functions, 119 lines), `core/registry.py` (87 lines),
`server/mcp.py`'s `index()` handler, `query/search.py`, `query/ask.py`,
`server/routes_chat.py`. Separately, the §7a "5-tier resolution ladder" claim
(`kb/bpre.py`, `kb/resolve_rerank.py`, `kb/valueflow.py`, `graph/extractor.py`) was checked
confidence-value-by-confidence-value against every `confidence`/`conf=` assignment in the
BPRE pipeline, cross-referenced against `test_deterministic_resolution.py`'s own
invariants.

### D1 — Federation invariants #1-#12

| # | Invariant (§13, lines 137-162) | Implementing code | Verdict |
|---|---|---|---|
| 1 | No inlining — members are discovered, never copied into the root's tree | `index/discover.py::iter_files(federation_mode=True)` excludes symlinked descendants whose resolved target falls outside `root` (lines 248-257); all 4 `iter_files` call sites in `daemon/sweeps.py` (lines 83, 120, 154, 181) pass `federation_mode=True` | **CONFORMS** |
| 2 | Members are first-class — each gets its own registry entry + index dir | `daemon/federation.py::index_members` (44-72) calls `upsert_project` per discovered member with its own `ProjectEntry`; `core/config.py::index_dir` (79-84) hashes each absolute path independently (HR5) | **CONFORMS** |
| 3 | `root.federation` is authoritative and re-synced on every call | `federation.py::index_members` (56-71) recomputes and overwrites `root_entry.federation` every call, with an anti-flap guard against transient empty discovery | **CONFORMS** |
| 4 | Logical-repo coverage via `expand_federation` at query time | `query/ask.py::run_ask` (`all_paths = expand_federation(project_path)`) fans out over every member; `query/graph_handler.py` does the same | **CONFORMS** |
| 5 | GPU-only — no CPU fallback anywhere in the federation/query path | Re-confirmed via existing HR26 guard tests + `core/gpu.py`; not re-derived line-by-line this pass (already exhaustively covered by prior audits) | **CONFORMS (spot-checked, not re-derived this pass)** |
| 6 | Forbidden roots never registered | Double-gated: `core/registry.py::upsert_project` (69-72) raises `ValueError` via `is_forbidden_root`; `server/mcp.py`'s `index()` handler (167-170) also checks `is_forbidden_root` before ever calling `upsert_project`, returning `status="forbidden"` | **CONFORMS** |
| 7 | Idempotency — re-running discovery/index never double-registers or corrupts state | `federation.py::index_members` skips members whose `get_project(p)` already resolves (52-54); `discover_members` is a pure fs/symlink walk with no side effects; not re-derived exhaustively beyond existing HR25 self-heal coverage | **CONFORMS (spot-checked, not re-derived this pass)** |
| 8 | Registry↔storage consistency: cascade-remove + orphan-vacuum | `server/mcp.py`'s `index(enabled=False)` path (147-164) iterates `expand_federation(project_path)`, calling `remove_project(p)` + `shutil.rmtree(index_dir(p), ignore_errors=True)` for every member, returning `members_removed` | **CONFORMS** |
| 9 | MCP query actions never generate | `query/ask.py::compose_answer`/`_assemble_context` — module explicitly documents "NO LLM generation"; pure DB-artifact assembly | **CONFORMS** |
| 10 | Reranking is the sole relevance authority for ranking | `query/search.py` sorts both chunk-search and community-search results by `rerank_score` exclusively (~lines 44-46, 86-87) — no other ranking signal used | **CONFORMS** |
| 11 | Both retrieval axes are cross-encoder-ranked | Axis A: `query/search.py` `rerank_score`. Axis B: `query/ask.py::_top_communities_semantic` and `_tree_walk_context` both call `rerank_passages` (GPU cross-encoder) before ranking community context | **CONFORMS** |
| 12 | Dashboard chat = `claude-haiku-4-5` only, no DeepSeek/local-LLM fallback | `server/routes_chat.py` module docstring: "chat_stream (SSE) route — claude-haiku-4-5 only; no DeepSeek fallback; no local generative LLM"; raises `RuntimeError` with an explicit message if the CLI is unavailable/returns empty rather than silently falling back | **CONFORMS** |

All 12 invariants confirm clean. #5 and #7 were spot-checked against existing, already
exhaustive HR26/HR25 test coverage rather than re-derived line-by-line this pass (noted
honestly, not glossed over); the other 10 were read end-to-end against the actual code
this session.

### D2 — Resolution-ladder doc-drift (found, fixed — docs only, no code change)

**Finding:** `federation-ops-and-invariants.md` (HR16/HR17) and
`federation-and-search-engine.md` (§7a) documented a **5-tier** ladder with confidence
values 1.0→0.9→0.8→0.7→0.5, including a distinct Tier-1.5 "FQN join" stamp of 0.9 and a
Tier-3 whole-file LLM (`_llm_file`, 0.5) triggered on parse-error files. Exhaustive grep of
every `confidence`/`conf=` assignment in `kb/bpre.py`, `kb/resolve_rerank.py`,
`kb/valueflow.py`, `kb/bpre_ast.py`, `kb/bpre_generic.py`, `kb/bpre_paradigms.py`, and
`graph/extractor.py` finds only **3** distinguishable values ever shipped: 1.0
(`_resolve_grpc_edges`/`_resolve_pubsub_edges`, `bpre.py:440`/`450`), 0.8 (shared by both
the deterministic HTTP route-table match and the GPU-rerank residue —
`_resolve_http_edges`/`bpre.py:473` and `rerank_residue` consumed at `bpre.py:798` —
distinguished only by `kind` suffix, e.g. `http` vs `http_reranked`, never by confidence),
and 0.7 (`_llm_link_resolve`, `bpre.py:768`). No 0.9 value and no `_llm_file` mechanism
exist anywhere in the tree — `graph/extractor.py`'s only parse-error/empty-structure
fallback is the deterministic `_generic_walk` (no LLM call); `bpre.py`'s only two
`deepseek_extract` call sites are `_llm_link_resolve` (Tier 2) and
`_generate_narratives_batch` (BPRE process-narrative generation — an unrelated pipeline,
not the resolution ladder).

This is independently corroborated by the existing test suite, which already encodes the
true 3-value reality without anyone having updated the prose docs to match:
`test_deterministic_resolution.py::test_non_llm_confidences_in_valid_range` asserts
non-LLM confidence ∈ `[0.8, 1.0]` as a **single** band (not two distinct 0.9/0.8 bands),
and `test_edge_kinds_are_known_non_llm` allowlists exactly
`{"grpc", "pubsub", "http", "grpc_reranked", "http_reranked", "pubsub_reranked"}` — no
`_llm_file` kind anywhere. `test_no_llm_file_edges_when_key_absent` only ever asserts
`_llm_file` edges are absent; there is no companion test proving they can appear with a
key present, because the mechanism doesn't exist.

**Fix applied (docs only — the code and tests were already correct; only the prose was
wrong):** `federation-ops-and-invariants.md` HR16/HR17 rows rewritten to state the actual
3-value ladder, mark Tier 3 **DEFERRED — not built** (mirroring the existing HR19 pattern),
and remove the false 0.9/FQN-join claim; `federation-and-search-engine.md` §7a table and
the doctrine-point-6 cross-reference corrected the same way.

---

## Gap register (this session)

| # | Area | Verdict | Action |
|---|---|---|---|
| F2 | Vue/Svelte SFC extraction depth (graph/BPRE) | **Defect, fixed** | Embedded-`<script>` sub-parsing added to both worker modules; regression test added |
| F3 | `indexed_at` freshness signal | **Observability gap, fixed** | `last_change_seen` field added, stamped by the incremental watcher path |
| C1 | Orphaned `kb/llm_escalation.py` (dead Tier-2 duplicate) | **Defect, fixed (deletion)** | Module + isolated test deleted; docs/model.yaml corrected to cite the real call site; dead `/api/metrics` key removed; guard test that imported the dead module by name fixed; replacement live coverage added (TE8) |
| HR23 | Token-accounting completeness | Covered-safe | All 4 `deepseek_extract` call sites verified paired 1:1 with `_accumulate_llm_tokens` |
| HR9/P2 | LLM-free query path | Covered-safe | Re-confirmed by direct grep of `query/*.py` + `server/mcp.py` |
| HR13 | Wiki path relativity | Covered-safe | `_rel()` and `_render_federation` both confirmed device-neutral |
| HR27/28/29 | Docgen/OKF kill-switches | Covered-safe | Read + existing test suite confirmed |
| D1 | Federation invariants #1-#12 re-derivation | **Covered-safe, exhaustively re-derived** | All 12 confirmed CONFORMS (10 read end-to-end this pass; #5/#7 spot-checked against existing exhaustive coverage) |
| D2 | Resolution-ladder doc-drift (documented 5-tier vs actual 3-tier; false 0.9 stamp; non-existent Tier-3 `_llm_file`) | **Doc-drift, fixed** | HR16/HR17 + §7a table corrected to the real 3-value ladder (1.0/0.8/0.7); Tier 3 marked DEFERRED; no code/test change needed — existing tests already encoded the true behavior |

---

## Verification

```
python scripts/check_world_model.py --all
→ CONFORMS — all checkable L1 invariants satisfied (six MANUAL principles honored)

ruff check src/rag_search src/tests
→ All checks passed!

python -m compileall -q src/rag_search
→ OK

.venv/bin/pytest src/tests/live/test_token_economy.py src/tests/live/test_http_surface.py \
    src/tests/live/test_no_code_semantic_regex.py -x --strict-markers --strict-config -ra -q
→ 32 passed

.venv/bin/pytest src/tests/live/ --ignore=src/tests/live/test_browser.py \
    -k "rtm or world_model" -x --strict-markers --strict-config -ra -q
→ 1 passed, 968 deselected

.venv/bin/pytest src/tests/live/ -m "live and not slow" \
    --ignore=src/tests/live/test_browser.py -x --strict-markers --strict-config -ra -q
→ 854 passed, 115 deselected, 195.5s — zero failures

systemctl --user restart rag-search-mcp-daemon
GET /healthz → {"ok": true, ...}
GET /api/metrics → 200; "llm_tokens" present, "llm_cache" absent (confirms C1 fix live)
```

---

## Files changed (Parts A + B + C combined)

- `src/rag_search/graph/extractor.py` — F2: `_iter_script_blocks` + sub-parse merge
- `src/rag_search/kb/bpre_ast.py` — F2: `_scan_ts_js` refactor + embedded-host re-parse
- `src/tests/live/test_embedded_script_extraction.py` — F2 regression test (new)
- `src/rag_search/core/config.py` — F3: `ProjectEntry.last_change_seen` field
- `src/rag_search/daemon/sweeps.py` — F3: stamp `last_change_seen` in `_index_project` + `_index_files`
- `src/rag_search/server/_overview.py` — F3: surface `last_change_seen`
- `src/tests/live/test_p6_daemon.py` — F3 regression assertions
- `src/rag_search/kb/llm_escalation.py` — **deleted** (C1)
- `src/tests/live/test_llm_escalation_ladder.py` — **deleted** (C1)
- `src/rag_search/server/routes_ops.py` — C1: removed `llm_cache` metric + import
- `src/tests/live/test_http_surface.py` — C1: `llm_cache` → `llm_tokens` assertion
- `src/rag_search/kb/bpre_generic.py` — C1: corrected inaccurate residue-ladder docstring
- `src/tests/live/test_no_code_semantic_regex.py` — C1: fixed dead-module reference in guard test + docstring
- `src/tests/live/test_token_economy.py` — C1: TE8 test added, relocated model-pin test, docstring updated
- `docs/world-model/model.yaml` — F2/C1 doc corrections
- `docs/architecture/federation-and-search-engine.md` — F2/C1 doc corrections
- `docs/architecture/federation-ops-and-invariants.md` — F2/F3/C1 doc corrections
- `CLAUDE.md` — F2/F3 doc corrections (extraction doctrine note, reload wording)
- `docs/audits/2026-07-09-root-federation-audit.md` — F2/F3 status updated to Fixed with back-references
- `docs/audits/2026-07-09-deep-conformance-audit.md` — this report (Part D added same day)

Part D additions (docs only, no runtime code changed):
- `docs/architecture/federation-ops-and-invariants.md` — HR16/HR17 rows + HR16 coverage-table row corrected to the real 3-value resolution ladder (1.0/0.8/0.7); Tier 3 marked DEFERRED
- `docs/architecture/federation-and-search-engine.md` — §7a table + doctrine-point-6 cross-reference corrected the same way

No commits made; all changes remain in the working tree pending explicit user approval to
commit and push.
