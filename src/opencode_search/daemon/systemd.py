"""Generate and install the systemd user service unit."""
from __future__ import annotations

from pathlib import Path


def unit_text(exec_path: str | None = None) -> str:
    if exec_path is None:
        import shutil
        exec_path = shutil.which("opencode-search") or "opencode-search"
    return (
        "[Unit]\n"
        "Description=opencode-search singleton MCP daemon (GPU-enforced)\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_path} daemon serve --host 127.0.0.1 --port 8765\n"
        "Restart=on-failure\n"
        "RestartSec=3s\n"
        "StartLimitBurst=20\n"
        "Environment=OPENCODE_EMBED_DEVICE=cuda\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install(dest: Path | None = None) -> Path:
    """Write the unit file; returns the path written."""
    if dest is None:
        dest = Path.home() / ".config" / "systemd" / "user" / "opencode-search-mcp-daemon.service"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(unit_text())
    return dest
