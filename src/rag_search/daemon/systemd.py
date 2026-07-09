"""Generate and install the systemd user service unit."""
from __future__ import annotations

from pathlib import Path


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
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install(dest: Path | None = None) -> Path:
    """Write the unit file; returns the path written."""
    if dest is None:
        dest = Path.home() / ".config" / "systemd" / "user" / "rag-search-mcp-daemon.service"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(unit_text())
    return dest
