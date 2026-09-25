"""`watchfiles.watch` in its own process, one JSON line per batch on stdout.

`RustNotify` holds the GIL while it arms, and arming walks every directory under
every root. In the daemon that stopped every thread: 110,827 directories held
the GIL for 7.7 s at load 12, and past the 90 s watchdog under load 27-40, so
systemd killed the daemon 7 times in 11 minutes on 2026-09-25. Here the walk
holds this process's GIL, and the daemon only waits on a pipe.

stdin carries the roots as one JSON array. argv carries debounce and timeout in
milliseconds. An empty batch is written on every timeout, so the reader learns
the watches are armed and gets a regular turn to check its own flags. A failure
is written as `{"error": ...}` before exit, because the daemon's journal is where
an arming failure has to be read.
"""

from __future__ import annotations

import contextlib
import json
import sys

from watchfiles import watch


def main() -> int:
    debounce, timeout = int(sys.argv[1]), int(sys.argv[2])
    roots = json.load(sys.stdin)
    try:
        for batch in watch(*roots, debounce=debounce, rust_timeout=timeout, yield_on_timeout=True):
            sys.stdout.write(json.dumps([[int(change), path] for change, path in batch]) + "\n")
            sys.stdout.flush()
    except BrokenPipeError:
        return 0
    except Exception as exc:
        with contextlib.suppress(BrokenPipeError):
            sys.stdout.write(json.dumps({"error": f"{type(exc).__name__}: {exc}"}) + "\n")
            sys.stdout.flush()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
