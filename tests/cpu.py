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
from live import held_mib, require_clear_gpu

# Env only. The three ORT session knobs were measured here on 2026-08-20, none
# cleared its threshold, and the constants behind them were deleted rather than
# left as unset switches -- so those arms would now set nothing. The numbers are
# in `constraints/the-cpu-side-of-an-indexing-pass-is-already-flat.md`.
ARMS = {
    "baseline": {},
    "+malloc": {"MALLOC_ARENA_MAX": "2"},
    "+rayon": {"RAYON_NUM_THREADS": "4"},
    # Not `+malloc` under another name. That one caps glibc's arena count and
    # cost 27% more CPU here. This replaces the allocator, and its background
    # thread purges dirty pages without anyone calling `malloc_trim`.
    "+jemalloc": {
        "LD_PRELOAD": "/usr/lib/x86_64-linux-gnu/libjemalloc.so.2",
        "MALLOC_CONF": "background_thread:true,dirty_decay_ms:5000",
    },
    # `gpu.adaptive_batch` returns its ceiling on any card with more than ~786
    # MiB free, so the batch is a constant and the arena grows to fit it. That
    # ceiling was 128 when this swept, and the sweep is why it is now 32.
    "+batch64": {"CODERAG_EMBED_BATCH": "64"},
    "+batch32": {"CODERAG_EMBED_BATCH": "32"},
    "+batch16": {"CODERAG_EMBED_BATCH": "16"},
}

# Written before the run, because a threshold chosen after one is a story. One
# rule reads both directions: a negative limit is a drop the arm has to reach,
# a positive one is a rise it may not pass, and `change <= limit` says each.
# `wall` at +11.1% is throughput at 90% of baseline, which is the gate agreed
# for the VRAM sweep.
THRESHOLDS = {
    "+jemalloc": {"rss_anon_mb": -25.0, "cpu_seconds": 5.0},
    "+batch64": {"vram_mib": -50.0, "wall": 11.1},
    "+batch32": {"vram_mib": -50.0, "wall": 11.1},
    "+batch16": {"vram_mib": -50.0, "wall": 11.1},
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


def _status_mb(field: str) -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(f"{field}:"):
            return int(line.split()[1]) / 1024
    return 0.0


def _worker(project: Path) -> int:
    from coderag import conns, index, registry

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
        conns.close_all()
        shutil.rmtree(config.INDEX_DIR, ignore_errors=True)
        before, peak, stop = _thread_cpu(), [], threading.Event()
        poller = threading.Thread(target=_peak_threads, args=(stop, peak), daemon=True)
        poller.start()
        started = time.perf_counter()
        result = index.index_project(corpus)
        wall = time.perf_counter() - started
        stop.set()
        after = _thread_cpu()
        # Read after the CPU accounting closes, never during it: an nvidia-smi
        # spawn every 0.5 s would add ~5% to a 3.12 CPU-s pass, against a best
        # inter-arm delta of 0.19. Sampling costs nothing here because the ORT
        # arena never shrinks, so the value at the end is the peak.
        vram = held_mib([str(os.getpid())])
        by_thread = {k: round(v - before.get(k, 0.0), 3) for k, v in after.items()}
        cpu = sum(by_thread.values())
        rows.append(
            {
                "wall": round(wall, 2),
                "cpu_seconds": round(cpu, 2),
                "mean_cores": round(cpu / wall, 2),
                "peak_threads": max(peak or [0]),
                "vram_mib": vram,
                "rss_anon_mb": round(_status_mb("RssAnon")),
                "vm_hwm_mb": round(_status_mb("VmHWM")),
                "chunks": result.get("chunks"),
                "by_thread": {k: v for k, v in sorted(by_thread.items()) if v >= 0.05},
            }
        )
    keys = ("wall", "cpu_seconds", "vram_mib", "rss_anon_mb", "vm_hwm_mb")
    median = {k: statistics.median([r[k] for r in rows]) for k in keys}
    print(json.dumps({"median": median, "repeats": rows}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--arms", default="")
    parser.add_argument("--scratch", type=Path, default=Path("/tmp/coderag-cpu"))
    # The gate refuses rather than warns, because a CPU number taken next to a
    # compile is not a number. Raising it is allowed and it is never silent: the
    # load lands in every arm's row, so a reader sees what the figure cost.
    parser.add_argument("--max-load", type=float, default=MAX_LOAD)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.worker:
        return _worker(args.project.resolve())

    require_clear_gpu()
    if (load := os.getloadavg()[0]) > args.max_load:
        print(f"1-minute load {load:.2f} > {args.max_load}; wait for the machine to go quiet")
        return 1

    args.scratch.mkdir(parents=True, exist_ok=True)
    # Baseline first and always. A threshold needs a before-number from the same
    # sitting: the baseline's own three passes spread 0.09 CPU-s, so a figure
    # carried over from an earlier run is not a comparison.
    asked = [a for a in (args.arms.split(",") if args.arms else ARMS) if a]
    names = ["baseline"] + [n for n in asked if n != "baseline"]
    partial, rows = args.scratch / "results.json", []
    for name in names:
        env = dict(ARMS[name])
        rows.append(_run(name, args.project.resolve(), args.scratch, env))
        partial.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    report = {"arms": rows, "verdicts": _verdicts(rows)}
    partial.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    # A losing arm is the answer a sweep is for, so it is not a failure. An arm
    # that never produced a number is.
    return 1 if any("error" in row for row in rows) else 0


def _change(base: dict, arm: dict, metric: str, limit: float) -> str:
    before, after = base.get(metric), arm.get(metric)
    if not before or after is None:
        return "no data"
    change = (after - before) / before * 100
    return f"{change:+.1f}% against {limit:+.1f}% -- {'PASS' if change <= limit else 'FAIL'}"


def _verdicts(rows: list[dict]) -> dict:
    """Every thresholded arm against the baseline of this run."""
    base = next((r for r in rows if r["arm"] == "baseline" and "median" in r), None)
    if base is None:
        return {"error": "no baseline row, so no threshold can be read"}
    return {
        row["arm"]: {
            metric: _change(base["median"], row["median"], metric, limit)
            for metric, limit in THRESHOLDS[row["arm"]].items()
        }
        for row in rows
        if row["arm"] in THRESHOLDS and "median" in row
    }


def _run(name: str, project: Path, scratch: Path, env: dict[str, str]) -> dict:
    """`eval.run_arm`'s shape rather than a call to it: that one prefixes every
    key with `CODERAG_`, and `MALLOC_ARENA_MAX` and `RAYON_NUM_THREADS` are not
    ours to rename."""
    full = os.environ | env | {"CODERAG_STATE_DIR": str(scratch / name.strip("+"))}
    entry_load = os.getloadavg()[0]
    out = subprocess.run(
        [sys.executable, __file__, "--worker", str(project)],
        env=full,
        capture_output=True,
        text=True,
        check=False,
    )
    # Both ends, because the arms run back to back and the machine drifts under
    # them. A `cpu_seconds` verdict read without these two is not comparable.
    load = {"load_in": round(entry_load, 2), "load_out": round(os.getloadavg()[0], 2)}
    if out.returncode != 0:
        # Not the last line: ORT logs a recoverable warning after the traceback.
        return {"arm": name, "error": out.stderr.strip().splitlines()[-20:] or ["no output"]} | load
    return {"arm": name} | json.loads(out.stdout) | load


if __name__ == "__main__":
    raise SystemExit(main())
