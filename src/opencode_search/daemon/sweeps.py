"""Background sweep jobs: auto_index, kb_sweep, maintenance."""
from __future__ import annotations

import logging
import shutil

log = logging.getLogger(__name__)


def auto_index() -> None:
    """Index any registered project whose vector DB is missing."""
    from opencode_search.core.config import project_vector_db
    from opencode_search.core.registry import list_projects

    for entry in list_projects():
        if not entry.enabled or project_vector_db(entry.path).exists():
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

    from opencode_search.core.config import project_vector_db
    from opencode_search.embed.embedder import Embedder
    from opencode_search.index.indexer import index_project
    from opencode_search.index.store import VectorStore

    embedder = Embedder()
    embedder.warmup()
    vs = VectorStore(project_vector_db(project_path))
    try:
        index_project(Path(project_path), embedder, vs)
    finally:
        vs.close()


def _enrich_project(project_path: str) -> None:
    from opencode_search.core.config import project_graph_db
    from opencode_search.graph.enrich import enrich_community, enrich_symbols
    from opencode_search.graph.store import GraphStore

    gs = GraphStore(project_graph_db(project_path))
    try:
        enrich_symbols(gs)
        for (cid,) in gs._con.execute(
            "SELECT id FROM communities WHERE title IS NULL LIMIT 20"
        ).fetchall():
            enrich_community(gs, cid)
        gs.commit()
    finally:
        gs.close()
