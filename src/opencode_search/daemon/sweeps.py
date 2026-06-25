"""Background sweep jobs: maintenance; event-driven on_change KB enrich."""
from __future__ import annotations

import hashlib
import logging
import os
import shutil

# Max DeepSeek completion tokens spent on L1 community narration per _enrich_project run.
# Prevents runaway cost on unexpectedly large projects.  Default 50k ≈ ≤250 head communities.
_ENRICH_BUDGET_TOKENS: int = int(os.environ.get("OSE_ENRICH_BUDGET_TOKENS", "50000"))

_PAUSED: bool = False
_KB_DEBOUNCE_S: float = 45.0  # min seconds between KB rebuilds per project after a file change
_INDEX_BACKOFF_S: float = 120.0  # min seconds before retrying a failed incremental reindex
_last_kb_enrich: dict[str, float] = {}
_last_index_fail: dict[str, float] = {}
_bpre_state: dict = {"last_run": None, "edge_count": 0, "last_error": None}

log = logging.getLogger(__name__)

# Composite pipeline algorithm version — bump either component constant to trigger re-derive.
# Also folds a SHA-4 of key pipeline modules so code-only changes self-heal without a manual bump.
def _code_fingerprint() -> str:
    """4-char SHA over source bytes of modules that determine graph output."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]  # src/opencode_search/
    modules = [
        root / "graph" / "extractor.py",
        root / "graph" / "enrich.py",
        root / "graph" / "community.py",
    ]
    import contextlib
    h = hashlib.sha1()
    for p in modules:
        with contextlib.suppress(OSError):
            h.update(p.read_bytes())
    return h.hexdigest()[:4]


def _pipeline_algo_version() -> str:
    from opencode_search.graph.community import ALGO_VERSION
    return f"{ALGO_VERSION}+{_code_fingerprint()}"


def _source_fingerprint(path: str) -> str:
    """SHA-1 over sorted 'relpath:mtime' for every project file — stat-only, GPU-free."""
    from pathlib import Path

    from opencode_search.index.discover import iter_files
    root = Path(path)
    parts: list[str] = []
    try:
        for f in iter_files(root, federation_mode=True):
            try:
                rel = str(f.relative_to(root))
                mtime = int(f.stat().st_mtime)
                parts.append(f"{rel}:{mtime}")
            except (OSError, ValueError):
                pass
    except Exception:
        pass
    parts.sort()
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()


def _graph_stale(path: str, gs) -> bool:  # gs: GraphStore
    """True if algo-version or source fingerprint has drifted from stored stamps."""
    return (
        gs.get_meta("algo_version") != _pipeline_algo_version()
        or gs.get_meta("source_sig") != _source_fingerprint(path)
    )


def _extract_graph(gs, root) -> None:
    """Extract symbols + call edges from source into gs (caller must gs.clear() first)."""
    from opencode_search.graph.extractor import (
        extract_calls_with_lines,
        extract_symbols,
        symbol_id,
    )
    from opencode_search.index.discover import detect_language, iter_files
    for fpath in iter_files(root, federation_mode=True):
        try:
            content = fpath.read_text(errors="replace")
        except OSError:
            continue
        lang = detect_language(fpath)
        for sym in extract_symbols(fpath, content, lang):
            if not sym.name:
                continue
            sid = symbol_id(sym.file, sym.name, sym.start_line)
            gs.upsert_symbol(sid, sym.name, sym.qualified_name, sym.kind,
                             sym.file, sym.start_line, sym.end_line, sym.language)
    gs.commit()
    gs.dedup_symbols()
    name_to_entries: dict[str, list[tuple[str, str]]] = {}
    file_to_sym_spans: dict[str, list[tuple[int, int, str]]] = {}
    for (sid, name, fstr, sl, el) in gs._con.execute(
        "SELECT sid, name, file, start_line, end_line FROM symbols"
    ):
        name_to_entries.setdefault(name, []).append((sid, fstr))
        if fstr:
            file_to_sym_spans.setdefault(fstr, []).append((sl, el, sid))
    for spans in file_to_sym_spans.values():
        spans.sort()
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
            caller_sid, best_span = "", -1
            for sl, el, sid in sym_spans:
                if sl <= call_line <= el:
                    span = el - sl
                    if caller_sid == "" or span < best_span:
                        best_span, caller_sid = span, sid
            if not caller_sid:
                continue
            for (callee_sid, callee_file) in name_to_entries.get(callee_name, []):
                if callee_file != fstr:
                    gs.upsert_edge(caller_sid, callee_sid)
    gs.commit()


def _rederive_graph(project_path: str) -> None:
    """Re-extract symbols+edges, re-detect communities, wipe stale L2+, stamp meta."""
    from pathlib import Path

    from opencode_search.core.config import project_graph_db
    from opencode_search.graph.community import detect_communities
    from opencode_search.graph.store import GraphStore
    root = Path(project_path)
    gs = GraphStore(project_graph_db(project_path))
    try:
        gs.clear()
        _extract_graph(gs, root)
        detect_communities(gs)
        gs._con.execute("DELETE FROM communities WHERE level>=2")
        gs.commit()
        gs.set_meta("algo_version", _pipeline_algo_version())
        gs.set_meta("source_sig", _source_fingerprint(project_path))
        gs.commit()
    finally:
        gs.close()
    log.info("_rederive_graph %s: re-extracted and re-detected", project_path)


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
        needs_rederive = False
        # Federation roots have 0 own communities by design (HR4) — skip staleness checks.
        if not needs_idx and not entry.federation:
            gdb = project_graph_db(entry.path)
            if gdb.exists():
                gs = GraphStore(gdb)
                try:
                    if gs.community_count() == 0:
                        needs_idx = True
                    elif _graph_stale(entry.path, gs):
                        needs_rederive = True
                finally:
                    gs.close()
        try:
            if needs_idx:
                _index_project(entry.path)
                _enrich_project(entry.path)
            elif needs_rederive:
                _rederive_graph(entry.path)
                _enrich_project(entry.path)
            elif _needs_enrich(entry.path):
                _enrich_project(entry.path)  # enrich-only; skip expensive re-index
        except Exception as exc:
            log.warning("reconcile %s: %s", entry.path, exc)

    # Federation root-pass: reconstruct BPRE process graph (backstop for quiescent fleet).
    for entry in list_projects():
        if not entry.enabled or not entry.federation:
            continue
        try:
            from opencode_search.kb.bpre import reconstruct_processes
            reconstruct_processes(entry.path)
        except Exception as exc:
            log.warning("reconcile bpre %s: %s", entry.path, exc)


_VACUUM_BLOAT_BYTES: int = 256 * 1024 * 1024  # VACUUM when freelist > 256 MB


def _vacuum_if_bloated(db_path, threshold: int = _VACUUM_BLOAT_BYTES) -> bool:
    """VACUUM db_path when freelist occupies more than threshold bytes. Returns True if vacuumed."""
    import sqlite3
    from pathlib import Path
    p = Path(db_path)
    if not p.exists():
        return False
    try:
        with sqlite3.connect(str(p), timeout=10) as con:
            page_size = con.execute("PRAGMA page_size").fetchone()[0]
            freelist = con.execute("PRAGMA freelist_count").fetchone()[0]
            if page_size * freelist <= threshold:
                return False
            log.info("VACUUM %s (freelist=%d pages, ~%d MB)", p.name,
                     freelist, page_size * freelist // (1024 * 1024))
            con.execute("VACUUM")
            log.info("VACUUM %s done", p.name)
            return True
    except Exception as exc:
        log.warning("VACUUM %s: %s", p.name, exc)
        return False


def maintenance() -> None:
    """Vacuum orphan index dirs; reclaim fragmented SQLite space (bloat-gated)."""
    if _PAUSED:
        return
    import sqlite3  # noqa: F401 — ensure available before list_projects() import

    from opencode_search.core.config import (
        INDEX_ROOT,
        index_dir,
        project_graph_db,
        project_vector_db,
    )
    from opencode_search.core.registry import list_projects

    if not INDEX_ROOT.exists():
        return
    known = {index_dir(p.path).name for p in list_projects()}
    for d in INDEX_ROOT.iterdir():
        if d.is_dir() and d.name not in known:
            log.info("vacuum orphan: %s", d)
            shutil.rmtree(d, ignore_errors=True)

    for entry in list_projects():
        if not entry.enabled:
            continue
        _vacuum_if_bloated(project_vector_db(entry.path))
        _vacuum_if_bloated(project_graph_db(entry.path))


def _index_project(project_path: str) -> None:
    from pathlib import Path

    from opencode_search.core.config import project_graph_db, project_vector_db
    from opencode_search.embed.embedder import get_embedder
    from opencode_search.graph.community import detect_communities
    from opencode_search.graph.store import GraphStore
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

    # 2. Tree-sitter extract + community detection → graph.db; stamp pipeline meta.
    gs = GraphStore(project_graph_db(project_path))
    try:
        gs.clear()
        _extract_graph(gs, root)
        detect_communities(gs)
        gs.set_meta("algo_version", _pipeline_algo_version())
        gs.set_meta("source_sig", _source_fingerprint(project_path))
        gs.commit()
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
    from opencode_search.core.index_config import effective_config, is_excluded
    from opencode_search.embed.embedder import get_embedder
    from opencode_search.index.discover import is_ignored_path
    from opencode_search.index.indexer import index_files
    from opencode_search.index.store import VectorStore

    root = Path(project_path)
    cfg = effective_config(root)
    filtered = [
        Path(str(f)) for f in files
        if not is_ignored_path(Path(str(f)), root)
        and not (cfg.exclude and is_excluded(Path(str(f)), cfg.exclude, root))
    ]
    if not filtered:
        return
    vs = VectorStore(project_vector_db(project_path))
    try:
        index_files(filtered, get_embedder(), vs, project_root=root)
    finally:
        vs.close()


def _enrich_project(project_path: str) -> None:
    from opencode_search.graph.llm import deepseek_key
    if not deepseek_key():
        raise RuntimeError(
            "KB build requires DEEPSEEK_API_KEY — local generative LLM is decommissioned. "
            "Set DEEPSEEK_API_KEY in env or ~/.bash_env."
        )

    from opencode_search.core.config import THERMAL_MAX_C, project_graph_db, project_wiki_dir
    from opencode_search.core.gpu import gpu_temp_c
    from opencode_search.graph.community import label_community_structural
    from opencode_search.graph.enrich import (
        classify_communities_semantic,
        compute_significance,
        enrich_communities_batch,
    )
    from opencode_search.graph.store import GraphStore
    from opencode_search.kb.wiki import build_wiki

    gs = GraphStore(project_graph_db(project_path))
    try:
        gs._con.execute(
            "DELETE FROM communities WHERE level=1 AND id NOT IN "
            "(SELECT DISTINCT community_id FROM symbols WHERE community_id IS NOT NULL)"
        )
        gs.commit()
        # Head (member_count≥8 OR cross-community edges≥2): LLM narration, batched+prefix-cached.
        # Tail (below gate): deterministic structural labels, zero tokens.
        _head_cids, _tail_cids = compute_significance(gs)
        for _cid in _tail_cids:
            label_community_structural(gs, _cid)
        gs.commit()
        if _head_cids:
            _enriched_ids, _usage = enrich_communities_batch(
                gs, _head_cids,
                thermal_guard_fn=lambda: gpu_temp_c() > THERMAL_MAX_C,
                budget=_ENRICH_BUDGET_TOKENS,
            )
            log.info(
                "_enrich_project %s: head=%d tail=%d enriched=%d "
                "tok(hit=%d miss=%d comp=%d) calls=%d",
                project_path, len(_head_cids), len(_tail_cids), len(_enriched_ids),
                _usage.get("prompt_cache_hit_tokens", 0),
                _usage.get("prompt_cache_miss_tokens", 0),
                _usage.get("completion_tokens", 0),
                _usage.get("calls", 0),
            )
        gs.commit()
        gs._con.execute(
            "UPDATE communities SET title='Community-' || CAST(id AS TEXT) "
            "WHERE level=1 AND (title IS NULL OR title='')"
        )
        gs.commit()
        _needs_classify = gs._con.execute(
            "SELECT COUNT(*) FROM communities "
            "WHERE level=1 AND narrated=1 AND summary IS NOT NULL AND summary!='' AND semantic_type IS NULL"
        ).fetchone()[0]
        if _needs_classify:
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
    # Docgen: write C4×Diátaxis docs/ tree into the project repo ($0 by default).
    # OSE_DOCGEN=0 skips; OSE_DOCGEN_LLM=1 enables Haiku narration. Never crashes the pass.
    try:
        from opencode_search.kb.docgen import run_docgen
        run_docgen(project_path)
    except Exception as exc:
        log.error("docgen %s: %s", project_path, exc, exc_info=True)
    # Re-embed generated docs/ under scope=docs (HR28); no-op if no generated docs/ exists.
    try:
        from opencode_search.core.config import project_vector_db
        from opencode_search.embed.embedder import get_embedder
        from opencode_search.index.indexer import index_docs
        from opencode_search.index.store import VectorStore
        _vs = VectorStore(project_vector_db(project_path))
        try:
            _n = index_docs(project_path, get_embedder(), _vs)
            if _n:
                log.info("index_docs %s: %d doc chunks", project_path, _n)
        finally:
            _vs.close()
    except Exception as exc:
        log.error("index_docs %s: %s", project_path, exc, exc_info=True)


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
            total = gs._con.execute("SELECT COUNT(*) FROM communities WHERE level>=1").fetchone()[0]
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
