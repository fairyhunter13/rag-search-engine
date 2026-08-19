"""Two actions, and the unit of both is the root together with its members.

Not "two tools that happen to have many modes". Everything the old engine
exposed as a third and fourth tool was an operator concern -- reindex, doctor,
list, orphan cleanup -- and operator concerns belong on the CLI, where a human
runs them deliberately and an agent never sees them.

Both tools take `root=""` meaning the session's cwd, and neither takes a list.
The federation expansion is the engine's job: a caller that had to name the
members would have to discover them, which is the work this engine exists to do.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import config, federation, index, registry, search, watch

INSTRUCTIONS = """\
Code retrieval over the current project and the repos it federates.

- `index` flags the current root as indexed and returns immediately; the work
  runs in the background. Call it again to read status -- there is no third
  action and no wait parameter.
- `search` returns ranked LOCATIONS: path, line range and a short preview. Read
  the ranges you want. Pass include_body=True only when you need bodies inline.
- Use mode="lexical" for an exact identifier, signature or error string, and
  mode="semantic" for a question in English. The default fuses both.
- Never index a project the user did not ask you to index.
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
)
def index_project(root: str = "", enabled: bool = True) -> dict:
    target = registry.resolve(root or Path.cwd())
    if not target.is_dir():
        return {"error": f"{target} is not a directory"}

    if not enabled:
        # Unflagging never deletes an index directory. Both fleet-wide index
        # wipes in this engine's history came from something that deleted store
        # directories on a computed set, so nothing here computes such a set.
        removed = federation.unregister(target)
        registry.set_enabled(target, False)
        watch.rearm()
        return {"root": str(target), "enabled": False, "members_released": removed}

    registry.claim(target, direct=True)
    members = federation.register(target)
    for project in [target, *members]:
        index.submit(project, reason="index tool")
    watch.rearm()
    watch.start()
    index.start_worker()

    return _status(target, members)


def _status(target: Path, members: list[Path]) -> dict:
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
    description="Search the current root and its federated projects. Returns ranked "
    "locations (path + line range + preview), not file bodies.",
)
def search_code(
    query: str,
    root: str = "",
    k: int = 10,
    mode: str = "hybrid",
    rerank: bool = True,
    path_glob: str | None = None,
    lang: str | None = None,
    max_per_file: int = 2,
    preview_lines: int = 3,
    include_body: bool = False,
) -> dict:
    try:
        return search.search(
            query,
            root,
            k=k,
            mode=mode,
            rerank=rerank,
            path_glob=path_glob,
            lang=lang,
            max_per_file=max_per_file,
            preview_lines=preview_lines,
            include_body=include_body,
        )
    except search.SearchError as exc:
        # Returned rather than raised: an error that names what to call next is
        # actionable to an agent, where a transport-level failure is not.
        return {"error": str(exc), "results": []}
