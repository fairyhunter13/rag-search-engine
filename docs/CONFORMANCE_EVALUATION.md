# Conformance Evaluation — World Model, Architecture, Principles & Rules

> **Date:** June 27 2026 (final verification pass; all 711 tests green)
> **Scope:** RSE (`rag-search-engine`) — `docs/world-model/` · `docs/architecture/` · `docs/info-hierarchy.md` · `vendor/okf/`
>
> **Amended 2026-07-28:** docgen was deleted (module, submodule, tests, CLI and dashboard routes).
> The rows it owned are marked *retired* rather than removed, so this scorecard still reads as a
> record of what was checked; nothing below claims a docgen guard is currently passing.
> **Method:** `check_world_model.py --all` + static source reads + `test_world_model_traceability.py`
> **Verdict:** CONFORMS (all checkable L1 invariants pass; 5 gaps found and remediated this session)

---

## 1. L1 Invariants (P0-P15) — per-principle scorecard

| P | Principle | Status | Evidence |
|---|-----------|--------|----------|
| P0 | GPU-only inference; CPU fallback fatal | Pass | `core/gpu.py` raises if providers empty; checker CONFORMS |
| P1 | No local generative LLM; KB=DeepSeek; chat=claude-haiku-4-5 | Pass | `test_no_local_llm_tokens_anywhere_in_src` passes |
| P2 | MCP query path: embed+rerank only (no generative LLM) | Pass | `server/mcp.py` graph tool uses deterministic substitutes; dead `semantic_trace`/`impact_narrative` deleted (commit 3fe4b29) |
| P3 | Federation = query-time union; no cross-repo edges | Pass | `test_inv1_no_inlining`; checker predicate scoped to graph/index/daemon |
| P4 | Event-driven indexing; no periodic sweeps | Pass | `daemon/watcher.py` + scheduler; checker CONFORMS |
| P5 | Two-stage retrieval: vector recall to cross-encoder rerank | Pass | `query/search.py`; checker predicate tightened to `rerank.*skip` |
| P6 | No heuristics — tree-sitter + LLM only in Category A | Pass | `test_no_code_semantic_regex_in_category_a` passes |
| P7 | Public-repo hygiene: no absolute device paths in artifacts | Pass | `test_no_absolute_device_path_leaks` passes |
| P8 | No mocks in tests | Pass | `test_no_mocks_or_fakes.py` passes; checker excludes guard file |
| P9 | Flat-L1 communities only (WS-B 2026-06-26) | Pass | `daemon/sweeps.py:147` enforces delete; predicate updated |
| P10 | Every line of code is a liability | Pass (MANUAL) | Dead LLM fns deleted this session |
| P11 | Push after every commit | Pass (MANUAL) | Zero unpushed maintained |
| P12 | Doc-tooling LLM-native via `claude -p`; no tree-sitter on doc path | Pass (OKF half) | `test_okf_no_tree_sitter_import_in_vendor` passes; docgen half retired 2026-07-28 |
| P13 | OKF = manual-trigger only | Pass | `test_okf_not_in_sweeps` passes; docgen half retired 2026-07-28 |
| P14 | LLM lanes; no cross-lane calls | Pass (MANUAL) | `test_inference_lanes.py` lane guards pass. **Three lanes since 2026-07-28** — doc-tooling deleted, so `claude -p` has exactly one caller; DeepSeek leaves with tier 3 |
| P15 | Kill-switch RSE_OKF=0 to no output | Pass | `test_okf_kill_switch_off` passes; `RSE_DOCGEN` retired 2026-07-28 (no reader) |

---

## 2. L3 HR Behavior Specs — traceability

`test_world_model_traceability.py::test_l3_rtm_all_tests_resolve` verifies every `model.yaml` L3_specs `test:` name resolves to a live `def test_...`. All 17 mappings resolve (7 were broken before this session; all fixed, commit d0305bb).

| HR | Spec | Test | Status |
|----|------|------|--------|
| HR1 | Watcher steady-state indexing | `test_p34_watcher_updates_vector_index` | Pass |
| HR4 | Federation query-time union | `test_inv1_no_inlining` | Pass (was broken) |
| HR6 | GPU-only + CPU fallback fatal | `test_select_gpu_providers_fatal_on_cpu_only` | Pass (was broken) |
| HR8 | Two-stage retrieval; rerank authority | `test_rerank_passages_only_in_gpu_lane` | Pass (was broken) |
| HR9 | MCP path no generative LLM | `test_mcp_handlers_have_no_llm_generation` | Pass (was broken) |
| HR10 | Chat = claude-haiku-4-5 only | `test_chat_lane_is_haiku_only` | Pass (was broken) |
| HR11 | semantic_type DeepSeek batch-20 | `test_ab3_no_type_dominates` | Pass |
| HR12 | KB enrichment = DeepSeek only | `test_kb_enrich_is_deepseek_only` | Pass |
| HR13 | Wiki paths root-relative; no abs paths | `test_community_page_structure` | Pass (was broken) |
| HR15 | No re.compile in Category A | `test_no_code_semantic_regex_in_category_a` | Pass |
| HR20 | Partition quality gate | `test_partition_quality_on_sample` | Pass (was broken) |
| HR25 | Doc-tooling LLM-native; no tree-sitter | `test_okf_no_tree_sitter_import_in_vendor` | Pass (docgen half retired 2026-07-28) |
| HR26 | OKF manual-trigger only | `test_okf_not_in_sweeps` | Pass (docgen half retired 2026-07-28) |
| HR27 | *(retired 2026-07-28 — docgen deleted, `RSE_DOCGEN` has no reader)* | — | Retired |
| HR28 | OKF v0.1 bundle structure | `test_okf_llm_generate_structure` | Pass |
| HR29 | Kill-switch RSE_OKF=0 | `test_okf_kill_switch_off` | Pass |
| HR30 | MCP surface = 5 tools only | `test_mcp_has_five_tools` | Pass |

