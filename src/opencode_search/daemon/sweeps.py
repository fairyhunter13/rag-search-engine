"""Background sweep jobs: maintenance; event-driven on_change KB enrich."""
from __future__ import annotations

import logging
import shutil

_PAUSED: bool = False
_KB_DEBOUNCE_S: float = 45.0  # min seconds between KB rebuilds per project after a file change
_INDEX_BACKOFF_S: float = 120.0  # min seconds before retrying a failed incremental reindex
_last_kb_enrich: dict[str, float] = {}
_last_index_fail: dict[str, float] = {}
_bpre_state: dict = {"last_run": None, "edge_count": 0, "last_error": None}

log = logging.getLogger(__name__)


def _needs_index(path: str) -> bool:
    """True if this project's index is absent or never completed.

    Keys on registry indexed_at (set only at the end of a successful _index_project)
    so a partial/aborted index with stray chunks is still treated as needing re-index.
    """
    import sqlite3

    from opencode_search.core.config import project_vector_db
    from opencode_search.core.registry import get_project

    e = get_project(path)
    if e is None or e.indexed_at is None:
        return True  # never completed; stray partial chunks do not count
    vdb = project_vector_db(path)
    if not vdb.exists():
        return True
    try:
        with sqlite3.connect(str(vdb)) as con:
            return con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    except Exception:
        return True


def _needs_enrich(path: str) -> bool:
    """True if any community in this project's graph is missing a summary."""
    import sqlite3

    from opencode_search.core.config import project_graph_db

    gdb = project_graph_db(path)
    if not gdb.exists():
        return False
    try:
        with sqlite3.connect(str(gdb)) as con:
            n = con.execute(
                "SELECT COUNT(*) FROM communities WHERE summary IS NULL OR summary = ''"
            ).fetchone()[0]
            return n > 0
    except Exception:
        return False


def reconcile_projects() -> None:
    """Idempotent: discover+register members, index any unindexed/stalled project, enrich any
    project with missing community summaries (any level).  Safe to call repeatedly."""
    if _PAUSED:
        return
    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import list_projects
    from opencode_search.daemon.federation import register_all_members
    from opencode_search.graph.store import GraphStore

    try:
        register_all_members()
    except Exception as exc:
        log.warning("reconcile member-discovery: %s", exc)

    for entry in list_projects():
        if not entry.enabled:
            continue
        needs_idx = _needs_index(entry.path)
        if not needs_idx:
            gdb = project_graph_db(entry.path)
            if gdb.exists():
                gs = GraphStore(gdb)
                try:
                    needs_idx = gs.community_count() == 0
                finally:
                    gs.close()
        try:
            if needs_idx:
                _index_project(entry.path)
                _enrich_project(entry.path)
            elif _needs_enrich(entry.path):
                _enrich_project(entry.path)  # enrich-only; skip expensive re-index
        except Exception as exc:
            log.warning("reconcile %s: %s", entry.path, exc)


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
    from opencode_search.graph.extractor import (
        extract_calls_with_lines,
        extract_symbols,
        symbol_id,
    )
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
        # 2b. Call-edge extraction — second pass: resolve each call to its enclosing symbol.
        # Build lookup structures from the now-deduped symbol table.
        name_to_entries: dict[str, list[tuple[str, str]]] = {}
        file_to_sym_spans: dict[str, list[tuple[int, int, str]]] = {}
        for (sid, name, fstr, sl, el) in gs._con.execute(
            "SELECT sid, name, file, start_line, end_line FROM symbols"
        ):
            name_to_entries.setdefault(name, []).append((sid, fstr))
            if fstr:
                file_to_sym_spans.setdefault(fstr, []).append((sl, el, sid))
        for spans in file_to_sym_spans.values():
            spans.sort()  # sort by start_line for innermost-enclosing scan
        for fpath in iter_files(root, federation_mode=True):
            fstr = str(fpath)
            sym_spans = file_to_sym_spans.get(fstr)
            if not sym_spans:
                continue
            try:
                content = fpath.read_text(errors="replace") if fpath.exists() else ""
            except OSError:
                continue
            call_sites = extract_calls_with_lines(content, detect_language(fpath))
            if not call_sites:
                continue
            for callee_name, call_line in call_sites:
                # Innermost enclosing symbol = smallest span containing call_line
                caller_sid = ""
                best_span = -1
                for sl, el, sid in sym_spans:
                    if sl <= call_line <= el:
                        span = el - sl
                        if caller_sid == "" or span < best_span:
                            best_span = span
                            caller_sid = sid
                if not caller_sid:
                    continue
                for (callee_sid, callee_file) in name_to_entries.get(callee_name, []):
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

    from opencode_search.graph.llm import deepseek_key
    if not deepseek_key():
        raise RuntimeError(
            "KB build requires DEEPSEEK_API_KEY — local generative LLM is decommissioned. "
            "Set DEEPSEEK_API_KEY in env or ~/.bash_env."
        )

    from opencode_search.core.config import THERMAL_MAX_C, project_graph_db, project_wiki_dir
    from opencode_search.core.gpu import gpu_temp_c
    from opencode_search.graph.enrich import (
        classify_communities_semantic,
        enrich_community,
        enrich_community_l2,
    )
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
        _l2_exists = gs._con.execute(
            "SELECT COUNT(*) FROM communities WHERE level>=2"
        ).fetchone()[0]
        if _l2_exists == 0:
            build_hierarchy(gs)
        for (cid,) in gs._con.execute(
            "SELECT id FROM communities WHERE (summary IS NULL OR summary = '') AND level >= 2"
        ).fetchall():
            enrich_community_l2(gs, cid)
            gs.commit()
            if gpu_temp_c() > THERMAL_MAX_C:
                time.sleep(5)
        # Orphan L2 communities (no L1 children) never get enriched by enrich_community_l2;
        # stamp a placeholder so l2_enriched_pct can reach 100%.
        gs._con.execute(
            "UPDATE communities SET title='(leaf)', summary='(no child communities)' "
            "WHERE level>=2 AND (summary IS NULL OR summary='')"
        )
        gs.commit()
        # Daemon: classify only new/unclassified communities (stable, no churn). The one-time
        # migration of stale projects uses reclassify_all=True explicitly.
        classify_communities_semantic(gs, lambda: gpu_temp_c() > 78, reclassify_all=False)
        build_wiki(gs, project_wiki_dir(project_path))
    finally:
        gs.close()
    # Federated wiki: (re)generate federation.md for this project if it is a root, and for any
    # root that owns it as a member (a member edit refreshes the root's aggregate). HR4 holds —
    # aggregation reads each member's own graph.db; no cross-repo edges. No-op for standalones.
    try:
        from opencode_search.kb.wiki import build_federated_index
        build_federated_index(project_path)
        _regen_owning_federations(project_path)
    except Exception as exc:
        log.warning("federation index %s: %s", project_path, exc)
    # Member-edit refresh: reconstruct processes for any root that owns this project as a member.
    try:
        _regen_owning_processes(project_path)
    except Exception as exc:
        log.error("owning-process regen %s: %s", project_path, exc, exc_info=True)
    # BPRE: reconstruct cross-service processes for federation roots (GPU-free deterministic pass).
    try:
        from opencode_search.daemon.federation import expand_federation
        if len(expand_federation(project_path)) >= 2:
            from opencode_search.kb.bpre import reconstruct_processes
            n = reconstruct_processes(project_path)
            _bpre_state["last_run"] = project_path
            _bpre_state["edge_count"] = n
            _bpre_state["last_error"] = None
    except Exception as exc:
        log.error("bpre reconstruct %s: %s", project_path, exc, exc_info=True)
        _bpre_state["last_error"] = str(exc)


