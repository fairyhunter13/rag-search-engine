# rag-search-engine — Claude Code Instructions

Instructions only. Rationale and incident history live in `docs/decisions/`; the write-path spec
lives in `docs/architecture/`. Don't restate either here.

## Mandatory tool-use protocol (no exceptions)

1. **`overview(what='projects')` first** — confirm the project is indexed.
2. **`search` immediately after** — natural-language query describing what you need.
3. Do NOT delegate codebase questions to sub-agents; they don't inherit these rules.
4. Fall back to Bash/Grep/Glob/Read only if `search` returns nothing useful.

Applies to EVERY codebase question, however simple. Training-data answers about this repo are
stale and untrustworthy.

## Running tests

Fast smoke, full live suite, and browser are the `run-tests` and `run-all-tests` skills —
they own the exact invocations, so there is one copy of the flags to keep correct. Add `-x`
when you want the manual inner loop to stop at the first red; the skills deliberately omit
it because they are required to report every failure.

```bash
# Inner loop only — the tests a working-tree change can reach, via graph(relation="impact"):
#   .venv/bin/pytest $(.venv/bin/python scripts/affected_tests.py)
# Never a CI gate: a missed edge would silently shrink it. CI always runs the whole suite.

ruff check src/rag_search src/tests scripts && python -m compileall -q src/rag_search
```

**Markers**: `live` (daemon at :8765 + GPU) · `costly` (spends real Claude session quota via
`claude -p`; 19 tests) · `exclusive` (must not run beside other load — either it measures a quiet
daemon, CB3/CB4/CB6, or it destroys quiet: the reload restart; 4 tests). `slow` is retired, and
`conftest.py` no longer re-registers it, so `--strict-markers` refuses a re-add.
**Skipping is forbidden** — `test_no_skip_markers_in_live_suite`; a
marker chooses which suite a test belongs to, it never lets one pass without running.

**Three hazards:**

- **One live suite at a time.** `pytest_configure` aborts naming the other run's pid and profile.
  Don't wrap runs in `flock` — see `docs/decisions/2026-07-30-one-live-suite-at-a-time.md`.
- **Foreground only.** The suite loads a real embedder in-process (~1 GB RSS + a full core,
  intrinsic to the no-mock invariant); as an unattended background task it stacks on
  Chrome/Java/Node and pushes the machine into swap.
- **Mass ORT failures ⇒ check free VRAM before you read the diff.** `CUBLAS failure 3` /
  `BFCArena` errors name neither the GPU nor the daemon. `POST /api/gpu/release`, then re-run.
  See `docs/decisions/2026-07-29-vram-starvation.md`.

## Daemon reload

`POST /api/reload` exits non-zero, so systemd's `Restart=on-failure` brings it back in ~1 s.
`?restart=false` exits 0 and intentionally stays down (used by `daemon stop`) — needs a manual
`systemctl --user restart rag-search-mcp-daemon`. There is no `daemon reload` subcommand.

Both paths refuse with **409** while a sweeps pause lease is held (a live suite or
`scripts/purge_unindexable.py` owns the daemon). The reply carries `lease_remaining_s`;
`?force=true` overrides; the lease self-expires after 30 min. Guarded by RL1/RL2 in
`test_daemon.py` — see `docs/decisions/2026-07-29-reload-under-a-sweeps-lease.md`.

**`systemctl --user restart` bypasses that 409, and the damage is silent and permanent for the run
(measured 2026-07-31).** `_PAUSED`/`_PAUSE_DEADLINE` are module globals, so a restart clears the lease
with no refusal and no log naming the suite it just unpaused. The suite *does* renew — the autouse
`_drain_graph_lane` hook calls `_sweeps.renew_pause_lease()` after **every** test — but that renewal
is deliberately one-way: it re-arms only while `sweeps_pause_lease_s > 0.0`, because a lease at 0.0
means "the mechanism decided to resume" and re-arming would race that decision. Right in isolation,
and it means **an externally cleared lease never comes back**. Observed: a live suite spent its whole
remaining run at `sweeps_pause_lease_s: 0.0` after an unrelated restart, sweeps competing for the same
GPU throughout; `previously_paused: false` on the manual re-pause is what proved the pause was gone
rather than merely unreported. **Read `/healthz` before restarting, and re-`POST /api/sweeps/pause` if
you cleared a lease** — nothing else will tell you, least of all the suite you disrupted.

