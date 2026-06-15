"""Background sweep jobs: auto_index, kb_sweep, maintenance."""
from __future__ import annotations

import logging
import shutil

_PAUSED: bool = False

log = logging.getLogger(__name__)


def _needs_index(path: str) -> bool:
    """True if this project has no vector chunks (missing or empty DB)."""
    import sqlite3

    from opencode_search.core.config import project_vector_db
    vdb = project_vector_db(path)
    if not vdb.exists():
        return True
    try:
        with sqlite3.connect(str(vdb)) as con:
            return con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    except Exception:
        return True


def auto_index() -> None:
    """Index any registered project whose vector DB is missing or empty."""
    if _PAUSED:
        return
    from opencode_search.core.registry import list_projects
    from opencode_search.daemon.federation import index_members

    for entry in list_projects():
        if not entry.enabled:
            continue
        try:
            index_members(entry.path)
        except Exception as exc:
            log.warning("federation discovery %s: %s", entry.path, exc)
        if not _needs_index(entry.path):
            continue
        try:
            _index_project(entry.path)
        except Exception as exc:
            log.warning("auto_index %s: %s", entry.path, exc)


def kb_sweep() -> None:
    """Enrich symbols + communities for all indexed projects."""
    if _PAUSED:
        return
    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import list_projects

    for entry in list_projects():
        if not entry.enabled:
            continue
        gdb = project_graph_db(entry.path)
        if not gdb.exists():
            continue
        try:
            _enrich_project(entry.path)
        except Exception as exc:
            log.warning("kb_sweep %s: %s", entry.path, exc)


def maintenance() -> None:
    """Vacuum orphan index dirs not present in the registry."""
    if _PAUSED:
        return
    from opencode_search.core.config import INDEX_ROOT, index_dir
    from opencode_search.core.registry import list_projects

    if not INDEX_ROOT.exists():
        return
    known = {index_dir(p.path).name for p in list_projects()}
    for d in INDEX_ROOT.iterdir():
        if d.is_dir() and d.name not in known:
            log.info("vacuum orphan: %s", d)
            shutil.rmtree(d, ignore_errors=True)


def _index_project(project_path: str) -> None:
    from pathlib import Path

    from opencode_search.core.config import project_graph_db, project_vector_db
    from opencode_search.embed.embedder import get_embedder
    from opencode_search.graph.community import detect_communities
    from opencode_search.graph.extractor import extract_calls, extract_symbols, symbol_id
    from opencode_search.graph.store import GraphStore
    from opencode_search.index.discover import detect_language, iter_files
    from opencode_search.index.indexer import index_project
    from opencode_search.index.store import VectorStore

    root = Path(project_path)
    embedder = get_embedder()

    # 1. Chunk + embed → vectors.db
    vs = VectorStore(project_vector_db(project_path))
    try:
        file_count, chunk_count = index_project(root, embedder, vs)
    finally:
        vs.close()

    # 2. Tree-sitter extract → graph.db
    gs = GraphStore(project_graph_db(project_path))
    try:
        for fpath in iter_files(root, federation_mode=True):
            try:
                content = fpath.read_text(errors="replace")
            except OSError:
                continue
            lang = detect_language(fpath)
            for sym in extract_symbols(fpath, content, lang):
                sid = symbol_id(sym.file, sym.name, sym.start_line)
                gs.upsert_symbol(
                    sid, sym.name, sym.qualified_name, sym.kind,
                    sym.file, sym.start_line, sym.end_line, sym.language,
                    sym.signature, sym.docstring,
                )
        gs.commit()
        gs.dedup_symbols()
        # 2b. Call-edge extraction — second pass: names now known, resolve callees
        file_to_sids: dict[str, list[str]] = {}
        name_to_entries: dict[str, list[tuple[str, str]]] = {}
        for (sid, name, fstr) in gs._con.execute("SELECT sid, name, file FROM symbols"):
            file_to_sids.setdefault(fstr, []).append(sid)
            name_to_entries.setdefault(name, []).append((sid, fstr))
        for fpath in iter_files(root, federation_mode=True):
            call_names = extract_calls(
                fpath.read_text(errors="replace") if fpath.exists() else "",
                detect_language(fpath),
            )
            if not call_names:
                continue
            fstr = str(fpath)
            caller_sids = file_to_sids.get(fstr, [])
            if not caller_sids:
                continue
            caller_sid = caller_sids[0]  # one representative caller per file
            for name in set(call_names):
                for (callee_sid, callee_file) in name_to_entries.get(name, []):
                    if callee_file != fstr:
                        gs.upsert_edge(caller_sid, callee_sid)
        gs.commit()
        # 3. Leiden community detection
        detect_communities(gs)
    finally:
        gs.close()

    from datetime import UTC, datetime

    from opencode_search.core.registry import get_project, upsert_project
    entry = get_project(project_path)
    if entry is not None:
        entry.indexed_at = datetime.now(UTC).isoformat()
        entry.file_count = file_count
        entry.chunk_count = chunk_count
        upsert_project(entry)


