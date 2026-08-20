---
type: Defect
resource: src/coderag/server.py, tests/test_restart.py, src/coderag/systemd.py
title: A deliberate stop was indistinguishable from a crash, because the exit that avoids the crash was unreachable
description: "uvicorn waited on MCP's open streams, so systemd SIGKILLed at 90 s and fired OnFailure on every ordinary stop — and the existing restart tests could not see it, because their stop helper kills after 30 s and reports success."
tags: [daemon, shutdown, systemd, mcp, testing]
status: fixed
generated: { by: claude/opus-5, at: 2026-08-20T09:15:00Z }
---

# What happened

`systemctl --user stop coderag` sat in `deactivating` for 90 seconds and then:

```
INFO:     Waiting for connections to close. (CTRL+C to force quit)
coderag.service: State 'stop-sigterm' timed out. Killing.
coderag.service: Main process exited, code=killed, status=9/KILL
coderag.service: Failed with result 'timeout'.
coderag.service: Triggering OnFailure= dependencies.
```

MCP streamable HTTP holds its stream open for the life of the client. uvicorn's graceful shutdown
waits for open connections to close, and these do not, so `uvicorn.run` never returned and
`_shutdown_exit` — the `os._exit(0)` that exists specifically so the CUDA EP's static destructors
cannot abort with 134 — was dead code. The process never chose how it died.

Three costs, and the third is the one that compounds:

- **90 seconds on every stop.** Every card-serial step begins by stopping this daemon.
- **SIGKILL, not the controlled exit.** The one lifecycle fact the module docstring calls
  "bought with an outage" was not in force.
- **`OnFailure` fired on ordinary stops.** The alert unit waits 8 s and re-checks precisely so a
  clean restart does not page; a stop that always ends in `result 'timeout'` pages every time, which
  is how the alert that matters gets muted.

# The fix

`timeout_graceful_shutdown=5` on `uvicorn.run`. uvicorn then returns, `_shutdown_exit` runs, and the
process exits 0 well inside `TimeoutStopSec`.

# Why no test caught it

`Daemon.stop` in the restart tests sends SIGTERM, waits 30 s, then `proc.kill()` — and reports
success either way. Both existing restart tests passed against a daemon that could only be killed.
A helper that recovers from the defect it should expose makes every test built on it blind to it.

The new test drives the process directly rather than through that helper, holds a live MCP stream
open while it signals — without the stream there is nothing for uvicorn to wait on and the defect
does not reproduce — and asserts the exit code is 0, not merely that the process is gone. Removing
`timeout_graceful_shutdown` fails it.

# Not changed

`TimeoutStopSec` stays at the systemd default. Shortening it would convert this class of hang into a
faster SIGKILL, which is the same wrong ending sooner.

# Corrected, 2026-08-20 — the exit was still unreachable

Two claims on this page were wrong, and its own test proved it: run today,
`test_sigterm_exits_promptly_with_a_client_connected` **fails**, `-15 != 0`. It has presumably
never passed.

*"uvicorn then returns, `_shutdown_exit` runs"* — it does not. uvicorn's `capture_signals` collects
the signal it caught, restores the handler it replaced, and then `signal.raise_signal`s it, all
inside `Server.run` and therefore **before** `uvicorn.run` returns. The restored handler is the
default disposition, so the process dies by SIGTERM and the `os._exit(0)` below is dead code — which
means the CUDA-134 protection the module docstring calls a lifecycle fact "bought with an outage"
was not in force either. It happened not to matter: a default-disposition SIGTERM does not run
CPython finalization, so the abort had nothing to abort in. The protection was absent, not
unnecessary.

Nor is this a production failure. systemd's `is_clean_exit` treats a daemon killed by SIGHUP, SIGINT,
SIGTERM or SIGPIPE as clean, so `code=killed, status=15/TERM` fires no `OnFailure`. The journal's
`status=9/KILL` and `result 'timeout'` were a different thing entirely, and this page's diagnosis of
*that* stands.

Fixed by installing our own SIGTERM handler in `serve` before `uvicorn.run`, so that ours is the
handler uvicorn restores and re-raises into. One line, and it is what makes every assertion on this
page and on [[a-cancelled-task-group-cannot-reach-a-shielded-thread]] mean what it says.

*"The new test ... holds a live MCP stream open"* — it holds no stream. `stateless_http=True` means
a fresh transport per request, and `tools/list` runs no user code. See the linked page for the shape
that does reproduce.
