---
type: Defect
resource: tests/test_live_results.py, tests/test_live_federation.py, tests/conftest.py, tests/live.py
title: One live test skipped the disable-don't-prune teardown, and it was the whole of doctor's red
description: "The suite's rule is that a live test disables what it registers and never prunes. Ten tests in the module did. One did not. Its two leaked rows were the entire 151-enabled/149-indexed gap, the entire `failed: 2` on `/healthz`, and the two `MISSING` lines `doctor` exited 1 on."
tags: [tests, registry, federation, alerting, resolved]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T20:15:00Z }
---

# The policy was not the problem

A first reading blamed the disable-don't-prune rule. Measurement refused it: of 88 disabled rows,
**zero** pointed at a path that no longer exists and **zero** sat under `/tmp`. The rule works, and
it exists because a live test that pruned rows destroyed the fleet's 236 rows once already.

The leak was one test -- `test_7_a_typo_in_the_config_is_an_error_that_names_the_nearest_key` --
which registered a tmp project to provoke a config error and never disabled it. Two runs, two rows,
retried and logged with a traceback at every daemon start since.

# Two fixes, because the teardown alone repeats

The test's disable now sits in a `finally`. The row it leaves behind is one the daemon can never
index. So the cost of skipping it is loudest exactly when the test failed.

And `conftest.fleet_unchanged` counts the daemon's enabled rows before and after every `live`
module, per module so the red names the file. `isolated_state` cannot cover this -- it redirects the
in-process paths, and a live test reaches the real registry through the daemon.

The count comes from `/healthz`, the only read of the real registry a test is allowed. Reading
`projects.json` from a test is the shape of the incident.

# It happened again, in the module the first fix did not reach

2026-09-04. Four rows -- `/tmp/pytest-of-<user>/pytest-{977,1010}/{wroot0,wmember0}` -- had paged
hourly for twelve hours, `notify-send -u critical` each time. The daemon was fine throughout: 0
restarts, 423 projects, both providers on CUDA. Only `coderag-health.service` was red.

The source was `tests/test_live_results.py`, the `tree` fixture. Its two disables were a trailing
line after the `yield`, the exact shape this concept was opened to remove. `test_live_agent.py` and
`test_live_scoping.py` had the `try` / `finally`. This module never got it.

The `try` now opens at the registration rather than at the `yield`. The fixture has three `until`
waits of 180 s, 180 s and 60 s between the two, and a timeout in any of them is a path where the
row is registered and no teardown exists to run at all.

# A test is not a teardown

`test_live_federation.py` had a second shape of the same hole, and it is the one worth naming:
`test_8_teardown_leaves_the_fleet_alone` did the cleanup for the `root` and `member` fixtures. A
red in tests 1 through 7, an interrupt, or a `-k` subset skips test 8, and both rows survive. The
disable moved into each fixture's own teardown. Test 8 keeps its `members_released == []`
assertion, which is a claim and not cleanup.

# Why `fleet_unchanged` did not stop it

Not determined, and the evidence to determine it is gone. `indexed_at` on all four rows is one
sweep timestamp within 0.11 s, so it records the last reconcile and not the registration. Two
explanations survive -- a fixture-setup error that errors the module before the guard's teardown
reads, or a run interrupted before any teardown ran -- and nothing on disk separates them. The
guard is unchanged: both fixes above are what make the guard's red unnecessary rather than
unread.

- The row could not be removed once dead, for a reason this concept did not predict --
  [a dead row paged hourly](a-dead-row-paged-hourly-and-nothing-could-remove-it.md).

# One leak survives both fixes, and it is not explained

The suite run that verified the two fixes above left two new enabled rows of its own:
`layer30/storefront` and its member `layer30/billing-core`, from `test_live_agent.py`'s `workspace`
fixture. That fixture already disables its root in a `finally` (`tests/test_live_agent.py:104`),
and `fleet_unchanged` did not red on the module. So the disable ran, the guard read a fleet it
judged unchanged, and the rows were enabled afterwards.

What the fixture has and the two fixed ones do not is a real `claude` session, started inside it,
pointed at the same root. A session that re-registers the root after the teardown would produce
exactly this. Not confirmed -- no transcript was kept -- so it stays a question and not a cause.

Both rows carry a real `st_dev`, so they are prunable once their directory goes. That is the
difference between this and the four that paged: same leak, working exit.


# A `finally` cannot disable a row through a daemon that is dead

The next full-suite run leaked `wroot0` and `wmember0` again, out of the fixture that had just been
given the `try` opening at the registration. The `finally` ran. Both `index(enabled=False)` calls
inside it raised `ConnectError: [Errno 111] Connection refused`, because systemd had SIGABRT'd the
daemon on a watchdog timeout 90 s earlier, mid-suite. The teardown is an RPC, so a dead daemon
defeats it wherever the `finally` sits.

There is no placement that fixes this. The row can only be disabled by the process that owns the
registry, and that process is the one that died. So the leak is not fully closable from the test
side, and what makes it survivable is the other half:
[a dead row paged hourly](a-dead-row-paged-hourly-and-nothing-could-remove-it.md). Both rows carried
a real `st_dev` -- the backfill had run -- so once pytest deleted the directories they answered
`deleted` rather than `unknown`, and `doctor --prune` removed them with no `forget` typed by hand.
That is the first time this leak cleaned itself up.

The watchdog kills are their own defect and are not this one: three at 09:11, 09:13 and 09:42 on
2026-09-04, each `Watchdog timeout (limit 1min 30s)` under a full-suite load, all in processes that
predate the backfill.