def _index_files(project_path: str, files: list) -> None:
    """Incremental reindex: re-embed only the changed files (no full-project rescan)."""
    from pathlib import Path

    from opencode_search.core.config import project_vector_db
    from opencode_search.embed.embedder import get_embedder
    from opencode_search.index.indexer import index_files
    from opencode_search.index.store import VectorStore

    vs = VectorStore(project_vector_db(project_path))
    try:
        index_files([Path(str(f)) for f in files], get_embedder(), vs)
    finally:
        vs.close()


def _enrich_project(project_path: str) -> None:
    import time

    from opencode_search.core.config import THERMAL_MAX_C, project_graph_db, project_wiki_dir
    from opencode_search.core.gpu import gpu_temp_c
    from opencode_search.graph.enrich import enrich_community
    from opencode_search.graph.store import GraphStore
    from opencode_search.kb.hierarchy import build_hierarchy
    from opencode_search.kb.wiki import build_wiki

    gs = GraphStore(project_graph_db(project_path))
    try:
        gs._con.execute(
            "DELETE FROM communities WHERE level=1 AND id NOT IN "
            "(SELECT DISTINCT community_id FROM symbols WHERE community_id IS NOT NULL)"
        )
        gs.commit()
        for (cid,) in gs._con.execute(
            "SELECT id FROM communities WHERE (summary IS NULL OR summary = '') AND level = 1"
        ).fetchall():
            enrich_community(gs, cid)
            if gpu_temp_c() > THERMAL_MAX_C:
                time.sleep(5)
        gs.commit()
        build_hierarchy(gs)
        build_wiki(gs, project_wiki_dir(project_path))
    finally:
        gs.close()


def on_change(project_path: str, files: list) -> None:
    """Watcher callback: incremental reindex changed files (or full reindex if no file list)."""
    try:
        if files:
            _index_files(project_path, files)
        else:
            _index_project(project_path)
    except Exception as exc:
        log.warning("incremental reindex %s: %s", project_path, exc)


def burst_enrich_federation(root_path: str) -> dict:
    """Burst-enrich root + all discovered federation members. Return aggregate totals."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.daemon.federation import discover_members
    from opencode_search.graph.store import GraphStore

    paths = [root_path, *discover_members(root_path)]
    results: list[dict] = []
    for path in paths:
        gdb = project_graph_db(path)
        if not gdb.exists():
            log.info("burst_enrich_federation: skip %s (no graph DB)", path)
            continue
        _enrich_project(path)
        gs = GraphStore(gdb)
        try:
            total = gs._con.execute("SELECT COUNT(*) FROM communities").fetchone()[0]
            pending = gs._con.execute(
                "SELECT COUNT(*) FROM communities WHERE (summary IS NULL OR summary = '') AND level = 1"
            ).fetchone()[0]
        finally:
            gs.close()
        results.append({"path": path, "total": total, "pending": pending})
        log.info("burst_enrich_federation %s: total=%d pending=%d", path, total, pending)

    total_communities = sum(r["total"] for r in results)
    total_pending = sum(r["pending"] for r in results)
    log.info("burst_enrich_federation %s: Σ=%d pending=%d", root_path, total_communities, total_pending)
    return {"root": root_path, "members": results,
            "total_communities": total_communities, "total_pending": total_pending}
