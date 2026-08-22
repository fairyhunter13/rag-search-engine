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
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse
from urllib.request import url2pathname

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.resolve import ListRoots, Resolve
from mcp_types import CLIENT_INFO_META_KEY, Implementation, ListRootsResult
from mcp_types.version import is_version_at_least

from . import config, peers, pinledger, registry

log = logging.getLogger(__name__)

# The revision that carries `roots/list` inside `InputRequiredResult` instead of
# a server-to-client request. Below it a stateless transport is built
# `can_send_request=False` and asking raises, so the ask is gated on the era.
MRTR = "2026-07-28"


class ScopeError(Exception):
    """The named root is outside the caller's workspace."""


@dataclass(frozen=True)
class Verdict:
    """Who called, over which revision, and why the pin came back as it did.

    Built once per `tools/call` and read by both the resolver and the tool body.
    This was a `ContextVar` set in `_ask`, which is why journald read `not
    asked, no resolver ran` on calls where a root had plainly arrived: a sync
    resolver runs through `anyio.to_thread.run_sync`, which copies the context
    and throws the copy away. Resolvers are memoized per call by identity, so
    depending on this one from both sides is the channel the SDK does offer.
    """

    client: str
    proto: str | None
    branch: str
    peer: str

    def line(self, roots: int) -> str:
        return (
            f"workspace pin: client={self.client} proto={self.proto} "
            f"branch={self.branch} roots={roots} peer={self.peer}"
        )


DIRECT = Verdict(client="unidentified", proto=None, branch="direct", peer="unknown")
"""No resolver ran, because no framework call did: tests and the CLI."""


def _client(ctx: Context) -> str:
    """Who is calling, as journald sees it.

    Read from this request's `_meta`, not from a session: 2026-07-28 removed the
    handshake, and this daemon is `stateless_http=True` besides. The previous
    read was `params.clientInfo`, a serialization alias rather than the pydantic
    attribute, so it named nobody on any path -- 197 lines out of 197.

    `_meta` is also the only place a caller that sent no capabilities can be
    named: `Connection.from_envelope` builds `client_params` only when info and
    capabilities both arrived, and that is the branch most in need of a name.
    """
    raw = (getattr(ctx.request_context, "meta", None) or {}).get(CLIENT_INFO_META_KEY)
    if raw is None:
        params = getattr(ctx.session, "client_params", None)
        info = getattr(params, "client_info", None)
    else:
        # camelCase only, the same call the SDK makes: `by_name=True` would
        # accept a snake_case body no client sends.
        info = Implementation.model_validate(raw, by_name=False)
    return f"{info.name}/{info.version}" if info else "unidentified"


def _verdict(ctx: Context) -> Verdict:
    """Which branch the pin takes, decided once.

    Which of the four, not how many: 4409 calls logged "0 root(s)" and the count
    alone cannot say whether the flag is one client setting away, whether the
    pin can never arrive at all, or whether the client was asked and answered
    with nothing.
    """
    caps = ctx.client_capabilities
    proto = ctx.protocol_version
    if not (proto and is_version_at_least(proto, MRTR)):
        # Capabilities are absent below the era because the transport has
        # nowhere to carry them: the legacy stateless path builds every
        # connection `from_envelope(version, None, None)`. Calling that
        # "advertises no roots capability" blamed a client setting for a
        # protocol revision, and no setting would have changed it.
        branch = "legacy-path" if caps is None else "below-era"
    elif caps is None or caps.roots is None:
        branch = "no-caps"
    else:
        branch = "asked"
    return Verdict(client=_client(ctx), proto=proto, branch=branch, peer=peers.of(ctx))


Verdicted = Annotated[Verdict | None, Resolve(_verdict)]
"""The verdict as a parameter, filled by the framework and invisible to the model."""


def _ask(ctx: Context, verdict: Verdicted = None) -> ListRoots | ListRootsResult:
    """Ask only where the answer can arrive; anywhere else, no pin.

    Claude Code advertises `roots` on every transport but negotiates the era
    behind a flag, so the capability alone is not enough to decide this. An
    empty result is not a silent pass: `enforce` refuses it under the flag.
    """
    verdict = verdict or _verdict(ctx)
    if verdict.branch != "asked":
        log.info("%s", verdict.line(0))
        return ListRootsResult(roots=[])
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

    An unregistered pin resolves to the project enclosing it, which `enforce`
    has always sanctioned and nothing ever computed. 14.1% of this machine's
    sessions pin a registered root and 79.0% pin somewhere inside one -- a
    subdirectory, or a worktree under `.claude/worktrees` -- and before this
    every one of the second group was told its own project was not indexed.
    `scripts/reach_census.py` re-derives those shares; do not quote them from
    here.

    `enclosing` and nothing else, because it is the only enabled-filtered
    walk. Short-circuiting on `registry.get(pin)` first read disabled rows too,
    which handed a pin on an unflagged row a root with no store.
    """
    roots = paths(pinned)
    if not roots:
        raise ScopeError("no workspace root arrived with this call -- pass root=<project path>")
    pin = roots[0]
    return registry.enclosing(pin) or pin


def resolution_note(target: Path, pinned: ListRootsResult) -> str:
    """Told to the caller when the search ran somewhere other than their pin.

    Claude Code learns this once per session from a SessionStart hook; every
    other client over `bridge.py` gets no hook at all, so without this the
    upward walk is silent. Keyed on the pin being its own checkout rather than
    on `.claude/worktrees`: git's own marker, not one client's convention.

    Disagreeing roots alone is not the condition. `enclosing` only ever returns
    an ancestor, so it means "the pin is a subdirectory" -- and 12,395 of 12,395
    live upward resolutions are plain subdirectories, whose files the answer
    already holds. Said there, the sentence below is false.
    """
    roots = paths(pinned)
    # A worktree carries `.git` as a file, a nested clone as a directory; both
    # hold content the target's index cannot. A nested clone the target does not
    # gitignore is over-warned, which is the safe direction to be wrong in.
    if not roots or roots[0] == target or not (roots[0] / ".git").exists():
        return ""
    return (
        f"searched {target}, the indexed project containing your workspace {roots[0]}; "
        "results are that checkout's, so edits present only in yours are not in them"
    )


def enforce(target: Path, pinned: ListRootsResult, verdict: Verdict | None = None) -> None:
    """Refuse a root the caller's workspace does not contain, or sit inside.

    Both directions. A workspace opened on a subdirectory has to be able to name
    its own project, and the ancestor arm cannot walk out: search still requires
    the row to be registered, enabled and indexed, and `FORBIDDEN_ROOTS` means
    `/` and `$HOME` can never become one.
    """
    roots = paths(pinned)
    # Kept past the rollout it was built for: the refusal below is now visible
    # breakage, and this is what says which client hit it and why. journald
    # keeps seven days and cannot be grouped by client.
    verdict = verdict or DIRECT
    log.info("%s", verdict.line(len(roots)))
    pinledger.record(verdict, len(roots))
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
