"""Live proof gates for the two-tier CPU budget (HR40): idle/steady-state must stay
< 1% of one core; active work is kernel-capped <= 1 core via cgroup-v2 CPUQuota, which
the daemon physically cannot exceed (no mocks, no sudo, no real device paths).

CB1  structural, fast  unit_text() carries CPUAccounting=yes + CPUQuota= (systemd#9647:
                       CPUQuota alone does not imply CPUAccounting)
CB2  live, fast        the running daemon's unit + its own cgroup both report a finite quota
CB3  live, slow        idle gate: daemon's own DeltaCPU/Deltawall < 1% of one core
CB4  live, slow        active-cap gate: sustained real indexing work never exceeds ~1 core,
                       and cpu.stat's throttle counters prove the cap physically bit
CB5  unit, fast        cpu_budget.py parsing helpers against synthetic cgroup-v2 text
CB6  live, slow        hermetic systemd-run --scope delegation self-test, independent of
                       the RSE daemon/unit entirely
"""
from __future__ import annotations

import json
import subprocess
import time

import pytest
import requests

from tests.live._sweeps import sweeps_state

pytestmark = pytest.mark.live

_BASE = "http://127.0.0.1:8765"
_MCP_URL = f"{_BASE}/mcp"
_HDR = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
_UNIT = "rag-search-mcp-daemon.service"

_DELEGATE_HINT = (
    "cpu.max reads back as 'max' (uncapped) -- the `cpu` controller is likely not delegated "
    "to the --user systemd manager. Enable it once via a root delegate.conf drop-in "
    "(/etc/systemd/system/user@.service.d/delegate.conf, [Service] Delegate=cpu memory pids), "
    "then `systemctl daemon-reload` and re-login. See federation-ops-and-invariants.md HR40."
)


def _sse_json(r: requests.Response) -> dict:
    for line in r.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise AssertionError(f"no data: line in SSE response: {r.text[:300]}")


# CB4 deliberately saturates the daemon's 1-core cgroup, and the daemon serves its control
# plane from inside that same cgroup — so while the cap is biting (which is the whole point of
# the test) even an `initialize` handshake queues behind throttled work. Measured idle it is
# 0.01 s; under CB4's load the old 10 s ceiling was reached and the test failed in *setup*,
# never reaching the assertions it exists for. The budget is the client's patience, not the
# invariant, so it is generous; the invariant is still avg_frac <= 1.05 plus throttle growth.
_MCP_TIMEOUT_S = 60


def _mcp_session() -> dict:
    r = requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "test-cpu-budget", "version": "0.1"}},
    }, headers=_HDR, timeout=_MCP_TIMEOUT_S)
    assert r.status_code == 200, f"initialize failed {r.status_code}"
    sid = r.headers.get("mcp-session-id", "")
    return {**_HDR, "mcp-session-id": sid} if sid else _HDR


def _mcp_call(name: str, arguments: dict, timeout: float = _MCP_TIMEOUT_S) -> dict:
    h = _mcp_session()
    r = requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, headers=h, timeout=timeout)
    assert r.status_code == 200, f"{name} call failed {r.status_code}: {r.text[:200]}"
    return json.loads(_sse_json(r)["result"]["content"][0]["text"])


def _cpu_snapshot() -> dict:
    r = requests.get(f"{_BASE}/api/metrics", timeout=5)
    assert r.status_code == 200, f"/api/metrics failed: {r.status_code}"
    return r.json()["cpu"]


# --------------------------------------------------------------------------- CB1


def test_cb1_unit_text_has_cpu_accounting_and_quota():
    """CPUQuota= does not imply CPUAccounting= (systemd issue #9647) -- both must be explicit
    for the 1-core ceiling to be both kernel-enforced and readable via cpu.stat."""
    from rag_search.daemon.systemd import unit_text

    text = unit_text("/usr/bin/rag-search")
    assert "CPUAccounting=yes" in text, "unit_text() missing CPUAccounting=yes (systemd#9647)"
    assert "CPUQuota=" in text, "unit_text() missing CPUQuota= (kernel-enforced 1-core ceiling)"


# --------------------------------------------------------------------------- CB2


