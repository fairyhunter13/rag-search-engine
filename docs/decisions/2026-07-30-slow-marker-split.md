# Splitting the `slow` marker into the two reasons it conflated

**2026-07-30** · `slow` is retired · guard: `test_no_skip_markers_in_live_suite`

`slow` meant "LLM-heavy" and "just takes a while" at once, and the one `-m "not slow"` gate
excluded both from every push.

Measured before the split: of **47** `slow` tests only **13** reached a model and **3** needed a
quiet daemon — the other **31 cost wall clock alone**, yet ran only on a manual dispatch nobody
dispatches. That is where six gates rotted in a single week:

- `test_federation_composed_entity.py` — `KeyError: 'kb_state'`
- `test_self_heal_e2e.py` — memo assertion failing on every run since the memo landed
- `test_p6_daemon.py` — an 8 s budget against 21.4 s recovery

Each was invisible for exactly this reason.

**Replacement markers:** `costly` (spends real Claude session quota via `claude -p` on
`/api/chat_stream`; 13 tests) and `exclusive` (needs a quiescent daemon to measure it — CB3/CB4/
CB6; 3 tests). The other 31 are unmarked and run on every push, which is the whole point: a gate
that only runs on a dispatch nobody dispatches is not a gate.

**Skipping remains forbidden.** A marker chooses which suite a test belongs to; it never lets one
pass without running.

## Consequence for CI

`live-fast` is no longer the "<5 min" job it was — the split moved 31 wall-clock-heavy tests onto
every push deliberately.

## Retired with this change

The convergence prescription that named `test_converge_smoke_standalone` and
`test_kb_state_ready_all_projects`, guarding `graph/enrich.py`'s enrich→converge loop. All three
left with [tier 3](2026-07-28-tier-3-retirement.md), and the property they measured — how far
DeepSeek narration had got — cannot vary now that structural labelling fills every summary in one
deterministic pass. The surviving cascade gate is HR38's FCG1–FCG4 in `test_idle_stability.py`,
which runs in `live-fast` on every push, so no manual-dispatch rule replaces it.
