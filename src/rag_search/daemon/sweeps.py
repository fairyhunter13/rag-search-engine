"""Background sweep jobs: maintenance; event-driven on_change KB enrich."""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading

# Max DeepSeek completion tokens spent on L1 community narration per _enrich_project run.
# Prevents runaway cost on unexpectedly large projects.  Default 50k ≈ ≤250 head communities.
_ENRICH_BUDGET_TOKENS: int = int(os.environ.get("RSE_ENRICH_BUDGET_TOKENS", "50000"))

_PAUSED: bool = False
_KB_DEBOUNCE_S: float = 45.0  # min seconds between KB rebuilds per project after a file change
_BPRE_CASCADE_DEBOUNCE_S: float = 45.0  # min seconds between owning-root BPRE/federation regens
_INDEX_BACKOFF_S: float = 120.0  # min seconds before retrying a failed incremental reindex
_INCREMENTAL_COMPACT_AT: int = 50  # incremental graph passes before one authoritative re-derive
_last_kb_enrich: dict[str, float] = {}
_last_index_fail: dict[str, float] = {}
# Files whose graph rows are owed but whose event lost the debounce race, per project. The
# debounce *drops* events rather than queueing them, which cost only a delayed enrich before the
# graph rode this gate: enrich is project-scoped, so the next event redid the same work. The
# graph path is file-list-scoped, and it stamps source_sig as if the whole tree were re-derived —
# so a dropped list is invisible to _graph_stale afterwards and only the compaction counter ever
# recovers it. Measured live: a delete 26s behind an add left a symbol for a deleted file while
# the stored sig matched the tree exactly.
_pending_graph_files: dict[str, set[str]] = {}
_last_owning_process_regen: dict[str, float] = {}  # debounce per federation root
_last_owning_federation_regen: dict[str, float] = {}  # debounce per federation root
_bpre_state: dict = {"last_run": None, "edge_count": 0, "last_error": None}
# Source-fingerprint memo: path → (coarse_dir_mtime, sig). Avoids re-walking unchanged projects.
_fingerprint_cache: dict[str, tuple[float, str]] = {}
# Drift gate: path → sig at last successful _enrich_project. Skips KB cascade when unchanged.
_last_enriched_sig: dict[str, str] = {}
# Set while reconcile_projects() is running a bulk pass. Suppresses per-member BPRE fan-out
# (_regen_owning_processes / self-BPRE in _enrich_project) so the reconcile root-pass is the
# sole BPRE trigger during bulk reconcile — avoids rebuilding the same root repeatedly on a
# mid-pass-shifting source sig.
_reconcile_active = threading.Event()
# Serializes CPU-bound KB work (community recompute / wiki / BPRE / index_docs) across the
# watcher and reconcile threads so at most one heavy pass runs at a time — caps daemon CPU at
# ~one core instead of pinning two concurrently. Never held around index/embed or GPU queries.
_KB_HEAVY_LOCK = threading.Lock()

log = logging.getLogger(__name__)

