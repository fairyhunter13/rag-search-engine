"""CLI entry point: opencode-search <command>."""
from __future__ import annotations

import typer

app = typer.Typer(name="opencode-search", help="GPU code intelligence CLI.", add_completion=False)
daemon_app = typer.Typer(help="Daemon lifecycle commands.")
app.add_typer(daemon_app, name="daemon")


@app.command()
def index(
    path: str = typer.Argument(..., help="Project root to index."),
    enabled: bool = typer.Option(True, help="Enable (True) or remove (False) the project."),
) -> None:
    """Register or remove a project from the index registry."""
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import remove_project, upsert_project
    if not enabled:
        ok = remove_project(path)
        typer.echo(f"{'Removed' if ok else 'Not found'}: {path}")
        return
    upsert_project(ProjectEntry(path=path, enabled=True))
    typer.echo(f"Registered: {path}")


@app.command()
def search(
    query: str = typer.Argument(..., help="Natural-language search query."),
    project: str | None = typer.Option(None, help="Limit to this project path."),
    scope: str = typer.Option("code", help="Scope: code|docs|all."),
    top_k: int = typer.Option(5, help="Number of results."),
) -> None:
    """Search indexed code semantically."""
    from opencode_search.core.config import project_vector_db
    from opencode_search.core.registry import list_projects
    from opencode_search.embed.embedder import Embedder
    from opencode_search.index.store import VectorStore
    from opencode_search.query.search import search as _search

    embedder = Embedder()
    embedder.warmup()
    paths = [project] if project else [p.path for p in list_projects() if p.enabled]
    results = []
    for path in paths:
        vdb = project_vector_db(path)
        if not vdb.exists():
            continue
        vs = VectorStore(vdb)
        try:
            results.extend(_search(query, embedder, vs, scope=scope, top_k=top_k))
        finally:
            vs.close()
    results.sort(key=lambda r: r.get("score", 0), reverse=True)
    if not results:
        typer.echo("No results.")
        return
    for r in results[:top_k]:
        typer.echo(f"{r['path']}:{r.get('start_line', '')}  score={r.get('score', 0):.3f}")
        typer.echo(f"  {r.get('content', '')[:120]}")


@app.command("list")
def list_projects_cmd() -> None:
    """List all registered projects."""
    from opencode_search.core.registry import list_projects
    projects = list_projects()
    if not projects:
        typer.echo("No projects registered.")
        return
    for p in projects:
        status = "✓" if p.enabled else "✗"
        typer.echo(f"  {status} {p.path}")


@app.command()
def status() -> None:
    """Show daemon status and registered projects."""
    from opencode_search.core.config import DAEMON_HOST, DAEMON_PORT
    from opencode_search.daemon.server import ensure_running
    running = ensure_running(DAEMON_HOST, DAEMON_PORT)
    typer.echo(f"Daemon: {'UP' if running else 'DOWN'} ({DAEMON_HOST}:{DAEMON_PORT})")
    list_projects_cmd()


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


@daemon_app.command("install-systemd")
def daemon_install_systemd() -> None:
    """Write the systemd user service unit file."""
    from opencode_search.daemon.systemd import install
    path = install()
    typer.echo(f"Installed: {path}")
    typer.echo("Run: systemctl --user daemon-reload && systemctl --user enable --now opencode-search")


@daemon_app.command("bridge-stdio")
def daemon_bridge_stdio() -> None:
    """Run FastMCP stdio bridge (for Claude Code MCP client integration)."""
    import asyncio

    from opencode_search.server.mcp import mcp
    asyncio.run(mcp.run_stdio_async())


def main() -> None:
    app()
