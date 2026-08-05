"""CLI entry point: rag-search <command>."""
from __future__ import annotations

import os

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
        # migrate=False — see mcp.py: the FTS backfill belongs to reconcile, never to a query.
        vs = VectorStore(vdb, migrate=False)
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


def _prune_stranded_vectors(yes: bool) -> None:
    """Vector rows whose `chunks` row is gone, across every registered store.

    The other kind of orphan this command sweeps, and the one that had no repair at all: the
    validator reports it as INVALID (`overview(what="validate")` → `stranded_vectors`) and
    nothing short of `clear()` could reach it. It lives here rather than in a command of its own
    because "orphan" is already the word the validator uses for it and one confirmation is
    enough for both sweeps.
    """
    from rag_search.core.config import project_vector_db
    from rag_search.core.registry import list_projects
    from rag_search.index.store import VectorStore
    for p in list_projects():
        if not p.enabled:
            continue
        vdb = project_vector_db(p.path)
        # Opening a store creates it — the same way the daemon once answered a deleted root by
        # writing a brand-new empty store. A path with no vector db is not a store to repair.
        if not vdb.exists():
            continue
        try:
            vs = VectorStore(vdb, migrate=False)
        except Exception as exc:
            # A store this phase cannot read is very likely one the *dir* sweep below exists to
            # take. Raising here would abort the command before it ever got there — the repair
            # for one kind of orphan killing the repair for the other.
            typer.echo(f"unreadable vector store, skipped: {vdb} ({exc})", err=True)
            continue
        try:
            # Vectors first: pruning one takes its code with it, so the code sweep that follows
            # only ever sees the codes whose vector was removed by something else.
            nv, nc = len(vs.orphan_vector_ids()), 0
            if yes and nv:
                typer.echo(f"pruned {vs.prune_orphan_vectors()} stranded vector row(s): {p.path}")
            elif nv:
                typer.echo(f"stranded vectors: {nv} in {p.path}")
            nc = len(vs.orphan_code_ids())
            if yes and nc:
                typer.echo(f"pruned {vs.prune_orphan_codes()} stranded bit-lane code(s): {p.path}")
            elif nc:
                typer.echo(f"stranded codes: {nc} in {p.path}")
        finally:
            vs.close()


@app.command("clean-orphans")
def clean_orphans(
    yes: bool = typer.Option(False, "--yes", "-y"),
    force: bool = typer.Option(False, "--force",
                               help="Proceed even when the sweep would take most of the tree."),
) -> None:
    """Remove orphan index dirs and stranded vector rows (dry-run by default)."""
    from rag_search.core.config import INDEX_ROOT
    from rag_search.core.orphans import (
        TRASH_DIRNAME,
        OrphanSweepRefusedError,
        orphan_dirs,
        quarantine,
    )
    # The ownership test and its floor both live in `core.orphans`: this command deleted the whole
    # fleet's index once by comparing a registry path against a dir name, and the corrected
    # comparison then lived here while `maintenance()` kept the broken one. One copy, one fix.
    #
    # A dry run is refused too. Printing 179 orphan lines and leaving the operator to notice would
    # make the refusal message — the single most useful thing this command can say in that state —
    # the one output it withholds right up until `--yes`.
    # Row orphans first, and unconditionally: the dir sweep below can refuse and exit, and a
    # refusal about whole stores is no reason to withhold the one repair for stranded rows.
    _prune_stranded_vectors(yes)
    try:
        orphans = orphan_dirs(allow_bulk=force)
    except OrphanSweepRefusedError as exc:
        typer.echo(f"Refused: {exc}", err=True)
        raise typer.Exit(1) from None
    removed = 0
    for d in orphans:
        if yes:
            if quarantine(d) is not None:
                removed += 1
        else:
            typer.echo(f"orphan: {d}")
    if yes:
        # Say where they went. "Removed 179." after the incident would have been the last chance to
        # learn the stores were still on disk, spent on a word that implies they are not.
        typer.echo(f"Quarantined {removed} to {INDEX_ROOT / TRASH_DIRNAME} (expires after 7 days).")
    else:
        typer.echo("Run with --yes to quarantine.")


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


@app.command()
def ask(
    query: str = typer.Argument(..., help="Question to answer."),
    project: str | None = typer.Option(None, "--project", "-p", help="Project path."),
    scope: str = typer.Option("all", help="Scope: all|architecture."),
) -> None:
    """Assemble context for a codebase question (LLM-free; GPU rerank only)."""
    from rag_search.query.ask import run_ask
    # The CLI (unlike the shared daemon) has a real cwd — default to the repo the user stands in.
    typer.echo(run_ask(query, project or os.getcwd(), scope))


@app.command()
def graph(
    symbol: str = typer.Argument(..., help="Symbol to analyze."),
    project: str | None = typer.Option(None, "--project", "-p", help="Project path."),
    relation: str = typer.Option("definition", help="definition|callers|callees|impact|impact_narrative|path."),
    to_symbol: str = typer.Option("", "--to-symbol", help="Target symbol for path."),
) -> None:
    """Analyze call graph for a symbol."""
    from rag_search.query.graph_handler import run_graph
    typer.echo(run_graph(symbol, project or os.getcwd(), relation, to_symbol))


@app.command()
def overview(
    project: str | None = typer.Option(None, "--project", "-p", help="Project path."),
    what: str = typer.Option("structure", help="structure|communities|status|projects|metrics|..."),
) -> None:
    """Overview of a project (same as MCP overview tool)."""
    from rag_search.server._overview import handle_overview
    typer.echo(handle_overview(project or os.getcwd(), what))


@app.command()
def status() -> None:
    """Show daemon status and registered projects."""
    from rag_search.core.config import DAEMON_HOST, DAEMON_PORT
    from rag_search.daemon.server import ensure_running
    running = ensure_running(DAEMON_HOST, DAEMON_PORT)
    typer.echo(f"Daemon: {'UP' if running else 'DOWN'} ({DAEMON_HOST}:{DAEMON_PORT})")
    list_projects_cmd()


def rse_index_main() -> None:
    """One-shot onboarding: index → label communities."""
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    import pathlib
    resolved = str(pathlib.Path(path).expanduser().resolve())
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import upsert_project
    from rag_search.daemon.sweeps import _index_project, _label_project
    print(f"Indexing {resolved}...")
    upsert_project(ProjectEntry(path=resolved, enabled=True))
    _index_project(resolved)
    print("Labelling communities...")
    _label_project(resolved)
    print("Done.")


def main() -> None:
    app()
