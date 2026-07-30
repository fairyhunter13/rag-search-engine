#!/usr/bin/env python3
"""Prove a suite consolidation did not quietly delete coverage.

Merging N per-test corpora into one shared fixture is the move that makes the live suite fast, and
it is also the move that loses coverage silently: "the suite got faster" and "I removed assertions"
produce the same wall clock. Nothing in a pytest run distinguishes them.

So the gate is a **set difference over covered lines**, not a percentage. A percentage is the wrong
instrument on purpose — it holds flat, or even rises, while the covered lines move underneath it:
drop a 40-line module from the suite and add a 40-line one and the number does not budge. What has
to be true after a conversion is that every line covered before is still covered, which is a
superset claim about a set, so that is what this measures.

    scripts/coverage_gate.py snapshot before.json -- -m "live and not costly and not exclusive"
    ...convert one file...
    scripts/coverage_gate.py snapshot after.json  -- -m "live and not costly and not exclusive"
    scripts/coverage_gate.py compare before.json after.json

`compare` exits non-zero naming the lost lines. Gained lines are reported and never fail — 7d adds
coverage deliberately.

This wraps pytest from the outside and is imported by no test, so it is invisible to the no-mock
invariant (`test_no_mocks_or_fakes.py`) that the rest of this work is bounded by.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
PKG = SRC / "rag_search"


def _ensure_subprocess_hook() -> Path:
    """Install coverage's documented `.pth` startup hook into the dev venv, once.

    Without this the gate is not merely incomplete, it is **inverted**. Measured before adding it:
    a run of `test_reconcile_order.py` — three passing tests whose entire subject is
    `sweeps.reconcile_order` — recorded *zero* covered lines in `sweeps.py`, because the tests drive
    it in a child interpreter and plain `coverage run` does not follow one.

    That blind spot lands exactly on the tests that matter most here. This plan's own sufficiency
    rule is "demonstrate red in a subprocess, never in-process", so every R1-R5 guard, the whole
    blast-radius series, runs in a child. A gate that cannot see them would hand a clean bill of
    health to a conversion that deleted all of them — the precise failure it exists to catch.

    The hook is inert unless `COVERAGE_PROCESS_START` is set, so it costs nothing outside this run.
    """
    import sysconfig
    site = Path(sysconfig.get_paths()["purelib"])
    pth = site / "rse-coverage-subprocess.pth"
    if not pth.exists():
        pth.write_text("import coverage; coverage.process_startup()\n")
    return pth


def _snapshot(out_path: Path, pytest_args: list[str]) -> int:
    """Run the suite under coverage — children included — and write {file: sorted[line]}."""
    import os
    data_file = out_path.with_suffix(".coverage-data")
    for stale in data_file.parent.glob(data_file.name + "*"):
        stale.unlink()
    _ensure_subprocess_hook()
    rc = out_path.with_suffix(".coveragerc")
    rc.write_text(f"[run]\nparallel = true\nsource = {PKG}\ndata_file = {data_file}\n")
    py = str(REPO / ".venv" / "bin" / "python")
    run = subprocess.run(
        [py, "-m", "coverage", "run", f"--rcfile={rc}", "-m", "pytest", *pytest_args],
        cwd=REPO, env={**os.environ, "COVERAGE_PROCESS_START": str(rc)},
    )
    # Deliberately not gated on run.returncode. A red suite still produces a real covered-line set,
    # and refusing to record one would make the gate unusable exactly when a conversion is in
    # progress and something is failing. The exit code is reported and carried, not obeyed.
    subprocess.run([py, "-m", "coverage", "combine", f"--rcfile={rc}"], cwd=REPO,
                   capture_output=True)
    import coverage
    cov = coverage.Coverage(data_file=str(data_file))
    cov.load()
    data = cov.get_data()
    covered: dict[str, list[int]] = {}
    for f in data.measured_files():
        try:
            rel = str(Path(f).resolve().relative_to(REPO))
        except ValueError:
            continue  # site-packages and stdlib: not ours to hold a line on
        lines = data.lines(f) or []
        if lines:
            covered[rel] = sorted(lines)
    total = sum(len(v) for v in covered.values())
    out_path.write_text(json.dumps(
        {"pytest_args": pytest_args, "pytest_returncode": run.returncode,
         "files": len(covered), "covered_lines": total, "covered": covered},
        indent=1, sort_keys=True))
    print(f"wrote {out_path}: {total} covered lines across {len(covered)} files "
          f"(pytest exit {run.returncode})")
    return 0


def _compare(before_path: Path, after_path: Path) -> int:
    before = json.loads(before_path.read_text())
    after = json.loads(after_path.read_text())

    def pairs(snap: dict) -> set[tuple[str, int]]:
        return {(f, n) for f, lines in snap["covered"].items() for n in lines}

    b, a = pairs(before), pairs(after)
    lost, gained = sorted(b - a), sorted(a - b)
    print(f"before: {len(b)} covered lines   after: {len(a)}   "
          f"lost: {len(lost)}   gained: {len(gained)}")
    if gained:
        by_file: dict[str, int] = {}
        for f, _ in gained:
            by_file[f] = by_file.get(f, 0) + 1
        print("\ngained (not a failure — 7d adds coverage on purpose):")
        for f, n in sorted(by_file.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  +{n:5d}  {f}")
    if not lost:
        print("\nOK: the after-set is a superset. No line lost coverage.")
        return 0
    by_file = {}
    for f, _ in lost:
        by_file[f] = by_file.get(f, 0) + 1
    print(f"\nFAIL: {len(lost)} line(s) covered before are not covered now.")
    for f, n in sorted(by_file.items(), key=lambda kv: -kv[1]):
        nums = [str(n2) for f2, n2 in lost if f2 == f][:12]
        print(f"  -{n:5d}  {f}: {', '.join(nums)}{' ...' if n > 12 else ''}")
    print("\nA conversion may merge setup. It may not merge assertions.")
    return 1


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] == "snapshot":
        out = Path(argv[1])
        rest = argv[2:]
        if rest and rest[0] == "--":
            rest = rest[1:]
        if not rest:
            print("snapshot needs pytest args after --", file=sys.stderr)
            return 2
        return _snapshot(out, rest)
    if len(argv) == 3 and argv[0] == "compare":
        return _compare(Path(argv[1]), Path(argv[2]))
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
