"""CPU cost of one indexing pass, one arm per subprocess.

The measurand is CPU-seconds per unit of indexing work taken **during a real
pass**, not at idle: ORT's intra-op pool spins inside `Run` and parks between
runs, so a harness that watches an idle daemon reports every arm identical and
is believed. A pass is `index.index_project` over a materialized corpus, which
is the real path at the real batch granularity -- a synthetic `session.run`
loop misses the tokenizer, the sqlite commits and the batch shape.

Per-thread accounting rather than a process total, because attributing the cost
to `ort-intra-op-*` versus a rayon worker is the whole difference between
shipping for a reason and shipping by coincidence.

Not a `test_*.py`: every knob here is a module constant read at import, so an
arm has to be its own process. Invocation matches `eval.py`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coderag import config
from eval import materialize
from live import require_clear_gpu

# Env only. The three ORT session knobs were measured here on 2026-08-20, none
# cleared its threshold, and the constants behind them were deleted rather than
# left as unset switches -- so those arms would now set nothing. The numbers are
# in `constraints/the-cpu-side-of-an-indexing-pass-is-already-flat.md`.
ARMS = {
    "+malloc": {"MALLOC_ARENA_MAX": "2"},
    "+rayon": {"RAYON_NUM_THREADS": "4"},
    "baseline": {},
}
REPEATS = 3
MAX_LOAD = 1.0


def _thread_cpu() -> dict[str, float]:
    """utime+stime per thread, keyed by name.

    Split after the last `)`, never on whitespace: `comm` is parenthesised and
    can contain spaces, which puts every field after it at the wrong index.
    """
    ticks = os.sysconf("SC_CLK_TCK")
    out: dict[str, float] = {}
    for task in Path("/proc/self/task").iterdir():
        try:
            stat = (task / "stat").read_text()
        except OSError:
            continue  # a thread that exited mid-walk is not an error
        head, _, rest = stat.rpartition(")")
        name = head.partition("(")[2]
        fields = rest.split()
        out[name] = out.get(name, 0.0) + (int(fields[11]) + int(fields[12])) / ticks
    return out


def _peak_threads(stop: threading.Event, seen: list[int]) -> None:
    while not stop.wait(0.5):
        seen.append(len(list(Path("/proc/self/task").iterdir())))


def _vm_hwm_mb() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) / 1024
    return 0.0


def _worker(project: Path) -> int:
    from coderag import index, registry, store

    corpus = materialize(project, config.STATE_DIR / "corpus")
    registry.claim(corpus, direct=True)
    index.index_project(corpus)  # warm-up, discarded: it pays the model load

    rows = []
    for _ in range(REPEATS):
        # A second pass over the same corpus is a content-hash no-op, so the
        # store has to go before each repetition or every one but the first
        # measures the diff and nothing else. `close_all` first: the cached
        # handle keeps the unlinked file alive, which is what made the first
        # run report three 0.03 s passes over 287 chunks it never built.
        store.close_all()
        shutil.rmtree(config.INDEX_DIR, ignore_errors=True)
        before, peak, stop = _thread_cpu(), [], threading.Event()
        poller = threading.Thread(target=_peak_threads, args=(stop, peak), daemon=True)
        poller.start()
        started = time.perf_counter()
        result = index.index_project(corpus)
        wall = time.perf_counter() - started
        stop.set()
        after = _thread_cpu()
        by_thread = {k: round(v - before.get(k, 0.0), 3) for k, v in after.items()}
        cpu = sum(by_thread.values())
        rows.append(
            {
                "wall": round(wall, 2),
                "cpu_seconds": round(cpu, 2),
                "mean_cores": round(cpu / wall, 2),
                "peak_threads": max(peak or [0]),
                "vm_hwm_mb": round(_vm_hwm_mb()),
                "chunks": result.get("chunks"),
                "by_thread": {k: v for k, v in sorted(by_thread.items()) if v >= 0.05},
            }
        )
    median = {k: statistics.median([r[k] for r in rows]) for k in ("wall", "cpu_seconds")}
    print(json.dumps({"median": median, "repeats": rows}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--arms", default="")
    parser.add_argument("--scratch", type=Path, default=Path("/tmp/coderag-cpu"))
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.worker:
        return _worker(args.project.resolve())

    require_clear_gpu()
    if (load := os.getloadavg()[0]) > MAX_LOAD:
        # A CPU number taken next to a compile is not a number.
        print(f"1-minute load {load:.2f} > {MAX_LOAD}; wait for the machine to go quiet")
        return 1

    args.scratch.mkdir(parents=True, exist_ok=True)
    partial, rows = args.scratch / "results.json", []
    for name in [a for a in (args.arms.split(",") if args.arms else ARMS) if a]:
        env = dict(ARMS[name])
        rows.append(_run(name, args.project.resolve(), args.scratch, env))
        partial.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(json.dumps(rows, indent=2))
    return 0


def _run(name: str, project: Path, scratch: Path, env: dict[str, str]) -> dict:
    """`eval.run_arm`'s shape rather than a call to it: that one prefixes every
    key with `CODERAG_`, and `MALLOC_ARENA_MAX` and `RAYON_NUM_THREADS` are not
    ours to rename."""
    full = os.environ | env | {"CODERAG_STATE_DIR": str(scratch / name.strip("+"))}
    out = subprocess.run(
        [sys.executable, __file__, "--worker", str(project)],
        env=full,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        # Not the last line: ORT logs a recoverable warning after the traceback.
        return {"arm": name, "error": out.stderr.strip().splitlines()[-20:] or ["no output"]}
    return {"arm": name} | json.loads(out.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
