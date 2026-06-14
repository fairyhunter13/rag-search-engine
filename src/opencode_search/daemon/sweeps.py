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
    from opencode_search.core.registry import list_projects

    for entry in list_projects():
        if not entry.enabled or not _needs_index(entry.path):
            continue
        try:
            _index_project(entry.path)
        except Exception as exc:
            log.warning("auto_index %s: %s", entry.path, exc)


def kb_sweep() -> None:
    """Enrich symbols + communities for all indexed projects."""
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
    from opencode_search.embed.embedder import Embedder
    from opencode_search.graph.community import detect_communities
    from opencode_search.graph.extractor import extract_symbols, symbol_id
    from opencode_search.graph.store import GraphStore
    from opencode_search.index.discover import detect_language, iter_files
    from opencode_search.index.indexer import index_project
    from opencode_search.index.store import VectorStore

    root = Path(project_path)
    embedder = Embedder()
    embedder.warmup()

    # 1. Chunk + embed → vectors.db
    vs = VectorStore(project_vector_db(project_path))
    try:
        index_project(root, embedder, vs)
    finally:
        vs.close()

    # 2. Tree-sitter extract → graph.db
    gs = GraphStore(project_graph_db(project_path))
    try:
        for fpath in iter_files(root):
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
        # 3. Leiden community detection
        detect_communities(gs)
    finally:
        gs.close()


def _enrich_project(project_path: str) -> None:
    from opencode_search.core.config import project_graph_db, project_wiki_dir
    from opencode_search.graph.enrich import enrich_community, enrich_symbols
    from opencode_search.graph.store import GraphStore
    from opencode_search.kb.hierarchy import build_hierarchy
    from opencode_search.kb.wiki import build_wiki

    gs = GraphStore(project_graph_db(project_path))
    try:
        # 4. LLM enrich symbols
        enrich_symbols(gs)
        # 5. LLM enrich communities
        for (cid,) in gs._con.execute(
            "SELECT id FROM communities WHERE title IS NULL LIMIT 20"
        ).fetchall():
            enrich_community(gs, cid)
        gs.commit()
        # 6. L2 hierarchy
        build_hierarchy(gs)
        # 7. Wiki pages
        build_wiki(gs, project_wiki_dir(project_path))
    finally:
        gs.close()
