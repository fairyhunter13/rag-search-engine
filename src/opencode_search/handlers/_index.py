"""Index-project handler: drives the indexer, registry, and watcher setup."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any

from opencode_search.config import (
    DEFAULT_DIMS,
    ProjectEntry,
    get_project_db_path,
    get_project_graph_db_path,
    load_registry,
    save_registry,
)
from opencode_search.discover import is_indexable_file_with_config
from opencode_search.embeddings import get_embed_workers_gpu
from opencode_search.index_config import ProjectConfig, load_project_config
from opencode_search.indexer import index_files as _index_files
from opencode_search.indexer import index_project as _index_project
from opencode_search.search import clear_search_cache
from opencode_search.storage import Storage
from opencode_search.watcher import watcher_manager

from ._common import _indexing_lock, _indexing_status, _now_iso

log = logging.getLogger(__name__)


def _build_incremental_on_change(
    *,
    db_path: str,
    dims: int,
    project_root: Path,
) -> Any:
    graph_db_path = get_project_graph_db_path(project_root)

    async def on_change(modified: list[Path], deleted: list[str]) -> None:
        st = Storage(db_path=db_path, dims=dims)
        await st.open()
        try:
            if deleted:
                from opencode_search.cleaner import remove_chunks_for_paths

                project_deleted = [
                    p for p in deleted if ".opencode" not in Path(p).parts
                ]
                await remove_chunks_for_paths(st, project_deleted)
                await asyncio.to_thread(
                    _update_graph_incremental,
                    [], project_deleted, graph_db_path,
                )
            if modified:
                try:
                    project_cfg: ProjectConfig | None = load_project_config(project_root)
                except Exception:
                    project_cfg = None
                project_modified = [
                    p
                    for p in modified
                    if is_indexable_file_with_config(p, root=project_root, project_config=project_cfg)
                ]
                await _index_files(st, project_modified, project_root=project_root)
                await asyncio.to_thread(
                    _update_graph_incremental,
                    [str(p) for p in project_modified], [], graph_db_path,
                )
                # Re-enrich communities affected by these file changes.
                # Fires from the indexer — not triggered by any MCP request.
                try:
                    from opencode_search.handlers._autopipeline import (
                        schedule_incremental_enrichment,
                    )
                    schedule_incremental_enrichment(
                        str(project_root), [str(p) for p in project_modified],
                    )
                except Exception as _ie:
                    log.debug("incremental enrichment scheduling failed: %s", _ie)
            clear_search_cache()
        finally:
            await st.close()

    return on_change


async def _run_index_project(
    path_str: str,
    project_path: Path,
    watch: bool,
    force: bool,
    follow_symlinks: bool,
    on_complete: Any,
    on_progress: Any = None,
) -> None:
    """Background task that performs the actual indexing work."""
    status: dict[str, Any] | None = None
    try:
        dims = DEFAULT_DIMS
        db_path = get_project_db_path(project_path)

        # Start watcher before indexing so no file changes are missed during
        # the index scan. For large projects (20K files) hashing alone takes
        # 30-60s — without early-start, changes during that window are lost.
        if watch and not watcher_manager.is_active(path_str):
            await watcher_manager.start(
                path_str,
                on_change=_build_incremental_on_change(
                    db_path=db_path,
                    dims=dims,
                    project_root=project_path,
                ),
            )

        storage = Storage(db_path=db_path, dims=dims)
        await storage.open()
        try:
            # Compact fragmented txn log before indexing to avoid the memory
            # spike caused by LanceDB loading thousands of tiny transaction files.
            # Skip when force=True: the table is cleared immediately after open,
            # so compacting before clearing is wasted work (~200s on 109K chunks).
            if not force:
                await storage.compact_before_index()
            t0 = time.perf_counter()
            result = await _index_project(
                storage, project_path,
                force=force, follow_symlinks=follow_symlinks,
                embed_workers=min(2, get_embed_workers_gpu()),
                # 8 file workers: I/O-bound hashing/reading keeps GPU fed even
                # when 2-3 slots are blocked by large files (>1MB).
                file_workers=8,
                progress_callback=on_progress,
            )
            elapsed = time.perf_counter() - t0
        finally:
            await storage.close()

        # Build structural code graph (CPU-only, runs after GPU pipeline)
        graph_db_path = get_project_graph_db_path(project_path)
        await asyncio.to_thread(
            _build_graph_sync, project_path, graph_db_path, follow_symlinks,
        )

        registry = load_registry()
        entry = registry.get(path_str)
        if entry is None:
            entry = ProjectEntry(
                path=path_str,
                db_path=db_path,
                dims=dims,
                indexed_at=_now_iso(),
                file_count=result.files_indexed + result.files_unchanged,
                watch=watch,
            )
        else:
            entry.db_path = db_path
            entry.dims = dims
            entry.indexed_at = _now_iso()
            entry.file_count = result.files_indexed + result.files_unchanged
            # A plain re-index should not implicitly disable an active watcher.
            entry.watch = entry.watch or watch
        entry.last_active = _now_iso()
        registry[path_str] = entry

        # Auto-discover federation members from symlinks and workspace files.
        # This runs after every index so the registry stays up-to-date as the
        # project grows. New members are pre-registered (not indexed) so they
        # appear in list_federation and can be acted on later.
        try:
            _auto_update_federation(path_str, project_path, entry, registry)
        except Exception as _fed_exc:
            log.debug("federation auto-discovery failed for %s: %s", path_str, _fed_exc)

        save_registry(registry)
        clear_search_cache()

        status = {
            "status": "ok",
            "path": path_str,
            "files_indexed": result.files_indexed,
            "files_unchanged": result.files_unchanged,
            "files_removed": result.files_removed,
            "chunks_total": result.chunks_total,
            "errors": result.errors,
            "elapsed_s": round(elapsed, 2),
            "watching": watcher_manager.is_active(path_str),
        }

        # Trigger knowledge-base build automatically after embedding and graph
        # are complete. This fires from the indexer itself — not from any MCP
        # request handler — so the pipeline runs regardless of the call site.
        try:
            from opencode_search.handlers._autopipeline import schedule_auto_pipeline
            schedule_auto_pipeline(path_str)
        except Exception as _ap_exc:
            log.debug("auto_pipeline: could not schedule: %s", _ap_exc)

        if on_complete is not None:
            with contextlib.suppress(Exception):
                await on_complete(status)
    except Exception as exc:
        log.exception("index_project failed for %s", path_str)
        status = {"status": "error", "path": path_str, "error": str(exc)}
    finally:
        async with _indexing_lock:
            _indexing_status[path_str] = {"running": False, **(status or {})}


def _auto_update_federation(
    path_str: str,
    project_path: Path,
    entry: Any,
    registry: dict,
) -> None:
    """Discover and register federation members in-place (no save — caller saves)."""
    from opencode_search.config import ProjectEntry, get_project_db_path
    from opencode_search.handlers._federation import (
        _discover_go_work_members,
        _discover_package_json_members,
        _discover_pnpm_members,
        _discover_symlink_members,
    )

    discovered: set[str] = set()
    for fn in (_discover_symlink_members, _discover_go_work_members,
               _discover_pnpm_members, _discover_package_json_members):
        for m in fn(project_path):
            if m != path_str:
                discovered.add(m)

    if not discovered:
        return

    existing = set(entry.federation)
    new_members = discovered - existing
    if not new_members:
        return

    for member in sorted(new_members):
        if member not in registry:
            registry[member] = ProjectEntry(
                path=member,
                db_path=get_project_db_path(member),
            )
        entry.federation.append(member)

    log.info(
        "auto-discovered %d new federation members for %s (total: %d)",
        len(new_members), path_str, len(entry.federation),
    )


async def handle_index_project(
    path: str,
    watch: bool = False,
    force: bool = False,
    follow_symlinks: bool = True,
    on_complete: Any = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Index a project directory and optionally start watching it.

    Returns immediately with ``status="indexing"``; the actual work runs in
    the background.  Poll ``project_status`` to check for completion.
    """
    project_path = Path(path).expanduser().resolve()
    if not project_path.is_dir():
        return {"error": f"Directory not found: {project_path}"}

    path_str = str(project_path)

    async with _indexing_lock:
        if path_str in _indexing_status and _indexing_status[path_str].get("running"):
            return {"status": "already_indexing", "path": path_str}
        started_at = _now_iso()
        _indexing_status[path_str] = {"running": True, "started_at": started_at}

    _indexing_task = asyncio.create_task(
        _run_index_project(
            path_str=path_str,
            project_path=project_path,
            watch=watch,
            force=force,
            follow_symlinks=follow_symlinks,
            on_complete=on_complete,
            on_progress=on_progress,
        ),
        name=f"index-{path_str}",
    )
    _indexing_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    return {"status": "indexing", "path": path_str, "started_at": started_at}