**After a stamp move: release the lease, then restart.** The reconcile pass is startup-once and then
parks (periodic resync is off by default; steady state is watcher-driven, and moving `EXTRACTOR_REV`
touches no file so fires no watcher event). A release is a permission, not a schedule — if that one
pass already ran inside your lease window it logged `reconcile: abandoned before start` and parked,
and the fleet stays stale behind a perfectly healthy `/healthz`. Verify by watching the stale count
fall, never by the absence of errors. See
`docs/decisions/2026-07-31-releasing-a-lease-schedules-nothing.md`.

**The stale count is read at `overview(what="metrics")` → `pipeline_version`**, whose `stale_stores`
counts every store — including unstamped ones — whose `graph.db` `meta.algo_version` differs from
`sweeps._pipeline_algo_version()`. It is **fleet-wide; `project_path` does not change it**, and it
answers even on an unscoped call where `extraction` refuses for want of a project. Built by
`_fleet_pipeline_block`/`_pipeline_block` (`server/_overview.py`), guarded by AU5 (arithmetic) and
AU6 (the surface can still reach it) in `test_extraction_ladder.py`. Recorded here because an
instruction to watch a number is useless without its source: one session lost it and reported the
convergence as unverifiable.

**Take the lease before the first edit, not before the re-derive.** `_code_fingerprint` re-reads the
fingerprinted modules off disk on every call, so a running daemon restamps the moment
`graph/extractor.py` is *saved* — no commit needed, and a docstring counts. Measured 2026-07-31: one
store re-derived with the daemon's old in-memory code and the new fingerprint. See
`docs/decisions/2026-07-31-an-edge-is-a-resolved-call.md`.

## Invariants

Every rule is enforced by a test, and the test is the rule's definition. The write-path spec is
`docs/architecture/federation-ops-and-invariants.md`, whose §14 map names the guard for each and
is itself gated by `test_coverage_map_names_resolve`. Why a rule exists, and what it cost to
learn: `docs/decisions/`. Every short id cited here resolves to its defining guard in
`docs/reference/invariant-ids.md` (generated; do not hand-edit).

Four stated inline because violating them fails silently rather than loudly:

- **GPU-only** — all inference on CUDA. CPU fallback must raise fatally, never degrade quietly.
  Exactly two residents: the embedder and the cross-encoder reranker. (`core/gpu.py`,
  `test_gpu_autodetect.py`)
- **No regex, keyword lists, or mapping tables for code semantics** — tree-sitter structure only,
  with no LLM escape hatch to fall through to. (`test_no_code_semantic_regex.py`)
- **No mocks** — real daemon, real GPU, real embedder. (`test_no_mocks_or_fakes.py`)
- **Public repo** — no secrets, real device paths, usernames, or company names; every
  machine-specific value is `os.environ.get(...)` in `core/config.py`. (`test_public_hygiene.py`)

## Project quick reference

- LLM: GPU = FastEmbed/ONNX/CUDA for embed + rerank and nothing else; chat = `claude -p`,
  dashboard-only, no fallback (HR10). `RSE_QUERY_LLM_MODEL` (default `claude-haiku-4-5`,
  `core/config.py`) is the **only** LLM env var — `_PROVIDER`/`_NUM_CTX`/`_TIMEOUT` parsed and
  did nothing, and were deleted 2026-07-31. SC9 in `test_schema_consistency.py` now blocks a re-add
- Metrics: `overview(what="metrics")` — the live payload is the key list
- Architecture: `docs/architecture/federation-and-search-engine.md` +
  `docs/architecture/federation-ops-and-invariants.md`
