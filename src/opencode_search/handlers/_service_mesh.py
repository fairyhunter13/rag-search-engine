"""Service mesh topology — derived from parsed .proto graph nodes + external imports.

No regex, no source-file scan, no LLM.

gRPC services: members whose graph contains tree-sitter-parsed .proto service nodes
(language='proto', kind='interface'). Labeled grpc by parsed fact.

Cross-member edges: derived from each member's external_imports.json (real parsed
imports, captured at index time). Protocol for non-proto edges is unlabeled — the
protocol prose of an arbitrary import cannot be determined without a banned
keyword/mapping; ask(scope=...) answers it from L1 summaries on demand.

Results cached (in-process 5 min, on-disk 24 h). Always returns llm_used: False.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── Two-tier cache ─────────────────────────────────────────────────────────────
_SERVICE_MESH_CACHE: dict[str, tuple[float, dict]] = {}
_SERVICE_MESH_TTL = 300.0         # 5 minutes in-process
_SERVICE_MESH_FILE_TTL = 86400.0  # 24 hours on-disk


def _get_cache_path(project_path: str) -> Path:
    from opencode_search.config import get_project_index_dir
    return get_project_index_dir(project_path) / "service_mesh_cache.json"


def load_service_mesh_cache(project_path: str) -> dict[str, Any] | None:
    """Return the cached service mesh result, or None if not present/expired."""
    cache_path = _get_cache_path(project_path)
    if not cache_path.exists():
        return None
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_service_mesh_cache(project_path: str, data: dict[str, Any]) -> None:
    cache_path = _get_cache_path(project_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def invalidate_service_mesh_cache(project_path: str) -> None:
    """Drop in-process and on-disk cache for a project (called on file change)."""
    import contextlib
    root = str(Path(project_path).expanduser().resolve())
    _SERVICE_MESH_CACHE.pop(root, None)
    with contextlib.suppress(Exception):
        _get_cache_path(root).unlink(missing_ok=True)


# ── Graph-based proto service discovery ───────────────────────────────────────

def _collect_proto_services(path: str) -> tuple[str, list[str], list[str]]:
    """Return (path, service_names, proto_files) from the graph DB.

    Queries language='proto' AND kind='interface' — exactly what _extract_proto
    emits for .proto `service` blocks. No file scan, no regex.
    """
    from opencode_search.config import get_project_graph_db_path
    from opencode_search.graph.storage import GraphStorage

    db_path = get_project_graph_db_path(path)
    if not Path(db_path).exists():
        return path, [], []
    gs = GraphStorage(db_path)
    try:
        gs.open()
        db = gs._db()
        rows = db.execute(
            "SELECT name, qualified_name, file FROM nodes "
            "WHERE language='proto' AND kind='interface'"
        ).fetchall()
        service_names = [r["name"] for r in rows]
        proto_files = list({r["file"] for r in rows})
        return path, service_names, proto_files
    except Exception:
        return path, [], []
    finally:
        import contextlib
        with contextlib.suppress(Exception):
            gs.close()


def _load_external_imports(path: str) -> list[str]:
    """Return top external import module names from external_imports.json."""
    from opencode_search.config import get_project_index_dir
    try:
        ext_file = get_project_index_dir(path) / "external_imports.json"
        if not ext_file.exists():
            return []
        data = json.loads(ext_file.read_text(encoding="utf-8"))
        return [item["module"] for item in data.get("top_imports", [])]
    except Exception:
        return []


# ── Main handler ───────────────────────────────────────────────────────────────

async def handle_detect_service_mesh(
    project_path: str,
    include_federation: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Derive inter-service topology from parsed facts — zero regex, zero LLM.

    Services with .proto `service` nodes in the graph → labeled grpc by parsed fact.
    Cross-member edges from each member's external_imports.json (real parsed imports).
    Non-proto cross-member edges listed unlabeled (protocol prose deferred to ask).
    Always returns llm_used: False. Completes well under 10s.
    """
    import asyncio

    from opencode_search.config import load_registry

    root = Path(project_path).expanduser().resolve()
    cache_key = str(root)

    # ── Cache check ────────────────────────────────────────────────────────────
    if not force:
        cached_entry = _SERVICE_MESH_CACHE.get(cache_key)
        if cached_entry and (time.monotonic() - cached_entry[0]) < _SERVICE_MESH_TTL:
            return {**cached_entry[1], "cached": True}
        try:
            disk_data = load_service_mesh_cache(cache_key)
            if disk_data:
                cached_at = disk_data.get("_cached_at", 0)
                if time.time() - cached_at < _SERVICE_MESH_FILE_TTL:
                    _SERVICE_MESH_CACHE[cache_key] = (time.monotonic(), disk_data)
                    return {**disk_data, "cached": True}
        except Exception:
            pass

    # ── Registry lookup ────────────────────────────────────────────────────────
    registry = load_registry()
    if project_path not in registry:
        return {"error": f"Project {project_path!r} not in registry"}

    paths_to_scan: list[str] = [project_path]
    if include_federation:
        entry = registry[project_path]
        if entry.federation:
            paths_to_scan.extend(m for m in entry.federation if m in registry)

    # ── Collect proto service nodes + external imports in parallel ─────────────
    proto_results, import_results = await asyncio.gather(
        asyncio.gather(*[asyncio.to_thread(_collect_proto_services, p) for p in paths_to_scan]),
        asyncio.gather(*[asyncio.to_thread(_load_external_imports, p) for p in paths_to_scan]),
    )

    # path → (service_names, proto_files)
    proto_by_path = {p: (snames, pfiles) for p, snames, pfiles in proto_results}
    # path → [import_module, ...]
    imports_by_path = dict(zip(paths_to_scan, import_results, strict=True))

    # Set of paths that are gRPC services (have proto service nodes)
    grpc_paths: set[str] = {p for p, (snames, _) in proto_by_path.items() if snames}

    # ── Build service nodes ────────────────────────────────────────────────────
    services = []
    path_to_name = {p: Path(p).name for p in paths_to_scan}

    for path in paths_to_scan:
        snames, pfiles = proto_by_path.get(path, ([], []))
        services.append({
            "name": path_to_name[path],
            "path": path,
            "proto_services": snames,
            "proto_files": pfiles,
            # grpc by parsed fact if the graph has .proto service nodes; else None
            "protocol": "grpc" if path in grpc_paths else None,
        })

    # ── Build edges from external imports ─────────────────────────────────────
    # If member A's external imports reference member B's package/name, emit an edge.
    # Protocol: grpc if the callee has proto service nodes, else unlabeled (None).
    edges = []
    for caller_path in paths_to_scan:
        caller_name = path_to_name[caller_path]
        my_imports = imports_by_path.get(caller_path, [])
        if not my_imports:
            continue
        for callee_path in paths_to_scan:
            if callee_path == caller_path:
                continue
            callee_name = path_to_name[callee_path]
            # Structural match: does any import path contain the callee's directory name?
            callee_key = callee_name.lower().replace("-", "_")
            for imp in my_imports:
                imp_norm = imp.lower().replace("-", "_")
                if callee_key in imp_norm:
                    protocol = "grpc" if callee_path in grpc_paths else None
                    edges.append({
                        "from": caller_name,
                        "to": callee_name,
                        "protocol": protocol,
                        "source": "external_imports",
                    })
                    break

    # ── Build result ───────────────────────────────────────────────────────────
    result: dict[str, Any] = {
        "services": services,
        "edges": edges,
        "service_count": len(services),
        "edge_count": len(edges),
        "grpc_services": [path_to_name[p] for p in sorted(grpc_paths)],
        "_cached_at": time.time(),
        "llm_used": False,
    }

    _SERVICE_MESH_CACHE[cache_key] = (time.monotonic(), result)
    try:
        await asyncio.to_thread(_save_service_mesh_cache, cache_key, result)
    except Exception as exc:
        log.debug("service_mesh: cache save failed: %s", exc)

    return result
