"""SE1, SE2, SE6: the configuration actually in force is the one that was declared.

SE1/SE2 cover the systemd drop-ins; SE6 covers `.rse-index.yaml`, where the federation
exclusion's layout patterns moved on 2026-08-17. The split matters to what each can see: a
drop-in only reaches the daemon at start, so SE1 reads `/proc`, while a config file is re-read
under an mtime cache and can be quarantined outright, so SE6 asks the daemon what it resolved.

Operator drop-ins are versioned under `scripts/systemd/`, and without this file nothing
compares them to the host — the repo copy reads as a backup while being free to drift from it.
Delete `indexing-throughput.conf` from the live drop-in dir and `RSE_EMBED_BATCH` falls 32 -> 8,
`MemoryHigh` 4G -> 3G: a ~4x indexing throughput regression with nothing else red anywhere
(`test_systemd_dropins_target_deployed_unit` checks only the directory name, FE8 only that one
env var is non-empty).

## What is compared against what, and why not the files

The obvious check — diff each versioned `*.conf` against its deployed twin — fires on a non-problem.
`cpu-budget.conf` is versioned but *not* deployed on this host, while its `CPUQuota=100%` is in force
anyway because `unit_text()` emits it into the generated main unit. A file-level diff reports that as
drift. What N1 actually names is a setting that has stopped applying, so this checks effective
configuration, which is strictly stronger for that regression and quiet about where a setting happens
to be written.

Env directives are read from `/proc/<MainPID>/environ` — the running process's real environment, not
this shell's and not a re-read of the file. A drop-in edited after the last daemon start is not yet in
force, and reporting it as in force would be the same lie one level down.

The two arms are not redundant, and the difference is visible in the demo that motivated them.
Deleting `indexing-throughput.conf` from the live drop-in dir reds SE2 at once while SE1 stays
green — correctly, because the running daemon read its environment at start and `RSE_EMBED_BATCH=32`
really is still in force. SE2 catches the file going missing; SE1 catches the moment it stops
applying, one restart later. Losing either leaves half the window uncovered.

Non-env directives are read from `systemctl --user cat`, systemd's own account of the files it reads.
**Stated limit:** that catches a directive that has gone missing, not one that is present and then
overridden by a later drop-in. Catching the second needs value comparison via `show`, which needs a
hand-written directive->property->unit translation table (`CPUQuota=100%` surfaces as
`CPUQuotaPerSecUSec=1s`, `MemoryHigh=4G` as a byte count) that fails open the moment someone adds a
directive it does not know. Missing is the failure that has happened; a silently incomplete table is
the failure this file exists to stop repeating.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

def _versioned_directives() -> list[tuple[str, str, str]]:
    """(conf name, directive, value) for every real directive line in the versioned drop-ins."""
    from rag_search.daemon.systemd import UNIT_NAME

    d = Path(__file__).resolve().parents[3] / "scripts" / "systemd" / f"{UNIT_NAME}.d"
    out = []
    for conf in sorted(d.glob("*.conf")):
        for raw in conf.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", ";", "[")):
                continue
            key, _, value = line.partition("=")
            out.append((conf.name, key.strip(), value.strip()))
    assert out, f"no versioned drop-in directives found under {d} — the check would pass vacuously"
    return out


def _systemctl(*args: str) -> str:
    r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)
    return r.stdout if r.returncode == 0 else ""


def _require_live_unit() -> str:
    from rag_search.daemon.systemd import UNIT_NAME

    assert _systemctl("show", "-p", "LoadState", "--value", UNIT_NAME).strip() == "loaded", (
        f"{UNIT_NAME} is not loaded, so there is no deployed configuration to compare the versioned "
        "drop-ins against — the drift this file exists to catch would simply go unmeasured")
    return UNIT_NAME


def _daemon_environ() -> dict[str, str]:
    pid = _systemctl("show", "-p", "MainPID", "--value", _require_live_unit()).strip()
    assert pid and pid != "0", (
        "the unit is loaded but reports no MainPID, so the environment actually in force cannot be "
        "read and SE1 would compare the versioned drop-ins against nothing")
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError as exc:
        raise AssertionError(
            f"cannot read /proc/{pid}/environ ({exc}) — the in-force environment is the only "
            "evidence SE1 has that a drop-in is deployed rather than merely versioned") from exc
    env = {}
    for item in raw.split(b"\0"):
        k, _, v = item.decode("utf-8", "replace").partition("=")
        if k:
            env[k] = v
    return env


def test_se1_every_versioned_environment_setting_is_in_force():
    """SE1: the running daemon's own environment carries every versioned `Environment=`.

    This is the arm that catches N1's named regression: `indexing-throughput.conf` disappearing off
    the host takes `RSE_EMBED_BATCH` from 32 back to the code default of 8, and every other test in
    the suite keeps passing while indexing runs at a quarter speed.
    """
    _require_live_unit()
    live = _daemon_environ()
    problems = []
    for conf, key, value in _versioned_directives():
        if key != "Environment":
            continue
        name, _, want = value.partition("=")
        got = live.get(name)
        if got is None:
            problems.append(f"{conf}: {name} is not set on the running daemon at all")
        elif got != want:
            problems.append(f"{conf}: {name} is {got!r} in force, versioned as {want!r}")
    assert not problems, (
        "the deployed configuration has drifted from the versioned drop-ins:\n  "
        + "\n  ".join(problems))


def test_se2_every_versioned_service_directive_is_still_read():
    """SE2: the non-env directives systemd reads still include each versioned one.

    Comments are stripped first, so a directive that survives only *inside* a comment cannot
    satisfy this — which is the state the retired `federation-exclude.conf` had drifted into.
    """
    unit = _require_live_unit()
    text = _systemctl("cat", unit)
    assert text, (
        f"`systemctl cat {unit}` returned nothing, so `effective` below is empty and every "
        "versioned directive would be reported as present in a set that contains none of them")
    effective = {
        line.strip() for line in text.splitlines()
        if line.strip() and not line.strip().startswith(("#", ";", "["))
    }
    missing = [f"{conf}: {key}={value}" for conf, key, value in _versioned_directives()
               if key != "Environment" and f"{key}={value}" not in effective]
    assert not missing, (
        "versioned directives systemd is not reading — the drop-in carrying them is gone from the "
        "host or was edited there:\n  " + "\n  ".join(missing))


def test_se6_every_declared_federation_exclusion_is_in_force(live_client):
    """SE6: the exclusion each federation root declares is the one the daemon is applying.

    The federation exclusion's layout patterns left `Environment=` for `.rse-index.yaml`, so SE1
    and SE2 stopped covering the half of it that matters most — and the pair exists precisely
    because one arm cannot see what the other catches. This is that pair rebuilt for a file
    source. Reading the file back would only prove YAML round-trips; the daemon caches its config
    read on mtime, holds a resolved copy per project, and quarantines a project whose config
    stopped parsing, so "in force" can only be answered by the running process. It is asked
    through `POST /api/overview`, the same discipline as SE1's `/proc` read.

    A root that declares nothing is skipped, since an env-only fleet is legitimate — but the run
    as a whole must find at least one declared pattern, or this passes by having nothing to check
    and the exclusion could be deleted outright with SE6 still green.
    """
    from rag_search.core.index_config import ProjectConfigError, load_project_config
    from rag_search.core.registry import list_projects

    declared_total, problems = 0, []
    for entry in [e for e in list_projects() if e.federation]:
        try:
            declared = load_project_config(Path(entry.path)).federation_exclude
        except ProjectConfigError as exc:
            # Caught rather than raised: a root whose config stopped parsing is the defect this
            # gate reports, and an error here would end the loop before the other roots are seen.
            problems.append(f"{entry.path}: the declared config no longer parses — {exc}")
            continue
        if not declared:
            continue
        declared_total += len(declared)
        r = live_client.post("/api/overview",
                             json={"project_path": entry.path, "what": "status"}, timeout=120)
        assert r.status_code == 200, f"{entry.path}: overview(status) returned {r.status_code}"
        cfg = r.json().get("config", {})
        if cfg.get("error"):
            problems.append(f"{entry.path}: the daemon quarantined it — {cfg['error']}")
        missing = [p for p in declared if p not in cfg.get("federation_exclude", [])]
        if missing:
            problems.append(f"{entry.path}: declares {missing} but the daemon is not applying them")

    # `problems` first: a root that failed to parse also declares nothing, and reporting that as
    # "nobody declared anything" would send the reader looking for a deletion that never happened.
    assert not problems, "declared and in-force exclusions disagree:\n  " + "\n  ".join(problems)
    assert declared_total, (
        "no federation root declares a `federation.exclude`, so SE6 checked nothing. Either the "
        "exclusion moved back to the environment (then SE1 covers it and this should be retired) "
        "or a root's .rse-index.yaml lost its federation block."
    )
