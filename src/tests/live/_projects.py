"""Project path resolvers for the live test suite.

Returns sample workspace paths (built by the `sample_workspace` session fixture
in conftest.py). Hard-fails with a clear message if the workspace is absent.
Never falls back to registry projects — a test must never silently bind to a
real device project.

Prefer using the `federation_root_path`, `standalone_project_path`, and
`service_member_path` fixtures in conftest.py rather than calling these directly.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pytest

_SAFE_BASE = Path.home() / ".local" / "share" / "rse-test-dirs"

# C1. Every temp dir this suite builds under `_SAFE_BASE` carries the identity of the run that
# owns it, so the session-start purge can tell "leaked by a dead run" from "in use by a live one".
# Without it the purge deleted on *existence*, and its docstring asserted the premise that made
# that safe — "anything under rse-test-dirs belongs to a dead prior session" — which is false the
# moment two runs overlap, and overlapping runs are the normal state in this checkout: three agent
# profiles share it. Each run then destroyed the other's workspace mid-test, and the failures
# surfaced as unrelated assertion errors somewhere downstream.
#
# The pid alone is not enough: pids are recycled, and across a reboot a stale dir's pid is very
# likely live again as something else. The boot id makes the pair unique for as long as the dirs
# can survive, and the cmdline check turns "some process holds this pid" into "a pytest process
# holds this pid" — the only claim that licenses skipping a purge.
_OWNER_RE = re.compile(r"rseown-([0-9a-f]{8})-(\d+)")


def _boot_id() -> str:
    """8 hex chars of this boot's id, or `nobootid` where the kernel does not publish one."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip().replace("-", "")[:8]
    except OSError:
        return "nobootid"


def owner_tag() -> str:
    """The token embedded in every temp dir name this run creates."""
    return f"rseown-{_boot_id()}-{os.getpid()}"


def owner_is_live(name: str) -> bool:
    """True when `name` is owned by a *pytest* process alive on this boot.

    Untagged names answer False: they predate C1 or come from something else, and
    purge-on-existence is the right answer for them. Erring towards False keeps the self-heal
    working for genuinely leaked state, which is the fixture's original purpose.
    """
    m = _OWNER_RE.search(name)
    if not m or m.group(1) != _boot_id():
        return False
    pid = int(m.group(2))
    if pid == os.getpid():
        return True
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"pytest" in cmdline


def make_run_dir(prefix: str = "") -> Path:
    """`mkdtemp` under `_SAFE_BASE`, stamped with this run's owner tag."""
    _SAFE_BASE.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=_SAFE_BASE, prefix=f"{prefix}{owner_tag()}-"))

# These resolvers only *find* a workspace that some fixture already built, so calling one directly
# makes a test pass in a full run and fail when its file is run alone — which is how it reads as a
# capability failure. The message names the fixture because the fix is to request it, not to run
# more files first.
_ABSENT = (
    "No sample {what} found. This test calls the resolver directly, so it depends on another "
    "file's `sample_workspace` fixture having run first. Take the `{fixture}` fixture as a test "
    "argument instead — then it works standalone."
)


def _sample_ws() -> Path | None:
    """Most-recently created sample-ws- dir, or None if absent."""
    candidates = sorted(_SAFE_BASE.glob("sample-ws-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def federation_root() -> str:
    """shop-federation root from the sample workspace."""
    ws = _sample_ws()
    if ws:
        p = ws / "shop-federation"
        if p.is_dir():
            return str(p)
    pytest.fail(_ABSENT.format(what="federation root", fixture="federation_root_path"))


def standalone_project() -> str:
    """ledger-standalone from the sample workspace."""
    ws = _sample_ws()
    if ws:
        p = ws / "ledger-standalone"
        if p.is_dir():
            return str(p)
    pytest.fail(_ABSENT.format(what="standalone project", fixture="standalone_project_path"))


def service_member() -> str:
    """promo-svc (business-rule-rich) from the sample workspace."""
    ws = _sample_ws()
    if ws:
        p = ws / "shop-federation" / "promo-svc"
        if p.is_dir():
            return str(p)
    pytest.fail(_ABSENT.format(what="service member", fixture="service_member_path"))


def sample_project_paths(ws) -> set[str]:
    """All sample workspace paths (root + members + ledger). Used for fleet-wide scope guards."""
    return {ws.fed_root, ws.cart, ws.checkout, ws.promo, ws.ledger}
