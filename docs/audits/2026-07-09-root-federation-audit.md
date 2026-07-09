# Adversarial Audit — Root + Federated Project Conformance

> **Date:** 2026-07-09
> **Scope:** Root + federated-project definition, behavior, and invariant conformance
> across the full world model (`docs/world-model/model.yaml` P0-P18 / HR1-HR40) and the
> federation architecture (`docs/architecture/federation-and-search-engine.md`,
> `docs/architecture/federation-ops-and-invariants.md`). Extends the 2026-06-27
> `../CONFORMANCE_EVALUATION.md` baseline (P0-P15/HR1-HR30) to the full P16-P18/HR31-HR40
> watcher / idle-CPU / CPU-budget hardening line, which that baseline predates.
> **Method:** Adversarial review + gap analysis — not a confirmatory checklist. For every
> invariant: read the implementing code directly, identify the uncovered edge no guard
> test exercises, and probe it live via the MCP tools and HTTP endpoints before accepting
> "Pass." Two real defects and one latent defect were found this way; two are fixed and
> verified in this session, one is root-caused but deliberately left unfixed pending a
> larger design decision (see F2).
> **Verdict:** CONFORMS — all checkable L1 invariants pass; two confirmed defects (F1,
> G-DUP) fixed and regression-tested; one confirmed structural extraction gap (F2)
> documented with root cause, not fixed this session; one observability gap (F3)
> documented, no code change needed.
> **Device neutrality:** Per P18/HR34 this report contains no real company/project names,
> home paths, hostnames, or GPU device identifiers. Example paths use placeholders
> (`<root>`, `member-a`, `member-b`, `foo`/`foo-bar`).

---

## §0 — Authoritative definition of "root + federated project"

This is the yardstick the rest of the audit measures against. Sources: architecture
Part 1 §2/§4/§5/§7/§9/§16, Part 2 §13 invariants #1-#12, `model.yaml` HR4/HR5/HR7/HR14.

- **Root** — a registered project whose tree contains symlinks pointing **outside**
  itself.
- **Member** — an external repo reached through such a symlink, stored as its **own
  independent project** (own index dir, vector DB, `graph.db`).
- **Logical repo** — the union of root + all members; realized only at **query time**,
  never materialized as a merged index.
- **Index isolation (HR5)** — one absolute path ⇒ exactly one index dir; a file's chunks
  live only in its owning project's store. **No cross-repo edges (HR4)** in any
  per-member `graph.db`; cross-service edges live only in the root-level
  `process_graph.db`.
