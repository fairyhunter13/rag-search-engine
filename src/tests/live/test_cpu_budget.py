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
                       the OSE daemon/unit entirely
"""
from __future__ import annotations

import json
import subprocess
import time

import pytest
import requests

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


def _mcp_session() -> dict:
    r = requests.post(_MCP_URL, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "test-cpu-budget", "version": "0.1"}},
    }, headers=_HDR, timeout=10)
    assert r.status_code == 200, f"initialize failed {r.status_code}"
    sid = r.headers.get("mcp-session-id", "")
    return {**_HDR, "mcp-session-id": sid} if sid else _HDR


def _mcp_call(name: str, arguments: dict, timeout: float = 10) -> dict:
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


@pytest.mark.slow
def test_cb3_idle_cpu_under_one_percent_core():
    """With sweeps quiescent, the daemon's own DeltaCPU/Deltawall must stay < 1% of one core."""
    r = requests.post(f"{_BASE}/api/sweeps/pause", timeout=5)
    assert r.status_code == 200
    try:
        time.sleep(2.0)  # settle any work in flight from a preceding test
        before = _cpu_snapshot()
        t0 = time.monotonic()
        time.sleep(_IDLE_WINDOW_S)
        after = _cpu_snapshot()
        wall_s = time.monotonic() - t0
        delta_cpu_s = (after["usage_nsec"] - before["usage_nsec"]) / 1_000_000_000
        frac = delta_cpu_s / wall_s
        assert frac < _IDLE_THRESHOLD, (
            f"idle CPU {frac:.4f} of one core over {wall_s:.1f}s "
            f"(usage_nsec {before['usage_nsec']}->{after['usage_nsec']}) -- exceeds the < 1% gate"
        )
    finally:
        requests.post(f"{_BASE}/api/sweeps/resume", timeout=5)


# --------------------------------------------------------------------------- CB4

# A single small synthetic project isn't enough to prove throttling: the pipeline is
# deliberately architected to stay within ~1 core per project (bounded_parse's single
# spawn worker matches the quota; _KB_HEAVY_LOCK single-flights the BPRE pass) -- see
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
            pool.submit(_mcp_call, "index", {"project_path": str(d), "enabled": True}, 15)
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
    """Independent of the OSE daemon: a fresh `systemd-run --user --scope` with
    CPUQuota=100% must actually throttle a 4-process CPU burn -- proves the `cpu`
    controller is genuinely delegated and kernel-enforcing on this host (the precondition
    CB2 depends on), without touching the running OSE unit."""
    import contextlib
    import sys
    from pathlib import Path

    from rag_search.daemon.cpu_budget import _parse_cpu_stat

    script = tmp_path / "cb6_burn.py"
    script.write_text(_CB6_BURN_PY)
    scope_name = f"ose-cb6-{int(time.time())}.scope"

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
