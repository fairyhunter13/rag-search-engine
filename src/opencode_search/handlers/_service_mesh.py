"""Service mesh detection — infer inter-service call topology from code patterns.

For federated/microservices projects, automatically detects which services call which
other services and via what protocol (gRPC, HTTP, Kafka/AMQP message queues, DB).

This capability is unique to opencode-search: GraphRAG has no equivalent.
"""
from __future__ import annotations

import logging
import re
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


def _scan_service_protocols(project_path: str) -> dict[str, int]:
    """Count protocol occurrences in a service directory. Returns {protocol: count}."""
    root = Path(project_path)
    counts: dict[str, int] = {}
    for dirpath, dirnames, filenames in root.walk() if hasattr(root, "walk") else _os_walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            if Path(fname).suffix not in _SCAN_EXTENSIONS:
                continue
            protocols = _detect_protocols_in_file(Path(dirpath) / fname)
            for p in protocols:
                counts[p] = counts.get(p, 0) + 1
    return counts


def _os_walk(root: Path):
    import os
    for dp, dns, fns in os.walk(str(root), followlinks=True):
        yield Path(dp), dns, fns


async def handle_detect_service_mesh(
    project_path: str,
    include_federation: bool = True,
) -> dict[str, Any]:
    """Detect inter-service communication patterns across federated repos.

    Scans source files for gRPC stubs, HTTP client calls, message queue
    publishers/consumers, and database clients. Returns a service graph:
    nodes = services (repos), edges = detected communication protocols.

    For microservices projects like redacted-name-3 (24 federation members),
    this builds a clear picture of which services talk to which.
    """
    import asyncio

    from opencode_search.config import load_registry
    from opencode_search.enricher import create_llm_client

    registry = load_registry()
    if project_path not in registry:
        return {"error": f"Project {project_path!r} not in registry"}

    # Collect all project paths to scan
    paths_to_scan: list[str] = [project_path]
    if include_federation:
        entry = registry[project_path]
        if entry.federation:
            paths_to_scan.extend(m for m in entry.federation if m in registry)

    log.info("service_mesh: scanning %d services", len(paths_to_scan))

    # Scan each service concurrently
    service_results: dict[str, dict[str, int]] = {}

    async def _scan_one(path: str) -> tuple[str, dict[str, int]]:
        result = await asyncio.to_thread(_scan_service_protocols, path)
        return path, result

    scan_tasks = [_scan_one(p) for p in paths_to_scan]
    for coro in asyncio.as_completed(scan_tasks):
        path, protocols = await coro
        service_results[path] = protocols

    # Build service nodes
    services = []
    for path, protocols in service_results.items():
        name = Path(path).name
        services.append({
            "name": name,
            "path": path,
            "protocols": list(protocols.keys()),
            "protocol_counts": protocols,
            "is_caller": any(v > 0 for v in protocols.values()),
        })

    # Build edge list: services that share protocols can call each other
    # We infer edges: if service A has gRPC calls and service B exposes gRPC, A→B
    grpc_callers = [s["name"] for s in services if "grpc" in s["protocols"]]
    http_callers = [s["name"] for s in services if "http" in s["protocols"]]
    mq_services = [s["name"] for s in services if "message_queue" in s["protocols"]]
    db_services = [s["name"] for s in services if "database" in s["protocols"]]

    edges = []
    # For gRPC and HTTP, create potential edges between all callers and the root project
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

    # LLM description
    mesh_description = ""
    if edges:
        try:
            llm = await asyncio.to_thread(create_llm_client)
            mesh_description = await asyncio.to_thread(llm.service_mesh_description, edges)
        except Exception as exc:
            log.debug("service_mesh: LLM description failed: %s", exc)

    return {
        "services": services,
        "edges": edges,
        "service_count": len(services),
        "edge_count": len(edges),
        "description": mesh_description,
        "protocols_detected": list({p for s in services for p in s["protocols"]}),
    }
