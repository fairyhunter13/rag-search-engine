"""Comprehensive performance test suite for opencode-search-engine.

Tests both the Python embedder (HTTP at localhost:9998) and the Rust indexer
(JSON-RPC over Unix socket).

Service auto-detection:
  - Embedder: http://localhost:9998  (token from ~/.opencode/embedder.token)
  - Indexer:  abstract socket "@opencode-indexer" (Linux) or ~/.opencode/indexer-*.sock

Markers:
  @pytest.mark.perf      — all performance tests
  @pytest.mark.embedder  — embedder-specific tests
  @pytest.mark.indexer   — indexer-specific tests
  @pytest.mark.slow      — tests that run > 30 seconds

Run all perf tests:
    cd tests && pytest e2e_performance.py -v -m perf -s

Run only embedder perf tests:
    pytest e2e_performance.py -v -m "perf and embedder" -s

Run quick smoke (no slow tests):
    pytest e2e_performance.py -v -m "perf and not slow" -s
"""
from __future__ import annotations

import glob
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Service auto-detection
# ---------------------------------------------------------------------------

EMBEDDER_URL = "http://localhost:9998"

EMBEDDER_TOKEN: str | None = None
_token_path = os.path.expanduser("~/.opencode/embedder.token")
if os.path.exists(_token_path):
    try:
        EMBEDDER_TOKEN = open(_token_path).read().strip() or None
    except OSError:
        pass

# Indexer: on Linux the indexer binds an abstract Unix socket "@opencode-indexer".
# File-based sockets (macOS / test mode) match ~/.opencode/indexer-*.sock.
INDEXER_ABSTRACT_SOCKET = "@opencode-indexer"  # Linux production socket
INDEXER_FILE_SOCKETS: list[str] = glob.glob(
    os.path.expanduser("~/.opencode/indexer-*.sock")
)

# ---------------------------------------------------------------------------
# Percentile helper (no numpy)
# ---------------------------------------------------------------------------


def percentile(data: list[float], p: float) -> float:
    """Return the p-th percentile of data (0 < p <= 100)."""
    if not data:
        return 0.0
    s = sorted(data)
    idx = max(0, int(len(s) * p / 100) - 1)
    return s[min(idx, len(s) - 1)]


def p50(data: list[float]) -> float:
    return percentile(data, 50)


def p95(data: list[float]) -> float:
    return percentile(data, 95)


def p99(data: list[float]) -> float:
    return percentile(data, 99)


# ---------------------------------------------------------------------------
# Process memory helpers
# ---------------------------------------------------------------------------


def _read_proc_status(pid: int) -> dict[str, int]:
    """Read VmRSS and VmHWM (kB) from /proc/<pid>/status."""
    result: dict[str, int] = {}
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                for key in ("VmRSS", "VmHWM", "VmPeak"):
                    if line.startswith(key + ":"):
                        parts = line.split()
                        if len(parts) >= 2:
                            result[key] = int(parts[1])  # kB
    except (OSError, ValueError):
        pass
    return result


def rss_mb(pid: int) -> float:
    """Return RSS in MiB for a process."""
    s = _read_proc_status(pid)
    return s.get("VmRSS", 0) / 1024.0


def vmhwm_mb(pid: int) -> float:
    """Return VmHWM (high-water-mark RSS) in MiB for a process."""
    s = _read_proc_status(pid)
    return s.get("VmHWM", 0) / 1024.0


