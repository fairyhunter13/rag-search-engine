"""CLI entry point: opencode-search <command>."""
from __future__ import annotations

import typer

app = typer.Typer(name="opencode-search", help="GPU code intelligence CLI.", add_completion=False)
daemon_app = typer.Typer(help="Daemon lifecycle commands.")
app.add_typer(daemon_app, name="daemon")


@app.command()
def init(
    path: str = typer.Argument(".", help="Project root to initialise (defaults to CWD)."),
    watch: bool = typer.Option(False, help="Enable file watcher after indexing."),
) -> None:
    """Register a project and kick off indexing (one-shot onboarding)."""
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import upsert_project
    resolved = str(__import__("pathlib").Path(path).expanduser().resolve())
    upsert_project(ProjectEntry(path=resolved, enabled=True))
    typer.echo(f"Initialised: {resolved}")


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


@app.command()
def watch(path: str = typer.Argument(...)) -> None:
    """Block until the watch flag is cleared (Ctrl+C to stop)."""
    import time

    from opencode_search.core.registry import list_projects
    typer.echo(f"Watching {path} — Ctrl+C to stop.")
    try:
        while any(p.path == path and p.enabled for p in list_projects()):
            time.sleep(2)
    except KeyboardInterrupt:
        pass


@app.command("stop-watching")
def stop_watching(path: str = typer.Argument(...)) -> None:
    """Stop watching a project."""
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import upsert_project
    upsert_project(ProjectEntry(path=path, enabled=False))
    typer.echo(f"Stopped: {path}")


@app.command()
def mcp() -> None:
    """Run FastMCP stdio bridge."""
    daemon_bridge_stdio()


@app.command("clean-orphans")
def clean_orphans(yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Remove orphan index dirs (dry-run by default)."""
    import shutil

    from opencode_search.core.config import INDEX_ROOT
    from opencode_search.core.registry import list_projects
    known = {p.path for p in list_projects()}
    removed = 0
    for d in (list(INDEX_ROOT.iterdir()) if INDEX_ROOT.exists() else []):
        if not any(k in str(d) for k in known):
            if yes:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
            else:
                typer.echo(f"orphan: {d}")
    typer.echo(f"Removed {removed}." if yes else "Run with --yes to delete.")


@app.command()
def storage(project: str | None = typer.Option(None, "--project", "-p")) -> None:
    """Show index storage size."""
    from opencode_search.core.config import project_vector_db
    from opencode_search.core.registry import list_projects
    paths = [project] if project else [p.path for p in list_projects() if p.enabled]
    for path in paths:
        idx = project_vector_db(path).parent
        mb = sum(f.stat().st_size for f in idx.rglob("*") if f.is_file()) / 1_048_576 if idx.exists() else 0
        typer.echo(f"{path}: {mb:.1f} MB")


@app.command()
def dashboard(no_open: bool = typer.Option(False, "--no-open")) -> None:
    """Open dashboard in browser."""
    from opencode_search.core.config import DAEMON_HOST, DAEMON_PORT
    url = f"http://{DAEMON_HOST}:{DAEMON_PORT}/dashboard"
    if not no_open:
        import webbrowser
        webbrowser.open(url)
    typer.echo(url)


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
def health(json_out: bool = typer.Option(False, "--json")) -> None:
    """Exit 1 if GPU unavailable."""
    from opencode_search.core.gpu import is_cuda_available
    ok = is_cuda_available()
    if json_out:
        import json
        typer.echo(json.dumps({"ok": ok}))
    else:
        typer.echo(f"GPU: {'OK' if ok else 'UNAVAILABLE'}")
    raise typer.Exit(0 if ok else 1)


@app.command("kb-status")
def kb_status(project: str | None = typer.Option(None, "--project", "-p")) -> None:
    """Show KB enrichment status per project."""
    import requests

    from opencode_search.core.config import DAEMON_HOST, DAEMON_PORT
    from opencode_search.core.registry import list_projects
    paths = [project] if project else [p.path for p in list_projects() if p.enabled]
    for path in paths:
        try:
            r = requests.get(f"http://{DAEMON_HOST}:{DAEMON_PORT}/api/kb_health",
                             params={"project": path}, timeout=5)
            d = r.json()
            typer.echo(f"{path}: {d.get('verdict')} ({d.get('enriched_pct', 0):.0f}%)")
        except Exception as exc:
            typer.echo(f"{path}: ERROR ({exc})")


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
    typer.echo("Run: systemctl --user daemon-reload && systemctl --user enable --now opencode-search")


@daemon_app.command("bridge-stdio")
def daemon_bridge_stdio() -> None:
    """Run FastMCP stdio bridge (for Claude Code MCP client integration)."""
    import asyncio

    from opencode_search.server.mcp import mcp
    asyncio.run(mcp.run_stdio_async())


def ocs_index_main() -> None:
    """One-shot onboarding: index → enrich → hierarchy → wiki."""
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    import pathlib
    resolved = str(pathlib.Path(path).expanduser().resolve())
    from opencode_search.core.config import ProjectEntry, project_graph_db, project_wiki_dir
    from opencode_search.core.registry import upsert_project
    from opencode_search.daemon.sweeps import _enrich_project, _index_project
    from opencode_search.graph.store import GraphStore
    from opencode_search.kb.hierarchy import build_hierarchy
    from opencode_search.kb.wiki import build_wiki
    print(f"Indexing {resolved}...")
    upsert_project(ProjectEntry(path=resolved, enabled=True))
    _index_project(resolved)
    print("Enriching...")
    _enrich_project(resolved)
    print("Building hierarchy...")
    gdb = project_graph_db(resolved)
    if gdb.exists():
        gs = GraphStore(gdb)
        try:
            build_hierarchy(gs)
            n = build_wiki(gs, project_wiki_dir(resolved))
            print(f"Wiki: {n} pages written.")
        finally:
            gs.close()
    print("Done.")


def main() -> None:
    app()