- **No-inlining (inv#1)** — `iter_files(..., federation_mode=True)` prunes outward
  symlink dirs so a root never absorbs member files into its own index.
- **Query-time union** — `expand_federation(path) = [path] + entry.federation`;
  `search`/`ask` fan out over each member's own `VectorStore` then rerank; `graph` fans
  out via `federated_map` over each member's own `graph.db`; **all query tools are
  embed+rerank only, never generative LLM (P2/HR9)**.
- **Freshness** — members are kept current by the **event-driven watcher** (OS
  notifications, never polling: P17/HR33/HR37) feeding incremental `_index_files` +
  the code-only drift gate (`_code_source_fingerprint`, HR36/HR38) that alone may wake
  enrich/wiki/BPRE.

Any behavior diverging from the above is recorded as a gap in §6.

---

## §1 — L1 invariant scorecard (P0-P18)

| P | Principle | Status | Evidence |
|---|-----------|--------|----------|
| P0 | GPU-only inference; CPU fallback raises fatally | CONFORMS | checker CONFORMS; `core/gpu.py::select_gpu_providers` raises on empty provider list |
| P1 | No local generative LLM; KB=DeepSeek-only; chat=claude-haiku-4-5 | CONFORMS | checker CONFORMS; grep for local-model tokens in `src/rag_search/` → none |
| P2 | MCP query path — embed+rerank only | CONFORMS | checker CONFORMS; grep `query/*.py` for generative-LLM tokens → zero matches (re-confirmed this session) |
| P3 | Federation = query-time union; no cross-repo edges | CONFORMS | checker CONFORMS; `test_inv1_no_inlining` passes; **G-DUP defect found+fixed this session** (§5) |
| P4 | Event-driven indexing; cascade only on source drift | CONFORMS | checker CONFORMS; **F1 defect found+fixed this session** was in the trigger surface feeding this gate, not the gate itself (§3) |
| P5 | Two-stage retrieval: vector recall → cross-encoder rerank | CONFORMS | checker CONFORMS |
| P6 | No heuristics — tree-sitter + LLM only in Category A | CONFORMS | checker CONFORMS; debt registry empty (2026-07-01) |
| P7 | Public-repo hygiene | CONFORMS | checker CONFORMS |
| P8 | No mocks in tests | CONFORMS | checker CONFORMS; both new tests use only real fixtures |
| P9 | Flat-L1 communities only | CONFORMS | checker CONFORMS |
| P10 | Every line of code is a liability | MANUAL — honored | Both fixes are single-method diffs reusing an existing pattern, no new abstractions |
| P11 | Push after every commit | MANUAL — N/A | No commits made this session |
| P12 | Doc-tooling LLM-native; no tree-sitter on doc path | CONFORMS | checker CONFORMS |
| P13 | Docgen + OKF manual-trigger only | CONFORMS | checker CONFORMS |
| P14 | Four LLM lanes; no cross-lane calls | MANUAL — honored | `test_inference_lanes.py`; untouched |
| P15 | Kill-switches → no output | MANUAL — honored | untouched |
| P16 | Idle frugality | MANUAL — verified live | `/healthz` → `cpu_percent_core` 0.0-0.07, `cpu_quota_cores: 1.0` |
| P17 | Event-driven file-watching | CONFORMS | checker CONFORMS; **F1 was an attribution bug, not a reversion to polling** — single-thread design intact |
| P18 | Public-release & device-neutrality | CONFORMS | checker CONFORMS; **actively enforced this session** (see notice at top) |

No AT_RISK verdicts. Six `MANUAL` principles are process/discipline invariants,
independently corroborated by session evidence rather than accepted on faith.

---

## §2 — L3 behavior-spec scorecard (HR1-HR40)

Combines `model.yaml`'s checkable `L3_specs` with the descriptive HR table in
`federation-ops-and-invariants.md` §13b (HR21/HR22 deleted). Status reflects this
session's read + live verification, not just prior test-suite state.

| HR | Spec (abbreviated) | Status |
|----|---|--------|
| HR1 | Watcher is the steady-state indexing trigger | Pass |
| HR2 | KB enrichment on same watcher event, 45s debounce, no periodic timer | Pass |
| HR3 | Re-running community detection must not wipe summaries | Pass |
| HR4 | Federation = query-time union; no cross-repo edges | Pass |
| HR5 | One path = one index dir | Pass — **F1 was a live threat, now fixed** (§3) |
| HR6 | GPU-only embed+rerank; CPU fallback fatal | Pass |
| HR7 | `kb_state` lifecycle; federated = worst-of-members | Pass |
| HR8 | Two-stage retrieval; rerank_score is ranking authority | Pass |
| HR9 | MCP query actions run no generative LLM | Pass — re-confirmed by direct handler read |
| HR10 | Dashboard chat = claude-haiku-4-5 only | Pass |
| HR11 | `semantic_type` via direct DeepSeek classification | Pass |
| HR12 | KB build = cloud DeepSeek only; crashes if key absent | Pass |
| HR13 | Wiki is a per-member artifact; root-relative citations | Pass |
| HR14 | BPRE process graph is root-level only; 3-tier extraction | Pass |
| HR15 | Engine-wide no-heuristic doctrine | Pass |
| HR16 | Universal 5-tier resolution ladder | Pass |
| HR17 | Deterministic Tier-1.5 value-flow + FQN join | Pass |
| HR18 | Tier-1.75 GPU rerank + Tier-2 SEA-style LLM | Pass |
| HR19 | Deterministic gating (deferred cross-service recall) | Pass |
| HR20 | Composite partition-quality gate | Pass |
| ~~HR21~~ | Federation L3 roll-up | **DELETED** (WS-B 2026-06-26) |
| ~~HR22~~ | Deterministic structural spine | **DELETED** (WS-B 2026-06-26) |
| HR23 | DIKW token economy (flat-L1) | Pass |
| HR24 | Flat-L1 community detection + flat tree-walk | Pass |
| HR25 | Self-healing graph pipeline | Pass — **nuance resolved as a doc fix, not a code fix** (§5) |
| HR26 | GPU execution-provider autodetect | Pass |
| HR27 | Docgen root-only + member cleanup | Pass |
| HR28 | Docgen output re-indexed under `scope=docs` | Pass |
| HR29 | `.opencode-index.yaml` honored by every enumerator | Pass |
| HR30 | MCP surface = exactly 5 tools | Pass |
| HR31 | Four-lane LLM separation, no cross-lane calls | Pass |
| HR32 | Idle-efficiency drift gate + idle-unload + bulkification | Pass |
| HR33 | Notification-first watcher contract | Pass |
| HR34 | Public-release & device-neutrality guard | Pass |
| HR35 | Gitignore/hidden-dir-aware discovery, OSE-config precedence | Pass |
| HR36 | BPRE code-only, discovery-unified reuse signature | Pass |
| HR37 | Watcher ignore-aware + storm-proof + root-boundary-safe | Pass — **F1 found in the root-boundary clause, fixed; WT5 is the new guard** (§3) |
| HR38 | Code-only enrich-cascade drift gate, unified with HR36 | Pass |
| HR39 | Bounded out-of-process tree-sitter parsing | Pass |
| HR40 | Two-tier CPU budget, kernel-enforced | Pass — re-confirmed live (§4) |

`test_l3_rtm_all_tests_resolve` passes — every `model.yaml` `L3_specs.test:` name
resolves to a live `def test_…`.

---

## §3 — Watcher deep-dive: F1 (root-attribution prefix collision)

**Defect (HIGH, confirmed live and fixed this session).**
`daemon/watcher.py::_owning_root` resolved a change's owning root with a raw
`path.startswith(proj)` string-prefix test — no path-boundary check. `self._paths` is an
unordered set. For any two registered roots where one path is a string-prefix extension
of another's name (e.g. `foo` vs `foo-bar`, `foo` vs `foo_sibling`), a change under the
longer root's tree could be misattributed to the shorter root, depending on set iteration
order.

**Confirmed failure chain (traced through the real code):**
1. `_filter(change, path)` calls `_owning_root(path)`; the buggy prefix match can return
   the shorter root even though `path` is really under the longer, sibling root.
2. `on_change(<shorter root>, [<longer-root file>])` fires; `_index_files` inserts the
   longer root's chunks into the shorter root's own `VectorStore` — **HR5 index isolation
   violated**, polluting `search` results for the shorter root.
3. The same event is never delivered to its true owner, so that member's own store goes
   stale, silently, with no error surfaced.
4. Nondeterministic across daemon restarts (depends on Python set hash order).

**Coverage gap:** none of `test_idle_stability.py`'s existing WT1-WT4 use prefix-colliding
root names (`root`, `member-repo`), so this was a genuinely uncovered edge, not a
regression a guard would have caught.

**Fix applied** (`daemon/watcher.py::_owning_root`): boundary-aware, longest-match lookup
using `Path.relative_to()` instead of raw string prefix — mirrors the existing pattern
already used by `server/mcp.py::_resolve_roots` for the identical class of problem.
Applies to both call sites (`_filter` and the `_loop` batch-grouping step).

**Regression test added:** `test_wt5_prefix_sibling_roots_no_misattribution`
(`test_idle_stability.py`) — two roots `foo`/`foo-bar`, edits `foo-bar`'s file, asserts the
event is attributed to `foo-bar` and never to `foo`.

**Doc corrected:** `federation-ops-and-invariants.md` HR37 row and §14 test-coverage map
updated to describe the boundary-aware fix (previously documented the buggy
`path.startswith(root)` behavior as if it were correct).

**Verified:** `test_idle_stability.py -k "wt1 or wt2 or wt3 or wt4 or wt5"` → 5 passed.
Full fast-smoke suite green after the fix (§8).

---

## §4 — MCP read-only / LLM-free evidence

- All 5 `@mcp.tool()` handlers (`search`, `ask`, `graph`, `overview`, `index`) read
  directly this session; `query/ask.py::compose_answer` assembles retrieved context only —
  generation is delegated to the calling agent, never performed server-side.
- Grep across `query/*.py` for generative-LLM tokens (DeepSeek/Haiku/API-key/Anthropic
  references) → zero matches (P2/HR9 re-confirmed).
- **HR40 CPU budget, live:** `/healthz` → `cpu_percent_core` in the 0.0-0.07 range (well
  under the 1% idle gate), `cpu_quota_cores: 1.0`; `overview(what='metrics')` → `cpu`
  block showed `nr_throttled`/`throttled_usec` climbing under a real prior load window —
  the doctrine's own canonical proof the cgroup-v2 quota is physically enforced, not just
  usage happening to stay low. No gap.
- Daemon was restarted once during this session's Phase C verification (see §8 note on
  the reload side-effect); post-restart health was re-confirmed (`ok: true`).

