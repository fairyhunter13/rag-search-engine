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

import logging
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import (
    config, federation, index, projcfg, quiet, registry, scope, search, searchledger, watch,
)

log = logging.getLogger(__name__)

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
- `search` takes `queries` for up to five questions in one call. A round trip is
  charged the whole resident context, so ask the questions you already have
  together rather than one per call. The reply carries one answer per question,
  in order.
- `index` is the fix when `search` says a root is not indexed, and the reply
  names it. Any other project, ask the user first.
- A client that defers a tool schema fails the first call. Where yours does,
  `ToolSearch` on `select:mcp__coderag__search` loads it, and that load is the
  first half of the call.
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
def index_project(
    pinned: scope.Pinned,
    root: str = "",
    enabled: bool = True,
    verdict: scope.Verdicted = None,
) -> dict[str, Any]:
    # Pinned, and `search` is not. Enrolling a root is fleet work -- an hourly
    # reconcile and an inotify arm on every file -- where reading one is a query.
    try:
        target = registry.resolve(root or scope.default_root(pinned))
        scope.enforce(target, pinned, verdict)
    except scope.ScopeError as exc:
        return {"error": str(exc)}
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
        watch.rearm_if_changed()
        # The rows themselves, not a count: the question after a teardown is
        # always *which* ones moved, and `Path` is not JSON.
        return {
            "root": str(target),
            "enabled": False,
            "members_released": [str(p) for p in removed],
        }

    return enroll(target)


def enroll(target: Path) -> dict[str, Any]:
    """The half of `index` that runs after the scope check.

    Shared with the daemon's `/register` route, whose caller is a SessionStart
    hook standing in the directory rather than a model naming one.
    """
    # After the unflag branch, not before it: a project whose directory has been
    # deleted is exactly the row an operator most needs to turn off, and gating
    # the whole tool on `is_dir` left two of them stuck enabled forever, retried
    # and logged as a failure at every start.
    if not target.is_dir():
        return {"error": f"{target} is not a directory"}

    try:
        # No claim before this line: `register` claims the root itself once the
        # config parses, and claiming first left a row for a project that can
        # never index -- which reconcile then retries at every start.
        members = federation.register(target)
    except projcfg.ConfigError as exc:
        # A broken `.coderag.yaml` is the caller's own mistake and it has to
        # reach them as something they can act on. Raised, it arrives as an
        # `isError` envelope with no status attached and no record of which
        # project is stuck; recorded, the next `index` call still says it.
        registry.record_error(target, str(exc))
        # Not `_status`: it reads the same broken file to compute
        # `suppressed_by_excludes` and would raise on the way out.
        return {"root": str(target), "error": str(exc), "last_error": str(exc)}
    for project in [target, *members]:
        index.submit(project, reason="index tool")
    watch.rearm_if_changed()
    watch.start()
    index.start_worker()

    return _status(target, members)


def _pending(unit: set[Path]) -> int:
    """Walks queued or held for this unit, where `queue_depth` is the fleet's.

    Here rather than in `index`, which owns the queue and does not read it. The
    held half counts: a watch job waits out the quiet window off the queue, and
    reporting 0 there is "I saved a file and nothing happened".
    """
    with index._queue.mutex:  # noqa: SLF001
        queued = {j.project for j in index._queue.queue if j is not None and j.project in unit}
    return len(queued | (quiet.projects() & unit))


def _status(target: Path, members: list[Path]) -> dict[str, Any]:
    entry = registry.get(target)
    roots = list(entry.roots) if entry else []
    # The unit is the root together with its members, so the counts are too. The
    # root's own row is 33,053 chunks of the 185,453 this project answers from,
    # and reporting it alone told a caller its project was 17.8% built.
    rows = registry.load()
    unit = [row for p in (target, *members) if (row := rows.get(str(p))) and row.enabled]
    out = {
        "root": str(target),
        "members": len(members),
        "roots": roots,
        "indexed": {
            "files": sum(row.file_count for row in unit),
            "chunks": sum(row.chunk_count for row in unit),
            "projects": len(unit),
        },
        # Per project, because that is the grain the store, the watcher and the
        # queue all work at, and one stuck member is invisible in any total.
        "root_indexed": {
            "files": entry.file_count if entry else 0,
            "chunks": entry.chunk_count if entry else 0,
        },
        "pending": _pending({row.path for row in unit}),
        "members_watching": sum(1 for row in unit if row.path != target and watch.armed(row.path)),
        "member_errors": [
            {"project": str(row.path), "error": row.last_error}
            for row in unit
            if row.path != target and row.last_error
        ],
        "suppressed_by_inherited_excludes": index.suppressed_by_excludes(target, tuple(roots)),
        "last_error": entry.last_error if entry else None,
        # Durable: last_error is cleared by the next success, so on an hourly reconcile
        # these are the only trace a failure that resolved itself ever leaves.
        "last_error_at": entry.last_error_at if entry else None,
        "error_total": entry.error_total if entry else 0,
        "watching": watch.armed(target),
    }
    return out | index.status()


