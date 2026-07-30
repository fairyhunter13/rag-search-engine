"""Generate and install the systemd user service unit."""
from __future__ import annotations

from pathlib import Path

# The deployed unit name, in one place: `scripts/systemd/<UNIT_NAME>.d/` holds the versioned
# operator drop-ins, and systemd only reads a drop-in dir whose name matches the unit exactly.
# It did not match for a year — the tracked dir said `rag-search.service.d`, so the CPU-budget
# drop-in it carried was inert everywhere except this host, where a hand-made copy under the
# real name was doing the work. test_systemd_dropins_target_deployed_unit ties them together.
UNIT_NAME = "rag-search-mcp-daemon.service"


def unit_text(exec_path: str | None = None) -> str:
    if exec_path is None:
        import shutil
        import sys
        # Prefer the binary adjacent to the current Python interpreter (venv-aware).
        _candidate = Path(sys.executable).parent / "rag-search"
        if _candidate.exists():
            exec_path = str(_candidate)
        else:
            exec_path = shutil.which("rag-search") or "rag-search"
    return (
        "[Unit]\n"
        "Description=rag-search singleton MCP daemon (GPU-enforced)\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_path} daemon serve --host 127.0.0.1 --port 8765\n"
        "Restart=on-failure\n"
        "RestartSec=3s\n"
        "StartLimitBurst=20\n"
        "Environment=RSE_EMBED_DEVICE=cuda\n"
        "EnvironmentFile=-%h/.config/rag-search/env\n"
        "Nice=5\n"
        "CPUWeight=20\n"
        "IOWeight=20\n"
        "MemoryHigh=3G\n"
        "MemoryMax=6G\n"
        # CPUQuota alone does not imply CPUAccounting (systemd#9647) — both required for the
        # kernel-enforced 1-core ceiling (HR40) to actually cap and be readable via cpu.stat.
        "CPUAccounting=yes\n"
        "CPUQuota=100%\n"
        # One federated query opens one sqlite store per member — 157 on the largest federation
        # here — and WAL costs ~3 fds each (db, -wal, -shm). Against systemd's *default* 1024 that
        # is one query landing within a factor of two of the ceiling, and on 2026-07-29 it went
        # over: accept() itself ran out of fds and /healthz served 500 until a restart. The limit
        # has to be derived from the member count, not left at a default chosen for shells.
        "LimitNOFILE=65536\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install(dest: Path | None = None) -> Path:
    """Write the unit file; returns the path written."""
    if dest is None:
        dest = Path.home() / ".config" / "systemd" / "user" / UNIT_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(unit_text())
    return dest
