# Decisions

**Incident narrative and design rationale go here, not in `CLAUDE.md`.**

`CLAUDE.md` is loaded verbatim into every agent session. Between 2026-06-26 and 2026-07-30 it
grew from 4.5 KB to 23 KB because every change appended its own post-mortem, so each turn paid
for five weeks of history it never acted on. The instructions stayed; the history moved here.

The split is by *what the reader does with it*:

- **`CLAUDE.md`** — what changes an agent's next action. A rule, a command, a hazard.
- **`docs/decisions/`** — why the rule exists, what it cost to learn, what was tried and rejected.
  Read on demand, linked from the one-line rule.
- **`docs/world-model/model.yaml`** — the machine-checked invariants (P0–P18, HR1–HR41).
  Single source of truth, verified by `scripts/check_world_model.py`. Never restated elsewhere.

Nothing here is deleted when a subsystem retires. A retired mechanism's lesson usually outlives
it — HR36 is gone, its rule about reuse stamps is not.

## Index

| Date | Decision |
|------|----------|
| 2026-07-31 | [A function bound to a name is a definition, and a heading is not an identifier](2026-07-31-e7-named-bindings-and-headings.md) |
| 2026-07-31 | [A re-derive writes through the graph, it does not empty it first](2026-07-31-atomic-graph-rederive.md) |
| 2026-07-31 | [The re-derive that never reached 89% of the fleet](2026-07-31-reconcile-walk-starvation.md) |
| 2026-07-31 | [Paying for context once: the CLAUDE.md trim and the deduplicated MCP block](2026-07-31-context-budget.md) |
| 2026-07-30 | [One live suite at a time, keyed on process not lock name](2026-07-30-one-live-suite-at-a-time.md) |
| 2026-07-30 | [Splitting the `slow` marker into the two reasons it conflated](2026-07-30-slow-marker-split.md) |
| 2026-07-29 | [Refusing to reload the daemon under a sweeps lease](2026-07-29-reload-under-a-sweeps-lease.md) |
| 2026-07-29 | [Giving the GPU back before idle](2026-07-29-vram-starvation.md) |
| 2026-07-28 | [What left with tier 3, recorded so it is not re-derived](2026-07-28-tier-3-retirement.md) |
| 2026-07-09 | [Public-release hardening and the runnable-by-anyone contract](2026-07-09-public-release-hardening.md) |
| 2026-07-01 | [Four root causes of idle CPU pinning](2026-07-01-idle-cpu-root-causes.md) |
