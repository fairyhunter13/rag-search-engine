---
type: Defect
resource: src/coderag/server.py, src/coderag/config.py, tests/test_restart.py
title: The stop hung again, because a cancelled task group waits for a thread it cannot cancel
description: "timeout_graceful_shutdown bounds the connection wait and nothing else. The 90 s was spent in the lifespan shutdown, waiting on a plain-`def` tool running under anyio's shield — and the tool restarted the threads the finally block had just stopped."
tags: [daemon, shutdown, anyio, mcp, systemd]
status: fixed
generated: { by: claude/opus-5, at: 2026-08-20T15:30:00Z }
---

# It reproduced after the fix

[[a-deliberate-stop-was-indistinguishable-from-a-crash]] is `status: fixed` and its fix is still in
force. This is a second, independent hang behind it. Journal, 2026-08-20:

```
09:01:06 Stopping coderag.service...      09:01:06 INFO: Shutting down
09:01:06 INFO: Waiting for connections to close.
09:01:44 / 09:01:54 / 09:02:15  "3 changes detected"   <- watcher still alive, 70s in
09:02:36 State 'stop-sigterm' timed out. Killing.  -> SIGKILL, Failed, OnFailure triggered
```

The daemon then sat `failed` for 3.5 hours, because `Restart=on-failure` does not restart a unit
that failed its own stop.

# Where the first fix stops

uvicorn applies `timeout_graceful_shutdown` to `_wait_tasks_to_complete()` **only**. On expiry it
logs `"Cancel N running task(s), timeout graceful shutdown exceeded"` and then awaits
`lifespan.shutdown()` **unbounded**. That error line is absent from the journal, which is the proof
the connection wait returned: the 90 seconds were spent in the lifespan shutdown, unwinding the
*outer* context in `build_app` — the MCP SDK's streamable-HTTP session manager.

This corrects the old page's reading of its own evidence. What it called a live stream held open by
the client was not one: `stateless_http=True` means a fresh transport per request and no stream to
hold. Its fix was still the right fix for the wait it did bound.

# Two mechanisms, both necessary

1. **The cancel cannot reach the tool.** `StreamableHTTPSessionManager.run()`'s finally calls
   `tg.cancel_scope.cancel()` and then falls out of the `async with`, where `TaskGroup.__aexit__`
   waits for every child task. In stateless mode every `/mcp` request runs on that task group, and a
   plain `def` tool is dispatched through `anyio.to_thread.run_sync`, which defaults to
   `abandon_on_cancel=False` and therefore runs under `CancelScope(shield=True)`. Both coderag tools
   are plain `def`. The wait is by construction unbounded, and the tool is blocking on
   `_GPU_INFER_LOCK` or on `embed._lock` held across a cold model load.
2. **The tool restarts the threads the finally just stopped.** `tools.py` calls `watch.rearm()`,
   `watch.start()` and `index.start_worker()`; `watch.start()` does `_stop.clear()`. An `index` call
   executing in that shielded thread *after* the finally ran resurrects both daemon threads, and
   nothing calls `stop` a second time. That is the "3 changes detected" at +70 s, and it keeps the
   GPU lock contended so the joins never complete — a closed loop only SIGKILL breaks.

The 5 s joins in `watch.stop()` and `index.stop_worker()` are therefore not wrong, they are
irrelevant: they return regardless, and nothing downstream of them is bounded.

# And the exit was not reachable either

Found while testing this: the old page's `_shutdown_exit` never ran on a stop. uvicorn's
`capture_signals` restores the handler it replaced and re-raises the caught signal *before*
`uvicorn.run` returns, so the process died under the default disposition at `-15`. Corrected in
full on [[a-deliberate-stop-was-indistinguishable-from-a-crash]]; the one-line fix is a SIGTERM
handler installed in `serve` before `uvicorn.run`, so ours is the handler uvicorn restores into.
Without it every `returncode == 0` assertion below is unreachable, and the deadline's own exit would
have been the only path that ever produced a zero.

# The fix

`SHUTDOWN_DEADLINE_S = 15` and a daemon `threading.Timer` armed as the first statement after
`_notify("STOPPING=1")`. Placement is the fix: that is the inner context, so the deadline is running
strictly before `served.__aexit__` is entered — the window that hangs. It calls the same
`_shutdown_exit`, so the process still leaves by `os._exit(0)` and still exits 0; the journal line
says "on shutdown deadline" so a forced unwind is distinguishable from a clean one.

Rejected. `anyio.move_on_after` around `served(scope)` cannot work — it would deliver a cancel into
a scope whose whole purpose is to absorb it, then hang at its own `__aexit__` on the same thread,
with an import that makes a reader believe there is a bound. `TimeoutStopSec` alone is the same wrong
ending sooner, which the old page already refused. Guarding `tools.py` against the restart is correct
but insufficient — it does not free a thread already blocked on the GPU lock — and the deadline
subsumes it.

# Why the existing test could not see it

`test_sigterm_exits_promptly_with_a_client_connected` sends SIGTERM with `tools/list` behind it.
`tools/list` holds no stream in stateless mode and runs no user code, and the fixture daemon has no
indexed project, so both joins return in milliseconds.

`test_the_deadline_bounds_a_stop_with_a_tool_call_in_flight` reproduces the shape instead: index the
corpus, restart so the models are cold, then fire a `search` and **leave it running** — waiting for
it is waiting for the thing the signal has to interrupt. The daemon's own log is the proof the
mechanism is the one diagnosed: both ONNX sessions are built *after* `"StreamableHTTP session
manager shutting down"`.

Measured: **5.7 s** for that stop with the deadline off. That is not the 90 s outage, which needed
mechanism 2 as well, and it is under the shipped 15 s — so a test at 15 s asserts nothing. The test
runs its daemon at a 2 s deadline and asserts under 4.5 s, between the two numbers. Falsified by
removing the Timer: 5.29 s, fails.

The first version of this test asserted 25 s with a 2 s client timeout, and passed with the deadline
neutralised. It was decoration, and the thing that caught it was running it both ways rather than
reading it.
