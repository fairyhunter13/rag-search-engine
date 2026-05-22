"""Command-line interface for opencode-search.

Commands:
  index          — index a project directory
  search         — search indexed projects
  status         — show status of one or all projects
  list           — list all indexed projects
  watch          — start live file-watcher for a project
  stop-watching  — stop live file-watcher for a project
  mcp            — start the MCP stdio server (for AI assistants)
  health         — GPU and system health check

GPU enforcement:  CPUExecutionProvider is FORBIDDEN.
                  GPUNotAvailableError is raised at startup if no CUDA device.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(
    name="opencode-search",
    help="GPU-accelerated local semantic code search.",
    add_completion=False,
    no_args_is_help=True,
)


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
def index(
    path: str = typer.Argument(..., help="Project root directory to index."),
    tier: str = typer.Option("balanced", help="Embedding tier: budget | balanced | premium."),
    watch: bool = typer.Option(False, "--watch", "-w", help="Start live watcher after indexing."),
    force: bool = typer.Option(False, "--force", "-f", help="Re-index all files ignoring hash cache."),
    json_output: bool = typer.Option(False, "--json", help="Output result as JSON."),
) -> None:
    """Index a project directory for semantic code search."""
    from opencode_search.handlers import handle_index_project

    result = _run(handle_index_project(path=path, tier=tier, watch=watch, force=force))

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
    projects: Optional[list[str]] = typer.Option(
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
    path: Optional[str] = typer.Argument(None, help="Project path (omit for all projects)."),
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
    tier: str = typer.Option("balanced", help="Embedding tier used when indexed."),
) -> None:
    """Start a live file-watcher for a project (incremental re-indexing on change).

    Runs `index_project` + `watcher_manager.start` then keeps the same event
    loop running so the watcher dispatches to a live loop. The first call to
    handle_index_project starts the watcher inside this loop, so callbacks bind
    to the loop that we then run forever.
    """
    from opencode_search.handlers import handle_index_project

    typer.echo(f"Starting watcher for {path} (tier={tier}) — press Ctrl+C to stop.")

    async def _watch_forever() -> None:
        await handle_index_project(path=path, tier=tier, watch=True)
        # Keep the loop alive so the watchdog Observer thread can dispatch
        # events into this loop indefinitely.
        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(_watch_forever())
    except KeyboardInterrupt:
        typer.echo("\nWatcher stopped.")


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