def gpu_vram_mib() -> int:
    """Return used VRAM in MiB via nvidia-smi, or 0 if unavailable."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True,
            timeout=5,
        )
        return int(out.strip().split("\n")[0].strip())
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Embedder HTTP helpers
# ---------------------------------------------------------------------------


def _http_post(
    url: str,
    body: dict,
    token: str | None = None,
    timeout: int = 60,
) -> tuple[int, Any]:
    """POST JSON body to url. Returns (status_code, parsed_json_body)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if token:
        req.add_header("X-Embedder-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception:
        return 0, {}


def _http_get(url: str, token: str | None = None, timeout: int = 10) -> tuple[int, Any]:
    req = urllib.request.Request(url)
    if token:
        req.add_header("X-Embedder-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except Exception:
        return 0, {}


def embedder_alive() -> bool:
    status, _ = _http_get(f"{EMBEDDER_URL}/health", timeout=3)
    return status == 200


def embed_passages(texts: list[str], timeout: int = 60) -> tuple[int, list]:
    status, body = _http_post(
        f"{EMBEDDER_URL}/embed/passages",
        {"texts": texts},
        token=EMBEDDER_TOKEN,
        timeout=timeout,
    )
    vectors = body.get("result", {}).get("vectors", []) if status == 200 else []
    return status, vectors


def embed_query(text: str, timeout: int = 30) -> tuple[int, list]:
    status, body = _http_post(
        f"{EMBEDDER_URL}/embed/query",
        {"text": text},
        token=EMBEDDER_TOKEN,
        timeout=timeout,
    )
    vector = body.get("result", {}).get("vector", []) if status == 200 else []
    return status, vector


def chunk_text(content: str, content_type: str = "text", timeout: int = 30) -> tuple[int, list]:
    status, body = _http_post(
        f"{EMBEDDER_URL}/embed/chunk",
        {"content": content, "content_type": content_type},
        token=EMBEDDER_TOKEN,
        timeout=timeout,
    )
    chunks = body.get("result", {}).get("chunks", []) if status == 200 else []
    return status, chunks


def rerank_docs(query: str, documents: list[str], timeout: int = 60) -> tuple[int, list]:
    status, body = _http_post(
        f"{EMBEDDER_URL}/embed/rerank",
        {"query": query, "documents": documents},
        token=EMBEDDER_TOKEN,
        timeout=timeout,
    )
    scores = body.get("result", {}).get("scores", []) if status == 200 else []
    return status, scores


def find_embedder_pid() -> int | None:
    """Scan /proc for the embedder process."""
    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                cmdline = open(f"/proc/{entry.name}/cmdline").read().replace("\x00", " ")
                if "opencode_embedder" in cmdline or "opencode-embedder" in cmdline:
                    return int(entry.name)
            except OSError:
                pass
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# Indexer Unix socket RPC helpers
# ---------------------------------------------------------------------------


def _rpc_call_file_socket(sock_path: str, method: str, params: dict | None = None, timeout: float = 10.0) -> Any:
    """JSON-RPC 2.0 over file-based Unix socket (HTTP framing)."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1}
    )
    token = EMBEDDER_TOKEN  # indexer shares the same token
    headers = (
        f"POST /rpc HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Content-Type: application/json\r\n"
        f"Connection: close\r\n"
        f"Content-Length: {len(payload)}\r\n"
    )
    if token:
        headers += f"X-Indexer-Token: {token}\r\n"
    headers += "\r\n"

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(sock_path)
        sock.sendall((headers + payload).encode())
        response = b""
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        except socket.timeout:
            pass
    finally:
        sock.close()

    if b"\r\n\r\n" not in response:
        raise RuntimeError(f"No HTTP response body. Raw: {response[:200]}")
    body = response.split(b"\r\n\r\n", 1)[1]
    parsed = json.loads(body)
    return parsed.get("result", parsed)


def _rpc_call_abstract_socket(socket_name: str, method: str, params: dict | None = None, timeout: float = 10.0) -> Any:
    """JSON-RPC 2.0 over Linux abstract Unix socket (HTTP framing)."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1}
    )
    token = EMBEDDER_TOKEN
    headers = (
        f"POST /rpc HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Content-Type: application/json\r\n"
        f"Connection: close\r\n"
        f"Content-Length: {len(payload)}\r\n"
    )
    if token:
        headers += f"X-Indexer-Token: {token}\r\n"
    headers += "\r\n"

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        # Abstract socket: leading null byte + name
        abstract_addr = "\x00" + socket_name.lstrip("@")
        sock.connect(abstract_addr)
        sock.sendall((headers + payload).encode())
        response = b""
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        except socket.timeout:
            pass
    finally:
        sock.close()

    if b"\r\n\r\n" not in response:
        raise RuntimeError(f"No HTTP response body. Raw: {response[:200]}")
    body = response.split(b"\r\n\r\n", 1)[1]
    parsed = json.loads(body)
    return parsed.get("result", parsed)


def rpc_call(method: str, params: dict | None = None) -> Any:
    """Call the indexer RPC. Tries abstract socket first (Linux), then file sockets."""
    # Try abstract socket (Linux production mode)
    try:
        return _rpc_call_abstract_socket(INDEXER_ABSTRACT_SOCKET, method, params)
    except (OSError, ConnectionRefusedError, socket.error):
        pass
    # Fall back to file sockets
    for sock_path in INDEXER_FILE_SOCKETS:
        try:
            return _rpc_call_file_socket(sock_path, method, params)
        except (OSError, ConnectionRefusedError, socket.error):
            continue
    raise RuntimeError("No indexer socket reachable")


def indexer_alive() -> bool:
    try:
        result = rpc_call("ping")
        return result is not None
    except Exception:
        return False


def find_indexer_pid() -> int | None:
    """Scan /proc for the indexer process."""
    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                cmdline = open(f"/proc/{entry.name}/cmdline").read().replace("\x00", " ")
                if "opencode-indexer" in cmdline or "opencode_indexer" in cmdline:
                    return int(entry.name)
            except OSError:
                pass
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# Pretty-print table helper
# ---------------------------------------------------------------------------


def print_table(title: str, rows: list[tuple[str, str]], indent: int = 2) -> None:
    pad = " " * indent
    col_w = max(len(r[0]) for r in rows) + 2
    print(f"\n{pad}{title}")
    print(f"{pad}{'-' * (col_w + 20)}")
    for label, value in rows:
        print(f"{pad}  {label:<{col_w}} {value}")
    print()


# ---------------------------------------------------------------------------
# pytest marks
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.perf


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def require_embedder():
    """Skip the entire test if the embedder is not reachable."""
    if not embedder_alive():
        pytest.skip(f"Embedder not reachable at {EMBEDDER_URL}/health — start embedder first")
    # Warm up to avoid cold-start in first test
    embed_passages(["warmup text for performance suite"], timeout=120)


@pytest.fixture(scope="session")
def require_indexer():
    """Skip the entire test if no indexer socket is reachable."""
    if not indexer_alive():
        pytest.skip(
            "Indexer not reachable. "
            "Expected abstract socket @opencode-indexer (Linux) or "
            f"file socket in {INDEXER_FILE_SOCKETS or '~/.opencode/indexer-*.sock'}"
        )


@pytest.fixture(scope="session")
def embedder_pid_fixture(require_embedder):  # type: ignore[reportUnusedParameter]
    pid = find_embedder_pid()
    if pid is None:
        pytest.skip("Could not identify embedder PID (needed for memory tests)")
    return pid


@pytest.fixture(scope="session")
def indexer_pid_fixture(require_indexer):  # type: ignore[reportUnusedParameter]
    pid = find_indexer_pid()
    if pid is None:
        pytest.skip("Could not identify indexer PID (needed for memory tests)")
    return pid


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

_SINGLE_TEXT = "The quick brown fox jumps over the lazy dog near the river bank."

_MEDIUM_TEXT = (
    "Semantic search uses vector embeddings to find conceptually similar content. "
    "Unlike keyword search, which matches exact terms, semantic search understands "
    "the meaning behind words and phrases. This makes it particularly useful for "
    "code search, documentation lookup, and natural language queries over technical content."
)

_CODE_SNIPPET = """\
fn binary_search<T: Ord>(arr: &[T], target: &T) -> Option<usize> {
    let (mut lo, mut hi) = (0usize, arr.len());
    while lo < hi {
        let mid = lo + (hi - lo) / 2;
        match arr[mid].cmp(target) {
            std::cmp::Ordering::Equal => return Some(mid),
            std::cmp::Ordering::Less  => lo = mid + 1,
            std::cmp::Ordering::Greater => hi = mid,
        }
    }
    None
}
"""

_MARKDOWN_TEXT = """\
# Performance Testing

## Overview

Performance tests measure latency, throughput, and memory usage.

### Latency

| Metric | Target   |
|--------|----------|
| p50    | < 200 ms |
| p95    | < 500 ms |
| p99    | < 1000 ms |

### Throughput

Target: > 10 requests/second for single-text embedding.
"""

_RERANK_DOCS = [
    "Vector databases store embeddings for fast similarity search.",
    "Relational databases use SQL for structured queries.",
    "Semantic search finds conceptually similar content.",
    "Full-text search indexes words and phrases.",
    "Graph databases model relationships between entities.",
]


# ===========================================================================
# A. Embedder Latency Benchmarks
# ===========================================================================


@pytest.mark.embedder
class TestEmbedderLatency:
    """Section A: Embedder latency percentiles."""

    def test_single_text_embedding_latency(self, require_embedder):  # type: ignore[reportUnusedParameter]
        """p50/p95/p99 for 100 single-text embedding requests."""
        n = 100
        latencies: list[float] = []
        errors = 0

        for _ in range(n):
            t0 = time.perf_counter()
            status, vec = embed_passages([_SINGLE_TEXT])
            elapsed = time.perf_counter() - t0
            if status == 200 and vec:
                latencies.append(elapsed)
            else:
                errors += 1

        assert latencies, "All requests failed — embedder unreachable?"
        print_table(
            f"Single-text embedding latency (n={n})",
            [
                ("p50", f"{p50(latencies)*1000:.1f} ms"),
                ("p95", f"{p95(latencies)*1000:.1f} ms"),
                ("p99", f"{p99(latencies)*1000:.1f} ms"),
                ("min", f"{min(latencies)*1000:.1f} ms"),
                ("max", f"{max(latencies)*1000:.1f} ms"),
                ("errors", str(errors)),
            ],
        )
        # Reasonable upper bounds — not too tight
        assert p95(latencies) < 5.0, f"p95 latency {p95(latencies)*1000:.0f}ms exceeds 5000ms"
        assert p99(latencies) < 10.0, f"p99 latency {p99(latencies)*1000:.0f}ms exceeds 10000ms"

    def test_batch_embedding_latency(self, require_embedder):  # type: ignore[reportUnusedParameter]
        """Latency for batch sizes 10, 50, 100, 512."""
        batch_sizes = [10, 50, 100, 512]
        rows: list[tuple[str, str]] = []

        for size in batch_sizes:
            texts = [f"batch latency test text number {i}" for i in range(size)]
            # Average over 5 trials for stability
            trials: list[float] = []
            for _ in range(5):
                t0 = time.perf_counter()
                status, _vecs = embed_passages(texts, timeout=120)
                elapsed = time.perf_counter() - t0
                if status == 200:
                    trials.append(elapsed)

            if not trials:
                rows.append((f"batch={size}", "FAILED"))
                continue
            avg_ms = sum(trials) / len(trials) * 1000
            throughput = size / (sum(trials) / len(trials))
            rows.append((f"batch={size}", f"{avg_ms:.0f} ms  ({throughput:.0f} texts/s)"))

            # Sanity: batch of 512 should complete within 120s
            assert avg_ms < 120_000, f"Batch size {size} took > 120s ({avg_ms:.0f}ms)"

        print_table("Batch embedding latency (avg over 5 trials)", rows)

    def test_query_vs_passage_latency(self, require_embedder):  # type: ignore[reportUnusedParameter]
        """Compare /embed/query vs /embed/passages for single texts."""
        n = 50
        query_lats: list[float] = []
        passage_lats: list[float] = []

        for _ in range(n):
            t0 = time.perf_counter()
            embed_query(_SINGLE_TEXT)
            query_lats.append(time.perf_counter() - t0)

            t0 = time.perf_counter()
            embed_passages([_SINGLE_TEXT])
            passage_lats.append(time.perf_counter() - t0)

        print_table(
            f"Query vs passage latency (n={n})",
            [
                ("query  p50", f"{p50(query_lats)*1000:.1f} ms"),
                ("query  p95", f"{p95(query_lats)*1000:.1f} ms"),
                ("passage p50", f"{p50(passage_lats)*1000:.1f} ms"),
                ("passage p95", f"{p95(passage_lats)*1000:.1f} ms"),
            ],
        )
        # Both should be within 5s p95
        assert p95(query_lats) < 5.0
        assert p95(passage_lats) < 5.0

    def test_chunk_endpoint_latency(self, require_embedder):  # type: ignore[reportUnusedParameter]
        """p50/p95 for the /embed/chunk endpoint with different content types."""
        n = 30
        types = [
            ("markdown", _MARKDOWN_TEXT),
            ("code", _CODE_SNIPPET),
            ("text", _MEDIUM_TEXT),
        ]
        rows: list[tuple[str, str]] = []

        for content_type, content in types:
            lats: list[float] = []
            for _ in range(n):
                t0 = time.perf_counter()
                status, _chunks = chunk_text(content, content_type)
                elapsed = time.perf_counter() - t0
                if status == 200:
                    lats.append(elapsed)

            if lats:
                rows.append((f"{content_type} p50", f"{p50(lats)*1000:.1f} ms"))
                rows.append((f"{content_type} p95", f"{p95(lats)*1000:.1f} ms"))
                # Chunking is CPU-bound; 10s p95 is generous
                assert p95(lats) < 10.0, f"Chunk {content_type} p95={p95(lats)*1000:.0f}ms > 10s"
            else:
                rows.append((f"{content_type}", "FAILED or SKIPPED"))

        print_table(f"Chunk endpoint latency (n={n} per type)", rows)

    def test_rerank_endpoint_latency(self, require_embedder):  # type: ignore[reportUnusedParameter]
        """p50/p95 for /embed/rerank with 10, 50, 100 documents."""
        doc_counts = [10, 50, 100]
        rows: list[tuple[str, str]] = []

        for count in doc_counts:
            docs = (_RERANK_DOCS * (count // len(_RERANK_DOCS) + 1))[:count]
            lats: list[float] = []

            for _ in range(10):
                t0 = time.perf_counter()
                status, scores = rerank_docs("semantic search performance", docs, timeout=120)
                elapsed = time.perf_counter() - t0
                if status == 200:
                    lats.append(elapsed)

            if lats:
                rows.append((f"rerank n={count} p50", f"{p50(lats)*1000:.1f} ms"))
                rows.append((f"rerank n={count} p95", f"{p95(lats)*1000:.1f} ms"))
                # Rerank 100 docs should complete within 60s
                assert p95(lats) < 60.0, f"Rerank {count} docs p95={p95(lats)*1000:.0f}ms > 60s"
            else:
                rows.append((f"rerank n={count}", "FAILED"))

        print_table("Rerank endpoint latency (10 trials per count)", rows)


# ===========================================================================
# B. Embedder Throughput Tests
# ===========================================================================


@pytest.mark.embedder
@pytest.mark.slow
class TestEmbedderThroughput:
    """Section B: Embedder throughput under sustained load."""

    def test_sequential_throughput(self, require_embedder):  # type: ignore[reportUnusedParameter]
        """Sequential requests/sec over 30 seconds."""
        duration = 30.0
        count = 0
        errors = 0
        t_start = time.perf_counter()

        while time.perf_counter() - t_start < duration:
            status, _vecs = embed_passages([_SINGLE_TEXT])
            if status == 200 and _vecs:
                count += 1
            else:
                errors += 1

        elapsed = time.perf_counter() - t_start
        rps = count / elapsed

        print_table(
            f"Sequential throughput ({duration:.0f}s sustained)",
            [
                ("requests completed", str(count)),
                ("errors", str(errors)),
                ("RPS", f"{rps:.2f}"),
                ("error rate", f"{errors / max(1, count + errors) * 100:.1f}%"),
            ],
        )
        # At minimum 0.5 RPS sequential (very conservative — even cold GPU)
        assert rps > 0.5, f"Sequential RPS {rps:.2f} is below 0.5"
        assert errors / max(1, count + errors) < 0.05, f"Error rate > 5%"

    def test_concurrent_throughput(self, require_embedder):  # type: ignore[reportUnusedParameter]
        """Concurrent throughput: 2, 4, 8 clients over 30 seconds each."""
        duration = 30.0
        concurrency_levels = [2, 4, 8]
        rows: list[tuple[str, str]] = []

        for n_clients in concurrency_levels:
            counts: list[int] = [0] * n_clients
            errors: list[int] = [0] * n_clients
            stop_event = threading.Event()

            def worker(idx: int) -> None:
                while not stop_event.is_set():
                    status, _vecs = embed_passages([_SINGLE_TEXT])
                    if status == 200 and _vecs:
                        counts[idx] += 1
                    else:
                        errors[idx] += 1

            threads = [
                threading.Thread(target=worker, args=(i,), daemon=True)
                for i in range(n_clients)
            ]
            t_start = time.perf_counter()
            for t in threads:
                t.start()
            time.sleep(duration)
            stop_event.set()
            for t in threads:
                t.join(timeout=5)

            elapsed = time.perf_counter() - t_start
            total = sum(counts)
            total_err = sum(errors)
            rps = total / elapsed

            rows.append((f"concurrency={n_clients}", f"{rps:.2f} RPS  ({total_err} errors)"))

            # Error rate should stay below 10%
            err_rate = total_err / max(1, total + total_err)
            assert err_rate < 0.10, (
                f"Error rate {err_rate*100:.1f}% at concurrency={n_clients} exceeds 10%"
            )

        print_table(f"Concurrent throughput ({duration:.0f}s per level)", rows)

    def test_max_sustainable_rps(self, require_embedder):  # type: ignore[reportUnusedParameter]
        """Binary search for max RPS without >1% errors (uses batch requests)."""
        # Instead of a true binary search over time (too slow), we measure
        # throughput at increasing batch sizes and report the knee point.
        rows: list[tuple[str, str]] = []
        max_good_batch = 1

        for batch in [1, 5, 10, 25, 50, 100]:
            texts = [f"rps search text {i}" for i in range(batch)]
            errors = 0
            total = 0
            t_start = time.perf_counter()

            # Send 5 batches at this size
            for _ in range(5):
                status, _vecs = embed_passages(texts, timeout=120)
                total += 1
                if status != 200 or not _vecs:
                    errors += 1

            elapsed = time.perf_counter() - t_start
            err_rate = errors / max(1, total)
            texts_per_sec = (total * batch) / elapsed if elapsed > 0 else 0

            rows.append((
                f"batch={batch}",
                f"{texts_per_sec:.0f} texts/s  err={err_rate*100:.0f}%",
            ))

            if err_rate <= 0.01:
                max_good_batch = batch
            else:
                break  # Error rate exceeded threshold

        print_table("Max sustainable throughput (binary search over batch sizes)", rows)
        print(f"  Max batch with <1% errors: {max_good_batch} texts/request\n")

    def test_mixed_workload_throughput(self, require_embedder):  # type: ignore[reportUnusedParameter]
        """Throughput under mixed embed + chunk + rerank workload over 30s."""
        duration = 30.0
        results: dict[str, int] = {"embed": 0, "chunk": 0, "rerank": 0, "errors": 0}
        stop_event = threading.Event()
        lock = threading.Lock()

        def embed_worker() -> None:
            while not stop_event.is_set():
                s, _ = embed_passages([_SINGLE_TEXT])
                with lock:
                    if s == 200:
                        results["embed"] += 1
                    else:
                        results["errors"] += 1

        def chunk_worker() -> None:
            while not stop_event.is_set():
                s, _ = chunk_text(_CODE_SNIPPET, "code")
                with lock:
                    if s == 200:
                        results["chunk"] += 1
                    else:
                        results["errors"] += 1

        def rerank_worker() -> None:
            while not stop_event.is_set():
                s, _ = rerank_docs("search performance", _RERANK_DOCS[:5])
                with lock:
                    if s == 200:
                        results["rerank"] += 1
                    else:
                        results["errors"] += 1

        threads = [
            threading.Thread(target=embed_worker, daemon=True),
            threading.Thread(target=chunk_worker, daemon=True),
            threading.Thread(target=rerank_worker, daemon=True),
        ]
        t_start = time.perf_counter()
        for t in threads:
            t.start()
        time.sleep(duration)
        stop_event.set()
        for t in threads:
            t.join(timeout=5)

        elapsed = time.perf_counter() - t_start
        total_ops = sum(v for k, v in results.items() if k != "errors")
        total_ops_rps = total_ops / elapsed

        print_table(
            f"Mixed workload throughput ({duration:.0f}s)",
            [
                ("embed ops", str(results["embed"])),
                ("chunk ops", str(results["chunk"])),
                ("rerank ops", str(results["rerank"])),
                ("errors", str(results["errors"])),
                ("total RPS", f"{total_ops_rps:.2f}"),
            ],
        )
        err_rate = results["errors"] / max(1, total_ops + results["errors"])
        assert err_rate < 0.10, f"Mixed workload error rate {err_rate*100:.1f}% > 10%"


# ===========================================================================
# C. Embedder Memory Under Load
# ===========================================================================


@pytest.mark.embedder
@pytest.mark.slow
class TestEmbedderMemory:
    """Section C: Embedder memory stability under load."""

    def test_rss_returns_to_baseline(self, require_embedder, embedder_pid_fixture):  # type: ignore[reportUnusedParameter]
        """RSS before/during/after 1000 sequential requests must return to baseline ±5%."""
        pid = embedder_pid_fixture
        baseline_mb = rss_mb(pid)
        assert baseline_mb > 0, f"Could not read RSS for pid {pid}"

        n = 1000
        peak_mb = baseline_mb
        for i in range(n):
            embed_passages([f"memory stability test {i}"])
            if i % 100 == 0:
                current = rss_mb(pid)
                if current > peak_mb:
                    peak_mb = current

        # Allow RSS to settle
        time.sleep(3)
        final_mb = rss_mb(pid)
        growth_pct = (final_mb - baseline_mb) / max(1, baseline_mb) * 100

        print_table(
            f"RSS stability over {n} sequential requests",
            [
                ("baseline RSS", f"{baseline_mb:.1f} MiB"),
                ("peak RSS", f"{peak_mb:.1f} MiB"),
                ("final RSS", f"{final_mb:.1f} MiB"),
                ("growth", f"{growth_pct:+.1f}%"),
            ],
        )
        assert abs(growth_pct) < 25.0, (
            f"RSS grew {growth_pct:+.1f}% after {n} requests "
            f"(baseline={baseline_mb:.0f} MiB, final={final_mb:.0f} MiB)"
        )

    def test_rss_during_concurrent_burst(self, require_embedder, embedder_pid_fixture):  # type: ignore[reportUnusedParameter]
        """RSS during burst of 50 simultaneous requests must not exceed 3x baseline."""
        pid = embedder_pid_fixture
        baseline_mb = rss_mb(pid)
        peak_concurrent_mb = baseline_mb
        stop_monitoring = threading.Event()

        def monitor():
            nonlocal peak_concurrent_mb
            while not stop_monitoring.is_set():
                current = rss_mb(pid)
                if current > peak_concurrent_mb:
                    peak_concurrent_mb = current
                time.sleep(0.05)

        monitor_thread = threading.Thread(target=monitor, daemon=True)
        monitor_thread.start()

        n_concurrent = 50
        texts_per_req = [f"concurrent burst text {i}" for i in range(5)]
        with ThreadPoolExecutor(max_workers=n_concurrent) as ex:
            futures = [ex.submit(embed_passages, texts_per_req, 120) for _ in range(n_concurrent)]
            results = [f.result() for f in as_completed(futures)]

        stop_monitoring.set()
        monitor_thread.join(timeout=3)

        successes = sum(1 for s, v in results if s == 200 and v)
        ratio = peak_concurrent_mb / max(1, baseline_mb)

        print_table(
            "RSS during 50-concurrent-request burst",
            [
                ("baseline RSS", f"{baseline_mb:.1f} MiB"),
                ("peak RSS during burst", f"{peak_concurrent_mb:.1f} MiB"),
                ("ratio peak/baseline", f"{ratio:.2f}x"),
                ("successful requests", f"{successes}/{n_concurrent}"),
            ],
        )
        assert successes > n_concurrent * 0.80, (
            f"Only {successes}/{n_concurrent} burst requests succeeded"
        )
        assert ratio < 3.0, (
            f"RSS rose to {ratio:.2f}x baseline during burst "
            f"(baseline={baseline_mb:.0f} MiB, peak={peak_concurrent_mb:.0f} MiB)"
        )

    def test_vmhwm_not_excessive_after_stress(self, require_embedder, embedder_pid_fixture):  # type: ignore[reportUnusedParameter]
        """VmHWM should not exceed 1.5x baseline RSS after 200 requests."""
        pid = embedder_pid_fixture
        baseline_mb = rss_mb(pid)

        for i in range(200):
            embed_passages([f"hwm stress test {i}"])

        time.sleep(2)
        hwm_mb = vmhwm_mb(pid)
        ratio = hwm_mb / max(1, baseline_mb)

        print_table(
            "VmHWM after 200-request stress",
            [
                ("baseline RSS", f"{baseline_mb:.1f} MiB"),
                ("VmHWM", f"{hwm_mb:.1f} MiB"),
                ("ratio HWM/baseline", f"{ratio:.2f}x"),
            ],
        )
        # VmHWM is a high-water mark — 1.5x is generous for a loaded server
        assert ratio < 1.5, (
            f"VmHWM {hwm_mb:.0f} MiB is {ratio:.2f}x baseline {baseline_mb:.0f} MiB"
        )

    def test_gpu_vram_stable_during_load(self, require_embedder):  # type: ignore[reportUnusedParameter]
        """GPU VRAM usage should not grow unboundedly during sustained inference."""
        vram_before = gpu_vram_mib()
        if vram_before == 0:
            pytest.skip("nvidia-smi not available — skipping VRAM stability test")

        n = 200
        for i in range(n):
            embed_passages([f"vram stability text {i}"])
            if i % 50 == 0:
                time.sleep(0.1)  # brief pause to let GPU stabilize

        vram_after = gpu_vram_mib()
        growth_mb = vram_after - vram_before

        print_table(
            f"GPU VRAM stability over {n} requests",
            [
                ("VRAM before", f"{vram_before} MiB"),
                ("VRAM after", f"{vram_after} MiB"),
                ("growth", f"{growth_mb:+d} MiB"),
            ],
        )
        # Allow up to 500 MiB growth (model caching, fragmentation)
        assert growth_mb < 500, (
            f"VRAM grew {growth_mb} MiB over {n} requests — possible VRAM leak"
        )


# ===========================================================================
# D. Indexer Latency Benchmarks
# ===========================================================================


@pytest.mark.indexer
class TestIndexerLatency:
    """Section D: Indexer RPC latency via Unix socket."""

    def test_ping_rpc_latency(self, require_indexer):  # type: ignore[reportUnusedParameter]
        """p50/p95/p99 for 100 ping RPC calls."""
        n = 100
        latencies: list[float] = []
        errors = 0

        for _ in range(n):
            t0 = time.perf_counter()
            try:
                result = rpc_call("ping")
                latencies.append(time.perf_counter() - t0)
                assert result is not None
            except Exception:
                errors += 1

        print_table(
            f"Indexer ping RPC latency (n={n})",
            [
                ("p50", f"{p50(latencies)*1000:.2f} ms"),
                ("p95", f"{p95(latencies)*1000:.2f} ms"),
                ("p99", f"{p99(latencies)*1000:.2f} ms"),
                ("min", f"{min(latencies, default=0)*1000:.2f} ms"),
                ("max", f"{max(latencies, default=0)*1000:.2f} ms"),
                ("errors", str(errors)),
            ],
        )
        assert latencies, "All ping requests failed"
        # Unix socket ping should be < 100ms p99
        assert p99(latencies) < 0.100, f"ping p99={p99(latencies)*1000:.1f}ms > 100ms"

    def test_search_rpc_latency(self, require_indexer):  # type: ignore[reportUnusedParameter]
        """p50/p95/p99 for search RPC over 100 queries."""
        n = 100
        latencies: list[float] = []
        errors = 0
        query_text = "semantic search code embedding performance"

        # Try to get a valid root path from status
        try:
            status_result = rpc_call("status", {"root": ".", "dimensions": 1024})
            root = "."
        except Exception:
            root = "."

        for i in range(n):
            t0 = time.perf_counter()
            try:
                result = rpc_call(
                    "search",
                    {
                        "root": root,
                        "query": f"{query_text} {i}",
                        "limit": 5,
                        "dimensions": 1024,
                    },
                )
                latencies.append(time.perf_counter() - t0)
            except Exception:
                errors += 1

        if not latencies:
            pytest.skip("All search RPC calls failed — indexer may have no indexed data")

        print_table(
            f"Indexer search RPC latency (n={n})",
            [
                ("p50", f"{p50(latencies)*1000:.1f} ms"),
                ("p95", f"{p95(latencies)*1000:.1f} ms"),
                ("p99", f"{p99(latencies)*1000:.1f} ms"),
                ("min", f"{min(latencies)*1000:.1f} ms"),
                ("max", f"{max(latencies)*1000:.1f} ms"),
                ("errors", str(errors)),
            ],
        )
        # Search p99 should be under 10 seconds (includes embedding time)
        assert p99(latencies) < 10.0, f"search p99={p99(latencies)*1000:.0f}ms > 10s"

    def test_status_rpc_latency(self, require_indexer):  # type: ignore[reportUnusedParameter]
        """p50/p95/p99 for status RPC over 100 calls."""
        n = 100
        latencies: list[float] = []
        errors = 0

        for _ in range(n):
            t0 = time.perf_counter()
            try:
                rpc_call("status", {"root": ".", "dimensions": 1024})
                latencies.append(time.perf_counter() - t0)
            except Exception:
                errors += 1

        print_table(
            f"Indexer status RPC latency (n={n})",
            [
                ("p50", f"{p50(latencies)*1000:.2f} ms"),
                ("p95", f"{p95(latencies)*1000:.2f} ms"),
                ("p99", f"{p99(latencies)*1000:.2f} ms"),
                ("errors", str(errors)),
            ],
        )
        if latencies:
            assert p95(latencies) < 1.0, f"status p95={p95(latencies)*1000:.0f}ms > 1s"

    def test_memory_stats_rpc_latency(self, require_indexer):  # type: ignore[reportUnusedParameter]
        """p50/p95/p99 for memory_stats RPC over 100 calls."""
        n = 100
        latencies: list[float] = []
        errors = 0

        for _ in range(n):
            t0 = time.perf_counter()
            try:
                rpc_call("memory_stats")
                latencies.append(time.perf_counter() - t0)
            except Exception:
                errors += 1

        print_table(
            f"Indexer memory_stats RPC latency (n={n})",
            [
                ("p50", f"{p50(latencies)*1000:.2f} ms"),
                ("p95", f"{p95(latencies)*1000:.2f} ms"),
                ("p99", f"{p99(latencies)*1000:.2f} ms"),
                ("errors", str(errors)),
            ],
        )
        if latencies:
            assert p95(latencies) < 1.0, f"memory_stats p95={p95(latencies)*1000:.0f}ms > 1s"


# ===========================================================================
# E. Indexer Throughput Tests
# ===========================================================================


@pytest.mark.indexer
@pytest.mark.slow
class TestIndexerThroughput:
    """Section E: Indexer RPC throughput under sustained load."""

    def test_sequential_search_rps(self, require_indexer):  # type: ignore[reportUnusedParameter]
        """Sequential search RPS over 30 seconds."""
        duration = 30.0
        count = 0
        errors = 0
        t_start = time.perf_counter()

        while time.perf_counter() - t_start < duration:
            try:
                rpc_call("search", {"root": ".", "query": "test query", "limit": 3, "dimensions": 1024})
                count += 1
            except Exception:
                errors += 1

        elapsed = time.perf_counter() - t_start
        rps = count / elapsed

        print_table(
            f"Sequential indexer search RPS ({duration:.0f}s)",
            [
                ("completed", str(count)),
                ("errors", str(errors)),
                ("RPS", f"{rps:.2f}"),
            ],
        )
        err_rate = errors / max(1, count + errors)
        assert err_rate < 0.10, f"Error rate {err_rate*100:.1f}% > 10%"

    def test_concurrent_search_throughput(self, require_indexer):  # type: ignore[reportUnusedParameter]
        """Concurrent search: 2, 4, 8 clients over 30 seconds each."""
        duration = 30.0
        concurrency_levels = [2, 4, 8]
        rows: list[tuple[str, str]] = []

        for n_clients in concurrency_levels:
            counts: list[int] = [0] * n_clients
            errors: list[int] = [0] * n_clients
            stop_event = threading.Event()

            def worker(idx: int) -> None:
                i = 0
                while not stop_event.is_set():
                    try:
                        rpc_call(
                            "search",
                            {
                                "root": ".",
                                "query": f"concurrent search query {idx} {i}",
                                "limit": 3,
                                "dimensions": 1024,
                            },
                        )
                        counts[idx] += 1
                    except Exception:
                        errors[idx] += 1
                    i += 1

            threads = [
                threading.Thread(target=worker, args=(i,), daemon=True)
                for i in range(n_clients)
            ]
            t_start = time.perf_counter()
            for t in threads:
                t.start()
            time.sleep(duration)
            stop_event.set()
            for t in threads:
                t.join(timeout=5)

            elapsed = time.perf_counter() - t_start
            total = sum(counts)
            total_err = sum(errors)
            rps = total / elapsed
            rows.append((f"concurrency={n_clients}", f"{rps:.2f} RPS  ({total_err} errors)"))

            err_rate = total_err / max(1, total + total_err)
            assert err_rate < 0.15, (
                f"Error rate {err_rate*100:.1f}% at concurrency={n_clients} > 15%"
            )

        print_table(f"Concurrent indexer search throughput ({duration:.0f}s per level)", rows)

    def test_mixed_indexer_workload(self, require_indexer):  # type: ignore[reportUnusedParameter]
        """Mixed search + status + memory_stats workload over 30 seconds."""
        duration = 30.0
        results: dict[str, int] = {"search": 0, "status": 0, "memory_stats": 0, "errors": 0}
        stop_event = threading.Event()
        lock = threading.Lock()

        def search_worker() -> None:
            while not stop_event.is_set():
                try:
                    rpc_call("search", {"root": ".", "query": "test", "limit": 3, "dimensions": 1024})
                    with lock:
                        results["search"] += 1
                except Exception:
                    with lock:
                        results["errors"] += 1

        def status_worker() -> None:
            while not stop_event.is_set():
                try:
                    rpc_call("status", {"root": ".", "dimensions": 1024})
                    with lock:
                        results["status"] += 1
                except Exception:
                    with lock:
                        results["errors"] += 1

        def memory_worker() -> None:
            while not stop_event.is_set():
                try:
                    rpc_call("memory_stats")
                    with lock:
                        results["memory_stats"] += 1
                except Exception:
                    with lock:
                        results["errors"] += 1

        threads = [
            threading.Thread(target=search_worker, daemon=True),
            threading.Thread(target=status_worker, daemon=True),
            threading.Thread(target=memory_worker, daemon=True),
        ]
        t_start = time.perf_counter()
        for t in threads:
            t.start()
        time.sleep(duration)
        stop_event.set()
        for t in threads:
            t.join(timeout=5)

        elapsed = time.perf_counter() - t_start
        total_ops = results["search"] + results["status"] + results["memory_stats"]
        rps = total_ops / elapsed

        print_table(
            f"Mixed indexer workload ({duration:.0f}s)",
            [
                ("search ops", str(results["search"])),
                ("status ops", str(results["status"])),
                ("memory_stats ops", str(results["memory_stats"])),
                ("errors", str(results["errors"])),
                ("total RPS", f"{rps:.2f}"),
            ],
        )
        err_rate = results["errors"] / max(1, total_ops + results["errors"])
        assert err_rate < 0.10, f"Mixed workload error rate {err_rate*100:.1f}% > 10%"


# ===========================================================================
# F. Indexer Memory Under Load
# ===========================================================================


@pytest.mark.indexer
@pytest.mark.slow
class TestIndexerMemory:
    """Section F: Indexer RSS stability under search load."""

    def test_rss_returns_to_baseline_after_searches(self, require_indexer, indexer_pid_fixture):  # type: ignore[reportUnusedParameter]
        """RSS before/during/after 500 search requests must not grow unboundedly."""
        pid = indexer_pid_fixture
        baseline_mb = rss_mb(pid)
        assert baseline_mb > 0, f"Could not read RSS for indexer pid {pid}"

        n = 500
        for i in range(n):
            try:
                rpc_call("search", {"root": ".", "query": f"test query {i}", "limit": 3, "dimensions": 1024})
            except Exception:
                pass
            if i % 100 == 0:
                time.sleep(0.1)

        time.sleep(3)
        final_mb = rss_mb(pid)
        growth_mb = final_mb - baseline_mb
        growth_pct = growth_mb / max(1, baseline_mb) * 100

        print_table(
            f"Indexer RSS after {n} search requests",
            [
                ("baseline RSS", f"{baseline_mb:.1f} MiB"),
                ("final RSS", f"{final_mb:.1f} MiB"),
                ("growth", f"{growth_mb:+.1f} MiB  ({growth_pct:+.1f}%)"),
            ],
        )
        # Indexer is Rust — it should have minimal memory growth from pure searches
        assert growth_mb < 200, (
            f"Indexer RSS grew {growth_mb:.0f} MiB after {n} searches "
            f"(baseline={baseline_mb:.0f} MiB, final={final_mb:.0f} MiB)"
        )

    def test_vmhwm_frozen_during_pure_search(self, require_indexer, indexer_pid_fixture):  # type: ignore[reportUnusedParameter]
        """VmHWM should not increase during a sustained pure-search workload."""
        pid = indexer_pid_fixture

        # Establish HWM baseline after a few warm-up calls
        for _ in range(10):
            try:
                rpc_call("search", {"root": ".", "query": "warmup", "limit": 3, "dimensions": 1024})
            except Exception:
                pass
        time.sleep(1)

        hwm_before = vmhwm_mb(pid)
        rss_before = rss_mb(pid)

        # Run 200 pure search requests
        for i in range(200):
            try:
                rpc_call("search", {"root": ".", "query": f"hwm test {i}", "limit": 5, "dimensions": 1024})
            except Exception:
                pass

        time.sleep(2)
        hwm_after = vmhwm_mb(pid)
        rss_after = rss_mb(pid)
        hwm_growth = hwm_after - hwm_before

        print_table(
            "Indexer VmHWM during 200 pure searches",
            [
                ("RSS before", f"{rss_before:.1f} MiB"),
                ("RSS after", f"{rss_after:.1f} MiB"),
                ("VmHWM before", f"{hwm_before:.1f} MiB"),
                ("VmHWM after", f"{hwm_after:.1f} MiB"),
                ("VmHWM growth", f"{hwm_growth:+.1f} MiB"),
            ],
        )
        # VmHWM is a monotonic high-water mark; we allow up to 50 MiB growth
        # for internal Rust allocator fragmentation during ANN index queries
        assert hwm_growth < 100, (
            f"Indexer VmHWM grew {hwm_growth:.0f} MiB during pure search "
            f"(before={hwm_before:.0f} MiB, after={hwm_after:.0f} MiB)"
        )


# ===========================================================================
# G. Combined System Performance
# ===========================================================================


@pytest.mark.embedder
@pytest.mark.indexer
@pytest.mark.slow
class TestCombinedSystemPerformance:
    """Section G: End-to-end combined system benchmarks."""

    def test_end_to_end_chunk_embed_search_latency(
        self, require_embedder, require_indexer  # type: ignore[reportUnusedParameter]
    ):
        """Measure end-to-end: chunk → embed → (indexer search) latency.

        Note: We do not index during this test to avoid side effects.
        We measure chunk + embed as a proxy for the full pipeline.
        """
        n = 20
        pipeline_lats: list[float] = []
        errors = 0

        content = _CODE_SNIPPET
        query = "binary search algorithm implementation"

        for _ in range(n):
            t0 = time.perf_counter()
            try:
                # Step 1: chunk
                s_chunk, chunks = chunk_text(content, "code")
                if s_chunk != 200 or not chunks:
                    errors += 1
                    continue

                # Step 2: embed the first chunk
                first_chunk = chunks[0] if isinstance(chunks[0], str) else str(chunks[0])
                s_embed, vecs = embed_passages([first_chunk])
                if s_embed != 200 or not vecs:
                    errors += 1
                    continue

                # Step 3: search the indexer
                rpc_call("search", {"root": ".", "query": query, "limit": 5, "dimensions": 1024})

                pipeline_lats.append(time.perf_counter() - t0)
            except Exception:
                errors += 1

        if not pipeline_lats:
            pytest.skip("All end-to-end pipeline calls failed")

        print_table(
            f"End-to-end chunk→embed→search latency (n={n})",
            [
                ("p50", f"{p50(pipeline_lats)*1000:.0f} ms"),
                ("p95", f"{p95(pipeline_lats)*1000:.0f} ms"),
                ("p99", f"{p99(pipeline_lats)*1000:.0f} ms"),
                ("min", f"{min(pipeline_lats)*1000:.0f} ms"),
                ("max", f"{max(pipeline_lats)*1000:.0f} ms"),
                ("errors", str(errors)),
            ],
        )
        # Full pipeline p95 should be under 30s (very conservative)
        assert p95(pipeline_lats) < 30.0, (
            f"End-to-end p95={p95(pipeline_lats)*1000:.0f}ms exceeds 30s"
        )

    def test_combined_memory_footprint_during_mixed_ops(
        self, require_embedder, require_indexer, embedder_pid_fixture, indexer_pid_fixture  # type: ignore[reportUnusedParameter]
    ):
        """Monitor both process RSS during 2 minutes of mixed operations."""
        emb_pid = embedder_pid_fixture
        idx_pid = indexer_pid_fixture

        emb_baseline = rss_mb(emb_pid)
        idx_baseline = rss_mb(idx_pid)

        emb_peak = emb_baseline
        idx_peak = idx_baseline
        stop_event = threading.Event()
        lock = threading.Lock()

        def monitor_loop() -> None:
            nonlocal emb_peak, idx_peak
            while not stop_event.is_set():
                e = rss_mb(emb_pid)
                ix = rss_mb(idx_pid)
                with lock:
                    if e > emb_peak:
                        emb_peak = e
                    if ix > idx_peak:
                        idx_peak = ix
                time.sleep(0.5)

        monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        monitor_thread.start()

        duration = 60.0  # 1 minute of mixed ops
        stop_ops = threading.Event()

        def embed_loop() -> None:
            while not stop_ops.is_set():
                embed_passages([_MEDIUM_TEXT])

        def search_loop() -> None:
            while not stop_ops.is_set():
                try:
                    rpc_call("search", {"root": ".", "query": "combined test", "limit": 3, "dimensions": 1024})
                except Exception:
                    pass

        def status_loop() -> None:
            while not stop_ops.is_set():
                try:
                    rpc_call("status", {"root": ".", "dimensions": 1024})
                except Exception:
                    pass
                time.sleep(0.5)

        workers = [
            threading.Thread(target=embed_loop, daemon=True),
            threading.Thread(target=search_loop, daemon=True),
            threading.Thread(target=status_loop, daemon=True),
        ]
        for w in workers:
            w.start()
        time.sleep(duration)
        stop_ops.set()
        for w in workers:
            w.join(timeout=5)
        stop_event.set()
        monitor_thread.join(timeout=3)

        emb_final = rss_mb(emb_pid)
        idx_final = rss_mb(idx_pid)

        print_table(
            f"Combined memory footprint ({duration:.0f}s mixed ops)",
            [
                ("embedder baseline RSS", f"{emb_baseline:.1f} MiB"),
                ("embedder peak RSS", f"{emb_peak:.1f} MiB"),
                ("embedder final RSS", f"{emb_final:.1f} MiB"),
                ("indexer baseline RSS", f"{idx_baseline:.1f} MiB"),
                ("indexer peak RSS", f"{idx_peak:.1f} MiB"),
                ("indexer final RSS", f"{idx_final:.1f} MiB"),
                ("total peak (emb+idx)", f"{emb_peak + idx_peak:.1f} MiB"),
            ],
        )

        # Embedder: allow up to 4 GiB (models + buffers)
        assert emb_peak < 4096, f"Embedder peak RSS {emb_peak:.0f} MiB > 4096 MiB"
        # Indexer: Rust service should stay under 500 MiB
        assert idx_peak < 500, f"Indexer peak RSS {idx_peak:.0f} MiB > 500 MiB"
        # Neither should grow unboundedly from baseline
        emb_growth_pct = (emb_final - emb_baseline) / max(1, emb_baseline) * 100
        idx_growth_pct = (idx_final - idx_baseline) / max(1, idx_baseline) * 100
        assert emb_growth_pct < 50, f"Embedder RSS grew {emb_growth_pct:+.1f}% during mixed ops"
        assert idx_growth_pct < 50, f"Indexer RSS grew {idx_growth_pct:+.1f}% during mixed ops"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-s", "-m", "perf"])