# ---------------------------------------------------------------------------
# Graph build helpers (CPU-only, called via asyncio.to_thread)
# ---------------------------------------------------------------------------

_GRAPH_RESOLVER_MAX_NODES = int(
    __import__("os").environ.get("OPENCODE_GRAPH_RESOLVER_MAX_NODES", "500000")
)


def _build_graph_sync(
    project_root: Path,
    graph_db_path: str,
    follow_symlinks: bool = True,
) -> None:
    """Build or incrementally update the structural graph for a project (blocking).

    Uses a per-file SHA-256 cache: files whose content hash matches the stored
    hash are skipped entirely, preserving their existing nodes/edges. Only
    changed or new files are re-extracted. Deleted files are pruned from the DB.
    On the first run (empty cache) all files are extracted as before.
    """
    import hashlib
    from concurrent.futures import ThreadPoolExecutor
    from os import cpu_count

    from opencode_search.discover import iter_files
    from opencode_search.graph.community import CommunityDetector
    from opencode_search.graph.extractor import GraphExtractor, language_for_file
    from opencode_search.graph.resolver import CallResolver
    from opencode_search.graph.storage import GraphStorage

    _GRAPH_EXTRACT_MAX_BYTES = 512 * 1024  # skip files larger than 512 KB

    try:
        extractor = GraphExtractor()

        storage = GraphStorage(graph_db_path)
        storage.open()
        try:
            files = list(iter_files(project_root, follow_symlinks=follow_symlinks))
            existing_files = {str(f) for f in files}

            # Load cached hashes: only re-extract files whose hash changed
            cached_hashes = storage.get_graph_file_hashes()
            storage.purge_deleted_file_hashes(existing_files)

            changed_files: list[Path] = []
            unchanged_count = 0
            for file_path in files:
                path_str = str(file_path)
                try:
                    if file_path.stat().st_size > _GRAPH_EXTRACT_MAX_BYTES:
                        unchanged_count += 1
                        continue
                    content = file_path.read_bytes()
                    current_hash = hashlib.sha256(content).hexdigest()[:16]
                    if cached_hashes.get(path_str) == current_hash:
                        unchanged_count += 1
                        continue
                    changed_files.append(file_path)
                except Exception:
                    unchanged_count += 1

            log.info(
                "graph extract: %d changed, %d unchanged (cache hit rate %.0f%%)",
                len(changed_files), unchanged_count,
                100 * unchanged_count / max(1, len(files)),
            )

            if not changed_files and unchanged_count > 0:
                # Nothing changed — skip extraction and resolver entirely
                log.info("graph extract: all files cached, skipping extraction")
                return

            # Delete stale nodes for changed files so re-extract starts clean
            for file_path in changed_files:
                storage.delete_file(str(file_path))

            all_nodes: list = []
            all_raw_edges: list = []
            new_hashes: dict[str, str] = {}

            def _extract_one(file_path: Path) -> tuple:
                try:
                    content = file_path.read_bytes()
                    file_hash = hashlib.sha256(content).hexdigest()[:16]
                    text = content.decode("utf-8", errors="replace")
                    lang = language_for_file(str(file_path))
                    nodes, raw_edges = extractor.extract_file(str(file_path), text, lang)
                    return nodes, raw_edges, str(file_path), file_hash
                except Exception:
                    return [], [], str(file_path), None

            max_workers = min(4, cpu_count() or 1)
            batch_size = 200
            total_changed = len(changed_files)
            processed = 0
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                for i in range(0, total_changed, batch_size):
                    batch = changed_files[i: i + batch_size]
                    results = list(pool.map(_extract_one, batch))
                    for nodes, raw_edges, path_str, file_hash in results:
                        all_nodes.extend(nodes)
                        all_raw_edges.extend(raw_edges)
                        if file_hash:
                            new_hashes[path_str] = file_hash
                    processed += len(batch)
                    if processed % 1000 < batch_size or processed == total_changed:
                        log.info(
                            "graph extract: %d/%d changed files, %d nodes so far",
                            processed, total_changed, len(all_nodes),
                        )

            # When running incrementally we can't do a full global resolve
            # (only changed-file nodes are in memory). Fall back to per-file resolve.
            is_incremental = unchanged_count > 0
            if not is_incremental and len(all_nodes) <= _GRAPH_RESOLVER_MAX_NODES:
                resolver = CallResolver(all_nodes)
                resolved_edges = resolver.resolve(all_raw_edges)
            else:
                if is_incremental:
                    log.debug("graph extract: incremental run — using per-file resolve")
                else:
                    log.warning(
                        "graph resolver: %d nodes exceeds cap %d — per-file resolve only",
                        len(all_nodes), _GRAPH_RESOLVER_MAX_NODES,
                    )
                resolved_edges = _per_file_resolve(all_nodes, all_raw_edges)

            # Write new nodes/edges in batches
            batch_write = 500
            for i in range(0, len(all_nodes), batch_write):
                storage.upsert_nodes(all_nodes[i: i + batch_write])
            for i in range(0, len(resolved_edges), batch_write):
                storage.upsert_edges(resolved_edges[i: i + batch_write])

            # Persist new hashes so future runs skip these files
            storage.set_graph_file_hashes_batch(new_hashes)

            # Community detection (always re-run to keep assignments fresh)
            detector = CommunityDetector()
            detector.detect_communities(storage)

        finally:
            storage.close()

    except Exception:
        log.exception("graph build failed for %s", project_root)


