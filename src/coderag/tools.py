"""Two actions, and the unit of both is the root together with its members.

Not "two tools that happen to have many modes". Everything the old engine
exposed as a third and fourth tool was an operator concern -- reindex, doctor,
list, orphan cleanup -- and operator concerns belong on the CLI, where a human
runs them deliberately and an agent never sees them.

Both tools take `root=""` meaning the caller's own workspace, and neither takes
a list. The federation expansion is the engine's job: a caller that had to name
the members would have to discover them, which is the work this engine exists to
do. The workspace arrives through `scope.Pinned`, which the framework fills and
the model never sees -- so the root a model can write is checked against a root
it cannot.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import config, federation, index, projcfg, registry, scope, search, watch

INSTRUCTIONS = """\
Code retrieval over the current project and the repos it federates.

- `search` is the primary code lookup here: reach for it before grep or a file
  read whenever the question is about behaviour rather than a literal string,
  and for anything that may live in a repo the root federates -- your own tools
  see one working tree, this sees all of them. Fall back to them when a call
  errors or hangs, and quote the error -- the two have different causes.
- `index` flags the current root as indexed and returns immediately; the work
  runs in the background. Call it again to read status -- there is no third
  action and no wait parameter.
- `search` returns ranked LOCATIONS: path, line range and a short preview. Read
  the ranges you want. Pass include_body=True only when you need bodies inline.
- Use mode="lexical" for an exact identifier, signature or error string, and
  mode="semantic" for a question in English. The default fuses both.
- `index` is the fix when `search` says a root is not indexed, and the reply
  names it. Any other project, ask the user first.
"""

mcp = MCPServer(
    name=config.APP,
    version="0.1.0",
    instructions=INSTRUCTIONS,
)


@mcp.tool(
    name="index",
    description="Flag the current root and its federated projects as indexed. "
    "Returns immediately; indexing runs in the background. Call again for status.",
    # Without this the reply carries the payload only as a JSON string inside a
    # text block, which every caller re-parses and no schema covers. It needs
    # the concrete `dict[str, Any]`: a bare `dict` is refused at import.
    structured_output=True,
)
def index_project(pinned: scope.Pinned, root: str = "", enabled: bool = True) -> dict[str, Any]:
    # Pinned here too, or the gate on `search` is a formality: a caller that can
    # index anything can make anything searchable.
    target = registry.resolve(root or scope.default_root(pinned))
    try:
        scope.enforce(target, pinned)
    except scope.ScopeError as exc:
        return {"error": str(exc)}
    if not target.is_dir():
        return {"error": f"{target} is not a directory"}

    if not enabled:
        # Unflagging never deletes an index directory. Both fleet-wide index
        # wipes in this engine's history came from something that deleted store
        # directories on a computed set, so nothing here computes such a set.
        removed = federation.unregister(target)
        registry.set_enabled(target, False)
        # Narrowing is applied when a member joins, so widening has to be
        # submitted when it leaves: nothing else re-walks a released member, and
        # until something does it answers under a root's excludes that no longer
        # apply to it.
        for project in removed:
            # Survivors only. `removed` carries the root itself, and a member
            # claimed by nothing else is out of the registry entirely -- walking
            # it would rebuild a store no search will ever read.
            if project != target and registry.get(project) is not None:
                index.submit(project, reason="index tool")
        index.start_worker()
        watch.rearm()
        # The rows themselves, not a count: the question after a teardown is
        # always *which* ones moved, and `Path` is not JSON.
        return {
            "root": str(target),
            "enabled": False,
            "members_released": [str(p) for p in removed],
        }

    registry.claim(target, direct=True)
    try:
        members = federation.register(target)
    except projcfg.ConfigError as exc:
        # A broken `.coderag.toml` is the caller's own mistake and it has to
        # reach them as something they can act on. Raised, it arrives as an
        # `isError` envelope with no status attached and no record of which
        # project is stuck; recorded, the next `index` call still says it.
        registry.update(target, last_error=str(exc))
        # Not `_status`: it reads the same broken file to compute
        # `suppressed_by_excludes` and would raise on the way out.
        return {"root": str(target), "error": str(exc), "last_error": str(exc)}
    for project in [target, *members]:
        index.submit(project, reason="index tool")
    watch.rearm()
    watch.start()
    index.start_worker()

    return _status(target, members)


def _status(target: Path, members: list[Path]) -> dict[str, Any]:
    entry = registry.get(target)
    roots = list(entry.roots) if entry else []
    out = {
        "root": str(target),
        "members": len(members),
        "roots": roots,
        "indexed": {
            "files": entry.file_count if entry else 0,
            "chunks": entry.chunk_count if entry else 0,
        },
        "suppressed_by_inherited_excludes": index.suppressed_by_excludes(target, tuple(roots)),
        "last_error": entry.last_error if entry else None,
        "watching": watch.watching(),
    }
    return out | index.status()


@mcp.tool(
    name="search",
    # The decision-time text, and the one a model weighs against its own grep.
    # What it returns is not enough on its own: naming what grep cannot do is
    # the part that competes, and on a literal string nothing here wins.
    description="Find code by describing it, across the current root and every project it "
    "federates -- your own tools see one working tree, this sees all of them. Returns ranked "
    "locations (path + line range + preview), not file bodies.",
    structured_output=True,
)
def search_code(
    query: str,
    pinned: scope.Pinned,
    root: str = "",
    k: int = 10,
    mode: str = "hybrid",
    rerank: bool = True,
    path_glob: str | None = None,
    lang: str | None = None,
    max_per_file: int = 2,
    preview_lines: int = 3,
    include_body: bool = False,
) -> dict[str, Any]:
    try:
        target = registry.resolve(root or scope.default_root(pinned))
        scope.enforce(target, pinned)
        return search.search(
            query,
            target,
            k=k,
            mode=mode,
            rerank=rerank,
            path_glob=path_glob,
            lang=lang,
            max_per_file=max_per_file,
            preview_lines=preview_lines,
            include_body=include_body,
        )
    except (search.SearchError, scope.ScopeError) as exc:
        # Returned rather than raised: an error that names what to call next is
        # actionable to an agent, where a transport-level failure is not.
        return {"error": str(exc), "results": []}
