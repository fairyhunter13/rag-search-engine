---
type: Defect
resource: src/coderag/watchdog.py, src/coderag/server.py, src/coderag/systemd.py, tests/test_server_tools.py
title: The watchdog ping rode the job loop, so slow and dead were the same observation
description: "`_tick` sent `WATCHDOG=1` as its first statement and then ran every job, and `WatchdogSec` was three of those ticks. A job slower than the remaining budget therefore reads as a dead process. systemd killed a working daemon at exactly 180 s, five times, and each restart re-entered the load that caused it."
tags: [scheduler, systemd, liveness, availability, resolved]
status: stable
generated: { by: claude/opus-5, at: 2026-08-27T00:00:00Z }
---

# One thread carried both the work and the proof of life

`_tick` was the whole liveness mechanism. It waited `SCHEDULER_TICK_S` and sent the ping. Then it
ran the watcher restart, the hourly sweep, the connection reap and the model release.
`systemd.py` derived `WatchdogSec` from the same constant, at `SCHEDULER_TICK_S * 3`. The deadline
was three of the loop's own iterations by construction.

Two consequences follow from the ordering alone. The first ping lands at t=60 s and not at start,
because the wait precedes it. And a job that blocks past the budget left in the current deadline
stops the pings, whatever the process is otherwise doing.

That is what happened. The daemon was killed at exactly 180 s five times running, at a 1-minute
load of 22. The same tree survived 334 s once the load fell to 10, and a `git stash` bisect
cleared the diff that was in flight. Faulthandler dumps over the kill showed no deadlock. No
thread was stuck on a lock, so nothing was dead. It was slow.

The jobs are slow by nature and not by accident. `_sweep` walks every direct root's tree and
reconciles 411 projects. Its 44 recorded runs give 2.09 s at the median, 3.34 s at p90 and
**15.41 s** at the worst, on a quiet card. The watcher re-arm is ~120,000 inotify watches. Load
stretches all of it. A restart then puts the whole fleet reconcile back at the front of the queue,
which is more load.

# What a deadline can honestly assert

Not "the last cycle was quick". A ping that means anything has to be cheap enough that only a
stuck process misses it. It also has to be gated on something a stuck process cannot satisfy.

`watchdog.py` splits those two. A thread that does nothing but sleep and send a datagram pings at
`WATCHDOG_USEC / 3`. That value comes from systemd's own environment and not from this repo's
config, because an installed unit can predate the constant it was generated from.

The gate is `watchdog.stall()`. The scheduler thread has to be alive, and it has to have called
`beat()` inside `SCHEDULER_STALL_S`. `beat()` is the last statement of a full cycle, and the
threshold is 900 s against a worst measured cycle of about 15 s. So a job that never returns still
restarts the daemon, and a job that takes two minutes no longer does.

The thread-alive half is not redundant. `release_models` runs outside `_guarded`, so the timer
thread can die and leave the process serving.

`WatchdogSec` is `WATCHDOG_SEC`, 90, and no longer derived from the tick. Once the ping left the
loop, deriving it there meant that a longer tick silently buys a wedged process more time.

# Withholding is visible

The pinger logs once per reason when it stops. `/healthz` carries the same string under
`scheduler_errors.heartbeat`. That matters more here than for a scheduler job. The withheld ping
is a restart within 90 s, and the restart is what erases the journal context explaining it. The
rule is [the scheduler suppressing its own death](the-scheduler-suppressed-its-own-death.md). A
failure that restarts the daemon must be written where the restart does not reach.

Five tests. The first holds the property the old shape cannot have: pings land while no cycle
completes at all. The wedged arm and the dead-thread arm were run against a gate mutated open,
and both fail there. So does the `/healthz` arm. The two unit arms were run against the restored
`SCHEDULER_TICK_S * 3`, and both fail there.
