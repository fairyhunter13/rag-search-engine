---
type: Defect
resource: src/rag_search/daemon/
title: A clean exit meant the reload never came back
description: "`POST /api/reload` shut the daemon down and left it down, because it exited 0 and systemd's `Restart=on-failure` does not restart a clean exit — one exit code was carrying two different intents."
tags: [daemon, systemd, reload, defect]
status: resolved
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# A clean exit meant the reload never came back

## Symptom

`POST /api/reload` stopped the daemon and it stayed stopped. The documented behaviour — "systemd
brings it back in about a second" — did not hold, and recovery needed a manual
`systemctl --user restart rag-search-mcp-daemon`.

## Root cause

The reload path exited 0. The unit's restart policy is `Restart=on-failure`, which by definition
does not act on a clean exit.

Underneath that: **one exit code was serving two intents.** "Reload me" and "stop, I meant it"
(`daemon stop`) both left through the same door, so the supervisor had no way to tell them apart.

## Why nothing caught it

It is only visible after a real reload against a real unit, and nothing asserted that the exit code
*differed* by intent. A test of either path alone passes.

## What covers it now

The exit code is split by intent: reload exits 3 so `Restart=on-failure` fires,
`?restart=false` exits 0 and stays down deliberately. `test_reload_exit_code_split` asserts both
values, and `test_parse_restart_param` covers the query parameter that chooses between them.
`CLAUDE.md` states both cases, because the second one looks like the first failing.

## The lease that grew around it

Both reload paths now refuse with **409** while a sweeps pause lease is held — a live test suite or
`scripts/purge_unindexable.py` owns the daemon — and the reply carries `lease_remaining_s`.
`?force=true` overrides, and the lease self-expires after 30 minutes so a client that dies mid-hold
cannot wedge the daemon permanently.

The trap that remains, and it is silent: **`systemctl --user restart` bypasses that 409.**
`_PAUSED` and `_PAUSE_DEADLINE` are module globals, so a restart clears the lease with no refusal
and no log naming the suite it just unpaused. The suite renews its lease after every test, but the
renewal is deliberately one-way — it re-arms only while the lease is still non-zero, because a lease
at zero means the mechanism decided to resume. Correct in isolation, and it means **an externally
cleared lease never comes back**. Read `/healthz` before restarting, and re-pause if you cleared
one.

Records: [reload under a sweeps lease](../../docs/decisions/2026-07-29-reload-under-a-sweeps-lease.md),
[releasing a lease schedules nothing](../../docs/decisions/2026-07-31-releasing-a-lease-schedules-nothing.md).
Procedure: [running the live suite](../runbooks/running-the-live-suite.md).
