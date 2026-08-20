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
