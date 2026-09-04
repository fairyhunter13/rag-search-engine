---
type: Defect
resource: src/coderag/server.py, tests/test_server_tools.py
title: The shutdown deadline killed the pytest process at exit 0, so a failing suite reported success
description: "`lifespan`'s finally arms `threading.Timer(15, _shutdown_exit)` and nothing cancels it. `tests/test_server_tools.py` runs that lifespan in-process four times, so 15 s later a timer thread called `os._exit(0)` inside pytest. The run died wherever it stood -- measured at 619, 639 and 74,747 bytes of log across three runs -- with no summary line and exit 0, which reads as a pass. `strace -k` named the caller. The fix arms the timer only in a process `serve` started, and the negative arm fails the predecessor 1 against 0."
tags: [testing, shutdown, threads, guards, silent-green]
status: stable
generated: { by: claude/opus-5, at: 2026-09-04T16:30:00Z }
sources:
  - id: deadline
    resource: src/coderag/server.py
  - id: negative-arm
    resource: tests/test_server_tools.py
---

# What happened

`uv run pytest -q` wrote a log holding three `F` marks, no summary line, and returned **0**. Those
three cannot all be true, so the verdict was refused and the suite was re-run another way. The
re-run was honest, and the question of what the first run did was left open.

It was not `uv`, and it was not the machine. The pytest process really did exit 0, because
something inside it called `os._exit(0)`.

# The timer

`server.lifespan` ends with a hard deadline on shutdown:

```python
deadline = threading.Timer(config.SHUTDOWN_DEADLINE_S, _shutdown_exit, [True])
deadline.daemon = True
deadline.start()
```

`SHUTDOWN_DEADLINE_S` is 15 and `_shutdown_exit` ends in `os._exit(0)`. For the daemon that is
right, and the comment above it defends the placement: the window that hangs is the session
manager's `__aexit__`, which runs after the lifespan, so the timer has to be armed before the
lifespan returns and cannot be cancelled inside it.

Nothing else cancels it either. In the daemon that costs nothing, because `serve` exits the process
anyway. `tests/test_server_tools.py` runs the same lifespan **in the pytest process** four times --
three `TestClient(server.build_app())` blocks and one `async with server.lifespan(None)`. Each exit
arms another 15-second timer inside pytest, and 15 seconds later one of them fires.

# Why every symptom follows from one line

- **Exit 0** is the literal in `_shutdown_exit`. The run never reached the code that would have
  returned 1.
- **The log stops mid-line.** `_shutdown_exit` calls `os.fsync(1)` and `os.fsync(2)`, which flush
  the kernel's copy of the file and not Python's own buffer. Whatever sat in that buffer is lost,
  which is why the last line has no newline.
- **The death point moves.** It lands 15 s after the last lifespan teardown, and that is a
  different place in a 5-minute run every time.
- **It never showed in a small run.** One test, or one file, finishes before the timer fires.

# How it was named

`strace -f -k -e trace=exit_group` on the whole run. One thread, not the main thread, reaching
`_exit` through CPython's `posixmodule`:

```
1897677 exit_group(0)
 > libc(_exit+0x1d)
 > python3.12(PyOS_AfterFork_Child+0x80c5)
 ...
 > python3.12(PyThread_start_new_thread+0x10c)
 > libc(__clone+0x24c)
```

Then reproduced directly, with one 25-second test placed after the lifespan file:

| run | exit | log bytes | last line |
|---|---|---|---|
| the slow test alone | 0 | full | `1 passed in 25.00s` |
| `test_server_tools.py`, then the slow test | 0 | **53** | a row of progress dots |
| the same, after the fix | 0 | full | `56 passed in 29.13s` |

Three full-suite runs died at 619, 639 and 74,747 bytes. The largest had already printed the whole
`FAILURES` section and all five warning blocks before it stopped mid-word.

# The fix

A module flag `serve` sets, read where the timer is armed. The timer stays exactly where its
comment defends it.

```python
if _serving:
    deadline = threading.Timer(config.SHUTDOWN_DEADLINE_S, _shutdown_exit, [True])
```

The deadline bounds `uvicorn.run`. A process that never called `serve` has no uvicorn to hang, so
it has nothing to bound and a process to lose.

# Why it survived

Nothing asserted on the arming. The new test parametrizes both arms and counts the `Timer`
constructions, and it was run against the predecessor first:

| arm | predecessor | fixed |
|---|---|---|
| not serving | **fails, `assert 1 == 0`** | passes |
| serving | passes | passes |

The failing arm names the timer it caught: `(15, _shutdown_exit, [True])`. The serving arm passes
on the predecessor on purpose -- it is a control against a repair that deletes the timer instead of
guarding it.

`monkeypatch.setattr(..., raising=False)` is deliberate. Without it the predecessor fails on a
missing attribute rather than on the count, and a red for the wrong reason proves nothing.

# What this invalidates

Every green full-suite verdict taken in a process that ran `tests/test_server_tools.py` is
unsound, because the run could have died at exit 0 before writing its summary. A green tick is not
the evidence here; **a summary line is**. The suite was re-run after the fix and the failures it
reports are the first ones this repo can trust from a whole-suite run.

Two things this does not explain. `tests/test_restart.py` and `tests/test_live_federation.py` fail
under full-suite load and pass alone, and both are real assertion failures with real numbers --
`assert 240 <= (240 - 128)` and a 5.79 s index return against a 1.0 s budget. Those are load, not
this.
