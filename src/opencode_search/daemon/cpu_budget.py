"""In-process CPU self-measurement against the cgroup-v2 CPUQuota ceiling (HR40).

No sudo, no subprocess — reads cgroup-v2 pseudo-files directly. `cpu.stat`'s throttling
counters (`nr_throttled`/`throttled_usec`) are the canonical proof a `CPUQuota` ceiling is
being kernel-enforced, not merely that usage happened to stay low (see
federation-ops-and-invariants.md HR40).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

_state_lock = threading.Lock()
_last_usage_usec: int | None = None
_last_monotonic: float | None = None


def _own_cgroup_dir() -> Path | None:
    """This process's cgroup-v2 directory under /sys/fs/cgroup, or None if unavailable
    (non-Linux, cgroup-v1-only host, or the unified hierarchy isn't mounted there)."""
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            hid, controllers, rel = line.split(":", 2)
            if hid == "0" and controllers == "":  # cgroup-v2 unified hierarchy
                if (Path("/sys/fs/cgroup") / rel.lstrip("/")).is_dir():
                    return Path("/sys/fs/cgroup") / rel.lstrip("/")
                return None
    except OSError:
        return None
    return None


def _parse_cpu_stat(text: str) -> dict[str, int]:
    """Parse cpu.stat's "key value" lines into a dict of ints."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            out[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return out


def _parse_cpu_max(text: str) -> float:
    """Parse cpu.max ("<quota> <period>" or "max <period>") into a cores ceiling; inf if uncapped."""
    parts = text.split()
    if not parts or parts[0] == "max":
        return float("inf")
    period = float(parts[1]) if len(parts) > 1 else 100000.0
    try:
        return float(parts[0]) / period
    except (ValueError, ZeroDivisionError):
        return float("inf")


def cpu_throttle_stat() -> dict[str, int]:
    """{nr_periods, nr_throttled, throttled_usec} — all 0 if the cgroup is unavailable.

    Rising nr_throttled/throttled_usec under load is the proof a CPUQuota ceiling is
    physically kernel-enforced, not just that usage happened to stay under it.
    """
    cg = _own_cgroup_dir()
    if cg is None:
        return {"nr_periods": 0, "nr_throttled": 0, "throttled_usec": 0}
    try:
        stat = _parse_cpu_stat((cg / "cpu.stat").read_text())
    except OSError:
        return {"nr_periods": 0, "nr_throttled": 0, "throttled_usec": 0}
    return {k: stat.get(k, 0) for k in ("nr_periods", "nr_throttled", "throttled_usec")}


def cpu_quota_cores() -> float:
    """Kernel-enforced ceiling in cores (inf if uncapped or the cgroup is unavailable)."""
    cg = _own_cgroup_dir()
    if cg is None:
        return float("inf")
    try:
        return _parse_cpu_max((cg / "cpu.max").read_text())
    except OSError:
        return float("inf")


def cpu_usage_nsec() -> int:
    """Cumulative CPU usage in nanoseconds (cpu.stat usage_usec * 1000 — matches systemd's
    CPUUsageNSec unit for direct cross-reference); 0 if the cgroup is unavailable."""
    cg = _own_cgroup_dir()
    if cg is None:
        return 0
    try:
        stat = _parse_cpu_stat((cg / "cpu.stat").read_text())
    except OSError:
        return 0
    return stat.get("usage_usec", 0) * 1000


def cpu_percent_core() -> float:
    """Fraction of one core consumed since the previous call (cached usage/monotonic delta).

    Returns 0.0 on the first call in a process (no prior sample to diff against) or when the
    cgroup is unavailable.
    """
    global _last_usage_usec, _last_monotonic
    cg = _own_cgroup_dir()
    if cg is None:
        return 0.0
    try:
        stat = _parse_cpu_stat((cg / "cpu.stat").read_text())
    except OSError:
        return 0.0
    usage_usec = stat.get("usage_usec")
    if usage_usec is None:
        return 0.0
    now = time.monotonic()
    with _state_lock:
        prev_usage, prev_time = _last_usage_usec, _last_monotonic
        _last_usage_usec, _last_monotonic = usage_usec, now
    if prev_usage is None or prev_time is None or now <= prev_time:
        return 0.0
    return (usage_usec - prev_usage) / ((now - prev_time) * 1_000_000)
