# `kb_state: indexing` While Search Is Live — Diagnosis: Expected, Not a Bug

> **Date:** 2026-07-17
> **Scope:** `overview(status)` `kb_state` reporting during and after a re-index — specifically
> the `indexing` and `enriching` rungs of the ladder in `src/rag_search/server/_overview.py`,
> and the `indexed_at` lifecycle in `core/registry.py` + `daemon/sweeps.py`.
> **Trigger:** live observation — after a manual `index()` re-index of a project, `overview(status)`
> kept reporting `kb_state: "indexing"` long after the searchable layer was already live and
> answering queries correctly, and later sat at `kb_state: "enriching"` (~60%) rather than climbing
> to `ready`. Question raised: is the status endpoint lagging/buggy, or is this by design?
> **Method:** code-path trace of the status ladder, the registry upsert, the index pipeline, and the
> enrichment triggers; corroborated against a live `overview(status)` snapshot. No code changed.
> **Verdict:** EXPECTED — both symptoms are the intended, internally-consistent behavior of the
> current design (unchanged through HEAD `40279d6`, 2026-07-15). One *non-blocking semantic
> enhancement* is recorded in §4; it is **not** a defect and is **not** implemented here.
> **Follow-up (2026-07-17, later):** driving a project to `ready` surfaced a *real* robustness gap
> in the enrichment path — DeepSeek narration is intermittently lossy and the loss was silently
> swallowed, so each pass plateaus a few communities short of `ready` (see §6). That **is** a bug
> and **has** been fixed (bounded in-call retry + failure logging in `graph/enrich.py`).
> **Device neutrality:** paths use placeholders (`<root>`); no secrets included.

---

## §1 — Symptom A: `indexing` persists while the old index still serves queries

`kb_state` is **computed live from the DB on every `status` call** (`server/_overview.py:139–210`)
— it is not a persisted flag that can get "stuck". The `indexing` rung is derived (line 157–159) as:

```
_ks = ("indexing" if (ep is None or ep.indexed_at is None or not project_vector_db(p).exists())
       else "ready" if _pct >= 95 else "enriching" if l1p > 0 else "searchable")
```

Two deliberate design facts make `indexing` appear for the whole duration of a re-index, even
though search keeps working:

1. **`index()` nulls `indexed_at` at re-registration via a full-replace upsert.**
   `server/mcp.py:236` calls `upsert_project(ProjectEntry(path=<root>, enabled=True))`. `indexed_at`
   defaults to `None` (`core/config.py:69`), and `upsert_project` does a **full replace** —
   `data[entry.path] = asdict(entry)`, no merge (`core/registry.py:147–149`). So the moment a
   re-index is requested, the stored `indexed_at` is wiped and `status` reads `indexing`.

2. **`indexed_at` is re-stamped only at the very END of the full pipeline** — after *both* the
   vector build and the graph/community build (`daemon/sweeps.py:459–468`). This is intentional:
   `_needs_index` keys on `indexed_at` precisely so a crashed / aborted / re-registered index is
   treated as needing a full rebuild rather than left half-done (`sweeps.py:245–266`).

Between those two points the whole rebuild runs with `indexed_at is None` → `status` stays
`indexing`, **while a working searchable layer answers queries the entire time**. `index_project`
embeds every file first and only calls `store.clear()` + reinsert at the very end
(`index/indexer.py:71`), so the **old** `vectors.db` / `graph.db` keep serving until the new
generation is atomically swapped in; then the graph/community phase (`_extract_graph` +
`detect_communities` + partition quality) runs — still with `indexed_at` unset — which is the
"graph/community layer finishing in the background" window that was observed.

**Conclusion:** the status was *truthful* — "this index *generation* is not certified complete" —
which is exactly true during a rebuild. Search availability was never compromised; trusting a
functional search probe over the status field was correct. Both signals were right simultaneously
because they measure different things (search availability vs. generation-complete).

## §2 — Symptom B: parks at `enriching` (~60%) instead of reaching `ready`

The `enriching`/`ready` rungs track **LLM enrichment coverage**, i.e. `l1p` = % of level-1
communities with a non-empty `summary`. This is a background-quality signal, **not** a search gate.

- Enrichment (`_enrich_project`, `sweeps.py:505`) narrates only **head** communities
  (`member_count ≥ 8` OR ≥ 2 cross-community edges) via the DeepSeek LLM, batched + prefix-cached;
  **tail** communities get zero-token deterministic structural summaries
  (`graph/community.py:117`). Both head and tail end up with a non-empty `summary`, so **`ready`
  (≥ 95%) is architecturally reachable** — the tail does not block the ceiling.
