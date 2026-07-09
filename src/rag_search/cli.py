"""CLI entry point: rag-search <command>."""
from __future__ import annotations

import typer

from rag_search.cli_daemon import daemon_app

app = typer.Typer(name="rag-search", help="GPU code intelligence CLI.", add_completion=False)
app.add_typer(daemon_app, name="daemon")


@app.command()
def init(
    path: str = typer.Argument(".", help="Project root to initialise (defaults to CWD)."),
    watch: bool = typer.Option(False, help="Enable file watcher after indexing."),
) -> None:
    """Register a project and kick off indexing (one-shot onboarding)."""
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import upsert_project
    resolved = str(__import__("pathlib").Path(path).expanduser().resolve())
    upsert_project(ProjectEntry(path=resolved, enabled=True))
    typer.echo(f"Initialised: {resolved}")


@app.command()
def index(
    path: str = typer.Argument(..., help="Project root to index."),
    enabled: bool = typer.Option(True, help="Enable (True) or remove (False) the project."),
) -> None:
    """Register or remove a project from the index registry."""
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import remove_project, upsert_project
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
    from rag_search.core.config import project_vector_db
    from rag_search.core.registry import list_projects
    from rag_search.embed.embedder import Embedder
    from rag_search.index.store import VectorStore
    from rag_search.query.search import search as _search

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
    results.sort(key=lambda r: r.get("rerank_score", r.get("score", 0.0)), reverse=True)
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

    from rag_search.core.registry import list_projects
    typer.echo(f"Watching {path} — Ctrl+C to stop.")
    try:
        while any(p.path == path and p.enabled for p in list_projects()):
            time.sleep(2)
    except KeyboardInterrupt:
        pass


@app.command("stop-watching")
def stop_watching(path: str = typer.Argument(...)) -> None:
    """Stop watching a project."""
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import upsert_project
    upsert_project(ProjectEntry(path=path, enabled=False))
    typer.echo(f"Stopped: {path}")


@app.command()
def mcp() -> None:
    """Run FastMCP stdio bridge."""
    from rag_search.cli_daemon import daemon_bridge_stdio
    daemon_bridge_stdio()


@app.command("clean-orphans")
def clean_orphans(yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Remove orphan index dirs (dry-run by default)."""
    import shutil

    from rag_search.core.config import INDEX_ROOT
    from rag_search.core.registry import list_projects
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
    from rag_search.core.config import project_vector_db
    from rag_search.core.registry import list_projects
    paths = [project] if project else [p.path for p in list_projects() if p.enabled]
    for path in paths:
        idx = project_vector_db(path).parent
        mb = sum(f.stat().st_size for f in idx.rglob("*") if f.is_file()) / 1_048_576 if idx.exists() else 0
        typer.echo(f"{path}: {mb:.1f} MB")


@app.command()
def dashboard(no_open: bool = typer.Option(False, "--no-open")) -> None:
    """Open dashboard in browser."""
    from rag_search.core.config import DAEMON_HOST, DAEMON_PORT
    url = f"http://{DAEMON_HOST}:{DAEMON_PORT}/dashboard"
    if not no_open:
        import webbrowser
        webbrowser.open(url)
    typer.echo(url)


@app.command("list")
def list_projects_cmd() -> None:
    """List all registered projects."""
    from rag_search.core.registry import list_projects
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
    from rag_search.core.gpu import is_gpu_available
    ok = is_gpu_available()
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

    from rag_search.core.config import DAEMON_HOST, DAEMON_PORT
    from rag_search.core.registry import list_projects
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
def docgen(
    path: str = typer.Argument(..., help="Project root to generate IH docs for."),
) -> None:
    """Generate Information Hierarchy docs for a project (manual trigger; LLM-native)."""
    from rag_search.kb.docgen import run_docgen
    typer.echo(f"Running docgen for {path} ...")
    run_docgen(path)
    typer.echo("Done.")


@app.command()
def okf(
    path: str = typer.Argument(..., help="Project root to generate OKF bundle for."),
) -> None:
    """Generate OKF v0.1 knowledge bundle for a project (manual trigger; LLM-native)."""
    from rag_search.kb.okf import run_okf
    typer.echo(f"Running OKF for {path} ...")
    result = run_okf(path)
    written = len(result.get("written", []))
    skipped = len(result.get("skipped", []))
    mode = result.get("mode", "on")
    typer.echo(f"Done. mode={mode} written={written} skipped={skipped}")


@app.command()
def ask(
    query: str = typer.Argument(..., help="Question to answer."),
    project: str | None = typer.Option(None, "--project", "-p", help="Project path."),
    scope: str = typer.Option("all", help="Scope: all|architecture|global|feature|wiki|business."),
) -> None:
    """Assemble context for a codebase question (LLM-free; GPU rerank only)."""
    from rag_search.query.ask import run_ask
    typer.echo(run_ask(query, project or "", scope))


@app.command()
def graph(
    symbol: str = typer.Argument(..., help="Symbol to analyze."),
    project: str | None = typer.Option(None, "--project", "-p", help="Project path."),
    relation: str = typer.Option("definition", help="definition|callers|callees|impact|impact_narrative|path|semantic_trace."),
    to_symbol: str = typer.Option("", "--to-symbol", help="Target symbol for path/semantic_trace."),
) -> None:
    """Analyze call graph for a symbol."""
    from rag_search.query.graph_handler import run_graph
    typer.echo(run_graph(symbol, project or "", relation, to_symbol))


@app.command()
def overview(
    project: str | None = typer.Option(None, "--project", "-p", help="Project path."),
    what: str = typer.Option("structure", help="structure|communities|status|projects|patterns|metrics|..."),
) -> None:
    """Overview of a project (same as MCP overview tool)."""
    from rag_search.server._overview import handle_overview
    typer.echo(handle_overview(project or "", what))


@app.command()
def wiki(
    path: str = typer.Argument(..., help="Project root to build wiki for."),
) -> None:
    """Build wiki pages for a project from its graph DB."""
    from rag_search.core.config import project_graph_db, project_wiki_dir
    from rag_search.graph.store import GraphStore
    from rag_search.kb.wiki import build_wiki
    gdb = project_graph_db(path)
    if not gdb.exists():
        typer.echo(f"Not indexed: {path}", err=True)
        raise typer.Exit(1)
    gs = GraphStore(gdb)
    try:
        n = build_wiki(gs, project_wiki_dir(path))
        typer.echo(f"pages_written={n}")
    finally:
        gs.close()


@app.command()
def status() -> None:
    """Show daemon status and registered projects."""
    from rag_search.core.config import DAEMON_HOST, DAEMON_PORT
    from rag_search.daemon.server import ensure_running
    running = ensure_running(DAEMON_HOST, DAEMON_PORT)
    typer.echo(f"Daemon: {'UP' if running else 'DOWN'} ({DAEMON_HOST}:{DAEMON_PORT})")
    list_projects_cmd()


def rse_index_main() -> None:
    """One-shot onboarding: index → enrich → wiki."""
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    import pathlib
    resolved = str(pathlib.Path(path).expanduser().resolve())
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import upsert_project
    from rag_search.daemon.sweeps import _enrich_project, _index_project
    print(f"Indexing {resolved}...")
    upsert_project(ProjectEntry(path=resolved, enabled=True))
    _index_project(resolved)
    print("Enriching...")
    _enrich_project(resolved)
    print("Done.")


def main() -> None:
    app()
