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
    from opencode_search.daemon.server import serve
    serve(host=host, port=port)


@daemon_app.command("status")
def daemon_status() -> None:
    """Check whether the daemon is running."""
    from opencode_search.core.config import DAEMON_HOST, DAEMON_PORT
    from opencode_search.daemon.server import ensure_running
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
    from opencode_search.core.config import DAEMON_HOST, DAEMON_PORT
    from opencode_search.daemon.server import ensure_running
    h, p = host or DAEMON_HOST, port or DAEMON_PORT
    up = ensure_running(h, p)
    typer.echo(__import__("json").dumps({"up": up}) if json_out else f"{'UP' if up else 'STARTED'}")


@daemon_app.command("stop")
def daemon_stop(
    host: str | None = typer.Option(None),
    port: int | None = typer.Option(None),
) -> None:
    """Stop the daemon via /api/reload (systemd will not restart it if disabled)."""
    import requests

    from opencode_search.core.config import DAEMON_HOST, DAEMON_PORT
    h, p = host or DAEMON_HOST, port or DAEMON_PORT
    try:
        requests.post(f"http://{h}:{p}/api/reload", timeout=3)
        typer.echo("Stop signal sent.")
    except Exception as exc:
        typer.echo(f"Could not reach daemon: {exc}")


@daemon_app.command("install-global")
def daemon_install_global(transport: str = typer.Option("stdio", "--transport")) -> None:
    """Inject MCP block into ~/CLAUDE.md and editor configs."""
    from opencode_search.daemon.global_prompt import inject_claude_md
    inject_claude_md()
    typer.echo("Injected global prompt into ~/CLAUDE.md.")


@daemon_app.command("install-systemd")
def daemon_install_systemd() -> None:
    """Write the systemd user service unit file."""
    from opencode_search.daemon.systemd import install
    path = install()
    typer.echo(f"Installed: {path}")
    typer.echo("Run: systemctl --user daemon-reload && systemctl --user enable --now opencode-search-mcp-daemon")


@daemon_app.command("bridge-stdio")
def daemon_bridge_stdio() -> None:
    """Run FastMCP stdio bridge (for Claude Code MCP client integration)."""
    import asyncio

    from opencode_search.server.mcp import mcp
    asyncio.run(mcp.run_stdio_async())
