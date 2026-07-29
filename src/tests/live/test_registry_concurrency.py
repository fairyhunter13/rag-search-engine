"""RG1: concurrent registry writers must not lose each other's rows.

`upsert_project` read the registry with `_load()` and wrote it with `_save()`, and the
exclusive flock lived *inside* `_save`. So every mutation was a read-modify-write whose
read was unsynchronised — a textbook lost update. RSE runs at least three writers against
that one file (the daemon's reconcile loop, the HTTP/MCP server, the CLI), so the losing
interleave is not a thought experiment: it cost two suite failures on 2026-07-29, both of
the shape "the project I just registered is not in the registry", and both green when the
test was run on its own.

A dropped row is not cosmetic. An unregistered project is not watched and not reconciled,
so it stops being maintained and nothing reports it — the same silent-staleness failure
the federation-root carve-out caused, arriving by a different route.

Why subprocesses rather than threads: `REGISTRY_PATH` is bound at import time from the
environment, so a child launched with `RSE_REGISTRY_PATH` is the only way to run the real
writer against a registry that is not the live fleet's. It is also the honest shape — the
racing writers in production are separate processes, not threads.

Discrimination, per [[feedback_guard_tests_must_discriminate]]: the gate demands that every
one of N distinct paths survives N writes made with no coordination. The tempting weaker
assertion — "the file still parses as JSON" — passes on the defect, because a lost update
leaves a perfectly well-formed file with rows missing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.live

_WORKERS = 6
_PER_WORKER = 30

_WRITER = """
import sys
from rag_search.core.config import ProjectEntry
from rag_search.core.registry import upsert_project
base, lo, hi = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
for i in range(lo, hi):
    upsert_project(ProjectEntry(path="%s/p%03d" % (base, i), enabled=True))
"""


def test_rg1_concurrent_upserts_lose_no_rows(safe_tmp_path):
    reg = safe_tmp_path / "projects.json"
    env = {**os.environ, "RSE_REGISTRY_PATH": str(reg)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER, str(safe_tmp_path),
             str(w * _PER_WORKER), str((w + 1) * _PER_WORKER)],
            env=env, stderr=subprocess.PIPE, text=True,
        )
        for w in range(_WORKERS)
    ]
    for p in procs:
        _, err = p.communicate(timeout=180)
        assert p.returncode == 0, f"RG1 writer failed: {err}"

    # Count rows straight off the file rather than through the read API, whose `_migrate`
    # prunes paths that don't exist on disk — that would hide the very rows this gate counts.
    written = set(json.loads(reg.read_text()))
    expected = {f"{safe_tmp_path}/p{i:03d}" for i in range(_WORKERS * _PER_WORKER)}
    missing = expected - written
    assert not missing, (
        f"RG1: {len(missing)} of {len(expected)} registrations were lost by concurrent "
        f"writers — the registry read-modify-write is not under the lock that guards its "
        f"write. e.g. {sorted(missing)[:3]}"
    )