def test_cb2_daemon_cpu_quota_enforced():
    """The running daemon's systemd unit AND its own self-measured cgroup both see a finite cap."""
    r = subprocess.run(
        ["systemctl", "--user", "show", _UNIT, "-p", "CPUQuotaPerSecUSec", "--value"],
        capture_output=True, text=True, timeout=5,
    )
    quota_str = r.stdout.strip()
    assert quota_str and quota_str != "infinity", (
        f"CPUQuotaPerSecUSec={quota_str!r} on {_UNIT} -- quota not installed. {_DELEGATE_HINT}"
    )

    r = requests.get(f"{_BASE}/healthz", timeout=5)
    assert r.status_code == 200
    quota_cores = r.json().get("cpu_quota_cores")
    assert quota_cores is not None and quota_cores < float("inf"), (
        f"/healthz cpu_quota_cores={quota_cores!r} -- daemon's own cgroup sees no cap. "
        f"{_DELEGATE_HINT}"
    )
    assert quota_cores <= 1.01, f"CPUQuota must cap at ~1 core; got {quota_cores}"


# --------------------------------------------------------------------------- CB3

_IDLE_WINDOW_S = 20.0
_IDLE_THRESHOLD = 0.01  # < 1% of one core
_IDLE_ATTEMPTS = 5


def _watcher_activity() -> tuple[int, int, int]:
    """(completed passes, projects in flight, files queued) — the window's contamination probe.

    All three, because completions alone do not describe a busy watcher: `dispatched` ticks when a
    pass *ends*, so a 20 s window falling entirely inside one long pass shows a delta of zero. That
    is not a corner case — the first pass after an idle unload loads the ONNX embedder, which is
    exactly the multi-second work worth excluding, and it was measured reading as "quiet" at 2.9%
    of a core.
    """
    r = requests.get(f"{_BASE}/api/watcher", timeout=5)
    assert r.status_code == 200, f"/api/watcher {r.status_code}: {r.text[:200]}"
    body = r.json()
    return (
        int(body.get("dispatched", -1)),
        len(body.get("inflight") or []),
        sum((body.get("pending") or {}).values()),
    )


@pytest.mark.slow
def test_cb3_idle_cpu_under_one_percent_core():
    """With sweeps quiescent, the daemon's own DeltaCPU/Deltawall must stay < 1% of one core.

    `sweeps_state` restores what it found rather than resuming: the session fixture pauses
    sweeps for the whole run, and this file sorts 7th of 76, so an unconditional resume here
    left ~70 files' worth of tests racing the daemon for the GPU.

    A window in which the watcher ran a pass is **discarded, not reported**. Pausing sweeps
    quiesces the daemon's own timers but says nothing about the 139 watched repos: on
    2026-07-30 this read 0.0783 of a core while another profile's session was editing
    inosoft-project, and 0.4424 while a second live suite shared the cgroup. Both were true
    measurements of a daemon doing the work it exists to do, and neither says anything about
    idle cost. Re-taking the window is the only honest way to separate them — raising the
    threshold to swallow them would retire the gate instead ([[perf numbers warm and quiet]]).
    A quiet window is still held to the original 1%.
    """
    with sweeps_state(paused=True):
        busy: list[str] = []
        for _ in range(_IDLE_ATTEMPTS):
            time.sleep(2.0)  # settle any work in flight from a preceding test
            act_before, before = _watcher_activity(), _cpu_snapshot()
            t0 = time.monotonic()
            time.sleep(_IDLE_WINDOW_S)
            after, act_after = _cpu_snapshot(), _watcher_activity()
            wall_s = time.monotonic() - t0
            delta_cpu_s = (after["usage_nsec"] - before["usage_nsec"]) / 1_000_000_000
            frac = delta_cpu_s / wall_s
            if act_after != act_before or any(a[1:] != (0, 0) for a in (act_before, act_after)):
                busy.append(f"watcher {act_before} -> {act_after}, {frac:.4f} core")
                continue
            assert frac < _IDLE_THRESHOLD, (
                f"idle CPU {frac:.4f} of one core over {wall_s:.1f}s with an idle watcher "
                f"(usage_nsec {before['usage_nsec']}->{after['usage_nsec']}) -- exceeds the < 1% "
                f"gate. Sweeps were paused and no file event was served, so this is the daemon "
                f"burning CPU with nothing to do."
            )
            return
        pytest.fail(
            f"no quiet window in {_IDLE_ATTEMPTS} attempts of {_IDLE_WINDOW_S:.0f}s: "
            + "; ".join(busy)
            + ". Something is writing continuously into a watched root (check "
            "`GET /api/watcher` pending, and the journal's 'N changes detected' lines) -- the "
            "idle cost of this daemon cannot be measured while that holds."
        )


