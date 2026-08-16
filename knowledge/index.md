---
okf_version: "0.2"
---

This bundle holds no concepts, on purpose. It is a signpost, and the reason is
[docs/decisions/2026-08-17-okf-lands-as-a-signpost.md](../docs/decisions/2026-08-17-okf-lands-as-a-signpost.md).

Durable knowledge in this repo already has three homes, split by what the reader does with it, and
each is gated by a live test. Write to the home, not here:

| If you would file it as | It goes to |
|---|---|
| `Constraint`, `Policy`, `Interface` | `docs/architecture/federation-ops-and-invariants.md` §13b — one row, keyed by `HR#`, plus its guard in the §14 map |
| `Decision`, `Defect` | `docs/decisions/<YYYY-MM-DD>-<slug>.md` |
| `Runbook` | `CLAUDE.md`, if and only if it changes an agent's next action; the narrative behind it goes to `docs/decisions/` |
| `Component`, `Glossary Term` | nowhere — a name that needs explaining is a name to fix |

A concept earns a file here only when it fits none of those rows. Say so in `log.md` when one does.