---

## 3. Architecture register sync

| Register | Before | After |
|---|---|---|
| L1 count | "P0-P11" in README/sec-1b | "P0-P15" |
| L3 count | "HR1-HR20" in README | "HR1-HR31" |
| HR30 | Marked DELETED in ops doc | Re-defined: MCP surface integrity |
| RTM verification | "Human-verified, no automated V&V" | Machine-verified via `test_world_model_traceability.py` |

---

## 4. Gaps found and remediated (June 26 2026)

| Gap | Finding | Fix | Commit |
|-----|---------|-----|--------|
| A — checker false positives | 4 spurious AT_RISK on clean HEAD (P3 comment, P9 enforcement, P8 guard test, P5 correct sort) | Strip comments; exclude_paths; tighten predicates | 3fe4b29 |
| B — dead LLM code | `query/graph_handler.py` `semantic_trace`/`impact_narrative` = zero callers | Deleted both functions | 3fe4b29 |
| C — L3 RTM broken | 7 of 17 HR-to-test names did not resolve | Re-mapped all 7; added traceability guard | d0305bb |
| D — stale conformance report | Prior snapshot showed OKF/docgen/flat-L1 as pending/gap | Rewritten as current scorecard | this commit |
| E — register drift | README/sec-1b said P0-P11/HR1-HR20; reality P0-P15/HR1-HR31 | Updated docs + HR30 definition | 2041de2 |

---

## 5. Remaining open items

Code conformance: all items resolved.

```bash
python scripts/check_world_model.py --all   # CONFORMS
.venv/bin/pytest src/tests/live/test_world_model_traceability.py -q  # 1 passed
.venv/bin/pytest src/tests/live/ --ignore=src/tests/live/test_browser.py -x -q  # 711 passed (Jun 27 2026)
```

Additional fixes applied during this audit (beyond the 5 gaps) — **a dated record of what was done
in June 2026, deliberately left as written.** Entries naming `ose-docgen`, `vendor/docgen/` or the
`test_ih_*` tests describe files that were true then and were deleted on 2026-07-28; rewriting them
to match a later decision is how a repo loses the trail of why something changed.
- Deleted `test_graph_narrative_and_trace_real_be` (was testing deleted P2-violating LLM functions)
- Added `test_fp17_no_llm_in_graph_handler` deletion guard to `test_feature_proof.py`
- Fixed `test_known_business_rule_classified_correctly` (community topology keyword set)
- Fixed `test_process_db_created` (missing `det_db` fixture dependency)
- Fixed ose-docgen architect tool-access: `if tools is not None:` prevents default tool use causing 180s timeout
- Added `max_pages` override to `generate()`/`portal()` for test speed control
- Fixed ose-docgen `stdin=subprocess.DEVNULL` in `run_claude_portal` (belt-and-suspenders subprocess isolation)
- Fixed `test_ih_generate_llm_structure`: `capfd.disabled()` wrapper prevents pytest fd-level capture from blocking the claude subprocess
- Fixed `test_okf_llm_generate_structure`: same fd-capture fix; added `capfd.disabled()` + `stdin=subprocess.DEVNULL`
- Fixed intermittent IH + OKF slow-test failures: `accounts.py` `_fetch_usage()` parsed stale key format; fixed to nested `{"five_hour": {"utilization": N}}` format; `pick_profile()` now correctly selects lowest-utilization account; `explore_repo()` no longer caches error results; profile failover added in both `portal()` and `generate()`
- `_TIMEOUT_ARCH` 180→300s in `vendor/docgen/src/ose_docgen/portal.py` (SONNET architect times out under full-suite load; commit 30cad5c)
- `cite_gate.py` basename fallback: LLM generates `promo.go` when real path is `promo/promo.go`; rglob fallback added (commit e027b3d)
- `_TIMEOUT` 180→300s in `vendor/okf/src/okf/generate.py` OKF discover call (commit d8bdb2f)
- `_converge_ready` retry: federation root's 6 head communities can fail DeepSeek batch under full-suite load, leaving l1_enriched_pct stuck at 53.8%; fixed to retry `_enrich_project` every 30s when l1 < 100% (commit 26c2908)
- `test_self_heal_e2e.py` `_proj` fixture: changed from `tmp_path` (blocks on `/tmp/` forbidden-root guard) to home-relative tmpdir (commit 4f96ca6)
- `test_p5_server.py` chat error-start guard: narrowed from `"error" not in al[:80]` to `not al.startswith("error")` — the broader check falsely rejects valid error-handling question answers (commit d7584fb)

---

## See also

- `docs/world-model/model.yaml` — machine-readable governance model (L1-L4)
- `docs/world-model/README.md` — key-invariants summary
- `scripts/check_world_model.py` — automated L1 conformance checker
- `src/tests/live/test_world_model_traceability.py` — automated L3 RTM guard
- `docs/architecture/federation-and-search-engine.md` — sec-1a principles prose form
- `docs/architecture/federation-ops-and-invariants.md` — sec-13b HR register