# --------------------------------------------------------------------------- CB4

# A single small synthetic project isn't enough to prove throttling: the pipeline is
# deliberately architected to stay within ~1 core per project (bounded_parse's single
# spawn worker matches the quota; _HEAVY_LOCK single-flights the graph pass) -- see
# HR39/A1. Multiple *distinct* projects registered close together each spawn their own
# reconcile_projects() thread (server/mcp.py::index() has no cross-project lock), so
# their chunk/embed and tree-sitter-extract steps genuinely overlap in time. That overlap
# -- not a single project's size -- is what pushes aggregate cgroup demand above 1 core
# for the kernel to throttle.
_CB4_PROJECT_COUNT = 4
_CB4_FILE_COUNT = 120
_CB4_FUNCS_PER_FILE = 30
_CB4_POLL_S = 2.0
_CB4_DEADLINE_S = 180.0


def _write_cb4_workspace(root, tag: str) -> None:
    for i in range(_CB4_FILE_COUNT):
        lines = []
        for j in range(_CB4_FUNCS_PER_FILE):
            lines.append(f"def func_{tag}_{i}_{j}(x):")
            lines.append("    total = 0")
            lines.append(f"    for k in range({20 + j}):")
            lines.append("        if (x + k) % 2 == 0:")
            lines.append(f"            total += x * k + {j}")
            lines.append("        else:")
            lines.append(f"            total -= k - {i}")
            if j > 0:
                lines.append(f"    total += func_{tag}_{i}_{j - 1}(total % 97)")
            lines.append("    return total")
        (root / f"mod_{tag}_{i}.py").write_text("\n".join(lines) + "\n")


@pytest.mark.slow
def test_cb4_active_work_capped_and_throttled(safe_tmp_path):
    """Sustained real indexing work must never exceed ~1 core, and cpu.stat's throttle
    counters must climb -- proof the ceiling is physically kernel-enforced, not merely that
    usage happened to stay low."""
    project_dirs = []
    for p in range(_CB4_PROJECT_COUNT):
        d = safe_tmp_path / f"proj_{p}"
        d.mkdir()
        _write_cb4_workspace(d, tag=f"p{p}")
        project_dirs.append(d)

    before = _cpu_snapshot()
    t0 = time.monotonic()

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=_CB4_PROJECT_COUNT) as pool:
        futures = [
            pool.submit(_mcp_call, "index", {"project_path": str(d), "enabled": True})
            for d in project_dirs
        ]
        results = [f.result() for f in futures]
    for result in results:
        assert result.get("status") in ("flagged", "already_registered"), (
            f"unexpected index() status: {result}"
        )

    last = before
    saw_throttle_growth = False
    deadline = time.monotonic() + _CB4_DEADLINE_S
    while time.monotonic() < deadline:
        time.sleep(_CB4_POLL_S)
        cur = _cpu_snapshot()
        if cur["nr_throttled"] > before["nr_throttled"]:
            saw_throttle_growth = True
        last = cur
        if saw_throttle_growth and cur["usage_nsec"] > before["usage_nsec"]:
            break

    wall_s = time.monotonic() - t0
    total_cpu_s = max(0, last["usage_nsec"] - before["usage_nsec"]) / 1_000_000_000
    avg_frac = total_cpu_s / wall_s

    assert avg_frac <= 1.05, (
        f"active work averaged {avg_frac:.2f} cores over {wall_s:.1f}s -- CPUQuota=100% "
        "ceiling was exceeded"
    )
    assert saw_throttle_growth, (
        f"nr_throttled never rose above {before['nr_throttled']} within {_CB4_DEADLINE_S:.0f}s "
        "of sustained indexing -- the cap may not be biting under this workload "
        "(see CB6 for the hermetic proof of the enforcement mechanism itself)"
    )


