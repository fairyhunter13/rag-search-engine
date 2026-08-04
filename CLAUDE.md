# rag-search-engine — Claude Code Instructions

Instructions only. Rationale and incident history live in `docs/decisions/`; the machine-checked
invariants live in `docs/world-model/model.yaml`. Don't restate either here.

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
`claude -p`; 13 tests) · `exclusive` (needs a quiescent daemon to measure it — CB3/CB4/CB6;
3 tests). `slow` is retired. **Skipping is forbidden** — `test_no_skip_markers_in_live_suite`; a
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
`test_p6_daemon.py` — see `docs/decisions/2026-07-29-reload-under-a-sweeps-lease.md`.

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

**The stale count is read at `overview(what="metrics", project_path=…)` → `pipeline_version`**, whose
`stale_stores` counts every store — including unstamped ones — whose `graph.db` `meta.algo_version`
differs from `sweeps._pipeline_algo_version()`. Built by `_pipeline_block` (`server/_overview.py`),
guarded by AU5 in `test_extraction_ladder.py`. Recorded here because an instruction to watch a number
is useless without its source: one session lost it and reported the convergence as unverifiable.

**Take the lease before the first edit, not before the re-derive.** `_code_fingerprint` re-reads the
fingerprinted modules off disk on every call, so a running daemon restamps the moment
`graph/extractor.py` is *saved* — no commit needed, and a docstring counts. Measured 2026-07-31: one
store re-derived with the daemon's old in-memory code and the new fingerprint. See
`docs/decisions/2026-07-31-an-edge-is-a-resolved-call.md`.

## Invariants

All governing laws live in `docs/world-model/model.yaml` — P0–P18, HR1–HR41 — and that file is
the single source of truth. Do not restate them here.

- Check the working tree: `python scripts/check_world_model.py` (GPU-free, daemon-free)
- Read them: the `world-model` skill · doctrine ladder: the `info-hierarchy` skill
- Regenerate those skills after editing `model.yaml`: `python scripts/gen_world_model_skills.py`
- Why a rule exists, and what it cost to learn: `docs/decisions/`

Four stated inline because violating them fails silently rather than loudly:

- **GPU-only** (P0) — all inference on CUDA. CPU fallback must raise fatally, never degrade
  quietly. Exactly two residents: the embedder and the cross-encoder reranker.
- **No regex, keyword lists, or mapping tables for code semantics** (P6, HR15) — tree-sitter
  structure only, with no LLM escape hatch to fall through to.
- **No mocks** (P8) — real daemon, real GPU, real embedder.
- **Public repo** (P18, HR34) — no secrets, real device paths, usernames, or company names; every
  machine-specific value is `os.environ.get(...)` in `core/config.py`.

## Project quick reference

- LLM: GPU = FastEmbed/ONNX/CUDA for embed + rerank and nothing else; chat = `claude -p`,
  dashboard-only, no fallback (HR10). `RSE_QUERY_LLM_MODEL` (default `claude-haiku-4-5`,
  `core/config.py`) is the **only** LLM env var — `_PROVIDER`/`_NUM_CTX`/`_TIMEOUT` parsed and
  did nothing, and were deleted 2026-07-31. SC9 in `test_schema_consistency.py` now blocks a re-add
- Metrics: `overview(what="metrics")` — the live payload is the key list
- Architecture: `docs/architecture/federation-and-search-engine.md` +
  `docs/architecture/federation-ops-and-invariants.md`
