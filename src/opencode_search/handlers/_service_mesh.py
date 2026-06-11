"""Service mesh detection — infer inter-service call topology from code patterns.

For federated/microservices projects, automatically detects which services call which
other services and via what protocol (gRPC, HTTP, Kafka/AMQP message queues, DB).

Results are cached in-process for 5 minutes and on-disk for 24 hours — the full file
walk on large repos (astro-project: 20k+ files) is expensive. Pass force=True to
bypass cache; used by the maintenance warmer to run the LLM description out-of-band.

On cold reads the LLM description is NOT called — returns description_pending=True
instead. The maintenance Step-5 warmer calls force=True to fill in description and
overwrites the cache entry so subsequent reads get the full result.

This capability is unique to opencode-search: GraphRAG has no equivalent.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── Pattern matchers ───────────────────────────────────────────────────────────

# gRPC: Go stubs (pb.go), Java stubs, proto service imports
_GRPC_PATTERNS = [
    re.compile(r'(?:grpc\.Dial|grpc\.NewClient)\s*\(\s*["\']([^"\']+)["\']', re.I),
    re.compile(r'NewGrpc(?:Client|Channel)\s*\(\s*["\']([^"\']+)["\']', re.I),
    re.compile(r'\.(?:NewClient|NewBlockingStub|NewStub)\(', re.I),
    re.compile(r'pb\.\w+Client\b'),
    re.compile(r'@GrpcClient\('),
]

# HTTP: common clients
_HTTP_PATTERNS = [
    re.compile(r'(?:http\.Get|http\.Post|http\.NewRequest)\s*\(\s*["\']([^"\']+)["\']', re.I),
    re.compile(r'(?:axios|fetch|request)\s*\(\s*["\']https?://([^"\']+)["\']', re.I),
    re.compile(r'(?:RestTemplate|WebClient|HttpClient)\b'),
    re.compile(r'@FeignClient\s*\(\s*(?:name\s*=\s*)?["\']([^"\']+)["\']', re.I),
    re.compile(r'httpClient\.(?:Get|Post|Do)\b'),
]

# Message queues
_MQ_PATTERNS = [
    re.compile(r'(?:kafka|sarama)\.(?:NewProducer|NewConsumer|NewSyncProducer)\b', re.I),
    re.compile(r'(?:rabbitmq|amqp)\.(?:Dial|Connect|NewConnection)\b', re.I),
    re.compile(r'(?:channel|exchange)\.(?:Publish|Consume|BasicPublish)\b', re.I),
    re.compile(r'@KafkaListener\b', re.I),
    re.compile(r'(?:Producer|Consumer)\.(?:send|receive|publish|subscribe)\b', re.I),
]

# Database
_DB_PATTERNS = [
    re.compile(r'(?:sql\.Open|gorm\.Open|db\.Connect|NewDB)\s*\(', re.I),
    re.compile(r'(?:mongo\.Connect|redis\.NewClient|NewRedisClient)\b', re.I),
    re.compile(r'DataSource\b|@Entity\b|EntityManager\b', re.I),
]

_SCAN_EXTENSIONS = {".go", ".java", ".kt", ".ts", ".tsx", ".js", ".py", ".rs"}
_SKIP_DIRS = {"vendor", ".git", ".venv", "venv", "node_modules", "__pycache__",
              "target", "dist", "build", "generated", "pb", "proto"}
_MAX_FILE_BYTES = 100_000
_MAX_SCAN_FILES = 8_000  # global cap across all shards per service

# ── Two-tier cache ─────────────────────────────────────────────────────────────
_SERVICE_MESH_CACHE: dict[str, tuple[float, dict]] = {}
_SERVICE_MESH_TTL = 300.0        # 5 minutes in-process
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


# ── Protocol detection ─────────────────────────────────────────────────────────

def _detect_protocols_in_file(path: Path) -> set[str]:
    """Return the set of detected inter-service protocols in a source file."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")[:_MAX_FILE_BYTES]
    except OSError:
        return set()
    detected: set[str] = set()
    for pat in _GRPC_PATTERNS:
        if pat.search(content):
            detected.add("grpc")
            break
    for pat in _HTTP_PATTERNS:
        if pat.search(content):
            detected.add("http")
            break
    for pat in _MQ_PATTERNS:
        if pat.search(content):
            detected.add("message_queue")
            break
    for pat in _DB_PATTERNS:
        if pat.search(content):
            detected.add("database")
            break
    return detected


# ── Parallel bounded scanner ───────────────────────────────────────────────────

