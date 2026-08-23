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
import shutil
import sys
from pathlib import Path

from . import config, embed, federation, gpu, health, index, registry, search, store, systemd


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
    out = search.search(args.query, args.root or Path.cwd(), k=args.k, mode=args.mode)
    print(json.dumps(out, indent=2))
    return 0


def _list(_args) -> int:
    for entry in sorted(registry.load().values(), key=lambda e: e.key):
        flags = "".join(("d" if entry.direct else "-", "e" if entry.enabled else "-"))
        print(f"{flags} {entry.file_count:>7} {entry.chunk_count:>8}  {entry.key}")
    return 0


def _doctor(args) -> int:
    problems = 0
    print(f"gpu: {gpu.providers()[0]}, {gpu.free_vram_bytes() // 2**20} MiB free")
    for entry in registry.enabled_projects():
        if not entry.path.is_dir():
            print(f"MISSING {entry.key}")
            problems += 1
            continue
        try:
            counts = store.orphans(store.connect(entry.path, create=False))
        except FileNotFoundError:
            print(f"unindexed {entry.key}")
            continue
        if any(counts.values()):
            print(f"ORPHANS {entry.key}: {counts}")
            problems += 1

    # The other direction, and against every row rather than the enabled ones:
    # unflagging keeps the store, so a disabled row's directory is claimed. What
    # is left over is a store whose row is gone -- 143 of them, 0.46 GiB, that a
    # row-driven walk could not see because there was no row to start from.
    if not getattr(args, "prune", False):
        for found in registry.unclaimed_stores():
            print(f"UNCLAIMED {found.name}: {_size_mib(found)} MiB")
            problems += 1
        print(f"{problems} problem(s)")
        return 1 if problems else 0

    pruned = freed = 0
    with registry.prunable_stores() as (candidates, busy):
        # Deleting inside the lock is the point: no row can appear for one of
        # these between the walk that named them and the rmtree that removes it.
        for found in candidates:
            freed += _size_mib(found)
            shutil.rmtree(found)
            pruned += 1
            print(f"pruned {found.name}")
        for found in busy:
            print(f"BUSY {found.name}: written to within {config.PRUNE_MIN_IDLE_S}s, kept")
            problems += 1
    print(f"{problems} problem(s), pruned {pruned} store(s), {freed} MiB")
    return 1 if problems else 0


def _size_mib(store_dir: Path) -> int:
    return sum(f.stat().st_size for f in store_dir.rglob("*") if f.is_file()) // 2**20


def _bridge(args) -> int:
    return bridge_run(args)


def bridge_run(args) -> int:
    from . import bridge

    return bridge.run(args.url, args.idle)


def _install(args) -> int:
    print(f"wrote {systemd.install(enable=not args.no_enable)}")
    return 0


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
    doctor = sub.add_parser("doctor", help="GPU, missing projects, orphan rows and stores")
    doctor.add_argument("--prune", action="store_true", help="delete stores no row claims")
    doctor.set_defaults(fn=_doctor)
    sub.add_parser("release", help="unload the models now").set_defaults(fn=_release)

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
