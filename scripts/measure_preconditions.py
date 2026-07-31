#!/usr/bin/env python3
"""Refuse to record a performance number taken on a dirty box.

Usage:
    python scripts/measure_preconditions.py           # exit 0 if warm and quiet, 1 if not
    python scripts/measure_preconditions.py --json    # same, machine-readable

Exists because the alternative is a number, and a number taken under contention is worse than no
number: it gets quoted. The same unchanged federated query has measured 123.26 s, 31.93 s and
3.01 s on this box across three runs with nothing changed but host conditions, so a single sample
carries no information at all about the code that produced it.

**It is in the repo rather than in a scratchpad, and that is the point of landing it.** The T4b
harness this replaces was written three times and lost three times, because each copy lived in a
session scratchpad; the item was then re-derived from scratch each time as though it were new. A
measurement precondition that does not outlive the session cannot make a measurement repeatable.

GPU-free and daemon-free by construction: it must be runnable *before* deciding whether to start
the thing being measured, so it may not depend on the thing being measured.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# One core's worth of headroom on a 24-core box is not "quiet" in the abstract; it is the level at
# which the 1-core daemon cgroup (HR40) stops competing for the core it is capped at. Overridable
# because the ceiling is a property of the host, not of the measurement.
MAX_LOAD_1M = float(os.environ.get("RSE_MEASURE_MAX_LOAD", "1.0"))
MAX_GPU_MB = int(os.environ.get("RSE_MEASURE_MAX_GPU_MB", "512"))

# Process-name fragments that mean a heavy pass owns the machine. Deliberately about *this repo's*
# own heavy work, not a general busy-process list: something else being busy is what load average
# already measures, and duplicating it here would just make the gate fire twice for one reason.
_HEAVY = ("python -m derive", "purge_unindexable", "bin/pytest")


def _load_1m() -> float:
    return os.getloadavg()[0]


_SHELLS = ("bash", "sh", "dash", "zsh")

# `timeout` is the only common wrapper that survives alongside the pass it launches: it forks and
# waits, so both it and the real interpreter are live processes with the same fragment on their
# command lines, and one pass reads as two. `env`/`nice`/`nohup`/`stdbuf` all *exec* into the
# target, so they are replaced by it and were never double-counted to begin with — which is why
# this list has one entry rather than the five that look like they belong.
_SUPERVISORS = ("timeout",)


def _heavy_processes() -> list[str]:
    """Repo-owned heavy passes currently running, by full command line.

    A `sh -c '<script>'` wrapper is skipped even when its payload names one of the fragments,
    because the payload is *text describing* a command, not a command being run — the pass it
    launches shows up separately, under its own interpreter, and is what deserves to be counted.

    Measured 2026-07-31, which is why this is not a tidy-up: against a single live suite the count
    read 3, then 6, then 4 within seconds. The variation was **the act of checking** — every probe
    ran under a wrapper whose command line contained the string `bin/pytest`, so the gate was
    counting its own observation. Two consequences beyond a wrong number, and both are worse than
    it: a wrapper that lingers after its pass exits keeps the gate refusing forever, which is a
    gate that cannot clear; and MP1 asserts the reason *names the pytest process*, an assertion a
    shell wrapper merely mentioning pytest satisfies just as well — so the witness was reading as
    stronger than it was.

    Over-counting is the safe direction for a refusal and that is exactly why it survived: the
    gate went on refusing for a good reason while its stated reason was noise.
    """
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue  # the process exited between listing and reading; not an error
        argv = [a.decode(errors="replace") for a in args if a]
        if not argv:
            continue  # kernel thread
        if Path(argv[0]).name in _SHELLS and "-c" in argv[1:]:
            continue
        if Path(argv[0]).name in _SUPERVISORS:
            continue
        line = " ".join(argv)
        if any(frag in line for frag in _HEAVY):
            found.append(" ".join(line.split())[:120])
    return found


def _gpu_used_mb() -> int | None:
    """VRAM in use, or None when there is no NVIDIA tooling to ask.

    None is reported as *unknown*, never as zero. HR41's incident was 12.2 GB still held at
    `active_clients: 0`, which turned a live suite into 60 allocation failures inside onnxruntime
    naming neither the GPU nor the daemon — so a missing reading must not read as headroom.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return max((int(line) for line in out.split() if line.isdigit()), default=None)


def check() -> tuple[bool, dict]:
    load, heavy, gpu = _load_1m(), _heavy_processes(), _gpu_used_mb()
    fails = []
    if load >= MAX_LOAD_1M:
        fails.append(f"1m load {load:.2f} >= {MAX_LOAD_1M}")
    if heavy:
        fails.append(f"{len(heavy)} heavy pass(es) running: {heavy[0]}")
    if gpu is None:
        fails.append("GPU usage unreadable — unknown is not headroom (HR41)")
    elif gpu > MAX_GPU_MB:
        fails.append(f"GPU holds {gpu} MB > {MAX_GPU_MB} MB")
    return not fails, {"load_1m": round(load, 2), "heavy_processes": heavy,
                       "gpu_used_mb": gpu, "failures": fails}


def main() -> int:
    ok, conditions = check()
    if "--json" in sys.argv:
        print(json.dumps({"ok": ok, **conditions}, indent=1))
    else:
        print(f"load_1m={conditions['load_1m']} gpu_used_mb={conditions['gpu_used_mb']} "
              f"heavy={len(conditions['heavy_processes'])}")
        for line in conditions["failures"]:
            print(f"  REFUSE: {line}")
        print("OK — conditions are warm and quiet; record the conditions line beside every figure"
              if ok else "REFUSED — do not record a number taken here")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