def _scan_shard(
    shard_root: Path,
    stop_event: threading.Event,
    file_counter: list,        # [int] shared mutable count
    counter_lock: threading.Lock,
    shared_presence: set,      # shared set of detected protocols (short-circuit)
    presence_lock: threading.Lock,
) -> tuple[dict[str, int], int, bool]:
    """Walk one shard subtree sequentially. Returns (counts, scanned, truncated)."""
    counts: dict[str, int] = {}
    local_scanned = 0
    truncated = False

    for dirpath, dirnames, filenames in _os_walk(shard_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        if stop_event.is_set():
            truncated = True
            break
        for fname in filenames:
            if stop_event.is_set():
                truncated = True
                break
            if Path(fname).suffix not in _SCAN_EXTENSIONS:
                continue
            with counter_lock:
                if file_counter[0] >= _MAX_SCAN_FILES:
                    stop_event.set()
                    truncated = True
                    break
                file_counter[0] += 1
            local_scanned += 1
            protocols = _detect_protocols_in_file(Path(dirpath) / fname)
            if not protocols:
                continue
            for p in protocols:
                counts[p] = counts.get(p, 0) + 1
            with presence_lock:
                shared_presence.update(protocols)
                if len(shared_presence) >= 4:  # all protocols found — short-circuit
                    stop_event.set()
                    break

    return counts, local_scanned, truncated


async def _scan_service_protocols_parallel(
    project_path: str,
) -> tuple[dict[str, int], int, bool]:
    """Parallel bounded walk: split by top-level subdir, scan shards concurrently.

    Returns (protocol_counts, total_scanned, truncated).
    """
    import asyncio

    root = Path(project_path)
    stop_event = threading.Event()
    file_counter: list = [0]
    counter_lock = threading.Lock()
    shared_presence: set = set()
    presence_lock = threading.Lock()

    # Scan root-level files inline (fast; usually few config/Makefile-type files)
    root_counts: dict[str, int] = {}
    root_scanned = 0
    try:
        for fpath in root.iterdir():
            if fpath.is_file() and fpath.suffix in _SCAN_EXTENSIONS:
                protos = _detect_protocols_in_file(fpath)
                for p in protos:
                    root_counts[p] = root_counts.get(p, 0) + 1
                root_scanned += 1
                with counter_lock:
                    file_counter[0] += 1
                with presence_lock:
                    shared_presence.update(protos)
    except OSError:
        pass

    # Enumerate immediate subdirs as shards
    try:
        subdirs = [
            d for d in sorted(root.iterdir())
            if d.is_dir() and d.name not in _SKIP_DIRS and not d.name.startswith(".")
        ]
    except OSError:
        return root_counts, root_scanned, False

    if not subdirs:
        return root_counts, root_scanned, False

    # Run each shard in a thread — asyncio.gather runs them concurrently
    results = await asyncio.gather(
        *[
            asyncio.to_thread(
                _scan_shard, s, stop_event, file_counter, counter_lock,
                shared_presence, presence_lock,
            )
            for s in subdirs
        ],
        return_exceptions=True,
    )

    merged = dict(root_counts)
    total_scanned = root_scanned
    was_truncated = False
    for r in results:
        if isinstance(r, Exception):
            log.debug("service_mesh: shard scan error: %s", r)
            continue
        shard_counts, scanned, truncated = r
        total_scanned += scanned
        was_truncated = was_truncated or truncated
        for p, c in shard_counts.items():
            merged[p] = merged.get(p, 0) + c

    return merged, total_scanned, was_truncated


def _os_walk(root: Path):
    import os
    for dp, dns, fns in os.walk(str(root), followlinks=True):
        yield Path(dp), dns, fns


# ── Main handler ───────────────────────────────────────────────────────────────

async def handle_detect_service_mesh(
    project_path: str,
    include_federation: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Detect inter-service communication patterns across federated repos.

    Scans source files for gRPC stubs, HTTP client calls, message queue
    publishers/consumers, and database clients. Returns a service graph:
    nodes = services (repos), edges = detected communication protocols.

    Results are cached (in-process 5 min, on-disk 24 h). On cold reads the
    LLM description is skipped (description_pending=True returned instead);
    the maintenance warmer calls force=True to fill the description and
    overwrite the cache so subsequent reads get the full result.

    Args:
        project_path: Root project path (must be in registry).
        include_federation: Whether to scan federation member repos.
        force: Bypass cache + run LLM description (maintenance warmer path only).
    """
    import asyncio

    from opencode_search.config import load_registry

    root = Path(project_path).expanduser().resolve()
    cache_key = str(root)

    # ── Cache check ────────────────────────────────────────────────────────────
    if not force:
        # 1. In-process cache (sub-second)
        cached_entry = _SERVICE_MESH_CACHE.get(cache_key)
        if cached_entry and (time.monotonic() - cached_entry[0]) < _SERVICE_MESH_TTL:
            return {**cached_entry[1], "cached": True}
        # 2. On-disk cache (24 h TTL — survives daemon restarts)
        try:
            disk_data = load_service_mesh_cache(cache_key)
            if disk_data:
                cached_at = disk_data.get("_cached_at", 0)
                if time.time() - cached_at < _SERVICE_MESH_FILE_TTL:
                    _SERVICE_MESH_CACHE[cache_key] = (time.monotonic(), disk_data)
                    return {**disk_data, "cached": True}
        except Exception:
            pass

    # ── Scan ───────────────────────────────────────────────────────────────────
    registry = load_registry()
    if project_path not in registry:
        return {"error": f"Project {project_path!r} not in registry"}

    paths_to_scan: list[str] = [project_path]
    if include_federation:
        entry = registry[project_path]
        if entry.federation:
            paths_to_scan.extend(m for m in entry.federation if m in registry)

    log.info("service_mesh: scanning %d services (force=%s)", len(paths_to_scan), force)

    # Each service is scanned with its own parallel shard walk
    service_results: dict[str, tuple[dict[str, int], int, bool]] = {}

    async def _scan_one(path: str) -> tuple[str, dict[str, int], int, bool]:
        counts, scanned, truncated = await _scan_service_protocols_parallel(path)
        return path, counts, scanned, truncated

    scan_tasks = [_scan_one(p) for p in paths_to_scan]
    total_scanned = 0
    any_truncated = False
    for coro in asyncio.as_completed(scan_tasks):
        path, protocols, scanned, truncated = await coro
        service_results[path] = (protocols, scanned, truncated)
        total_scanned += scanned
        any_truncated = any_truncated or truncated

    if any_truncated:
        log.info(
            "service_mesh: scan truncated at %d files (cap=%d per service)",
            total_scanned, _MAX_SCAN_FILES,
        )

    # ── Build nodes ────────────────────────────────────────────────────────────
    services = []
    for path, (protocols, _scanned, _truncated) in service_results.items():
        name = Path(path).name
        services.append({
            "name": name,
            "path": path,
            "protocols": list(protocols.keys()),
            "protocol_counts": protocols,
            "is_caller": any(v > 0 for v in protocols.values()),
        })

    # ── Build edges ────────────────────────────────────────────────────────────
    grpc_callers = [s["name"] for s in services if "grpc" in s["protocols"]]
    http_callers = [s["name"] for s in services if "http" in s["protocols"]]
    mq_services = [s["name"] for s in services if "message_queue" in s["protocols"]]
    db_services = [s["name"] for s in services if "database" in s["protocols"]]

    edges = []
    root_name = Path(project_path).name
    for caller in grpc_callers:
        if caller != root_name:
            edges.append({"from": caller, "to": root_name, "protocol": "grpc"})
    for caller in http_callers:
        if caller != root_name:
            edges.append({"from": caller, "to": root_name, "protocol": "http"})
    for svc in mq_services:
        edges.append({"from": svc, "to": "message_bus", "protocol": "message_queue"})
    for svc in db_services:
        edges.append({"from": svc, "to": "database", "protocol": "database"})

    # ── LLM description (warmer path only — never on the read path) ────────────
    description = ""
    if force and edges:
        try:
            from opencode_search.enricher import create_llm_client
            llm = await asyncio.to_thread(create_llm_client)
            if llm:
                description = await asyncio.to_thread(llm.service_mesh_description, edges)
        except Exception as exc:
            log.debug("service_mesh: LLM description failed: %s", exc)

    # ── Build and cache result ─────────────────────────────────────────────────
    result: dict[str, Any] = {
        "services": services,
        "edges": edges,
        "service_count": len(services),
        "edge_count": len(edges),
        "description": description,
        "protocols_detected": list({p for s in services for p in s["protocols"]}),
        "scanned_files": total_scanned,
        "truncated": any_truncated,
        "_cached_at": time.time(),
    }
    if not force:
        result["description_pending"] = True

    _SERVICE_MESH_CACHE[cache_key] = (time.monotonic(), result)
    try:
        await asyncio.to_thread(_save_service_mesh_cache, cache_key, result)
    except Exception as exc:
        log.debug("service_mesh: cache save failed: %s", exc)

    return result
