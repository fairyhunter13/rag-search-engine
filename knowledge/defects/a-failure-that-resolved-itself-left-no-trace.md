---
type: Defect
resource: src/coderag/registry.py, src/coderag/entry.py, src/coderag/server.py, tests/test_registry.py
title: A failure that resolved itself left no trace, and liveness called that healthy
description: "`last_error` was one overwritten string cleared by the next success, and the reconcile sweep supplies a success every hour — so a project failing every sweep for a week read clean at every moment anyone looked, while `coderag-alert@.service` checked only `is-active`."
tags: [registry, observability, alerting, systemd, resolved]
status: stable
generated: { by: claude/opus-5, at: 2026-08-22T20:30:00Z }
---

# The clock that erased the evidence was the one meant to fix things

`reconcile_all` enqueues every enabled project hourly. A pass that succeeds clears `last_error`, so
the field's shelf life is bounded by the sweep, not by the failure: a project that fails on write
pressure and succeeds on the retry has a window under an hour in which anything could have seen it,
and after that the registry states it is fine. There was no timestamp and no count, so "failing
intermittently for a week" and "never failed" are the same row.

The pair that fixes it is `last_error_at` and `error_total`, and neither is ever cleared.
`last_error` keeps its clear-on-success meaning — a healthy project must still read clean — so the
two answer different questions: is it broken now, and has it ever been.

# Four writers, one counter

The four sites that recorded a failure — the indexer, the `index` tool, the watcher and the
federation sweep — each set `last_error` directly. Three fields updated at four call sites is three
chances for the count to drift from the string, so they now go through `registry.record_error`,
which is the only place a failure is written. It keeps `update()`'s contract of refusing to
resurrect a row nobody claims: work finishing for a project the user already removed must not
recreate it.

# Nothing outside the daemon could see any of it

`/healthz` counted enabled projects, which stayed green through every one of them failing to index,
and the alert unit asks systemd whether the process is up. It now also reports `projects_failing`
and `errors_total`, which is what makes "up but nothing is indexing" a state something can alert on
rather than a state only a reader of the journal could infer.

Wiring the alert to it was recorded here as outstanding and host-owned, which was wrong:
`src/coderag/systemd.py` generates both units and `cli install-systemd` writes and enables them, so
none of it was outside this repo. What was actually in the way is that `OnFailure=` cannot see a
daemon that is up, and that no single field of the row separates a transient failure from a stuck
project. Both are settled in
[a fleet alert decides on two samples](../constraints/a-fleet-alert-decides-on-two-samples-not-on-a-count.md):
an hourly `coderag-health.timer` runs `cli health`, which pages only for a project failing at two
consecutive checks, and reuses the existing alert unit through `OnFailure`.

Raising the log level was considered and refused: 8,140 lines in 24 h at INFO is already the volume,
and the defect was that errors were ephemeral and unstructured, not that they went unlogged.

# What covers it now

Four tests in `tests/test_registry.py`. The load-bearing one records a failure, then a success, and
asserts the durable pair survives the clear — it fails against the old code because the old code has
no durable pair. The accumulation test discriminates an increment from an assignment, which would
report 1 forever and read as a single blip. The round-trip test exists because a field the registry
drops on reload reports zero to every reader that opens the file fresh.

A fifth arm holds the chokepoint itself. The four call sites were found by grep, which says nothing
about the next one, and a writer that sets `last_error` directly leaves both counters behind — a row
that reports a failure the total never saw. An AST walk over `src/coderag/` asserts `record_error` is
the only function that flags one; clearing to `None` stays every caller's to do.

Confirmed end to end rather than only at the unit: a project carrying a retired `.coderag.toml`
raises `ConfigError` in the watcher, and the row came back `error_total: 1` with `last_error_at` set,
`armed: False`, and the durable pair intact after a success cleared the string. Run against an
isolated `CODERAG_STATE_DIR` in a subprocess, which is what keeps a red demo away from the fleet
registry -- 244 rows before and after.

Eight more in `tests/test_health.py` hold the alerting rule, and each was confirmed against a broken
checker rather than only against the fixed one: paging on a sample breaks four of them, comparing
counts instead of identities breaks the two-projects-once-each arm, and writing state after a failed
fetch breaks the restart arm.
