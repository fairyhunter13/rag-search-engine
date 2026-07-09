"""Daemon lifecycle commands — sub-typer mounted as 'daemon' in cli.py."""
from __future__ import annotations

import typer

daemon_app = typer.Typer(help="Daemon lifecycle commands.")


@daemon_app.command("serve")
def daemon_serve(
    host: str | None = typer.Option(None),
    port: int | None = typer.Option(None),
) -> None:
    """Start the HTTP server and background jobs."""
    from rag_search.daemon.server import serve
    serve(host=host, port=port)


@daemon_app.command("status")
def daemon_status() -> None:
    """Check whether the daemon is running."""
    from rag_search.core.config import DAEMON_HOST, DAEMON_PORT
    from rag_search.daemon.server import ensure_running
    up = ensure_running(DAEMON_HOST, DAEMON_PORT)
    typer.echo(f"{'UP' if up else 'DOWN'} — {DAEMON_HOST}:{DAEMON_PORT}")
    raise typer.Exit(0 if up else 1)


@daemon_app.command("ensure")
def daemon_ensure(
    host: str | None = typer.Option(None),
    port: int | None = typer.Option(None),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Ensure daemon is running; start if not."""
    from rag_search.core.config import DAEMON_HOST, DAEMON_PORT
    from rag_search.daemon.server import ensure_running
    h, p = host or DAEMON_HOST, port or DAEMON_PORT
    up = ensure_running(h, p)
    typer.echo(__import__("json").dumps({"up": up}) if json_out else f"{'UP' if up else 'STARTED'}")


@daemon_app.command("stop")
def daemon_stop(
    host: str | None = typer.Option(None),
    port: int | None = typer.Option(None),
) -> None:
    """Stop the daemon via /api/reload?restart=false (exit 0 -> systemd will not restart it)."""
    import requests

    from rag_search.core.config import DAEMON_HOST, DAEMON_PORT
    h, p = host or DAEMON_HOST, port or DAEMON_PORT
    try:
        requests.post(f"http://{h}:{p}/api/reload?restart=false", timeout=3)
        typer.echo("Stop signal sent.")
    except Exception as exc:
        typer.echo(f"Could not reach daemon: {exc}")


@daemon_app.command("install-global")
def daemon_install_global(transport: str = typer.Option("http", "--transport")) -> None:
    """Register the MCP server in every discovered Claude Code profile.

    Delegates to scripts/configure_integrations.py --apply-all, which discovers
    ~/.claude{,-1,-2,...} + hermes and registers the canonical HTTP entry in each
    profile's ~/.claude.json via `claude mcp add` (the only file Claude Code reads
    MCP definitions from — settings.json is not a valid target).
    """
    from pathlib import Path

    from rag_search.daemon.global_prompt import remove_claude_md
    remove_claude_md()  # clean up any legacy bare ~/CLAUDE.md written by older versions

    if transport != "http":
        typer.echo(
            f"transport={transport!r} is not registered by install-global (only the canonical "
            "'http' daemon endpoint is). For a stdio client, point it at: rag-search daemon bridge-stdio"
        )
        raise typer.Exit(1)

    import subprocess
    import sys
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "configure_integrations.py"
    if not script.exists():
        typer.echo(f"configure_integrations.py not found at {script} — run from a rag-search-engine checkout.")
        raise typer.Exit(1)
    result = subprocess.run([sys.executable, str(script), "--apply-all"], cwd=str(repo_root))
    raise typer.Exit(result.returncode)


@daemon_app.command("install-systemd")
def daemon_install_systemd() -> None:
    """Write the systemd user service unit file."""
    from rag_search.daemon.systemd import install
    path = install()
    typer.echo(f"Installed: {path}")
    typer.echo("Run: systemctl --user daemon-reload && systemctl --user enable --now rag-search-mcp-daemon")


@daemon_app.command("bridge-stdio")
def daemon_bridge_stdio() -> None:
    """Run FastMCP stdio bridge (for Claude Code MCP client integration)."""
    import asyncio
    import contextlib
    import os

    from rag_search.server.mcp import mcp

    idle_s = float(os.environ.get("RSE_BRIDGE_IDLE_S", "600"))

    async def _run() -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(mcp.run_stdio_async(), timeout=idle_s)

    asyncio.run(_run())