- Each pass is capped at **`RSE_ENRICH_BUDGET_TOKENS` = 50,000** completion tokens
  (`sweeps.py:12`; `graph/enrich.py:271–273`). One pass narrates until the budget is hit, then
  stops.
- **Resumable & non-wasteful:** `compute_significance` selects only
  `WHERE (summary IS NULL OR summary = '') AND level = 1` (`graph/enrich.py:113–116`), so each
  subsequent pass advances the *unenriched* frontier. No re-narration, no wasted budget.
- **CORRECTION (see §6):** the original draft claimed enrichment "climbs monotonically toward
  `ready`." That was wrong. Empirically, a *single* pass does **not** reach `ready` — DeepSeek
  narration is intermittently lossy (a transient malformed/unparseable response silently dropped a
  whole 20-community chunk), so a project plateaus a few short every pass and only a *sequence* of
  passes converges. That silent drop was a genuine bug; it is fixed in §6. The token budget is a
  secondary factor — for small projects the miss is the flakiness, not the 50k cap.

## §3 — What re-triggers enrichment (so a parked KB reaches `ready`)

`reconcile_projects()` / `_enrich_project` fire on:

1. daemon **startup** — once, after a ~30 s grace (`server.py:154–159`);
2. every **`index()`** MCP call (`server/mcp.py:239–240`);
3. **source-file changes** via the watcher (`on_change`, `sweeps.py:657–692`; debounced, gated by a
   code-only fingerprint per HR38 so non-code churn does not wake the cascade);
4. **periodic resync — only if `RSE_RECONCILE_RESYNC_S > 0`, which is OFF by default**
   (`server.py:14,160–168`).

**Design tradeoff (not a bug):** with periodic resync OFF and an *idle* repo (no source edits, no
re-index, no restart), enrichment parks below `ready` at whatever the last budgeted pass reached.
It is not looping or broken and completes on the next trigger, but it does not self-complete while
idle — intentional, to cap daemon CPU/GPU and DeepSeek token spend.

## §4 — Optional enhancement (open idea, NOT implemented)

The only arguable sharp edge is *semantic*, not correctness: there is no distinct rung for
"a working index is serving while a newer generation rebuilds," so `indexing` conservatively covers
that case and a human can misread `kb_state=indexing` as "search is down."

If that misread ever has real operational cost, a possible refinement:

- Report `searchable` whenever a populated `vectors.db` exists (regardless of `indexed_at`), and
- Surface index-generation freshness as a separate field — `validate_index` already computes
  `indexed_at_fresh` (`index/validate.py:21–22`); `status` could expose it alongside `kb_state`.

This is an enhancement, not a defect repair. Per the "prefer no change" doctrine it is **not**
recommended unless the misread proves costly, and it is deliberately left unimplemented here.

## §5 — Verification (read-only)

- Live `overview(status)` snapshot: `kb_state=enriching`, `enriched_pct=59.7`,
  `indexed_at=2026-07-17T00:27`, `symbol_hollow=false`, `hierarchy_quality.degenerate=false` — a
  healthy, fully-searchable, mid-enrichment KB.
- Traced: the ladder derivation (`_overview.py:147–159`), the `indexed_at` reset
  (`registry.py:147–149` + `mcp.py:236`) and end-of-pipeline stamp (`sweeps.py:459–468`), the
  clear-after-embed swap (`indexer.py:71`), the 50k budget (`enrich.py:271–273`), the resumable
  frontier query (`enrich.py:113–116`), and all four enrichment triggers (§3).
- History: `git log -L 147,159:src/rag_search/server/_overview.py` — ladder semantics last modified
  2026-06-30 (`2554973`); unchanged through HEAD `40279d6` (2026-07-15). Not a regression.

## §6 — Real bug found while driving a project to `ready`, and its fix

Attempting to push a project from `enriching` to `ready` exposed that enrichment silently loses
communities each pass. Root cause in `graph/enrich.py`:

- `_enrich_one_batch` sends a chunk of ≤20 communities in one `deepseek_extract` call, parses the
  JSON array, and upserts each. On a **transient** malformed/unparseable/`</think>`-truncated
  response it returned `[], usage` — the **entire chunk dropped**, with no log. Same for a
  swallowed per-item `except: continue`.
- `enrich_communities_batch` iterated the chunks **once** and returned. So any chunk that flaked
  on its single attempt was simply lost for that pass.

**Observed:** the *same* chunk narrates 0 on one attempt and all of it on the next — purely
intermittent. Example convergence trace for a 27-head-community project (`RSE_ENRICH_BUDGET_TOKENS`
never approached):

```
pass1: head=27 narrated=0      pass2: head=27 narrated=20
pass3: head=7  narrated=0      pass4: head=7  narrated=7  -> 67/67 READY
```