def _regen_owning_federations(member_path: str) -> None:
    """Regenerate federation.md for any enabled root whose federation list contains member_path."""
    from opencode_search.core.registry import list_projects
    from opencode_search.kb.wiki import build_federated_index
    for entry in list_projects():
        if entry.enabled and entry.federation and member_path in entry.federation:
            try:
                build_federated_index(entry.path)
            except Exception as exc:
                log.warning("owning-federation regen %s: %s", entry.path, exc)


def _regen_owning_processes(member_path: str) -> None:
    """Reconstruct processes for any enabled root whose federation contains member_path (HR14)."""
    from opencode_search.core.registry import list_projects
    from opencode_search.kb.bpre import reconstruct_processes
    for entry in list_projects():
        if entry.enabled and entry.federation and member_path in entry.federation:
            try:
                reconstruct_processes(entry.path)
            except Exception as exc:
                log.error("owning-process regen %s: %s", entry.path, exc, exc_info=True)


def on_change(project_path: str, files: list) -> None:
    """Watcher callback: incremental reindex; then KB enrich (debounced) if not recently done."""
    import time

    if _PAUSED:
        return
    now = time.monotonic()
    if now - _last_index_fail.get(project_path, 0.0) < _INDEX_BACKOFF_S:
        return  # in backoff window after a previous failure; skip this event
    try:
        if files:
            _index_files(project_path, files)
        else:
            _index_project(project_path)
    except Exception as exc:
        log.warning("incremental reindex %s: %s", project_path, exc)
        _last_index_fail[project_path] = now  # back off before retrying
        return
    if now - _last_kb_enrich.get(project_path, 0.0) < _KB_DEBOUNCE_S:
        return
    _last_kb_enrich[project_path] = now
    try:
        _enrich_project(project_path)
    except Exception as exc:
        log.warning("kb enrich on_change %s: %s", project_path, exc)


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
