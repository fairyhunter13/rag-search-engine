---
type: Constraint
resource: src/coderag/health.py, src/coderag/systemd.py, src/coderag/server.py
title: A fleet alert decides on two samples, because every field in the row describes the wrong span
description: "`last_error` is cleared by the hourly sweep, `last_error_at` is restamped by every failure, and `error_total` never resets. So no single read of the registry separates a transient failure from a stuck project. The checker holds the previous failing set instead."
tags: [observability, alerting, systemd, registry]
status: stable
generated: { by: claude/opus-5, at: 2026-08-23T01:10:00Z }
---

# Liveness answers a question nobody was asking

`coderag-alert@.service` hangs off `OnFailure=` on the daemon unit, so it fires when the process
fails. "Up and indexing nothing" never fails the unit, and pointing that alert at the new counts
reaches nothing. The check has to be periodic, which is a timer and a second unit.

Asking systemd is also the wrong source. `is-active` was green through the outage where the session
manager's task group was never entered and every `/mcp` request returned 500 while `/healthz` stayed
fine. The check asks the daemon over HTTP, and a daemon that does not answer pages.

# One sample cannot decide it, and two obvious fields make it look like it can

`last_error` clears on the next success, and `reconcile_all` supplies one every hour. So a single
transient failure holds `projects_failing` at 1 for up to a sweep. An hourly sampler keyed on
`projects_failing > 0` would page for almost every one of them. [The alert that cries
wolf](../defects/a-failure-that-resolved-itself-left-no-trace.md) is how the outage that mattered
goes unread. That is the reason `alert_text` already sleeps eight seconds and re-checks before
paging for a restart.

Two fields look like they answer it and neither does:

- `last_error_at` is restamped by every failure, so a project failing at each sweep carries a
  permanently fresh timestamp and reads as never stuck. A rule built on its age is decoration, the
  same shape as the refuted `indexed_at` staleness clause.
- `error_total` is a lifetime count no success resets, by design. It says a project has failed,
  not that it is failing.

What is left is state the checker holds. A project pages only when it is in the failing set now
and was in it at the previous check. That is why `/healthz` returns `failing` — the identities — beside
the count. A count cannot tell one project failing twice from two projects failing once each, and
those are an incident and a quiet week.

# The period is part of the rule

`HEALTH_EVERY_S` defaults to `SWEEP_EVERY_S` and the timer test refuses anything shorter. Checked
twice inside one sweep, nothing has had the chance to retry. So every failure appears in both
samples, and the persistence rule degrades back into the sampling rule it was written to replace.

`scheduler_errors` joins the failing set as `scheduler:<job>` under the same rule.
[The scheduler suppressed its own death](../defects/the-scheduler-suppressed-its-own-death.md) is
why it has to. A sweep that raises reaches no project, so no row is flagged and a fleet-wide count
built on `last_error` reads clean through it.

An unreachable daemon pages but does not write state. Otherwise a restart between checks resets the
comparison, and a project failing straight through it starts over as a first sighting every time.

# The live lane is what pages first

Its first real page on this machine was test litter, an hour after the suite ran. `test_live_*`
claims a root under `tmp_path`, and pytest deletes the directory. The row survives with
`FileNotFoundError: ... is not a directory`. It fails at every sweep, so it clears both samples.

That is the rule working. It is also why `coderag doctor --prune` belongs at the end of a live
run, with an explicit release of the dead rows. Waiting until someone notices is too late. Removal stays by
hand, and `claim()` gets no path-shaped refusal. `tmp_path` is where the registry's own tests
claim, and a rule refusing temporary roots would fail the suite that proves the registry works.

# Amendment, 2026-08-25: three readings the checker was not taking

`/healthz` publishes twelve fields and `check` read two of them. Three failures went through that
gap. Each is now an identity in the same failing set, under the same two-sample rule.

**A dead watcher.** `watching` was published and unread, so a thread that died hours ago passed
every check. [The watcher went blind in two
ways](../defects/the-watcher-went-blind-in-two-ways-and-said-neither.md) is what it was hiding. An
absent field reads as watching, because an older daemon is not evidence of a failure.

**A queue that stopped draining.** Depth alone is not a failure. The hourly reconcile enqueues every
enabled row, so a deep queue is the ordinary state of a healthy fleet. The rule is a depth at or
above `HEALTH_QUEUE_STUCK` that did not fall since the previous check. That is why the state file
now holds the depth beside the failing set.

That identity carries its own two samples, so it pages at the check that sees the stall. The
persistence rule on top of it would page three checks later instead.

**A fleet failing past the cap.** `failing` is truncated at `HEALTH_FAILING_CAP`, which is 20.
`projects_failing` carries the true count and was unread. Past 20, the extra failures were in no set
the checker compares. The identity is a constant string, because one carrying the count changes
between two samples and pages on neither.

# The report that is not an alert

`coderag-doctor.timer` runs `doctor` daily and pages nobody. `doctor` exits 1 on any finding, so
`OnFailure` here would fire every day until a human acts. That is how the alert beside it gets
muted. `SuccessExitStatus=0 1` is what keeps a finding out of systemd's failure path.

`--prune` stays hand-typed, because the registry records two fleet-wide index wipes caused by code
that pruned. The orphan stores and the missing rows this reports were paging hourly through health
instead, because nothing else ever looked.