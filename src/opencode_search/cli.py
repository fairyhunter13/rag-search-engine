"""Command-line interface for opencode-search.

Commands:
  index          — index a project directory
  search         — search indexed projects
  status         — show status of one or all projects
  list           — list all indexed projects
  watch          — start live file-watcher for a project
  stop-watching  — stop live file-watcher for a project
  mcp            — start the MCP stdio server (for AI assistants)
  daemon         — manage the shared singleton MCP HTTP daemon
  health         — GPU and system health check

GPU enforcement:  CPUExecutionProvider is FORBIDDEN.
                  GPUNotAvailableError is raised at startup if no CUDA device.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Optional

import typer

app = typer.Typer(
    name="opencode-search",
    help="GPU-accelerated local semantic code search.",
    add_completion=False,
    no_args_is_help=True,
)
daemon_app = typer.Typer(help="Manage the shared singleton MCP HTTP daemon.")
app.add_typer(daemon_app, name="daemon")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine from a sync CLI handler.

    Uses asyncio.run() to create a fresh event loop per invocation. Do NOT use
    asyncio.get_event_loop() — it's deprecated and creates inconsistent loops
    across CLI subcommands.
    """
    return asyncio.run(coro)


def _print_json(obj: object) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------


@app.command()
def init(
    path: str = typer.Argument(".", help="Project root directory to index. Defaults to current directory."),
    tier: str = typer.Option("balanced", help="Embedding tier: budget | balanced | premium."),
    watch: bool = typer.Option(False, "--watch", "-w", help="Start live watcher after indexing."),
    force: bool = typer.Option(False, "--force", "-f", help="Re-index all files ignoring hash cache."),
    follow_symlinks: bool = typer.Option(True, "--follow-symlinks/--no-follow-symlinks", help="Follow symlinked directories (default: enabled for monorepo support)."),
    json_output: bool = typer.Option(False, "--json", help="Output result as JSON."),
) -> None:
    """Initialize semantic indexing for a project, defaulting to the current directory."""
    from opencode_search.handlers import handle_index_project

    result = _run(handle_index_project(path=path, tier=tier, watch=watch, force=force, follow_symlinks=follow_symlinks))

    if json_output:
        _print_json(result)
        return

    if "error" in result:
        typer.echo(f"Error: {result['error']}", err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"Indexed {result['path']}\n"
        f"  tier:          {result['tier']}\n"
        f"  files indexed: {result['files_indexed']}\n"
        f"  files skipped: {result.get('files_unchanged', 0)}\n"
        f"  chunks total:  {result.get('chunks_total', 0)}\n"
        f"  errors:        {result.get('errors', 0)}\n"
        f"  elapsed:       {result['elapsed_s']}s\n"
        f"  watching:      {result['watching']}"
    )


@app.command()
def index(
    path: str = typer.Argument(..., help="Project root directory to index."),
    tier: str = typer.Option("balanced", help="Embedding tier: budget | balanced | premium."),
    watch: bool = typer.Option(False, "--watch", "-w", help="Start live watcher after indexing."),
    force: bool = typer.Option(False, "--force", "-f", help="Re-index all files ignoring hash cache."),
    follow_symlinks: bool = typer.Option(True, "--follow-symlinks/--no-follow-symlinks", help="Follow symlinked directories (default: enabled for monorepo support)."),
    json_output: bool = typer.Option(False, "--json", help="Output result as JSON."),
) -> None:
    """Index a project directory for semantic code search."""
    from opencode_search.handlers import handle_index_project

    result = _run(handle_index_project(path=path, tier=tier, watch=watch, force=force, follow_symlinks=follow_symlinks))

    if json_output:
        _print_json(result)
        return

    if "error" in result:
        typer.echo(f"Error: {result['error']}", err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"Indexed {result['path']}\n"
        f"  tier:          {result['tier']}\n"
        f"  files indexed: {result['files_indexed']}\n"
        f"  files skipped: {result.get('files_unchanged', 0)}\n"
        f"  chunks total:  {result.get('chunks_total', 0)}\n"
        f"  errors:        {result.get('errors', 0)}\n"
        f"  elapsed:       {result['elapsed_s']}s\n"
        f"  watching:      {result['watching']}"
    )


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@app.command("search")
def search_cmd(
    query: str = typer.Argument(..., help="Natural-language or code search query."),
    projects: list[str] | None = typer.Option(
        None, "--project", "-p", help="Limit to this project path (repeatable)."
    ),
    top_k: int = typer.Option(10, "--top", "-k", help="Number of results to return."),
    no_rerank: bool = typer.Option(False, "--no-rerank", help="Disable cross-encoder reranking."),
    json_output: bool = typer.Option(False, "--json", help="Output results as JSON."),
) -> None:
    """Search indexed projects for code matching the query."""
    from opencode_search.handlers import handle_search_code

    result = _run(
        handle_search_code(
            query=query,
            project_paths=projects or None,
            top_k=top_k,
            use_rerank=not no_rerank,
        )
    )

    if json_output:
        _print_json(result)
        return

    if "error" in result:
        typer.echo(f"Error: {result['error']}", err=True)
        raise typer.Exit(code=1)

    hits = result.get("results", [])
    if not hits:
        typer.echo("No results found.")
        return

    typer.echo(
        f"Found {len(hits)} result(s) in {result['elapsed_ms']:.0f}ms "
        f"across {result['projects_searched']} project(s):\n"
    )
    for i, r in enumerate(hits, 1):
        typer.echo(
            f"[{i}] {r['path']}:{r['start_line']}-{r['end_line']}  "
            f"score={r['score']:.4f}  lang={r['language']}"
        )
        snippet = r["content"][:200].replace("\n", " ")
        typer.echo(f"    {snippet}\n")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@app.command()
