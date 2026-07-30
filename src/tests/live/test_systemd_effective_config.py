"""SE1-SE2: the versioned systemd drop-ins are the configuration actually in force.

Residue N1 from the 07-30 federation-exclusion work. Five operator drop-ins are versioned under
`scripts/systemd/`, and nothing compared them to the host — so the repo copy is a template people
reach for as a backup while being free to drift from it. Delete `indexing-throughput.conf` from the
live drop-in dir and `RSE_EMBED_BATCH` falls 32 -> 8 and `MemoryHigh` 4G -> 3G: a ~4x indexing
throughput regression with nothing red anywhere. `test_systemd_dropins_target_deployed_unit` checks
only the *directory name*; FE8 checks only that one env var is non-empty.

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

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

# The live value carries this host's own repo exclusions ahead of the versioned glob, and the
# versioned file stays device-neutral on purpose (this is a public repo and must not carry real
# device paths). The exception is expressed as "live must contain every versioned entry" rather than
# by dropping the file from the check, which is how an exception becomes a hole.
_SUPERSET_OK = {"RSE_FEDERATION_EXCLUDE"}


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

    if _systemctl("show", "-p", "LoadState", "--value", UNIT_NAME).strip() != "loaded":
        pytest.skip(f"{UNIT_NAME} is not loaded — nothing to compare the versioned config against")
    return UNIT_NAME


def _daemon_environ() -> dict[str, str]:
    pid = _systemctl("show", "-p", "MainPID", "--value", _require_live_unit()).strip()
    if not pid or pid == "0":
        pytest.skip("daemon has no MainPID — cannot read the environment actually in force")
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError as exc:
        pytest.skip(f"cannot read /proc/{pid}/environ: {exc}")
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
        elif name in _SUPERSET_OK:
            missing = [e for e in want.split(os.pathsep) if e and e not in got.split(os.pathsep)]
            if missing:
                problems.append(f"{conf}: {name} is in force but missing entries {missing}")
        elif got != want:
            problems.append(f"{conf}: {name} is {got!r} in force, versioned as {want!r}")
    assert not problems, (
        "the deployed configuration has drifted from the versioned drop-ins:\n  "
        + "\n  ".join(problems))


def test_se2_every_versioned_service_directive_is_still_read():
    """SE2: the non-env directives systemd reads still include each versioned one.

    Comments are stripped first, so a directive that survives only *inside* a comment — the state
    most of `federation-exclude.conf` is in — cannot satisfy this.
    """
    unit = _require_live_unit()
    text = _systemctl("cat", unit)
    if not text:
        pytest.skip(f"systemctl cat {unit} returned nothing")
    effective = {
        line.strip() for line in text.splitlines()
        if line.strip() and not line.strip().startswith(("#", ";", "["))
    }
    missing = [f"{conf}: {key}={value}" for conf, key, value in _versioned_directives()
               if key != "Environment" and f"{key}={value}" not in effective]
    assert not missing, (
        "versioned directives systemd is not reading — the drop-in carrying them is gone from the "
        "host or was edited there:\n  " + "\n  ".join(missing))
