"""Which process is on the other end of this request.

The client names itself in `_meta` and nothing checks that it told the truth --
the SDK's own docstring says never to treat client-supplied input as an identity
assertion. On loopback the kernel already knows the answer, so the rollout's
census does not have to take a client's word for who it is: this is the half of
the attribution that a caller cannot write.

Bounded on purpose. A miss is `unknown`, never an exception and never a retry:
this runs on the request path to decide a log field, not a permission.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

_PROC_NET = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))
# Keyed by the peer's source port, which is unique per live connection. MCP
# clients hold one connection across many calls, so this is a lookup per
# connection rather than per request -- the /proc/*/fd walk below is the reason
# that distinction matters.
_CACHE: dict[int, str] = {}
_CACHE_MAX = 512


def of(ctx) -> str:
    """`pid:comm` for the caller, or `unknown`."""
    request = getattr(getattr(ctx, "request_context", None), "request", None)
    client = getattr(request, "client", None)
    port = getattr(client, "port", None)
    if port is None:
        # stdio, or a direct in-process call: there is no socket to ask about.
        return "unknown"
    if port not in _CACHE:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[port] = _owner(port)
    return _CACHE[port]


def _inode(port: int) -> str | None:
    """The socket inode whose *local* port is `port`.

    Local, not remote: the connection has two ends in this table and the one
    that names the client is the end the client opened.
    """
    want = f"{port:04X}"
    for table in _PROC_NET:
        with contextlib.suppress(OSError):
            for row in table.read_text().splitlines()[1:]:
                fields = row.split()
                if len(fields) > 9 and fields[1].rsplit(":", 1)[-1] == want:
                    return fields[9]
    return None


def _owner(port: int) -> str:
    inode = _inode(port)
    if inode is None:
        return "unknown"
    target = f"socket:[{inode}]"
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        with contextlib.suppress(OSError):
            for fd in (entry / "fd").iterdir():
                if str(fd.readlink()) == target:
                    comm = (entry / "comm").read_text().strip()
                    return f"{entry.name}:{comm}"
    return "unknown"