---

## §5 — Root + federation health

### G-DUP — duplicate federation members via multiple symlinks to one target (fixed)

**Defect (confirmed, latent — not observed live prior to the fix, found by code read +
targeted test).** `daemon/federation.py::discover_members` appends every symlink target
that looks like a repo, with no dedup guard. If two symlinks in a root resolve to the
same external target, the member path is appended twice; `index_members` persists the
duplicate into `root_entry.federation`, and `expand_federation`'s query-time union —
the function `search`/`ask`/`graph` fan-out directly relies on — would then double-process
that member's store per query.

**Fix applied:** dedup at `expand_federation`, the query-time union boundary, via
`dict.fromkeys([path, *members])` (order-preserving) — preferred over fixing at the
discovery-list level since `discover_members`'s list is also consumed elsewhere (the
disable-on-removal diff) where duplicates are harmless.

**Regression test added:** `test_gdup_duplicate_symlink_members_deduped`
(`test_federation_architecture.py`) — two symlinks to one member target, asserts
`expand_federation(root).count(member) == 1`.

**Verified:** `test_federation_architecture.py` → all 6 tests pass (the new test plus
the 5 pre-existing invariant tests, confirming no regression to inv#1/#2/#3/#6/#8).

### F2 — Vue SFC extraction blind spot (confirmed root cause; not fixed this session)

**Original suspicion (from the approved plan):** two federation members with substantial
Vue/JS source showed `symbol_hollow: true, edges: 0` in `overview(what='status')` —
looked like a per-repo under-extraction that a re-index might fix.

**Root cause, confirmed empirically this session (not a data-staleness issue):** a direct
tree-sitter parse probe against a sample `.vue` single-file component showed the `vue`
grammar (via `tree_sitter_language_pack`) parses the entire `<script>` block as a single
opaque `raw_text` leaf node — it never descends into the embedded JS/TS as call, function,
or class nodes. The probe found **zero** call-like nodes anywhere in the parse tree of a
script-bearing sample SFC.

**Consequence:** `graph/extractor.py`'s `extract_calls`, `extract_calls_with_lines`,
`extract_call_sites`, and `extract_symbols` are **structurally blind** to any logic inside
a Vue SFC `<script>` block — this is a systemic gap for the entire file format, not a
per-repo config or sparsity issue. Re-indexing such a member cannot change the outcome;
the extractor would reproduce the identical hollow result every time. The original plan's
proposed remediation ("explicit `index(enabled=True)` re-index of the affected paths") is
**withdrawn as incorrect** — it was never executed, correctly, since it would have been a
no-op.

Grep across `kb/bpre_generic.py`, `kb/bpre_ast.py`, and other `kb/bpre*.py` files for
`vue|svelte|astro|script_element|raw_text` returned zero matches, suggesting (but not
independently confirming) that BPRE's structural classifier shares the same blind spot.

**Cascade-gate integrity unaffected:** `is_code_language("vue")` returns `True`, so the
HR36/HR38 code-only drift gate still correctly triggers on `.vue` file edits — the gap is
purely in extraction *depth*, not in cascade *triggering*.

**Recommended remediation (not built, requires design work + explicit future
authorization):** embedded-language sub-parsing support in `graph/extractor.py` — parsing
the `<script>` block's content with the JS/TS grammar once the outer `vue` grammar
identifies its byte range, mapped back to the SFC's line numbers. This is a nontrivial
feature addition (new sub-parse pass, position remapping across the boundary), not a
small bug fix, so it is recorded as a finding rather than implemented this session, per
"every line of code is a liability" / "smallest sufficient diff" / "extraction changes
require evidence + explicit scope."

### F3 — `indexed_at` freshness signal gap (observability only, documented)

`indexed_at` is stamped only by a full `_index_project` run, never by the incremental
`_index_files`/`_enrich_project` path the watcher actually drives in steady state. An
actively-watched, fully current member can therefore read as "stale" by `indexed_at`
alone, even though its content is current. This is an observability gap, not a
correctness violation — no query result is wrong, only the freshness *signal* is
misleading. No code change proposed; a future `last_change_seen` registry field (stamped
by the watcher's incremental path too) would close it if the user wants that improvement.

### Reconcile self-heal guard vs. `symbol_hollow` (resolved as a doc fix, not a behavior change)

`daemon/sweeps.py::reconcile_projects` only forces a full re-index when
`community_count() == 0` (no graph built yet), and only re-derives the graph when
`_graph_stale()` (source-fingerprint drift) — neither directly checks
`edge_count() == 0`/`symbol_hollow`. This initially looked like a missing trigger
condition, since `test_graph_health.py`'s module docstring described GH4 as checking
`edge_count()==0`, which did not match the code.

On inspection this is **correct-to-keep runtime behavior**: since F2's Vue-SFC hollowness
is structural, not stale data, automatically re-triggering a full re-index whenever
`symbol_hollow` is true would produce a futile infinite retry loop on every reconcile pass
for Vue-heavy members — violating the same P16/P17 idle-CPU efficiency invariants this
audit checks elsewhere. The fix applied was to correct the stale docstring to describe
the actual (correct) behavior, not to change the guard condition. Verified:
`test_graph_health.py` → all 5 tests still pass (docstring-only change).

---

## §6 — Gap register

| # | Area | Verdict | Action |
|---|---|---|---|
| F1 | Watcher root attribution (HR5/HR37) | **Defect, confirmed live, fixed** | `_owning_root` made boundary-aware; WT5 added; HR37 doc corrected |
| G-DUP | Federation member dedup (inv#1, HR4/HR5) | **Defect, confirmed latent, fixed** | Dedup at `expand_federation`; regression test added |
| F2 | Vue SFC extraction depth (graph/BPRE) | **Defect, root cause confirmed, not fixed** | Documented; sub-parsing flagged as future work needing explicit scope |
| F3 | `indexed_at` freshness signal | **Observability gap, not a violation** | Documented only |
| Reconcile guard vs. `symbol_hollow` | HR25 self-heal | **Doc/runtime mismatch, resolved as doc fix** | Docstring corrected; behavior kept (avoids futile retry loop) |
| HR4 no-cross-repo-edges | BPRE write sites | Covered-safe | Writes only to `root_process_db`; existing guards sufficient |
| HR9/P2 LLM-free query tools | All 5 MCP handlers | Covered-safe | Re-confirmed by direct read + grep |
| HR35/HR37 shared ignore resolver | Watcher + drift gate | Covered-safe, with F1 caveat | F1 corrupted the *root argument* fed to it, not the resolver — fixed |
| HR36/HR38 code-only cascade gate | Vue `.vue` files | Covered-safe | Cascade triggering intact even though extraction depth (F2) is not |
| HR39 bounded parse | Worker pool | Covered-safe | Not touched this session |
| HR40 CPU budget | Kernel enforcement | Covered-safe, re-verified live | Throttling counters climbing under real load confirmed |
| P18/HR34 public hygiene | This report itself | Covered-safe | Written device-neutral; real names in transient tool output excluded by design |

---

## §7 — Findings & actions summary

| Finding | Severity | Status | Verification |
|---|---|---|---|
| **F1** — watcher prefix-collision root misattribution | High | **Fixed** | WT5 passes; fast-smoke suite green; HR37 doc corrected |
| **G-DUP** — duplicate-symlink member double-counting | Medium (latent) | **Fixed** | New regression test passes; full federation-architecture suite green |
| **F2** — Vue SFC `<script>` block opaque to extraction | Medium (systemic) | **Root-caused, not fixed** | Confirmed via direct parse probe; recommendation recorded, scope deferred |
| **F3** — `indexed_at` not a reliable freshness signal | Low (observability) | **Documented** | No action taken |
| Reconcile-guard vs. `symbol_hollow` docstring mismatch | Low (doc accuracy) | **Fixed (doc only)** | Docstring corrected; runtime behavior intentionally unchanged |

---

## §8 — Verification log

Commands run this session, in order, with results:

```
python scripts/check_world_model.py --all
→ CONFORMS — all checkable L1 invariants satisfied (P0-P18; six MANUAL principles honored)

ruff check src/rag_search src/tests   (on the files touched this session)
→ clean, no findings

python -m compileall -q src/rag_search
→ OK

.venv/bin/pytest src/tests/live/ -m "live and not slow" \
    --ignore=src/tests/live/test_browser.py -x --strict-markers --strict-config -ra -q
  (run 1, before Phase C fixes)  → 845 passed, 120 deselected
  (run 2, after Phase C fixes)   → 847 passed, 120 deselected, 237.26s
  (+2 passed exactly matches the two new regression tests added — zero regressions)

.venv/bin/pytest src/tests/live/test_idle_stability.py -k "wt1 or wt2 or wt3 or wt4 or wt5"
→ 5 passed

.venv/bin/pytest src/tests/live/test_federation_architecture.py -v
→ 6 passed (test_gdup_..., test_inv1, test_inv2, test_inv3, test_inv6, test_inv8)

.venv/bin/pytest src/tests/live/test_graph_health.py -v
→ 5 passed (docstring-only change; behavior untouched)

Live health check post-fix (daemon reloaded to pick up code changes):
GET /healthz → {"ok": true, "cpu_percent_core": ~0.0, "cpu_quota_cores": 1.0, ...}
```

**Daemon-reload note:** `POST /api/reload` cleanly shuts the daemon process down; the
systemd unit's `Restart=on-failure` policy does not restart on a clean exit, so the
daemon needed a follow-up `systemctl --user restart rag-search-mcp-daemon` to come back
up during this session. This is an operational note for future sessions, not a
world-model conformance finding — `CLAUDE.md` documents `/api/reload` as restarting "via
systemd in ~1s," which held for the `restart` path but not the bare reload-endpoint path
under this unit's current restart policy.

---

## Files changed this session

- `src/rag_search/daemon/watcher.py` — F1 fix (`_owning_root` boundary-aware longest-match)
- `src/rag_search/daemon/federation.py` — G-DUP fix (`expand_federation` dedup)
- `src/tests/live/test_idle_stability.py` — WT5 regression test added
- `src/tests/live/test_federation_architecture.py` — G-DUP regression test added
- `src/tests/live/test_graph_health.py` — GH4 docstring corrected
- `docs/architecture/federation-ops-and-invariants.md` — HR37 row + §14 map corrected
- `docs/audits/2026-07-09-root-federation-audit.md` — this report (new)

No commits made; all changes remain in the working tree pending explicit request.