# Composite pipeline algorithm version — bump either component constant to trigger re-derive.
# Also folds a SHA-4 of key pipeline modules so code-only changes self-heal without a manual bump.
def _code_fingerprint() -> str:
    """4-char SHA over source bytes of modules that determine graph output."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]  # src/rag_search/
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
    from rag_search.graph.community import ALGO_VERSION
    return f"{ALGO_VERSION}+{_code_fingerprint()}"


def _source_fingerprint(path: str) -> str:
    """SHA-1 over sorted 'relpath:mtime' for every project file — stat-only, GPU-free.

    Coarse pre-gate: if the project root dir mtime and file-count are unchanged since the
    last call, return the cached sig (avoids the full stat-walk for quiescent projects).
    """
    from pathlib import Path

    from rag_search.index.discover import iter_files
    root = Path(path)
    # Coarse check: root dir mtime as a fast pre-gate before the full stat-walk.
    try:
        coarse = root.stat().st_mtime
    except OSError:
        coarse = 0.0
    cached = _fingerprint_cache.get(path)
    if cached is not None and cached[0] == coarse:
        return cached[1]
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
    sig = hashlib.sha1("\n".join(parts).encode()).hexdigest()
    _fingerprint_cache[path] = (coarse, sig)
    return sig


# Code-only fingerprint memo (HR38): same 'relpath:mtime' shape as _fingerprint_cache but
# filtered to is_code_language files only, mirroring kb.bpre._bpre_code_sig (HR36) so the
# enrich/wiki/BPRE cascade gate and the graph re-derive gate are code-only like BPRE's own
# reuse stamp — non-code churn (docs/wiki/config/images) never wakes either.
_code_fingerprint_cache: dict[str, tuple[float, str]] = {}


def _code_source_fingerprint(path: str) -> str:
    """SHA-1 over sorted 'relpath:mtime' for CODE files only — stat-only, GPU-free."""
    from pathlib import Path

    from rag_search.index.discover import (
        detect_language,
        is_code_language,
        is_generated_path,
        iter_files,
    )
    root = Path(path)
    try:
        coarse = root.stat().st_mtime
    except OSError:
        coarse = 0.0
    cached = _code_fingerprint_cache.get(path)
    if cached is not None and cached[0] == coarse:
        return cached[1]
    parts: list[str] = []
    try:
        for f in iter_files(root, federation_mode=True):
            if not is_code_language(detect_language(f)):
                continue
            if is_generated_path(f.name):
                continue  # derived codegen output — regen is not source drift (HR38)
            try:
                rel = str(f.relative_to(root))
                mtime = int(f.stat().st_mtime)
                parts.append(f"{rel}:{mtime}")
            except (OSError, ValueError):
                pass
    except Exception:
        pass
    parts.sort()
    sig = hashlib.sha1("\n".join(parts).encode()).hexdigest()
    _code_fingerprint_cache[path] = (coarse, sig)
    return sig


def _vectors_stale(path: str) -> str | None:
    """The index's recorded embed signature if it disagrees with the running config.

    Mirrors _graph_stale for the vector side. Vectors are only comparable to a query
    embedded the same way, so a model or token-budget change silently invalidates
    every stored vector — this is what makes that visible instead of permanent.
    """
    from rag_search.core.config import project_vector_db
    from rag_search.index.store import VectorStore

    vdb = project_vector_db(path)
    if not vdb.exists():
        return None
    vs = VectorStore(vdb)
    try:
        return vs.stale_signature()
    except Exception:
        return None
    finally:
        vs.close()


def _graph_stale(path: str, gs) -> bool:  # gs: GraphStore
    """True if algo-version or code-only source fingerprint has drifted from stored stamps.

    Also true once enough incremental watcher passes have accumulated: `symbol_id` is
    (file, name, start_line), so inserting a line shifts every symbol below it and silently
    drops the *incoming* edges from files the watcher did not re-scan. Full re-derive stays
    the backstop — the incremental path is a fast path, not a replacement.
    """
    return (
        gs.get_meta("algo_version") != _pipeline_algo_version()
        or int(gs.get_meta("incremental_since_full") or 0) >= _INCREMENTAL_COMPACT_AT
        or gs.get_meta("source_sig") != _code_source_fingerprint(path)
    )


def _extract_graph(gs, root, only: list | None = None) -> None:
    """Extract symbols + call edges from source into gs (caller must gs.clear() first).

    `only` restricts both passes to those files (the watcher's incremental path); the callee
    resolution tables are still built from the whole symbol table, so calls into untouched
    files still resolve. Pass `only=None` for a full re-derive.
    """
    from rag_search.graph.extractor import (
        extract_calls_with_lines,
        extract_symbols,
        symbol_id,
    )
    from rag_search.index.bounded_parse import PARSE_TIMEOUT, run_bounded
    from rag_search.index.discover import detect_language, iter_files
    targets = list(only) if only is not None else None
    for fpath in (targets if targets is not None else iter_files(root, federation_mode=True)):
        try:
            content = fpath.read_text(errors="replace")
        except OSError:
            continue
        lang = detect_language(fpath)
        syms = run_bounded(extract_symbols, (fpath, content, lang), path_for_log=str(fpath))
        if not syms or syms == PARSE_TIMEOUT:
            continue
        for sym in syms:
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
    for fpath in (targets if targets is not None else iter_files(root, federation_mode=True)):
        fstr = str(fpath)
        sym_spans = file_to_sym_spans.get(fstr)
        if not sym_spans:
            continue
        try:
            content = fpath.read_text(errors="replace") if fpath.exists() else ""
        except OSError:
            continue
        call_sites = run_bounded(extract_calls_with_lines, (content, detect_language(fpath)),
                                  path_for_log=fstr)
        if not call_sites or call_sites == PARSE_TIMEOUT:
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


def _persist_partition_quality(gs) -> None:  # type: ignore[no-untyped-def]
    """Persist partition-quality verdict to the meta table immediately after detect_communities.
    The sig (symbol:edge:community counts) self-invalidates on any content change, so the
    read-side (overview status) can use the cached verdict for O(1) lookup instead of a full scan."""
    import json as _json

    from rag_search.graph.quality import partition_quality
    hq = partition_quality(gs)
    sig = f"{gs.symbol_count()}:{gs.edge_count()}:{gs.community_count()}"
    gs.set_meta("partition_quality", _json.dumps({"sig": sig, "q": hq}))


def _rederive_graph(project_path: str) -> None:
    """Re-extract symbols+edges, re-detect communities, wipe stale L2+, stamp meta."""
    from pathlib import Path

    from rag_search.core.config import project_graph_db
    from rag_search.graph.community import detect_communities
    from rag_search.graph.store import GraphStore
    root = Path(project_path)
    gs = GraphStore(project_graph_db(project_path))
    try:
        gs.clear()
        _extract_graph(gs, root)
        detect_communities(gs)
        gs._con.execute("DELETE FROM communities WHERE level>=2")
        gs.commit()
        gs.set_meta("algo_version", _pipeline_algo_version())
        gs.set_meta("source_sig", _code_source_fingerprint(project_path))
        gs.set_meta("incremental_since_full", "0")
        _persist_partition_quality(gs)
        gs.commit()
    finally:
        gs.close()
    log.info("_rederive_graph %s: re-extracted and re-detected", project_path)


def _graph_needs_update(project_path: str) -> bool:
    """True if the watcher should re-extract this project's graph for the current change.

    Mirrors reconcile's own conditions so the two paths cannot drift apart, minus the cases
    that belong to reconcile: a project with no graph.db yet has never been indexed, and a
    federation root has 0 own communities by design (HR4), so neither is the watcher's job.
    Reuses the memoised code-only fingerprint on_change has already computed — no second walk.
    """
    from rag_search.core.config import project_graph_db
    from rag_search.core.registry import list_projects
    from rag_search.graph.store import GraphStore
    gdb = project_graph_db(project_path)
    if not gdb.exists():
        return False
    if any(e.path == project_path and e.federation for e in list_projects()):
        return False
    gs = GraphStore(gdb)
    try:
        return _graph_stale(project_path, gs)
    finally:
        gs.close()


def _update_graph_files(project_path: str, files: list) -> None:
    """Re-extract just `files` into the existing graph, then re-detect communities whole.

    Extraction is single-worker-bound (HR40) and scales with repo size, not edit size — a full
    re-derive costs ~190s on a 17k-file repo, which is not something a 45s debounce window can
    afford. detect_communities is <4s even there, so it is simply re-run.

    Deletion is deliberately not filtered by is_ignored_path: a file that was removed, renamed
    or newly gitignored must still lose its rows, and `upsert_symbol` alone never subtracts.

    An algo bump or an exhausted compaction budget falls through to the full re-derive here
    rather than being left for reconcile — reconcile is startup-once by design, so deferring
    to it would freeze that project's graph until the next daemon restart.
    """
    from pathlib import Path

    from rag_search.core.config import project_graph_db
    from rag_search.graph.community import detect_communities
    from rag_search.graph.store import GraphStore
    from rag_search.index.discover import detect_language, is_code_language, is_ignored_path
    root = Path(project_path)
    changed = [Path(f) for f in files if is_code_language(detect_language(Path(f)))]
    if not changed:
        return
    targets = [p for p in changed if p.exists() and not is_ignored_path(p, root)]
    owed = True  # assume the expensive path until the cheap one is proven safe
    gs = GraphStore(project_graph_db(project_path))
    try:
        owed = (gs.get_meta("algo_version") != _pipeline_algo_version()
                or int(gs.get_meta("incremental_since_full") or 0) >= _INCREMENTAL_COMPACT_AT)
        if not owed:
            for fpath in changed:
                gs.delete_file_symbols(str(fpath))
            gs.commit()
            if targets:
                _extract_graph(gs, root, only=targets)
            # Only now: a symbol whose start_line did not move keeps its sid, so its incoming
            # edges are live again and must not be swept as dangling.
            gs.purge_dangling_edges()
            detect_communities(gs)
            gs._con.execute("DELETE FROM communities WHERE level>=2")
            passes = int(gs.get_meta("incremental_since_full") or 0) + 1
            gs.set_meta("incremental_since_full", str(passes))
            gs.set_meta("source_sig", _code_source_fingerprint(project_path))
            _persist_partition_quality(gs)
            gs.commit()
            log.info("_update_graph_files %s: %d file(s) re-extracted (pass %d)",
                     project_path, len(targets), passes)
    finally:
        gs.close()
    if owed:
        _rederive_graph(project_path)  # must run with the store closed — it reopens it


def _reindex_vectors(project_path: str) -> None:
    """Rebuild only the vector index, leaving the graph and its summaries intact.

    An embed-signature drift invalidates vectors and nothing else — chunk shape has
    no bearing on the tree-sitter graph or the LLM narration derived from it. Routing
    drift through _index_project would gs.clear() the graph and force every community
    to be re-narrated, spending a fleet's worth of DeepSeek calls to fix a chunking bug.
    """
    from pathlib import Path

    from rag_search.core.config import project_vector_db
    from rag_search.core.registry import get_project, upsert_project
    from rag_search.embed.embedder import get_embedder
    from rag_search.index.indexer import index_project
    from rag_search.index.store import VectorStore

    vs = VectorStore(project_vector_db(project_path))
    try:
        file_count, chunk_count = index_project(Path(project_path), get_embedder(), vs)
    finally:
        vs.close()
    # indexed_at is deliberately left alone: it marks graph+vector completeness for
    # _needs_index, and this pass rebuilt only half of that.
    if (entry := get_project(project_path)) is not None:
        entry.file_count = file_count
        entry.chunk_count = chunk_count
        upsert_project(entry)
    log.info("_reindex_vectors %s: %d files -> %d chunks", project_path, file_count, chunk_count)


def _needs_index(path: str) -> bool:
    """True if this project's index is absent or never completed.

    Keys on registry indexed_at (set only at the end of a successful _index_project)
    so a partial/aborted index with stray chunks is still treated as needing re-index.
    """
    import sqlite3

    from rag_search.core.config import project_vector_db
    from rag_search.core.registry import get_project

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

    from rag_search.core.config import project_graph_db

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
    project with missing community summaries (any level).  Safe to call repeatedly.

    While running, suppresses per-member BPRE fan-out (_reconcile_active) so the federation
    root-pass below is the sole BPRE trigger for a bulk pass — see Part D / sweeps.py module
    docstring for why (avoids rebuilding the same root repeatedly on a mid-pass-shifting sig).
    """
    if _PAUSED:
        return
    from rag_search.core.config import project_graph_db
    from rag_search.core.registry import list_projects
    from rag_search.daemon.federation import register_all_members
    from rag_search.graph.store import GraphStore

    _reconcile_active.set()
    try:
        try:
            register_all_members()
        except Exception as exc:
            log.warning("reconcile member-discovery: %s", exc)

        from rag_search.core.config import is_federation_excluded

        # Most-recently-touched projects first. The pass is one linear walk with no ordering,
        # and it is startup-once by design, so an unordered registry starves exactly the repos
        # being worked on: a measured pass here ground through 198 projects for 7.6 h without
        # reaching either repo edited that day. One sort key, no new timer, same work per pass.
        for entry in sorted(list_projects(), key=lambda e: e.last_change_seen or "", reverse=True):
            if _PAUSED:
                return
            if not entry.enabled:
                continue
            if is_federation_excluded(entry.path):
                continue
            needs_idx = _needs_index(entry.path)
            needs_rederive = needs_vectors = False
            if not needs_idx and (drifted := _vectors_stale(entry.path)):
                from rag_search.core.config import AUTO_MIGRATE_VECTORS
                from rag_search.index.store import embed_signature
                log.warning(
                    "%s: vectors built by %r, config is now %r — %s",
                    entry.path, drifted, embed_signature(),
                    "re-embedding" if AUTO_MIGRATE_VECTORS
                    else "set RSE_AUTO_MIGRATE_VECTORS=1 to migrate",
                )
                needs_vectors = bool(AUTO_MIGRATE_VECTORS)
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
                # Orthogonal to the chain below, not a branch of it: re-embedding touches
                # only vectors.db, so it neither satisfies nor preempts a graph rederive.
                # needs_vectors is only ever set when needs_idx is False, so the two can
                # never both rebuild the same vectors.
                if needs_vectors:
                    _reindex_vectors(entry.path)
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

        # Federation root-pass: reconstruct BPRE process graph (backstop for quiescent fleet;
        # sole BPRE trigger during this bulk pass — see _reconcile_active above). Unconditional:
        # reconstruct_processes() carries its own persistent stamp (bpre_algo/bpre_source_sig in
        # process_graph.db) and is a cheap no-op on an unchanged root, so no extra in-memory gate
        # is needed here — and a persistent gate survives restarts, unlike one kept in memory.
        for entry in list_projects():
            if _PAUSED:
                return
            if not entry.enabled or not entry.federation:
                continue
            if is_federation_excluded(entry.path):
                continue
            try:
                from rag_search.kb.bpre import reconstruct_processes
                with _KB_HEAVY_LOCK:
                    n = reconstruct_processes(entry.path)
                _bpre_state["last_run"] = entry.path
                _bpre_state["edge_count"] = n
                _bpre_state["last_error"] = None
            except Exception as exc:
                log.warning("reconcile bpre %s: %s", entry.path, exc)
                _bpre_state["last_error"] = str(exc)
    finally:
        _reconcile_active.clear()


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

    from rag_search.core.config import (
        INDEX_ROOT,
        index_dir,
        project_graph_db,
        project_vector_db,
    )
    from rag_search.core.registry import list_projects

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

    from rag_search.core.config import project_graph_db, project_vector_db
    from rag_search.embed.embedder import get_embedder
    from rag_search.graph.community import detect_communities
    from rag_search.graph.store import GraphStore
    from rag_search.index.indexer import index_project
    from rag_search.index.store import VectorStore

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
        gs.set_meta("source_sig", _code_source_fingerprint(project_path))
        gs.set_meta("incremental_since_full", "0")  # this IS the authoritative full pass
        _persist_partition_quality(gs)
        gs.commit()
    finally:
        gs.close()

    from datetime import UTC, datetime

    from rag_search.core.registry import get_project, upsert_project
    entry = get_project(project_path)
    if entry is not None:
        entry.indexed_at = datetime.now(UTC).isoformat()
        entry.last_change_seen = entry.indexed_at
        entry.file_count = file_count
        entry.chunk_count = chunk_count
        upsert_project(entry)


def _index_files(project_path: str, files: list) -> None:
    """Incremental reindex: re-embed only the changed files (no full-project rescan)."""
    from pathlib import Path

    from rag_search.core.config import project_vector_db
    from rag_search.core.index_config import effective_config
    from rag_search.embed.embedder import get_embedder
    from rag_search.index.discover import is_ignored_path
    from rag_search.index.indexer import index_files
    from rag_search.index.store import VectorStore

    root = Path(project_path)
    cfg = effective_config(root)
    filtered = [
        Path(str(f)) for f in files
        if not is_ignored_path(Path(str(f)), root, cfg)
    ]
    if not filtered:
        return
    vs = VectorStore(project_vector_db(project_path))
    try:
        index_files(filtered, get_embedder(), vs, project_root=root)
    finally:
        vs.close()

    from datetime import UTC, datetime

    from rag_search.core.registry import get_project, upsert_project
    entry = get_project(project_path)
    if entry is not None:
        entry.last_change_seen = datetime.now(UTC).isoformat()
        upsert_project(entry)


def _enrich_project(project_path: str) -> None:
    from rag_search.graph.llm import deepseek_key
    if not deepseek_key():
        raise RuntimeError(
            "KB build requires DEEPSEEK_API_KEY — local generative LLM is decommissioned. "
            "Set DEEPSEEK_API_KEY in env or ~/.config/rag-search/env."
        )

    # Single-flight: at most one CPU-bound KB pass (this function + the reconcile BPRE
    # root-pass) runs at a time across the watcher and reconcile threads — caps daemon CPU at
    # ~one core instead of pinning two concurrently. Never held around index/embed or GPU
    # queries, so search freshness and query latency are unaffected.
    with _KB_HEAVY_LOCK:
        from rag_search.core.config import THERMAL_MAX_C, project_graph_db, project_wiki_dir
        from rag_search.core.gpu import gpu_temp_c
        from rag_search.graph.community import label_community_structural
        from rag_search.graph.enrich import (
            classify_communities_semantic,
            compute_significance,
            enrich_communities_batch,
        )
        from rag_search.graph.store import GraphStore
        from rag_search.kb.wiki import build_wiki

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
            from rag_search.kb.wiki import build_federated_index
            build_federated_index(project_path)
            _regen_owning_federations(project_path)
        except Exception as exc:
            log.warning("federation index %s: %s", project_path, exc)
        # Member-edit refresh + self-BPRE: skipped during a bulk reconcile pass (_reconcile_active)
        # — the reconcile root-pass is the sole BPRE trigger then, so the same root is not rebuilt
        # once per member on a mid-pass-shifting source sig (Part D). Steady-state on_change still
        # triggers both normally.
        if not _reconcile_active.is_set():
            try:
                _regen_owning_processes(project_path)
            except Exception as exc:
                log.error("owning-process regen %s: %s", project_path, exc, exc_info=True)
            try:
                from rag_search.daemon.federation import expand_federation
                if len(expand_federation(project_path)) >= 2:
                    from rag_search.kb.bpre import reconstruct_processes
                    n = reconstruct_processes(project_path)
                    _bpre_state["last_run"] = project_path
                    _bpre_state["edge_count"] = n
                    _bpre_state["last_error"] = None
            except Exception as exc:
                log.error("bpre reconstruct %s: %s", project_path, exc, exc_info=True)
                _bpre_state["last_error"] = str(exc)
        # Docgen is manual-trigger only (CLI/dashboard). Not wired into the auto-pipeline.
        # Re-embed generated docs/ under scope=docs (HR28); no-op if no generated docs/ exists.
        try:
            from rag_search.core.config import project_vector_db
            from rag_search.embed.embedder import get_embedder
            from rag_search.index.indexer import index_docs
            from rag_search.index.store import VectorStore
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
    import time

    from rag_search.core.registry import list_projects
    from rag_search.kb.wiki import build_federated_index
    now = time.monotonic()
    for entry in list_projects():
        if entry.enabled and entry.federation and member_path in entry.federation:
            if now - _last_owning_federation_regen.get(entry.path, 0.0) < _BPRE_CASCADE_DEBOUNCE_S:
                continue
            # Stamp BEFORE the long operation so concurrent triggers see the lock.
            _last_owning_federation_regen[entry.path] = now
            try:
                build_federated_index(entry.path)
            except Exception as exc:
                log.warning("owning-federation regen %s: %s", entry.path, exc)


def _regen_owning_processes(member_path: str) -> None:
    """Reconstruct processes for any enabled root whose federation contains member_path (HR14)."""
    import time

    from rag_search.core.registry import list_projects
    from rag_search.kb.bpre import reconstruct_processes
    now = time.monotonic()
    for entry in list_projects():
        if entry.enabled and entry.federation and member_path in entry.federation:
            if now - _last_owning_process_regen.get(entry.path, 0.0) < _BPRE_CASCADE_DEBOUNCE_S:
                continue
            # Stamp BEFORE the long operation so concurrent triggers see the lock.
            _last_owning_process_regen[entry.path] = now
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
    # Invalidate fingerprint caches so the next reconcile pass re-walks this project.
    _fingerprint_cache.pop(project_path, None)
    _code_fingerprint_cache.pop(project_path, None)
    from rag_search.kb.bpre import _invalidate_bpre_code_sig
    _invalidate_bpre_code_sig(project_path)
    try:
        if files:
            _index_files(project_path, files)
        else:
            _index_project(project_path)
    except Exception as exc:
        log.warning("incremental reindex %s: %s", project_path, exc)
        _last_index_fail[project_path] = now  # back off before retrying
        return
    # HR38: code-only sig (mirrors BPRE's HR36 stamp) — non-code churn (docs/wiki/config/
    # images) never wakes the KB/wiki/BPRE cascade, only real source drift does.
    sig = _code_source_fingerprint(project_path)
    if sig == _last_enriched_sig.get(project_path):
        return  # source unchanged — KB/wiki/BPRE cascade not needed
    if files:
        _pending_graph_files.setdefault(project_path, set()).update(str(f) for f in files)
    if now - _last_kb_enrich.get(project_path, 0.0) < _KB_DEBOUNCE_S:
        return  # carried in _pending_graph_files above, so this event's files are not lost
    _last_kb_enrich[project_path] = now
    pending = _pending_graph_files.pop(project_path, set())
    try:
        if pending and _graph_needs_update(project_path):
            # Must release before _enrich_project: _KB_HEAVY_LOCK is a plain threading.Lock
            # and _enrich_project acquires it itself, so holding it across that call would
            # deadlock the watcher thread. Held here so extraction can never run concurrently
            # with another heavy KB pass (the daemon's ~1-core budget).
            with _KB_HEAVY_LOCK:
                _update_graph_files(project_path, sorted(pending))
        _enrich_project(project_path)
        _last_enriched_sig[project_path] = sig
    except Exception as exc:
        # Put the batch back rather than stamping it done: source_sig is only written by a pass
        # that completed, so a lost batch here would leave the same silent phantom rows.
        _pending_graph_files.setdefault(project_path, set()).update(pending)
        log.warning("kb enrich on_change %s: %s", project_path, exc)


def burst_enrich_federation(root_path: str) -> dict:
    """Burst-enrich root + all discovered federation members. Return aggregate totals."""
    from rag_search.core.config import project_graph_db
    from rag_search.daemon.federation import discover_members
    from rag_search.graph.store import GraphStore

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
