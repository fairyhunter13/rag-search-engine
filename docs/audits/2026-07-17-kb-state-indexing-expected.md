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
  stops — so a fresh re-index parks partway.
- **Resumable & non-wasteful:** `compute_significance` selects only
  `WHERE (summary IS NULL OR summary = '') AND level = 1` (`graph/enrich.py:113–116`), so each
  subsequent pass advances the *unenriched* frontier by another 50k. No re-narration, no wasted
  budget, no architectural stall — enrichment climbs monotonically toward `ready`.

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