def _batched(answers: list[dict[str, Any]], note: str) -> dict[str, Any]:
    """One envelope for N questions, with the fields that do not vary hoisted out.

    `mode` and `searched` are the same for every question in one call, so they
    are hoisted and only the four per-answer keys repeat.
    """
    keys = ("query", "reranked", "results", "hint")
    return {
        "mode": answers[0]["mode"],
        "searched": answers[0]["searched"],
        "took_ms": round(sum(a["took_ms"] for a in answers), 2),
        "answers": [{k: a[k] for k in keys} for a in answers],
        "hint": note,
    }


@mcp.tool(
    name="search",
    # The decision-time text, and the one a model weighs against its own grep.
    # What it returns is not enough on its own: naming what grep cannot do is
    # the part that competes, and on a literal string nothing here wins.
    description="Find code by describing it, across the current root and every project it "
    "federates -- your own tools see one working tree, this sees all of them. Returns ranked "
    "locations (path + line range + preview), not file bodies. Pass `query`, or `queries` to ask "
    "up to five questions in one call -- a round trip is charged the whole resident context, so "
    "three questions batched cost about a third of three separate calls.",
    structured_output=True,
)
def search_code(
    query: str = "",
    pinned: scope.Pinned = None,
    root: str = "",
    k: int = 10,
    mode: str = "hybrid",
    rerank: bool = True,
    path_glob: str | None = None,
    lang: str | None = None,
    max_per_file: int = 2,
    preview_lines: int = 3,
    include_body: bool = False,
    queries: list[str] | None = None,
    verdict: scope.Verdicted = None,
) -> dict[str, Any]:
    trace = searchledger.trace_id()
    seen = scope.observe(pinned, verdict)
    # `queries` replaces `query` when given, and the reply keeps the question order.
    asked = [q for q in (queries if queries is not None else [query]) if q.strip()]
    base = {
        "trace": trace, "client": (verdict or scope.DIRECT).client,
        "peer": (verdict or scope.DIRECT).peer, "asked": root, "pinned": len(seen),
        "mode": mode, "k": k, "glob": path_glob or "", "lang": lang or "",
    }
    try:
        # No `enforce`. The registry is the boundary: `search` refuses a row that
        # is not registered, enabled and indexed, and `FORBIDDEN_ROOTS` keeps `/`
        # and `$HOME` from ever being one. The pin never was authorization -- this
        # daemon is localhost and unauthenticated -- and it had already stopped
        # being containment, because a member's unit answers from every project
        # its root claims, none of which the caller named. `require_pin` is the
        # half that survives: a client declaring no workspace is still refused.
        scope.require_pin(seen)
        if not asked:
            raise search.SearchError("query is empty")
        if len(asked) > config.MAX_QUERIES:
            raise search.SearchError(
                f"at most {config.MAX_QUERIES} queries in one call, not {len(asked)}")
        target = registry.resolve(root or scope.default_root(pinned))
        base["root"] = str(target)
        answers = []
        for question in asked:
            row = dict(base)
            out = search.search(question, target, k=k, mode=mode, rerank=rerank,
                                path_glob=path_glob, lang=lang, max_per_file=max_per_file,
                                preview_lines=preview_lines, include_body=include_body)
            # The stages are evidence, not an answer. A model that reads them spends
            # context on pool sizes it cannot act on.
            row |= out.pop("trace", {})
            row["took_ms"] = out["took_ms"]
            # One row per search, never per call, so a batched question stays countable.
            searchledger.record(row)
            answers.append(out)
        note = scope.resolution_note(target, pinned)
        if queries is None:
            # `hint` is the reply's existing advisory slot, so this follows prior
            # art rather than adding a key; both notes can be true at once.
            answers[0]["hint"] = "; ".join(filter(None, (answers[0]["hint"], note)))
            return answers[0]
        return _batched(answers, note)
    except (search.SearchError, scope.ScopeError) as exc:
        # A failed search wrote nothing anywhere until now, so the one call a
        # reader most wants to find was the one call leaving no trace.
        base["error"] = f"{type(exc).__name__}: {exc}"
        searchledger.record(base)
        log.warning("search %s failed: %s", trace, exc)
        # Returned rather than raised: an error that names what to call next is
        # actionable to an agent, where a transport-level failure is not.
        return {"error": f"{exc} [trace {trace}]", "results": []}
