---
type: Constraint
resource: src/rag_search/daemon/cpu_budget.py
title: The daemon lives inside one core
description: HR40 — every earlier idle-CPU fix was cooperative, so a cgroup-v2 `CPUQuota=100%` was added underneath them as a kernel ceiling, proven by throttle counters rather than by observing low usage.
tags: [cgroup, systemd, cpu-quota, idle, hr40]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# The daemon lives inside one core

Four independent idle-CPU causes were found and fixed in one evening, and every one of those fixes
is **cooperative**: the drift gates, the shared discovery predicate, the single-generator watcher
and the code-only fingerprint each stop the daemon from doing work it should not do. None of them
physically bounds it if a gate is ever wrong again.

HR40 is the layer that assumes a gate will be wrong.

## Two tiers

**Idle tier — measurement.** `cpu_budget.py` resolves the daemon's own cgroup-v2 directory via
`/proc/self/cgroup` (unified hierarchy only) and reads `cpu.stat`'s `usage_usec`.
`cpu_percent_core()` diffs successive samples into a fraction of one core, exposed at `/healthz`
(`cpu_percent_core`, `cpu_quota_cores`) and at `/api/metrics` (`percent_core`, `quota_cores`,
`usage_nsec`, `nr_periods`, `nr_throttled`, `throttled_usec`).

The gate measures **the running daemon's own cgroup** through that HTTP surface over a quiescent
≥20 s window — not the pytest process, which is uncapped and would report something unrelated.

**Active tier — enforcement.** `daemon/systemd.py::unit_text()` writes `CPUAccounting=yes` and
`CPUQuota=100%` into `[Service]`, mirrored by a `cpu-budget.conf` drop-in for operator overrides.
`CPUAccounting=yes` is stated explicitly because **`CPUQuota=` alone does not imply it** (systemd
issue #9647) — a unit with only the quota line accounts nothing and the measurement tier reads
zeros.

This is a kernel ceiling over the whole service cgroup, which includes
[the bounded-parse worker pool](every-parse-is-bounded-out-of-process.md): its workers are children
of the daemon's cgroup, not a separate one, so they cannot buy capacity by spawning.

## Presence is not proof

A cap that is never approached and a cap that does not exist produce the same low number. The
canonical evidence that the quota is *biting* is `cpu.stat`'s `nr_throttled` and `throttled_usec`
climbing under sustained real load — driven by an HTTP-triggered MCP `index()` against a synthetic
multi-file workspace, which runs `reconcile_projects` inside the daemon's own process and cgroup.

There is a third test below that. Everything above depends on the `cpu` controller actually being
delegated to the `--user` systemd manager on this host, which is a host property this repo does not
control. `test_cb6_systemd_scope_delegation_hermetic_proof` runs a fresh
`systemd-run --user --scope -p CPUQuota=100%` four-process CPU burn, entirely independent of the RSE
daemon and unit, and asserts *its* `nr_throttled` rises — with a remediation hint (a root
`delegate.conf` drop-in) if it does not. A precondition that is assumed rather than proven is how a
whole family of gates goes quietly vacuous.

## Sources

Row HR40 in [§13b](../../docs/architecture/federation-ops-and-invariants.md).

| Claim | Guard | File |
|---|---|---|
| the unit declares both lines | `test_cb1_unit_text_has_cpu_accounting_and_quota` | `test_cpu_budget.py` |
| the live daemon is capped | `test_cb2_daemon_cpu_quota_enforced` | `test_cpu_budget.py` |
| idle stays under 1 % of a core | `test_cb3_idle_cpu_under_one_percent_core` | `test_cpu_budget.py` |
| the cap physically throttles | `test_cb4_active_work_capped_and_throttled` | `test_cpu_budget.py` |
| delegation is real on this host | `test_cb6_systemd_scope_delegation_hermetic_proof` | `test_cpu_budget.py` |

CB3, CB4 and CB6 carry the `exclusive` marker — they measure or destroy a quiet machine, so they
must not run beside other load. See [running the live suite](../runbooks/running-the-live-suite.md).
