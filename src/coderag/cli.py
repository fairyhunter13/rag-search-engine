"""The operator surface, and the reason it is not the MCP surface.

Everything here is a thing a human does deliberately: start the daemon, check
it, force a rebuild, list what is registered, install the unit. An agent has no
reason to call any of it, and the old engine's four tools became four because
these leaked into the protocol.

`doctor` inherits exactly one structural check from the old `validate`: orphan
rows in the two virtual tables. That is the check that catches a delete path
which forgot a table, and its symptom is a plausible search result pointing at a
line range that no longer exists.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import (
    config,
    daemon,
    doctor,
    embed,
    federation,
    gpu,
    health,
    index,
    quarantine,
    registry,
    search,
    store,
    systemd,
    trace,
)


def _serve(args) -> int:
    from . import server

    # Before the socket, not inside the lifespan: a raise in there is logged as
    # a failed startup that still leaves the port bound and answering.
    gpu.assert_gpu_available()
    server.serve(args.host, args.port)
    return 0


def _index(args) -> int:
    target = registry.resolve(args.root or Path.cwd())
    registry.claim(target, direct=True)
    members = federation.register(target)
    if args.full:
        # An explicit rebuild is the only thing that empties a store: nothing
        # in the automatic paths deletes chunks it did not diff first.
        for project in [target, *members]:
            conn = store.connect(project)
            index._wipe(conn)
            conn.commit()
    for project in [target, *members]:
        print(json.dumps(index.index_project(project)))
    return 0


def _search(args) -> int:
    root = args.root or Path.cwd()
    try:
        out = daemon.call("search", root, query=args.query, k=args.k, mode=args.mode)
    except daemon.Unreachable:
        # Nothing to share the card with, so the second session this avoids
        # does not exist. Locally is the only way the answer arrives at all.
        out = search.search(args.query, root, k=args.k, mode=args.mode)
    # The tool returns a failed search rather than raising it, and an empty
    # result set that exits 0 is the false zero a caller cannot see.
    if out.get("error"):
        raise search.SearchError(out["error"])
    print(json.dumps(out, indent=2))
    return 0


def _list(_args) -> int:
    for entry in sorted(registry.load().values(), key=lambda e: e.key):
        flags = "".join(("d" if entry.direct else "-", "e" if entry.enabled else "-"))
        print(f"{flags} {entry.file_count:>7} {entry.chunk_count:>8}  {entry.key}")
    return 0


def _forget(args) -> int:
    """The other half of `index` for a caller that creates a project and then
    deletes it. Without it such a row can only be removed once its directory is
    already gone, by which time the hourly sweep has been paging on it.
    """
    dropped, released = registry.forget(args.roots)
    for key in dropped + released:
        # The row leaving used to free nothing: the store stayed until someone
        # ran `doctor --prune`. It goes to quarantine, not to an rmtree.
        moved = quarantine.take(config.index_path(key).parent)
        print(f"forgot {key}" + (" (store quarantined)" if moved else ""))
    for key in sorted({str(registry.resolve(r)) for r in args.roots} - set(dropped)):
        print(f"not registered {key}")
    return 0


def _bridge(args) -> int:
    return bridge_run(args)


def bridge_run(args) -> int:
    from . import bridge

    return bridge.run(args.url, args.idle)


def _install(args) -> int:
    print(f"wrote {systemd.install(enable=not args.no_enable)}")
    return 0


def _trace(args) -> int:
    return trace.render(args.kind, args.n, args.errors)


def _health(args) -> int:
    ok, message = health.check(args.url)
    print(message)
    return 0 if ok else 1


def _release(_args) -> int:
    embed.release_models()
    print("models released")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=config.APP, description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the daemon in the foreground")
    serve.add_argument("--host", default="")
    serve.add_argument("--port", type=int, default=0)
    serve.set_defaults(fn=_serve)

    idx = sub.add_parser("index", help="index a root and its members, synchronously")
    idx.add_argument("root", nargs="?", default="")
    idx.add_argument("--full", action="store_true", help="discard the store and rebuild")
    idx.set_defaults(fn=_index)

    find = sub.add_parser("search", help="one query against a root and its members")
    find.add_argument("query")
    find.add_argument("root", nargs="?", default="")
    find.add_argument("-k", type=int, default=10)
    find.add_argument("--mode", default="hybrid", choices=config.MODES)
    find.set_defaults(fn=_search)

    sub.add_parser("list", help="every registered project").set_defaults(fn=_list)

    drop = sub.add_parser("forget", help="remove the named rows from the registry")
    drop.add_argument("roots", nargs="+")
    drop.set_defaults(fn=_forget)

    doc = sub.add_parser("doctor", help="GPU, missing projects, orphan rows and stores")
    doc.add_argument("--prune", action="store_true", help="delete stores no row claims")
    doc.add_argument("--force", action="store_true", help="prune past the half-the-tree refusal")
    doc.add_argument("--compact", action="store_true", help="VACUUM every store, and nothing else")
    doc.set_defaults(fn=doctor.run)
    sub.add_parser("release", help="unload the models now").set_defaults(fn=_release)

    trace = sub.add_parser(
        "trace", help="the ledgers: what a search, an index pass or the watcher did"
    )
    # `search` is the default, so the invocation that existed before the other
    # three ledgers did still means what it meant.
    trace.add_argument(
        "kind",
        nargs="?",
        default="search",
        choices=["search", "index", "watch", "sweep", "arm", "sched", "reap"],
    )
    trace.add_argument("-n", type=int, default=20)
    trace.add_argument("--errors", action="store_true", help="only the calls that failed")
    trace.set_defaults(fn=_trace)

    checkup = sub.add_parser("health", help="ask the daemon whether the fleet is indexing")
    checkup.add_argument("--url", default="")
    checkup.set_defaults(fn=_health)

    bridge = sub.add_parser("bridge-stdio", help="forward stdio JSON-RPC to the daemon")
    bridge.add_argument("--url", default="")
    bridge.add_argument("--idle", type=float, default=0)
    bridge.set_defaults(fn=_bridge)

    unit = sub.add_parser("install-systemd", help="write and enable the user unit")
    unit.add_argument("--no-enable", action="store_true")
    unit.set_defaults(fn=_install)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (search.SearchError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