def status(
    path: str | None = typer.Argument(None, help="Project path (omit for all projects)."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Show indexing and watching status for one or all projects."""
    if path:
        from opencode_search.handlers import handle_project_status
        result = _run(handle_project_status(path=path))
        if json_output:
            _print_json(result)
            return
        if not result.get("indexed"):
            typer.echo(f"Not indexed: {result['path']}")
            return
        typer.echo(
            f"Project: {result['path']}\n"
            f"  tier:     {result['tier']}\n"
            f"  chunks:   {result.get('chunks', 'unknown')}\n"
            f"  watching: {result['watching']}\n"
            f"  indexed_at: {result.get('indexed_at', 'unknown')}"
        )
    else:
        from opencode_search.handlers import handle_list_indexed_projects
        result = _run(handle_list_indexed_projects())
        if json_output:
            _print_json(result)
            return
        projects = result.get("projects", [])
        if not projects:
            typer.echo("No indexed projects.")
            return
        for p in projects:
            watching = "watching" if p["watching"] else "idle"
            typer.echo(f"  [{watching}] {p['path']}  tier={p['tier']}")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@app.command("list")
def list_cmd(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List all indexed projects."""
    from opencode_search.handlers import handle_list_indexed_projects

    result = _run(handle_list_indexed_projects())
    if json_output:
        _print_json(result)
        return

    projects = result.get("projects", [])
    if not projects:
        typer.echo("No indexed projects.")
        return

    for p in projects:
        watching = "watching" if p["watching"] else "idle"
        typer.echo(f"  [{watching}] {p['path']}  tier={p['tier']}  indexed_at={p.get('indexed_at', '?')}")


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


@app.command()
def watch(
    path: str = typer.Argument(..., help="Project root to watch for file changes."),
    tier: Optional[str] = typer.Option(None, help="Embedding tier. Defaults to the tier already in the registry, or 'budget' for new projects."),
) -> None:
    """Start a live file-watcher for a project (incremental re-indexing on change).

    Runs `index_project` + `watcher_manager.start` then keeps the same event
    loop running so the watcher dispatches to a live loop. The first call to
    handle_index_project starts the watcher inside this loop, so callbacks bind
    to the loop that we then run forever.
    """
    from pathlib import Path

    from opencode_search.config import load_registry
    from opencode_search.handlers import handle_index_project
    from opencode_search.watcher import watcher_manager

    project_path = str(Path(path).expanduser().resolve())
    # Use the persisted tier if the project is already indexed, so the watcher
    # stays consistent with the existing index without requiring --tier on every call.
    if tier is None:
        registry = load_registry()
        entry = registry.get(project_path)
        tier = entry.tier if entry is not None else "budget"
    typer.echo(f"Starting watcher for {path} (tier={tier}) — press Ctrl+C to stop.")

    async def _watch_forever() -> None:
        result = await handle_index_project(path=path, tier=tier, watch=True)
        if "error" in result or result.get("status") != "ok":
            raise RuntimeError(result.get("error", f"watch start failed: {result}"))

        # Keep the loop alive so the watchdog Observer thread can dispatch
        # events into this loop indefinitely. Another process can stop this
        # watcher by clearing the persisted registry watch flag.
        while True:
            registry = load_registry()
            entry = registry.get(project_path)
            if entry is None or not entry.watch:
                if watcher_manager.is_active(project_path):
                    await watcher_manager.stop(project_path)
                break
            if not watcher_manager.is_active(project_path):
                break
            await asyncio.sleep(0.5)

    try:
        asyncio.run(_watch_forever())
    except KeyboardInterrupt:
        typer.echo("\nWatcher stopped.")
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    else:
        typer.echo("Watcher stopped.")


# ---------------------------------------------------------------------------
# stop-watching
# ---------------------------------------------------------------------------


@app.command("stop-watching")
def stop_watching(
    path: str = typer.Argument(..., help="Project path to stop watching."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Stop the live file-watcher for a project."""
    from opencode_search.handlers import handle_stop_watching

    result = _run(handle_stop_watching(path=path))
    if json_output:
        _print_json(result)
        return

    if result.get("was_watching"):
        typer.echo(f"Stopped watcher for {result['path']}")
    else:
        typer.echo(f"No active watcher for {result['path']}")


# ---------------------------------------------------------------------------
# mcp
# ---------------------------------------------------------------------------


@app.command()
def mcp() -> None:
    """Start the MCP stdio server (for AI assistants like Claude Code)."""
    from opencode_search.mcp import run_mcp_server

    run_mcp_server()


@daemon_app.command("serve")
def daemon_serve(
    host: str | None = typer.Option(None, help="Bind host for the HTTP daemon."),
    port: int | None = typer.Option(None, help="Bind port for the HTTP daemon."),
) -> None:
    """Run the singleton MCP daemon in the foreground."""
    from opencode_search.daemon import (
        DEFAULT_DAEMON_HOST,
        DEFAULT_DAEMON_PORT,
        run_http_daemon_server,
    )

    run_http_daemon_server(host=host or DEFAULT_DAEMON_HOST, port=port or DEFAULT_DAEMON_PORT)


@daemon_app.command("ensure")
def daemon_ensure(
    host: str | None = typer.Option(None, help="Expected daemon host."),
    port: int | None = typer.Option(None, help="Expected daemon port."),
    json_output: bool = typer.Option(False, "--json", help="Output result as JSON."),
) -> None:
    """Start the singleton daemon if it is not already running."""
    from opencode_search.daemon import (
        DEFAULT_DAEMON_HOST,
        DEFAULT_DAEMON_PORT,
        ensure_daemon_running,
    )

    result = ensure_daemon_running(
        host=host or DEFAULT_DAEMON_HOST,
        port=port or DEFAULT_DAEMON_PORT,
    )
    if json_output:
        _print_json(result)
        return
    typer.echo(f"{result['status']}: {result['url']}")


@daemon_app.command("bridge-stdio")
def daemon_bridge_stdio() -> None:
    """Run the stdio MCP bridge that auto-starts and forwards to the singleton daemon."""
    from opencode_search.mcp_bridge import run_stdio_bridge

    run_stdio_bridge()


@daemon_app.command("status")
def daemon_status_cmd(
    host: str | None = typer.Option(None, help="Expected daemon host."),
    port: int | None = typer.Option(None, help="Expected daemon port."),
    json_output: bool = typer.Option(False, "--json", help="Output result as JSON."),
) -> None:
    """Show current singleton daemon status."""
    from opencode_search.daemon import DEFAULT_DAEMON_HOST, DEFAULT_DAEMON_PORT, daemon_status

    result = daemon_status(host=host or DEFAULT_DAEMON_HOST, port=port or DEFAULT_DAEMON_PORT)
    if json_output:
        _print_json(result)
        return
    state = "running" if result["running"] else "stopped"
    typer.echo(f"{state}: {result['url']}")


@daemon_app.command("stop")
def daemon_stop(
    host: str | None = typer.Option(None, help="Daemon host to stop."),
    port: int | None = typer.Option(None, help="Daemon port to stop."),
    json_output: bool = typer.Option(False, "--json", help="Output result as JSON."),
) -> None:
    """Stop the singleton daemon."""
    from opencode_search.daemon import DEFAULT_DAEMON_HOST, DEFAULT_DAEMON_PORT, stop_daemon

    result = stop_daemon(host=host or DEFAULT_DAEMON_HOST, port=port or DEFAULT_DAEMON_PORT)
    if json_output:
        _print_json(result)
        return
    typer.echo(str(result["status"]))


@daemon_app.command("install-systemd")
def daemon_install_systemd(
    host: str | None = typer.Option(None, help="Daemon host for the systemd unit."),
    port: int | None = typer.Option(None, help="Daemon port for the systemd unit."),
    json_output: bool = typer.Option(False, "--json", help="Output result as JSON."),
) -> None:
    """Install and enable a user systemd service for login-time daemon startup."""
    from opencode_search.daemon import (
        DEFAULT_DAEMON_HOST,
        DEFAULT_DAEMON_PORT,
        install_systemd_user_service,
    )

    result = install_systemd_user_service(
        host=host or DEFAULT_DAEMON_HOST,
        port=port or DEFAULT_DAEMON_PORT,
    )
    if json_output:
        _print_json(result)
        return
    if result.get("installed"):
        typer.echo(f"Installed systemd user service: {result['service_path']}")
    else:
        typer.echo(f"Systemd install failed: {result.get('reason', 'unknown error')}", err=True)
        raise typer.Exit(code=1)


@app.command("clean-orphans")
def clean_orphans(
    yes: bool = typer.Option(False, "--yes", "-y", help="Actually delete (default is dry-run)."),
    json_output: bool = typer.Option(False, "--json", help="Output result as JSON."),
) -> None:
    """Remove index directories that are no longer tracked in the registry.

    Runs as a dry-run by default — pass --yes to actually delete.
    """
    import shutil

    from opencode_search.config import get_index_root, load_registry

    registry = load_registry()
    known_dbs: set[str] = {v.db_path for v in registry.values()}

    index_root = get_index_root()
    if not index_root.exists():
        typer.echo("No index root found — nothing to clean.")
        return

    orphans: list[str] = []
    for candidate in sorted(index_root.glob("*/index_*")):
        if str(candidate) not in known_dbs:
            orphans.append(str(candidate))

    if not orphans:
        typer.echo("No orphan index directories found.")
        return

    if json_output:
        _print_json({"orphans": orphans, "deleted": yes})
        if yes:
            for path in orphans:
                shutil.rmtree(path, ignore_errors=True)
        return

    action = "Deleting" if yes else "Would delete (dry-run — pass --yes to delete)"
    for path in orphans:
        typer.echo(f"  {action}: {path}")

    if yes:
        for path in orphans:
            shutil.rmtree(path, ignore_errors=True)
        # Remove empty parent dirs
        for candidate in sorted(index_root.glob("*")):
            if candidate.is_dir() and not any(candidate.iterdir()):
                candidate.rmdir()
        typer.echo(f"Deleted {len(orphans)} orphan director{'y' if len(orphans)==1 else 'ies'}.")
    else:
        typer.echo(f"\n{len(orphans)} orphan director{'y' if len(orphans)==1 else 'ies'} found. Re-run with --yes to delete.")


@daemon_app.command("install-global")
def daemon_install_global(
    host: str | None = typer.Option(None, help="Daemon host to register in client configs."),
    port: int | None = typer.Option(None, help="Daemon port to register in client configs."),
    json_output: bool = typer.Option(False, "--json", help="Output result as JSON."),
) -> None:
    """Register the singleton daemon globally in Claude Code, Codex, and Hermes."""
    from opencode_search.daemon import (
        DEFAULT_DAEMON_HOST,
        DEFAULT_DAEMON_PORT,
        install_global_integration,
    )

    result = install_global_integration(
        host=host or DEFAULT_DAEMON_HOST,
        port=port or DEFAULT_DAEMON_PORT,
    )
    if json_output:
        _print_json(result)
        return
    typer.echo(f"Installed global MCP integration: {result['url']}")


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


@app.command()
def health(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """GPU and system health check."""
    import platform

    info: dict[str, object] = {
        "python": sys.version,
        "platform": platform.platform(),
    }

    try:
        from opencode_search.embeddings import (
            assert_gpu_available,
            get_active_provider,
            get_gpu_stats,
            is_gpu_available,
        )
        info["gpu_available"] = is_gpu_available()
        info["active_provider"] = get_active_provider()
        info["gpu_stats"] = get_gpu_stats()
        assert_gpu_available()
        info["gpu_ok"] = True
    except Exception as exc:
        info["gpu_ok"] = False
        info["gpu_error"] = str(exc)

    try:
        import lancedb
        info["lancedb"] = lancedb.__version__
    except Exception:
        info["lancedb"] = "not installed"

    try:
        import fastembed
        info["fastembed"] = getattr(fastembed, "__version__", "installed")
    except Exception:
        info["fastembed"] = "not installed"

    if json_output:
        _print_json(info)
        return

    gpu_status = "OK" if info.get("gpu_ok") else f"FAIL ({info.get('gpu_error', '?')})"
    typer.echo(
        f"GPU:        {gpu_status}\n"
        f"Provider:   {info.get('active_provider', 'unknown')}\n"
        f"LanceDB:    {info.get('lancedb')}\n"
        f"FastEmbed:  {info.get('fastembed')}\n"
        f"Python:     {sys.version.split()[0]}"
    )
    if not info.get("gpu_ok"):
        raise typer.Exit(code=1)
