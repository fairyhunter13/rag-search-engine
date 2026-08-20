"""The caller's own workspace, used as the boundary on which root may be named.

Everything downstream of the root argument is already scoped: `search` searches
`federation.expand(root)` and there is no fleet-wide mode at all. The hole is
upstream -- the root is a string the model writes, so any registered project is
reachable from any session by naming it, and the daemon keeps no session of its
own to check it against (`stateless_http=True`).

MCP's `roots` is the one channel that carries the client's workspace without the
model writing it, and the SDK's `ListRoots` resolver marker is excluded from the
LLM-visible schema -- a parameter the model cannot write is a parameter it cannot
get wrong. SEP-2577 deprecated `roots` in the same revision that made it reachable
from a stateless server, with a twelve-month floor and removal eligible no earlier
than 2027-07-28; the named successors are a tool parameter (which is the thing
being fixed) and server configuration (refused: it means a file in ~159 repos).

**Containment, not authorization.** The daemon is localhost and unauthenticated,
so a `curl` with a fabricated roots response defeats this completely. What it
buys is that coderag stops being an easier way out of the workspace than the
tools the client already gates -- which is also the argument for reusing the
client's boundary instead of inventing a second one.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse
from urllib.request import url2pathname

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.resolve import ListRoots, Resolve
from mcp_types import ListRootsResult
from mcp_types.version import is_version_at_least

from . import config, registry

log = logging.getLogger(__name__)

# The revision that carries `roots/list` inside `InputRequiredResult` instead of
# a server-to-client request. Below it a stateless transport is built
# `can_send_request=False` and asking raises, so the ask is gated on the era.
MRTR = "2026-07-28"


class ScopeError(Exception):
    """The named root is outside the caller's workspace."""


# What happened upstream of this request's pin, for the line `enforce` writes.
# Set in `_ask`, read one call later in the same task; a direct call sets
# neither and reads the default, which is the honest answer there.
_ASKED: ContextVar[str] = ContextVar("asked", default="not asked, no resolver ran")


def _client(ctx: Context) -> str:
    """Who is calling, as journald sees it.

    1768 zero-root pins were attributed to nobody because this was never
    logged, so the population that would decide the flag could not be split by
    client. `clientInfo` is optional on 2026-07-28+, hence the fallback.
    """
    params = getattr(ctx.session, "client_params", None)
    info = getattr(params, "clientInfo", None)
    return f"{info.name}/{info.version}" if info else "unidentified"


def _ask(ctx: Context) -> ListRoots | ListRootsResult:
    """Ask only where the answer can arrive; anywhere else, no pin.

    Claude Code advertises `roots` on every transport but negotiates the era
    behind a flag, so the capability alone is not enough to decide this. An
    empty result is not a silent pass: `enforce` refuses it under the flag.
    """
    caps = ctx.client_capabilities
    client = _client(ctx)
    # Which of the three, not how many: 4409 calls logged "0 root(s)" and the
    # count alone cannot say whether the flag is one client setting away,
    # whether the pin can never arrive at all, or whether the client was asked
    # and answered with nothing.
    if caps is None or caps.roots is None:
        _ASKED.set(f"not asked, {client} advertises no roots capability")
        log.info("workspace pin: no ask, client %s advertises no roots capability", client)
        return ListRootsResult(roots=[])
    if not (ctx.protocol_version and is_version_at_least(ctx.protocol_version, MRTR)):
        _ASKED.set(f"not asked, {client} speaks {ctx.protocol_version}")
        log.info(
            "workspace pin: no ask, client %s protocol %s is below %s",
            client,
            ctx.protocol_version,
            MRTR,
        )
        return ListRootsResult(roots=[])
    _ASKED.set(f"asked {client}")
    return ListRoots()


Pinned = Annotated[ListRootsResult, Resolve(_ask)]
"""The tool-parameter annotation. Filled by the framework, invisible to the model."""


def paths(pinned: ListRootsResult) -> list[Path]:
    """Roots as registry keys. Nothing filters the scheme because nothing can
    reach here with another: `Root.uri` is a `FileUrl` and pydantic refuses it."""
    return [registry.resolve(url2pathname(urlparse(str(root.uri)).path)) for root in pinned.roots]


def default_root(pinned: ListRootsResult) -> Path:
    """What `root=""` means, and an error when nothing says.

    The fallback used to be the daemon's cwd, which is `$HOME`: a rootless call
    from a client that sends no roots came back as "call index(root=$HOME)",
    telling the caller to index their entire home directory. Measured against a
    real session, which took the advice.
    """
    roots = paths(pinned)
    if not roots:
        raise ScopeError("no workspace root arrived with this call -- pass root=<project path>")
    return roots[0]


def enforce(target: Path, pinned: ListRootsResult) -> None:
    """Refuse a root the caller's workspace does not contain, or sit inside.

    Both directions. A workspace opened on a subdirectory has to be able to name
    its own project, and the ancestor arm cannot walk out: search still requires
    the row to be registered, enabled and indexed, and `FORBIDDEN_ROOTS` means
    `/` and `$HOME` can never become one.
    """
    roots = paths(pinned)
    # The rollout's only observable: journald is where "a real pin arrived from
    # this profile" is read before the flag flips. Goes when the unit line goes.
    log.info("workspace pin: %d root(s), %s", len(roots), _ASKED.get())
    if not roots:
        if not config.REQUIRE_CLIENT_ROOTS:
            return
        raise ScopeError("the client sent no workspace roots, so no root can be checked against it")
    if any(target.is_relative_to(r) or r.is_relative_to(target) for r in roots):
        return
    raise ScopeError(
        f"{target} is outside this session's workspace -- search reaches the current "
        "project and the projects it federates, nothing else"
    )