Every parked project matched this shape (e.g. cx-be `head=51 enriched=40`, 11 silently dropped).

**Fix (this change):**
- `enrich_communities_batch` now re-attempts the still-unnarrated subset, bounded by **three
  independent caps** so it can never spin: `max_rounds` (iterations), `budget` (tokens), and
  `max_seconds=120` (**wall-clock** — the real latency guard, so a slow/throttled DeepSeek cannot
  make one call run for minutes). Whatever is unnarrated when a cap trips is left for the next
  trigger (the pipeline is resumable). Common case (no drops) is a single round — **zero added
  overhead**, verified on `infra` (5→0, `calls=1`).
- `_enrich_one_batch` now **logs** each silent-drop path (deepseek call failure, no JSON array,
  JSON parse failure) at `WARNING` with the community count — observability instead of a black hole.

**Note on the wall-clock cap — why it matters:** an early draft of the retry used only round/token
caps. Under a slow/throttled DeepSeek that let one call stretch for many minutes. The live
full-pipeline test (`test_p22_kb_e2e.py`) is `@pytest.mark.slow` and does **not** complete within a
short timeout **even on unmodified HEAD** (both HEAD and this change hit a 600–900s `timeout` on a
loaded dev box; it is CI-only, 60-min budget), so it is not a usable local gate — the wall-clock cap
was added defensively to bound worst-case latency rather than in response to a reproduced local
regression. Convergence + no-overhead are verified by the targeted tests above, not the slow e2e.

**Verification of the fix:**
- Offline suite green (15 passed); live enrichment tests green (`test_lazy_wisdom.py`, 8 passed,
  real DeepSeek).
- Live single-call convergence: `infra` `missing=5 → 0` (100%) in **one** `enrich_communities_batch`
  call; the earlier 4-pass hand-loop drove a 27-head project to `67/67`, confirmed by the daemon's
  own `overview(status)` reporting `kb_state=ready`, `enriched_pct=100.0`.

## §7 — Follow-up (2026-07-17): closing the three caveats the fix left open

The fix above shipped green, but three loose ends remained. Each is now addressed.

**7a — The convergence path now has a push-level gate (was: nightly-only).**
The full e2e proof (`test_kb_state_ready_all_projects`) is `@slow`/nightly and, as §6 notes, is not a
usable local gate. A regression could therefore ship and sit unnoticed until the next nightly. Added
`test_converge_smoke_standalone` (NOT `@slow`) in `test_p22_kb_e2e.py`: it converges the **smallest**
sample project (standalone ledger, fewest communities → one DeepSeek round) to `ready`/`l1==100`
within a bounded 180s, so it runs in the every-push `live-fast` job (`-m "live and not slow"`). This
is the exact behavior §6 fixed — a dropped narration chunk leaving a project short of `ready` — so the
smoke would have caught the original bug. Precedent for real-DeepSeek in the fast gate already exists
(`test_lazy_wisdom`). The full 3-project sweep stays `@slow`/nightly. Process rule added to
`CLAUDE.md`: changes touching `graph/enrich.py` or the converge loop **MUST** be pushed with
`[slow-ci]` so the full sweep runs on that same push (the `ci.yml:146` trigger already exists).

**7b — Registry pollution now self-heals (was: manual prune after every killed run).**
Live fixtures register temp projects under `~/.local/share/rse-test-dirs` and deregister in teardown.
A *killed* run (CI `timeout-minutes` kill, SIGKILL, crash, Ctrl-C) never runs teardown, leaking
registrations that `registry._migrate()` cannot drop (it only prunes paths **gone from disk**; the
leaked dir still exists). The next run's `test_no_junk_paths_in_live_registry` (IS2) then fails — the
cause of the red scheduled run that previously needed a manual prune of 8 entries. Added a
session-scoped `autouse` fixture `_purge_leaked_test_state` (`conftest.py`) that, at session start,
removes every `rse-test-dirs` registry entry + stale child dir before the workspace builds. Idempotent
and runs regardless of how the prior session died. (2 orphaned dirs from Jul-14 were also swept.)

**7c — Fleet convergence verified (read-only, was: "in progress in the background").**
Confirmed via the daemon's own `overview(status)` that the deployed fix converges real
previously-parked projects:
- `payment-gateway` (105 communities): `kb_state=ready`, `enriched_pct=100.0` — **converged from
  parked**, the end-to-end proof on the live fleet.
- `rag-search-engine` (this repo, 58 communities): `ready`, `100.0`.
- `ts_fleet` (19 communities): `enriching`, `52.6%` — mid-convergence, awaiting its next daemon
  reconcile/enrich pass (daemon idle at check time); consistent with §6 resumability, not a
  regression. Will converge when enrich next runs, as `payment-gateway` did.