def _update_graph_incremental(
    modified_paths: list[str],
    deleted_paths: list[str],
    graph_db_path: str,
) -> None:
    """Incrementally update graph for changed/deleted files (blocking)."""
    from opencode_search.graph.extractor import GraphExtractor, language_for_file
    from opencode_search.graph.resolver import CallResolver
    from opencode_search.graph.storage import GraphStorage

    try:
        storage = GraphStorage(graph_db_path)
        storage.open()
        try:
            # Delete stale nodes (cascades to edges via FK)
            for path in deleted_paths:
                storage.delete_file(path)
            for path in modified_paths:
                storage.delete_file(path)

            # Re-extract modified files
            if modified_paths:
                extractor = GraphExtractor()
                new_nodes: list = []
                new_raw_edges: list = []
                for path in modified_paths:
                    try:
                        from pathlib import Path as _P  # noqa: N814
                        text = _P(path).read_text(encoding="utf-8", errors="replace")
                        lang = language_for_file(path)
                        nodes, raw_edges = extractor.extract_file(path, text, lang)
                        new_nodes.extend(nodes)
                        new_raw_edges.extend(raw_edges)
                    except Exception:
                        pass

                # Partial resolution: use existing nodes + new nodes
                existing_nodes = storage.all_nodes()
                resolver = CallResolver(existing_nodes + new_nodes)
                resolved = resolver.resolve(new_raw_edges)

                storage.upsert_nodes(new_nodes)
                storage.upsert_edges(resolved)
        finally:
            storage.close()
    except Exception:
        log.exception("graph incremental update failed")


def _per_file_resolve(all_nodes: list, all_raw_edges: list) -> list:
    """Fallback: resolve only same-file calls (no cross-file strategies)."""
    from collections import defaultdict

    from opencode_search.graph.storage import EdgeData

    file_to_nodes: dict[str, list] = defaultdict(list)
    id_to_file: dict[str, str] = {}
    for n in all_nodes:
        file_to_nodes[n.file].append(n)
        id_to_file[n.id] = n.file

    result: list[EdgeData] = []
    for raw in all_raw_edges:
        caller_file = id_to_file.get(raw.from_id)
        if not caller_file:
            continue
        for n in file_to_nodes[caller_file]:
            if n.name == raw.raw_callee or n.qualified_name.endswith(f".{raw.raw_callee}"):
                result.append(EdgeData(
                    from_id=raw.from_id,
                    to_id=n.id,
                    kind=raw.kind,
                    confidence=0.90,
                    resolution_strategy="same_module",
                ))
                break
    return result