# --------------------------------------------------------------------------- CB5


def test_cb5_parse_cpu_max_synthetic():
    from rag_search.daemon.cpu_budget import _parse_cpu_max

    assert _parse_cpu_max("100000 100000\n") == pytest.approx(1.0)
    assert _parse_cpu_max("50000 100000\n") == pytest.approx(0.5)
    assert _parse_cpu_max("max 100000\n") == float("inf")
    assert _parse_cpu_max("max\n") == float("inf")


def test_cb5_parse_cpu_stat_synthetic():
    from rag_search.daemon.cpu_budget import _parse_cpu_stat

    text = "usage_usec 123456\nnr_periods 10\nnr_throttled 2\nthrottled_usec 5000\n"
    assert _parse_cpu_stat(text) == {
        "usage_usec": 123456, "nr_periods": 10, "nr_throttled": 2, "throttled_usec": 5000,
    }


def test_cb5_cpu_throttle_stat_shape():
    from rag_search.daemon.cpu_budget import cpu_throttle_stat

    stat = cpu_throttle_stat()
    assert set(stat) == {"nr_periods", "nr_throttled", "throttled_usec"}
    assert all(isinstance(v, int) and v >= 0 for v in stat.values())


def test_cb5_cpu_percent_core_non_negative():
    from rag_search.daemon.cpu_budget import cpu_percent_core

    frac = cpu_percent_core()
    assert isinstance(frac, float) and frac >= 0.0


# --------------------------------------------------------------------------- CB6

_CB6_BURN_PY = (
    "import multiprocessing as mp, time\n"
    "def _burn(seconds):\n"
    "    end = time.monotonic() + seconds\n"
    "    while time.monotonic() < end:\n"
    "        pass\n"
    "if __name__ == '__main__':\n"
    "    procs = [mp.Process(target=_burn, args=(6,)) for _ in range(4)]\n"
    "    [p.start() for p in procs]\n"
    "    [p.join() for p in procs]\n"
)


@pytest.mark.slow
def test_cb6_systemd_scope_delegation_hermetic_proof(tmp_path):
    """Independent of the RSE daemon: a fresh `systemd-run --user --scope` with
    CPUQuota=100% must actually throttle a 4-process CPU burn -- proves the `cpu`
    controller is genuinely delegated and kernel-enforcing on this host (the precondition
    CB2 depends on), without touching the running RSE unit."""
    import contextlib
    import sys
    from pathlib import Path

    from rag_search.daemon.cpu_budget import _parse_cpu_stat

    script = tmp_path / "cb6_burn.py"
    script.write_text(_CB6_BURN_PY)
    scope_name = f"rse-cb6-{int(time.time())}.scope"

    proc = subprocess.Popen(
        ["systemd-run", "--user", "--scope", "--unit", scope_name,
         "-p", "CPUAccounting=yes", "-p", "CPUQuota=100%",
         sys.executable, str(script)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        cgroup_rel = None
        resolve_deadline = time.monotonic() + 5.0
        while time.monotonic() < resolve_deadline and not cgroup_rel:
            r = subprocess.run(
                ["systemctl", "--user", "show", scope_name, "-p", "ControlGroup", "--value"],
                capture_output=True, text=True, timeout=5,
            )
            val = r.stdout.strip()
            if val and val != "/":
                cgroup_rel = val
            else:
                time.sleep(0.2)
        assert cgroup_rel, f"could not resolve ControlGroup for {scope_name}"

        stat_path = Path("/sys/fs/cgroup") / cgroup_rel.lstrip("/") / "cpu.stat"
        max_throttled = 0
        poll_deadline = time.monotonic() + 8.0
        while time.monotonic() < poll_deadline and proc.poll() is None:
            with contextlib.suppress(OSError):
                stat = _parse_cpu_stat(stat_path.read_text())
                max_throttled = max(max_throttled, stat.get("nr_throttled", 0))
            time.sleep(0.3)
        proc.wait(timeout=15)
        assert max_throttled > 0, (
            f"nr_throttled never rose above 0 for {scope_name} under a 4-process CPU burn -- "
            "the `cpu` controller may not be delegated to the --user manager on this host"
        )
    finally:
        if proc.poll() is None:
            proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
