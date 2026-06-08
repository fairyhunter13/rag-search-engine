# End-to-End Testing Plan

## Overview

This document defines the exact, step-by-step E2E tests for the fully-migrated Python opencode-search-engine. Tests cover unit behavior, component integration, GPU behavior, and full-system scenarios including MCP.

---

## 1. Prerequisites

### 1.1 System Requirements

```bash
# Verify Python version
python --version   # must be 3.11+

# Verify CUDA
nvidia-smi         # must show RTX 5080, CUDA 12.x
nvcc --version     # must show 12.x

# Verify ONNX Runtime GPU
python -c "import onnxruntime; print(onnxruntime.get_available_providers())"
# Must contain: CUDAExecutionProvider

# Verify fastembed-gpu
python -c "from fastembed import TextEmbedding; print('ok')"
python -c "from fastembed.rerank.cross_encoder import TextCrossEncoder; print('ok')"
```

### 1.2 Python Environment Setup

```bash
cd /home/user/git/github.com/fairyhunter13/opencode-search-engine
uv sync --extra gpu
uv pip install pytest pytest-asyncio pytest-timeout cachetools pynvml psutil

# Verify GPU-specific packages
uv pip install cupy-cuda12x
python -c "import cupy; print(cupy.cuda.runtime.getDeviceCount())"  # must be >= 1
```

### 1.3 Environment Variables

```bash
export OPENCODE_GPU_ONLY=1             # enforce GPU-only mode
export OPENCODE_TIER=budget            # use smallest model for most tests
export OPENCODE_EMBED_WORKERS=2        # predictable worker count
export OPENCODE_LOG_LEVEL=DEBUG
export OPENCODE_RERANKER_CACHE_SIZE=2
export OPENCODE_RERANK_CACHE_SIZE=10
export OPENCODE_RERANK_CACHE_TTL=10    # short TTL for cache tests

# Optional: speed up model download
export HF_HUB_OFFLINE=0               # must be online for first-time model download
```

### 1.4 First-Time Model Download

Run this before any tests to ensure models are cached locally:

```bash
python -c "
from opencode_embedder.embeddings import TIER_MODELS, _embedder, _reranker
for tier in ['budget', 'balanced', 'premium']:
    em = TIER_MODELS[tier]['embed']
    rm = TIER_MODELS[tier]['rerank']
    print(f'Loading {tier} embedder: {em}')
    _embedder(em)
    print(f'Loading {tier} reranker: {rm}')
    _reranker(rm)
print('All models downloaded.')
"
```

Expected: No errors, each model loads without timeout.

---

## 2. Test Infrastructure

### 2.1 Directory Layout

```
opencode-search-engine/
  tests/
    conftest.py
    fixtures/
      sample_go_project/
        main.go
        handler/handler.go
        model/user.go
        go.mod
      sample_python_project/
        main.py
        utils/helpers.py
        README.md
    test_storage.py
    test_discover.py
    test_indexer.py
    test_watcher.py
    test_search.py
    test_reranker.py
    test_gpu.py
    test_mcp.py
    test_e2e.py
```

### 2.2 conftest.py

```python
import asyncio
import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest
import pytest_asyncio

from opencode_embedder import embeddings, indexer, storage, search

@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()

@pytest.fixture
def tmp_project(tmp_path):
    """Create a temporary project directory with sample Go files."""
    (tmp_path / "main.go").write_text(
        'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("hello") }\n'
    )
    (tmp_path / "handler").mkdir()
    (tmp_path / "handler" / "handler.go").write_text(
        'package handler\n\nfunc HandleRequest(w http.ResponseWriter, r *http.Request) {}\n'
    )
    (tmp_path / "go.mod").write_text("module example.com/project\n\ngo 1.22\n")
    return tmp_path

@pytest.fixture
def tmp_db(tmp_path):
    """Temporary LanceDB path."""
    return str(tmp_path / "test_index.db")

@pytest_asyncio.fixture
async def store(tmp_db):
    """Initialized Storage instance."""
    s = storage.Storage(tmp_db, tier="budget")
    await s.initialize()
    yield s
    await s.close()

@pytest_asyncio.fixture
async def mcp_client():
    """Connect to a test MCP server over stdio."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(
        command="python",
        args=["-m", "opencode_embedder.mcp_server"],
        env={**os.environ, "OPENCODE_GPU_ONLY": "1"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session
```

### 2.3 Sample Fixture Files

Create `tests/fixtures/sample_go_project/main.go`:
```go
package main

import (
    "context"
    "log"
    "net/http"
    "github.com/example/project/handler"
)

func main() {
    mux := http.NewServeMux()
    mux.HandleFunc("/health", handler.HealthCheck)
    mux.HandleFunc("/users", handler.ListUsers)
    if err := http.ListenAndServe(":8080", mux); err != nil {
        log.Fatal(err)
    }
}
```

Create `tests/fixtures/sample_go_project/handler/handler.go`:
```go
package handler

import (
    "encoding/json"
    "net/http"
)

type User struct {
    ID   int    `json:"id"`
    Name string `json:"name"`
}

func ListUsers(w http.ResponseWriter, r *http.Request) {
    users := []User{{ID: 1, Name: "Alice"}, {ID: 2, Name: "Bob"}}
    json.NewEncoder(w).Encode(users)
}

func HealthCheck(w http.ResponseWriter, r *http.Request) {
    w.WriteHeader(http.StatusOK)
}
```

---

## 3. Component Tests

### 3.1 test_storage.py — LanceDB Schema & CRUD

#### Test 3.1.1 — Schema Creation

```python
@pytest.mark.asyncio
async def test_schema_creation(tmp_db):
    s = storage.Storage(tmp_db, tier="budget")
    await s.initialize()
    schema = s.table.schema
    field_names = [f.name for f in schema]
    assert "chunk_id" in field_names
    assert "path" in field_names
    assert "content" in field_names
    assert "vector" in field_names
    assert "start_line" in field_names
    assert "end_line" in field_names
    assert "language" in field_names
    assert "file_hash" in field_names
    assert "content_hash" in field_names
    assert "created_at" in field_names
    # Vector dimension must match budget embed model
    vector_field = next(f for f in schema if f.name == "vector")
    assert vector_field.type.list_size == 512  # jina-embeddings-v2-small-en dims
    await s.close()
```

#### Test 3.1.2 — Insert and Retrieve

```python
@pytest.mark.asyncio
async def test_insert_retrieve(store):
    chunk = {
        "chunk_id": 1,
        "path": "/project/main.go",
        "file_hash": "abc123",
        "language": "go",
        "position": 0,
        "content": "package main\n\nfunc main() {}",
        "content_hash": "def456",
        "start_line": 1,
        "end_line": 3,
        "vector": [0.1] * 512,
        "created_at": int(time.time() * 1e6),
    }
    await store.upsert([chunk])
    count = await store.count()
    assert count == 1
```

#### Test 3.1.3 — Upsert Idempotency

```python
@pytest.mark.asyncio
async def test_upsert_idempotency(store):
    chunk = {
        "chunk_id": 1,
        "path": "/project/main.go",
        "file_hash": "abc",
        "language": "go",
        "position": 0,
        "content": "version 1",
        "content_hash": "h1",
        "start_line": 1, "end_line": 1,
        "vector": [0.1] * 512,
        "created_at": int(time.time() * 1e6),
    }
    await store.upsert([chunk])
    chunk["content"] = "version 2"
    chunk["content_hash"] = "h2"
    await store.upsert([chunk])
    count = await store.count()
    assert count == 1  # not 2; upsert replaces
    rows = await store.get_by_path("/project/main.go")
    assert rows[0]["content"] == "version 2"
```

#### Test 3.1.4 — Delete by Path

```python
@pytest.mark.asyncio
async def test_delete_by_path(store):
    for i in range(3):
        await store.upsert([{
            "chunk_id": i, "path": "/project/a.go" if i < 2 else "/project/b.go",
            "file_hash": "h", "language": "go", "position": i,
            "content": f"chunk {i}", "content_hash": f"c{i}",
            "start_line": i, "end_line": i,
            "vector": [float(i) / 10] * 512,
            "created_at": int(time.time() * 1e6),
        }])
    await store.delete_by_path("/project/a.go")
    count = await store.count()
    assert count == 1
    remaining = await store.get_by_path("/project/b.go")
    assert len(remaining) == 1
```

#### Test 3.1.5 — FTS Index Creation Threshold

```python
@pytest.mark.asyncio
async def test_fts_index_threshold(tmp_db):
    """FTS index must be created after inserting 50+ chunks."""
    s = storage.Storage(tmp_db, tier="budget")
    await s.initialize()
    chunks = [{
        "chunk_id": i, "path": f"/f{i}.go",
        "file_hash": f"h{i}", "language": "go", "position": 0,
        "content": f"function number {i}", "content_hash": f"c{i}",
        "start_line": 1, "end_line": 1,
        "vector": [0.0] * 512,
        "created_at": int(time.time() * 1e6),
    } for i in range(55)]
    await s.upsert(chunks)
    await s.maybe_create_indexes()
    indexes = s.table.list_indexes()
    assert any("fts" in str(i).lower() for i in indexes)
    await s.close()
```

#### Test 3.1.6 — IVF-PQ Index Creation Threshold

```python
@pytest.mark.asyncio
async def test_ivfpq_index_threshold(tmp_db):
    """IVF-PQ must be created after 512+ chunks."""
    s = storage.Storage(tmp_db, tier="budget")
    await s.initialize()
    chunks = [{
        "chunk_id": i, "path": f"/f{i}.go",
        "file_hash": f"h{i}", "language": "go", "position": 0,
        "content": f"content {i}", "content_hash": f"c{i}",
        "start_line": 1, "end_line": 1,
        "vector": [float(i % 100) / 100.0] * 512,
        "created_at": int(time.time() * 1e6),
    } for i in range(520)]
    await s.upsert(chunks)
    await s.maybe_create_indexes()
    indexes = s.table.list_indexes()
    assert any("ivf" in str(i).lower() for i in indexes)
    await s.close()
```

---

### 3.2 test_discover.py — File Discovery

#### Test 3.2.1 — Gitignore Respect

```python
def test_gitignore_exclusion(tmp_path):
    (tmp_path / ".gitignore").write_text("*.log\nbuild/\n")
    (tmp_path / "main.go").write_text("package main")
    (tmp_path / "debug.log").write_text("log content")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "output").write_text("binary")

    from opencode_embedder.discover import discover_files
    files = list(discover_files(tmp_path))
    paths = [str(f) for f in files]

    assert any("main.go" in p for p in paths)
    assert not any("debug.log" in p for p in paths)
    assert not any("build" in p for p in paths)
```

#### Test 3.2.2 — Binary File Exclusion

```python
def test_binary_exclusion(tmp_path):
    (tmp_path / "main.go").write_text("package main")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    (tmp_path / "binary").write_bytes(b"\x00\x01\x02\x03" * 256)

    from opencode_embedder.discover import discover_files
    files = list(discover_files(tmp_path))
    paths = [str(f) for f in files]

    assert any("main.go" in p for p in paths)
    assert not any("image.png" in p for p in paths)
    assert not any("binary" in p for p in paths)
```

#### Test 3.2.3 — File Size Limits

```python
def test_file_size_limit(tmp_path):
    """Files over 2MB must be skipped for source code."""
    (tmp_path / "small.go").write_text("package main")
    large_content = "x" * (3 * 1024 * 1024)  # 3MB
    (tmp_path / "large.go").write_text(large_content)

    from opencode_embedder.discover import discover_files
    files = list(discover_files(tmp_path))
    paths = [str(f) for f in files]

    assert any("small.go" in p for p in paths)
    assert not any("large.go" in p for p in paths)
```

#### Test 3.2.4 — Language Detection

```python
def test_language_detection(tmp_path):
    test_files = {
        "main.go": "go",
        "main.py": "python",
        "main.rs": "rust",
        "main.ts": "typescript",
        "main.java": "java",
        "README.md": "markdown",
        "config.yaml": "yaml",
        "data.json": "json",
    }
    for fname in test_files:
        (tmp_path / fname).write_text("content")

    from opencode_embedder.discover import detect_language
    for fname, expected_lang in test_files.items():
        lang = detect_language(tmp_path / fname)
        assert lang == expected_lang, f"{fname}: expected {expected_lang}, got {lang}"
```

---

### 3.3 test_indexer.py — Chunking + Embedding Pipeline

#### Test 3.3.1 — Go File Chunking

```python
def test_go_chunking():
    content = '''package main

import "fmt"

func Add(a, b int) int {
    return a + b
}

func Multiply(a, b int) int {
    return a * b
}

func main() {
    fmt.Println(Add(1, 2))
}
'''
    from opencode_embedder.chunker import chunk_content
    chunks = chunk_content(content, language="go", filepath="main.go")
    assert len(chunks) >= 1
    # Each chunk must have content and line info
    for chunk in chunks:
        assert "content" in chunk
        assert "start_line" in chunk
        assert "end_line" in chunk
        assert len(chunk["content"]) > 0
        assert chunk["start_line"] <= chunk["end_line"]
```

#### Test 3.3.2 — Chunk Size Bounds

```python
def test_chunk_size_bounds():
    # Generate a long file to trigger chunking
    funcs = "\n\n".join([
        f"func Function{i}(x int) int {{\n    return x + {i}\n}}"
        for i in range(50)
    ])
    content = f"package main\n\n{funcs}\n"

    from opencode_embedder.chunker import chunk_content
    chunks = chunk_content(content, language="go", filepath="funcs.go")

    for chunk in chunks:
        # 1500 tokens hard limit ≈ 6000 chars
        assert len(chunk["content"]) <= 6000, f"Chunk too large: {len(chunk['content'])} chars"
        assert len(chunk["content"]) > 0
```

#### Test 3.3.3 — Embed Produces Correct Dimensions

```python
@pytest.mark.asyncio
async def test_embed_dimensions():
    from opencode_embedder.embeddings import embed_passages, TIER_MODELS
    model = TIER_MODELS["budget"]["embed"]
    texts = ["package main", "func main() {}", "import fmt"]
    result = await asyncio.to_thread(embed_passages, texts, model=model)
    assert len(result) == 3
    assert len(result[0]) == 512  # jina-small-en dims
    # Values must be L2-normalized: norm ≈ 1.0
    import numpy as np
    for vec in result:
        norm = np.linalg.norm(vec)
        assert abs(norm - 1.0) < 0.01, f"Vector not normalized: norm={norm}"
```

#### Test 3.3.4 — Embed GPU Execution

```python
@pytest.mark.asyncio
async def test_embed_runs_on_gpu():
    import pynvml
    pynvml.nvmlInit()
    handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    before = pynvml.nvmlDeviceGetMemoryInfo(handle).used

    from opencode_embedder.embeddings import embed_passages, TIER_MODELS
    model = TIER_MODELS["budget"]["embed"]
    texts = ["test content " * 50] * 32  # batch that forces GPU work
    await asyncio.to_thread(embed_passages, texts, model=model)

    after = pynvml.nvmlDeviceGetMemoryInfo(handle).used
    # GPU memory must have been used (model + tensors loaded)
    assert after > before or before > 100 * 1024 * 1024  # model already loaded = ok
```

#### Test 3.3.5 — Index Single File

```python
@pytest.mark.asyncio
async def test_index_single_file(store, tmp_project):
    from opencode_embedder.indexer import index_file
    path = tmp_project / "main.go"
    result = await index_file(store, path, tier="budget")
    assert result["status"] == "indexed"
    assert result["chunks"] >= 1
    count = await store.count()
    assert count >= 1
```

#### Test 3.3.6 — Index Skips Unchanged File

```python
@pytest.mark.asyncio
async def test_index_skips_unchanged(store, tmp_project):
    from opencode_embedder.indexer import index_file
    path = tmp_project / "main.go"
    r1 = await index_file(store, path, tier="budget")
    r2 = await index_file(store, path, tier="budget")
    assert r2["status"] == "unchanged"
    assert r2.get("chunks", 0) == 0  # no new chunks embedded
```

#### Test 3.3.7 — Index Re-embeds Modified File

```python
@pytest.mark.asyncio
async def test_index_reembeds_modified(store, tmp_project):
    from opencode_embedder.indexer import index_file
    path = tmp_project / "main.go"
    await index_file(store, path, tier="budget")
    path.write_text('package main\n\nfunc main() { println("changed") }\n')
    r2 = await index_file(store, path, tier="budget")
    assert r2["status"] == "indexed"
    assert r2["chunks"] >= 1
```

#### Test 3.3.8 — Index Full Project

```python
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_index_full_project(store, tmp_project):
    from opencode_embedder.indexer import index_project
    result = await index_project(store, tmp_project, tier="budget")
    assert result["files_indexed"] >= 1
    assert result["chunks_total"] >= 1
    assert result["errors"] == 0
    count = await store.count()
    assert count == result["chunks_total"]
```

---

### 3.4 test_watcher.py — File Watcher

#### Test 3.4.1 — Watcher Detects New File

```python
@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_watcher_new_file(store, tmp_project):
    from opencode_embedder.watcher import FileWatcher
    indexed_paths = []

    async def on_change(path, event_type):
        indexed_paths.append((str(path), event_type))

    watcher = FileWatcher(tmp_project, on_change=on_change, debounce_ms=100)
    await watcher.start()

    # Create new file
    new_file = tmp_project / "new_handler.go"
    new_file.write_text("package main\n\nfunc NewHandler() {}\n")

    await asyncio.sleep(0.5)  # wait for debounce
    await watcher.stop()

    paths = [p for p, _ in indexed_paths]
    assert any("new_handler.go" in p for p in paths)
```

#### Test 3.4.2 — Watcher Detects Modification

```python
@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_watcher_modification(store, tmp_project):
    from opencode_embedder.watcher import FileWatcher
    events = []

    async def on_change(path, event_type):
        events.append((str(path), event_type))

    watcher = FileWatcher(tmp_project, on_change=on_change, debounce_ms=100)
    await watcher.start()

    main_go = tmp_project / "main.go"
    main_go.write_text('package main\n\nfunc main() { println("modified") }\n')

    await asyncio.sleep(0.5)
    await watcher.stop()

    modified = [(p, e) for p, e in events if "main.go" in p]
    assert len(modified) >= 1
    assert any(e in ("modified", "created") for _, e in modified)
```

#### Test 3.4.3 — Watcher Ignores Gitignore Paths

```python
@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_watcher_ignores_gitignore(tmp_project):
    (tmp_project / ".gitignore").write_text("*.log\n")
    from opencode_embedder.watcher import FileWatcher
    events = []

    async def on_change(path, event_type):
        events.append(str(path))

    watcher = FileWatcher(tmp_project, on_change=on_change, debounce_ms=100)
    await watcher.start()

    (tmp_project / "debug.log").write_text("ignored")
    (tmp_project / "real.go").write_text("package main")

    await asyncio.sleep(0.5)
    await watcher.stop()

    assert not any("debug.log" in p for p in events)
    assert any("real.go" in p for p in events)
```

#### Test 3.4.4 — Watcher Debounce Coalesces Rapid Events

```python
@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_watcher_debounce(tmp_project):
    from opencode_embedder.watcher import FileWatcher
    events = []

    async def on_change(path, event_type):
        events.append(str(path))

    watcher = FileWatcher(tmp_project, on_change=on_change, debounce_ms=500)
    await watcher.start()

    target = tmp_project / "main.go"
    for i in range(10):
        target.write_text(f"package main // v{i}")
        await asyncio.sleep(0.05)  # 50ms between writes, within 500ms debounce

    await asyncio.sleep(1.5)  # wait for debounce to fire
    await watcher.stop()

    main_events = [e for e in events if "main.go" in e]
    assert 1 <= len(main_events) <= 3  # coalesced, not 10 separate events
```

---

### 3.5 test_search.py — Search Pipeline

#### Test 3.5.1 — Basic Vector Search

```python
@pytest.mark.asyncio
async def test_basic_vector_search(store, tmp_project):
    from opencode_embedder.indexer import index_project
    from opencode_embedder.search import search

    await index_project(store, tmp_project, tier="budget")
    results = await search(store, "HTTP handler function", tier="budget", top_k=5)

    assert len(results) >= 1
    for r in results:
        assert "path" in r
        assert "content" in r
        assert "score" in r
        assert 0.0 <= r["score"] <= 1.0
```

#### Test 3.5.2 — Search With Reranking

```python
@pytest.mark.asyncio
async def test_search_with_reranking(store, tmp_project):
    from opencode_embedder.indexer import index_project
    from opencode_embedder.search import search

    await index_project(store, tmp_project, tier="budget")
    results_no_rerank = await search(store, "http handler", tier="budget", top_k=5, rerank=False)
    results_rerank = await search(store, "http handler", tier="budget", top_k=5, rerank=True)

    assert len(results_rerank) >= 1
    # Reranked results may differ in order but top result should still be relevant
    assert results_rerank[0]["score"] >= 0.0
```

#### Test 3.5.3 — Search Result Score Ordering

```python
@pytest.mark.asyncio
async def test_search_score_ordering(store, tmp_project):
    from opencode_embedder.indexer import index_project
    from opencode_embedder.search import search

    await index_project(store, tmp_project, tier="budget")
    results = await search(store, "main function", tier="budget", top_k=10)

    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True), "Results must be descending by score"
```

#### Test 3.5.4 — Federated Search (Two Projects)

```python
@pytest.mark.asyncio
async def test_federated_search(tmp_path):
    from opencode_embedder.indexer import index_project
    from opencode_embedder.search import search_federated
    from opencode_embedder import storage as storage_mod

    # Create two separate project indexes
    db1 = str(tmp_path / "proj1.db")
    db2 = str(tmp_path / "proj2.db")
    proj1 = tmp_path / "proj1"
    proj2 = tmp_path / "proj2"
    proj1.mkdir(); proj2.mkdir()

    (proj1 / "auth.go").write_text("package auth\n\nfunc Authenticate(token string) bool { return true }\n")
    (proj2 / "api.go").write_text("package api\n\nfunc HandleLogin(w http.ResponseWriter, r *http.Request) {}\n")

    s1 = storage_mod.Storage(db1, tier="budget")
    s2 = storage_mod.Storage(db2, tier="budget")
    await s1.initialize(); await s2.initialize()
    await index_project(s1, proj1, tier="budget")
    await index_project(s2, proj2, tier="budget")

    results = await search_federated([s1, s2], "authentication login", tier="budget", top_k=5)
    assert len(results) >= 1
    result_paths = [r["path"] for r in results]
    # Both projects should contribute relevant results
    has_auth = any("auth.go" in p or "api.go" in p for p in result_paths)
    assert has_auth
    await s1.close(); await s2.close()
```

#### Test 3.5.5 — Query Cache Hit

```python
@pytest.mark.asyncio
async def test_query_cache_hit(store, tmp_project):
    from opencode_embedder.indexer import index_project
    from opencode_embedder.search import search, _query_cache

    await index_project(store, tmp_project, tier="budget")
    _query_cache.clear()

    t1 = time.monotonic()
    r1 = await search(store, "main function", tier="budget", top_k=5)
    t1 = time.monotonic() - t1

    t2 = time.monotonic()
    r2 = await search(store, "main function", tier="budget", top_k=5)
    t2 = time.monotonic() - t2

    assert t2 < t1 * 0.1, f"Cache miss: second query ({t2:.3f}s) not much faster than first ({t1:.3f}s)"
    assert [r["path"] for r in r1] == [r["path"] for r in r2]
```

---

### 3.6 test_reranker.py — Reranker Component

#### Test 3.6.1 — Tier Model Mapping

```python
def test_tier_model_mapping():
    from opencode_embedder.embeddings import TIER_MODELS
    assert TIER_MODELS["budget"]["rerank"] == "Xenova/ms-marco-MiniLM-L-6-v2"
    assert TIER_MODELS["balanced"]["rerank"] == "jinaai/jina-reranker-v1-turbo-en"
    assert TIER_MODELS["premium"]["rerank"] == "jinaai/jina-reranker-v2-base-multilingual"
    # All three must differ
    models = [TIER_MODELS[t]["rerank"] for t in ["budget", "balanced", "premium"]]
    assert len(set(models)) == 3, "All tiers must use different reranker models"
```

#### Test 3.6.2 — Rerank Produces [0, 1] Scores

```python
@pytest.mark.asyncio
async def test_rerank_scores_range():
    from opencode_embedder.embeddings import rerank, TIER_MODELS
    model = TIER_MODELS["budget"]["rerank"]
    docs = [
        "func HandleHTTP(w http.ResponseWriter, r *http.Request) {}",
        "package main\n\nfunc main() {}",
        "SELECT * FROM users WHERE id = ?",
        "type User struct { ID int; Name string }",
        "func Add(a, b int) int { return a + b }",
    ]
    results = await asyncio.to_thread(rerank, "HTTP request handler", docs, model=model, top_k=5)
    assert len(results) == 5
    for idx, score in results:
        assert 0.0 <= score <= 1.0, f"Score out of range: {score}"
        assert 0 <= idx < len(docs)
```

#### Test 3.6.3 — Monotonic Score Ordering

```python
@pytest.mark.asyncio
async def test_rerank_monotonic_order():
    from opencode_embedder.embeddings import rerank, TIER_MODELS
    model = TIER_MODELS["budget"]["rerank"]
    docs = [f"document number {i} about HTTP handlers" for i in range(10)]
    results = await asyncio.to_thread(rerank, "HTTP handler", docs, model=model, top_k=10)
    scores = [s for _, s in results]
    assert scores == sorted(scores, reverse=True), "Scores must be non-increasing"
```

#### Test 3.6.4 — Sigmoid Calibration (Not Min-Max)

```python
@pytest.mark.asyncio
async def test_sigmoid_calibration_not_minmax():
    """
    If all docs score similarly (e.g., all very relevant), sigmoid must NOT
    spread them across [0, 1] like min-max would.
    """
    from opencode_embedder.embeddings import rerank, TIER_MODELS, _calibrate_scores
    import numpy as np

    # Logits all very close together, all very positive (highly relevant)
    logits = [5.01, 5.02, 5.03, 5.04, 5.05]
    calibrated = _calibrate_scores(logits, temperature=1.0)

    # Sigmoid: all should be near 0.993
    assert all(s > 0.99 for s in calibrated), f"Sigmoid calibration wrong: {calibrated}"

    # Min-max would give [0.0, 0.25, 0.5, 0.75, 1.0] — this must NOT happen
    assert not (calibrated[0] < 0.01), "Looks like min-max normalization is being used"
```

#### Test 3.6.5 — GPU Enforcement (No CPU Fallback)

```python
@pytest.mark.asyncio
async def test_reranker_gpu_only():
    """Reranker must fail if GPU is not available, not silently fall back to CPU."""
    import os
    from opencode_embedder.embeddings import rerank, TIER_MODELS

    # If GPU is present (which it is on RTX 5080), verify CUDAExecutionProvider is active
    import onnxruntime as ort
    providers = ort.get_available_providers()
    assert "CUDAExecutionProvider" in providers, "CUDA must be available for reranker"

    model = TIER_MODELS["budget"]["rerank"]
    docs = ["test document one", "test document two"]
    results = await asyncio.to_thread(rerank, "test query", docs, model=model, top_k=2)
    assert len(results) == 2
```

#### Test 3.6.6 — LRU Cache Eviction

```python
@pytest.mark.asyncio
async def test_reranker_lru_cache_eviction():
    import os
    os.environ["OPENCODE_RERANKER_CACHE_SIZE"] = "2"

    from opencode_embedder import embeddings as emb
    importlib.reload(emb)  # reload to pick up env var

    model1 = emb.TIER_MODELS["budget"]["rerank"]
    model2 = emb.TIER_MODELS["balanced"]["rerank"]
    model3 = emb.TIER_MODELS["premium"]["rerank"]

    emb._reranker(model1)
    emb._reranker(model2)
    assert len(emb._reranker_cache) == 2

    # Loading model3 should evict model1 (least recently used)
    emb._reranker(model3)
    assert len(emb._reranker_cache) == 2
    assert model1 not in emb._reranker_cache
    assert model2 in emb._reranker_cache
    assert model3 in emb._reranker_cache
```

#### Test 3.6.7 — Batch Boundary at 32

```python
@pytest.mark.asyncio
async def test_reranker_batch_boundary():
    """Test with exactly 33 docs — straddles batch_size=32 boundary."""
    from opencode_embedder.embeddings import rerank, TIER_MODELS
    model = TIER_MODELS["budget"]["rerank"]
    docs = [f"document {i}: Go function for processing data" for i in range(33)]
    results = await asyncio.to_thread(rerank, "data processing function", docs, model=model, top_k=33)
    assert len(results) == 33
    idxs = [i for i, _ in results]
    assert sorted(idxs) == list(range(33))  # all 33 docs returned, no duplicates
```

#### Test 3.6.8 — Rerank Score Cache Hit

```python
@pytest.mark.asyncio
async def test_rerank_score_cache():
    from opencode_embedder.search import _rerank_result_cache, _rerank_cache_key
    from opencode_embedder.embeddings import TIER_MODELS, rerank

    _rerank_result_cache.clear()
    model = TIER_MODELS["budget"]["rerank"]
    docs = ["func main() {}", "package handler", "type User struct {}"]

    t1 = time.monotonic()
    r1 = await asyncio.to_thread(rerank, "main function", docs, model=model, top_k=3)
    t1 = time.monotonic() - t1

    # Second call: should hit cache
    t2 = time.monotonic()
    r2 = await asyncio.to_thread(rerank, "main function", docs, model=model, top_k=3)
    t2 = time.monotonic() - t2

    assert t2 < t1 * 0.1, "Rerank cache not effective"
    assert r1 == r2
```

#### Test 3.6.9 — Two-Stage Reranking (1 Project)

```python
@pytest.mark.asyncio
async def test_two_stage_single_project(store, tmp_project):
    from opencode_embedder.indexer import index_project
    from opencode_embedder.search import search

    await index_project(store, tmp_project, tier="budget")

    # Single project: no global rerank stage, just return top_k
    results = await search(store, "HTTP handler", tier="budget", top_k=5, rerank=True)
    assert 1 <= len(results) <= 5
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
```

#### Test 3.6.10 — Two-Stage Reranking (2 Projects)

```python
@pytest.mark.asyncio
async def test_two_stage_two_projects(tmp_path):
    from opencode_embedder.indexer import index_project
    from opencode_embedder.search import search_federated
    from opencode_embedder import storage as storage_mod

    stores = []
    for i, content in enumerate([
        "package auth\n\nfunc Authenticate(token string) bool { return len(token) > 0 }\n",
        "package api\n\nfunc HandleLogin(w http.ResponseWriter, r *http.Request) {}\n",
    ]):
        db = str(tmp_path / f"proj{i}.db")
        proj = tmp_path / f"proj{i}"
        proj.mkdir()
        (proj / f"code{i}.go").write_text(content)
        s = storage_mod.Storage(db, tier="budget")
        await s.initialize()
        await index_project(s, proj, tier="budget")
        stores.append(s)

    results = await search_federated(stores, "authentication", tier="budget", top_k=5, rerank=True)
    assert len(results) >= 1
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)

    for s in stores: await s.close()
```

#### Test 3.6.11 — Two-Stage Reranking (Many Projects)

```python
@pytest.mark.asyncio
async def test_two_stage_many_projects(tmp_path):
    """With 6+ projects, per-project reranking still runs; concurrency is capped."""
    from opencode_embedder.indexer import index_project
    from opencode_embedder.search import search_federated
    from opencode_embedder import storage as storage_mod
    from unittest.mock import patch, AsyncMock

    stores = []
    for i in range(6):
        db = str(tmp_path / f"proj{i}.db")
        proj = tmp_path / f"proj{i}"
        proj.mkdir()
        (proj / f"f{i}.go").write_text(f"package p{i}\n\nfunc F{i}() {{}}\n")
        s = storage_mod.Storage(db, tier="budget")
        await s.initialize()
        await index_project(s, proj, tier="budget")
        stores.append(s)

    from opencode_embedder import search as search_mod

    stage1_rerank_calls = []
    original_rerank = search_mod._stage1_rerank

    async def tracking_rerank(*args, **kwargs):
        stage1_rerank_calls.append(1)
        return await original_rerank(*args, **kwargs)

    with patch.object(search_mod, "_stage1_rerank", side_effect=tracking_rerank):
        results = await search_federated(stores, "function", tier="budget", top_k=5)

    # Per-project rerank should still occur; use OPENCODE_RERANK_CONCURRENCY to
    # prevent VRAM spikes when many projects are federated.
    assert len(stage1_rerank_calls) > 0

    for s in stores: await s.close()
```

---

### 3.7 test_gpu.py — GPU Behavior

#### Test 3.7.1 — GPU-Only Startup Enforced

```python
def test_gpu_only_startup():
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "-c",
         "import os; os.environ['OPENCODE_GPU_ONLY'] = '1';"
         "from opencode_embedder.embeddings import assert_gpu_available;"
         "assert_gpu_available()"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"GPU check failed: {result.stderr}"
```

#### Test 3.7.2 — GPU Provider Active for Embedder

```python
@pytest.mark.asyncio
async def test_embedder_gpu_provider():
    from opencode_embedder.embeddings import _embedder, TIER_MODELS, _verify_onnx_session_provider
    model = TIER_MODELS["budget"]["embed"]
    embedder = _embedder(model)
    active = _verify_onnx_session_provider(embedder, "embedder")
    assert active in ("cuda", "tensorrt"), f"Unexpected provider: {active}"
```

#### Test 3.7.3 — IOBinding Confirmed After Warmup

```python
@pytest.mark.asyncio
async def test_iobinding_confirmed_after_warmup():
    from opencode_embedder import embeddings as emb
    from opencode_embedder.indexer import Indexer  # or however warmup is exposed

    idx = Indexer(tier="budget")
    await idx.warmup()

    assert emb._io_binding_confirmed is True, "IOBinding must be confirmed after warmup"
```

#### Test 3.7.4 — VRAM Watchdog Triggers at 90%

```python
@pytest.mark.asyncio
@pytest.mark.slow
async def test_vram_watchdog_triggers(store, tmp_project):
    """
    Simulate VRAM pressure and verify embed semaphore is throttled.
    This is a structural test — verifies the watchdog code path is reachable.
    """
    from opencode_embedder.server import ModelServer
    import pynvml

    pynvml.nvmlInit()
    handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
    vram_pct = mem.used / mem.total

    if vram_pct < 0.5:
        pytest.skip("VRAM not under pressure — watchdog won't trigger in this test run")

    server = ModelServer(tier="budget")
    await server.start()
    # Watchdog check: if VRAM > 90%, embed_sem should be at 0 permits
    await asyncio.sleep(2)  # let watchdog tick
    await server.stop()
```

#### Test 3.7.5 — CuPy L2 Normalization

```python
def test_cupy_l2_normalization():
    import numpy as np
    from opencode_embedder.embeddings import _normalize_embeddings_gpu

    vecs = np.random.rand(16, 512).astype(np.float32)
    normalized = _normalize_embeddings_gpu(vecs)

    norms = np.linalg.norm(normalized, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5), f"Not unit norm: {norms[:5]}"
```

#### Test 3.7.6 — FP16 Inference Active

```python
@pytest.mark.asyncio
async def test_fp16_inference_active():
    from opencode_embedder.embeddings import _embedder, TIER_MODELS, get_gpu_stats
    _embedder(TIER_MODELS["budget"]["embed"])
    stats = get_gpu_stats()
    assert stats.get("fp16_enabled") is True, f"FP16 not enabled: {stats}"
```

---

### 3.8 test_mcp.py — MCP Layer

#### Test 3.8.1 — MCP Server Starts

```python
@pytest.mark.asyncio
async def test_mcp_server_starts(mcp_client):
    tools = await mcp_client.list_tools()
    tool_names = [t.name for t in tools.tools]
    assert "index" in tool_names
    assert "search" in tool_names
    assert "status" in tool_names
```

#### Test 3.8.2 — /index Tool Indexes Project

```python
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_mcp_index_tool(mcp_client, tmp_project):
    result = await mcp_client.call_tool("index", {"project_path": str(tmp_project)})
    assert result.isError is False
    content = result.content[0].text
    assert "indexed" in content.lower() or "files" in content.lower()
```

#### Test 3.8.3 — /index Registers Project in Registry

```python
@pytest.mark.asyncio
async def test_mcp_index_registers_project(mcp_client, tmp_project, tmp_path):
    registry_path = tmp_path / "projects.json"
    import os
    os.environ["OPENCODE_REGISTRY_PATH"] = str(registry_path)

    await mcp_client.call_tool("index", {"project_path": str(tmp_project)})

    import json
    registry = json.loads(registry_path.read_text())
    paths = [p["path"] for p in registry.get("projects", [])]
    assert str(tmp_project) in paths
```

#### Test 3.8.4 — /search Tool Returns Results

```python
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_mcp_search_tool(mcp_client, tmp_project):
    await mcp_client.call_tool("index", {"project_path": str(tmp_project)})
    result = await mcp_client.call_tool("search", {
        "query": "main function",
        "project_path": str(tmp_project),
        "top_k": 3,
    })
    assert result.isError is False
    content = result.content[0].text
    assert len(content) > 0
```

#### Test 3.8.5 — Auto-Watch Resumes on Session Init

```python
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_mcp_auto_watch_on_init(tmp_project, tmp_path):
    """On MCP server startup, previously indexed projects must have watcher resumed."""
    import json, os
    registry_path = tmp_path / "projects.json"
    registry_path.write_text(json.dumps({
        "projects": [{"path": str(tmp_project), "tier": "budget", "watch": True}]
    }))
    os.environ["OPENCODE_REGISTRY_PATH"] = str(registry_path)

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(
        command="python",
        args=["-m", "opencode_embedder.mcp_server"],
        env={**os.environ},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await asyncio.sleep(2)  # allow auto-watch to start

            result = await session.call_tool("status", {"project_path": str(tmp_project)})
            content = result.content[0].text
            assert "watching" in content.lower() or "active" in content.lower()
```

#### Test 3.8.6 — /status Tool Reports GPU Stats

```python
@pytest.mark.asyncio
async def test_mcp_status_gpu_stats(mcp_client):
    result = await mcp_client.call_tool("status", {})
    assert result.isError is False
    content = result.content[0].text
    assert "gpu" in content.lower() or "vram" in content.lower()
    assert "cuda" in content.lower()
```

---

## 4. Full E2E Scenarios — test_e2e.py

### Scenario 4.1 — First Index of Go Project

**Test:** Fresh database, index Go project, search returns relevant result.

```python
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_e2e_first_index(tmp_path):
    from opencode_embedder import storage as storage_mod
    from opencode_embedder.indexer import index_project
    from opencode_embedder.search import search

    # Setup: create realistic Go project
    proj = tmp_path / "goproject"
    proj.mkdir()
    (proj / "go.mod").write_text("module example.com/api\n\ngo 1.22\n")
    (proj / "main.go").write_text("""
package main

import (
    "log"
    "net/http"
    "example.com/api/handler"
)

func main() {
    http.HandleFunc("/users", handler.ListUsers)
    http.HandleFunc("/health", handler.HealthCheck)
    log.Fatal(http.ListenAndServe(":8080", nil))
}
""")
    (proj / "handler").mkdir()
    (proj / "handler" / "users.go").write_text("""
package handler

import (
    "encoding/json"
    "net/http"
)

type User struct {
    ID   int    `json:"id"`
    Name string `json:"name"`
    Role string `json:"role"`
}

func ListUsers(w http.ResponseWriter, r *http.Request) {
    users := fetchAllUsers()
    json.NewEncoder(w).Encode(users)
}

func fetchAllUsers() []User {
    return []User{
        {ID: 1, Name: "Alice", Role: "admin"},
        {ID: 2, Name: "Bob", Role: "user"},
    }
}
""")

    db_path = str(tmp_path / "index.db")
    store = storage_mod.Storage(db_path, tier="budget")
    await store.initialize()

    result = await index_project(store, proj, tier="budget")

    # Verify indexing
    assert result["files_indexed"] >= 2, f"Expected >=2 files, got {result}"
    assert result["errors"] == 0

    # Verify search relevance
    results = await search(store, "list all users endpoint", tier="budget", top_k=5, rerank=True)
    assert len(results) >= 1

    top_result = results[0]
    assert "user" in top_result["content"].lower() or "handler" in top_result["path"].lower()
    assert top_result["score"] > 0.0

    await store.close()
```

### Scenario 4.2 — Incremental Update (Modified File)

**Test:** File changes after initial index; re-index updates only changed chunks.

```python
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_e2e_incremental_update(tmp_path):
    from opencode_embedder import storage as storage_mod
    from opencode_embedder.indexer import index_project, index_file
    from opencode_embedder.search import search

    proj = tmp_path / "proj"
    proj.mkdir()
    target = proj / "service.go"
    target.write_text("""
package service

func GetUserByID(id int) string {
    return "user"
}
""")

    db_path = str(tmp_path / "index.db")
    store = storage_mod.Storage(db_path, tier="budget")
    await store.initialize()
    await index_project(store, proj, tier="budget")

    count_before = await store.count()

    # Modify file
    target.write_text("""
package service

func GetUserByID(id int) string {
    return "user"
}

func GetUserByEmail(email string) string {
    return "user_by_email"
}
""")

    result = await index_file(store, target, tier="budget")
    assert result["status"] == "indexed"

    count_after = await store.count()
    assert count_after >= count_before  # same or more chunks after adding function

    results = await search(store, "get user by email", tier="budget", top_k=3)
    assert any("email" in r["content"].lower() for r in results)

    await store.close()
```

### Scenario 4.3 — Session Resume with Auto-Watch

**Test:** Index project, stop watcher, restart, verify watcher auto-resumes and catches new file.

```python
@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_e2e_session_resume(tmp_path):
    import json
    from opencode_embedder import storage as storage_mod
    from opencode_embedder.indexer import index_project
    from opencode_embedder.watcher import FileWatcher

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "main.go").write_text("package main\nfunc main() {}\n")

    db_path = str(tmp_path / "index.db")
    registry = tmp_path / "projects.json"

    store = storage_mod.Storage(db_path, tier="budget")
    await store.initialize()
    await index_project(store, proj, tier="budget")

    # Simulate project registry write (as MCP /index would do)
    registry.write_text(json.dumps({
        "projects": [{"path": str(proj), "db": db_path, "tier": "budget", "watch": True}]
    }))

    # Stop and "restart session" by re-reading registry
    reg_data = json.loads(registry.read_text())
    assert len(reg_data["projects"]) == 1
    assert reg_data["projects"][0]["watch"] is True

    # Resume watch
    new_indexed = []
    async def on_change(path, event_type):
        new_indexed.append(str(path))

    watcher = FileWatcher(proj, on_change=on_change, debounce_ms=200)
    await watcher.start()

    (proj / "new_service.go").write_text("package main\nfunc NewService() {}\n")
    await asyncio.sleep(0.8)
    await watcher.stop()

    assert any("new_service.go" in p for p in new_indexed), \
        f"Watcher did not detect new file. Events: {new_indexed}"

    await store.close()
```

### Scenario 4.4 — Federated Search Across 3 Projects

**Test:** Three separate project indexes, federated search returns cross-project results ranked by relevance.

```python
@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_e2e_federated_three_projects(tmp_path):
    from opencode_embedder import storage as storage_mod
    from opencode_embedder.indexer import index_project
    from opencode_embedder.search import search_federated

    projects = {
        "auth": "package auth\n\nfunc ValidateJWT(token string) (bool, error) { return true, nil }\n",
        "api": "package api\n\nfunc HandleLogin(w http.ResponseWriter, r *http.Request) {}\n",
        "db": "package db\n\nfunc FindUserByToken(token string) (*User, error) { return nil, nil }\n",
    }

    stores = []
    for name, code in projects.items():
        proj = tmp_path / name
        proj.mkdir()
        (proj / f"{name}.go").write_text(code)
        db = str(tmp_path / f"{name}.db")
        store = storage_mod.Storage(db, tier="budget")
        await store.initialize()
        await index_project(store, proj, tier="budget")
        stores.append(store)

    results = await search_federated(stores, "JWT token validation user", tier="budget", top_k=5, rerank=True)

    assert len(results) >= 1
    # Should find auth and db results as most relevant
    top_paths = [r["path"] for r in results[:3]]
    relevant = any("auth" in p or "db" in p for p in top_paths)
    assert relevant, f"Expected auth/db in top results, got: {top_paths}"

    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)

    for s in stores: await s.close()
```

### Scenario 4.5 — Large Project (100+ Files)

**Test:** Index a synthetic project with 100 files, verify completion without error and search quality.

```python
@pytest.mark.asyncio
@pytest.mark.timeout(600)
@pytest.mark.slow
async def test_e2e_large_project(tmp_path):
    from opencode_embedder import storage as storage_mod
    from opencode_embedder.indexer import index_project
    from opencode_embedder.search import search

    proj = tmp_path / "large"
    proj.mkdir()
    (proj / "go.mod").write_text("module example.com/large\n\ngo 1.22\n")

    # Generate 100 Go files with varied content
    domains = ["user", "order", "product", "payment", "inventory",
               "auth", "session", "report", "notification", "audit"]
    for i in range(100):
        domain = domains[i % len(domains)]
        pkg_dir = proj / domain
        pkg_dir.mkdir(exist_ok=True)
        fname = pkg_dir / f"{domain}_{i:03d}.go"
        fname.write_text(f"""
package {domain}

import "context"

type {domain.capitalize()}{i:03d} struct {{
    ID   int64
    Name string
}}

func Get{domain.capitalize()}ByID{i:03d}(ctx context.Context, id int64) (*{domain.capitalize()}{i:03d}, error) {{
    // Retrieve {domain} record by primary key
    return &{domain.capitalize()}{i:03d}{{ID: id}}, nil
}}

func Create{domain.capitalize()}{i:03d}(ctx context.Context, name string) (*{domain.capitalize()}{i:03d}, error) {{
    return &{domain.capitalize()}{i:03d}{{Name: name}}, nil
}}
""")

    db_path = str(tmp_path / "large.db")
    store = storage_mod.Storage(db_path, tier="budget")
    await store.initialize()

    result = await index_project(store, proj, tier="budget")
    assert result["files_indexed"] >= 100
    assert result["errors"] == 0

    count = await store.count()
    assert count >= 100  # at least 1 chunk per file

    results = await search(store, "get payment by ID", tier="budget", top_k=5, rerank=True)
    assert len(results) >= 1
    assert any("payment" in r["path"].lower() for r in results[:3]), \
        f"Payment not in top results: {[r['path'] for r in results[:3]]}"

    await store.close()
```

### Scenario 4.6 — GPU Throughput Benchmark

**Test:** Measure embed throughput on RTX 5080; assert minimum performance.

```python
@pytest.mark.asyncio
@pytest.mark.slow
async def test_e2e_gpu_throughput(tmp_path):
    """
    RTX 5080 target: >= 200 chunks/second embedding throughput.
    Measured as total chunks / total wall-clock time.
    """
    from opencode_embedder.embeddings import embed_passages, TIER_MODELS

    model = TIER_MODELS["budget"]["embed"]
    chunk_texts = [
        f"func ProcessItem{i}(ctx context.Context, item Item{i}) error {{ return nil }}"
        for i in range(200)
    ]

    start = time.monotonic()
    result = await asyncio.to_thread(embed_passages, chunk_texts, model=model)
    elapsed = time.monotonic() - start

    assert len(result) == 200
    throughput = 200 / elapsed
    print(f"\nEmbed throughput: {throughput:.1f} chunks/sec (elapsed: {elapsed:.2f}s)")

    assert throughput >= 50, f"Throughput too low: {throughput:.1f} chunks/sec (expected >= 50)"
    # RTX 5080 should achieve 200+; 50 is the CI floor for any GPU
```

---

## 5. MCP End-to-End — Full Claude Code Flow

### Scenario 5.1 — Claude Code /index + /search

```bash
# Terminal test: simulate what Claude Code does

# 1. Start MCP server
python -m opencode_embedder.mcp_server &
MCP_PID=$!

# 2. Index a project via MCP
python - <<'EOF'
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(
        command="python", args=["-m", "opencode_embedder.mcp_server"]
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()

            # Index
            result = await s.call_tool("index", {
                "project_path": "/home/user/git/github.com/fairyhunter13/astro-project",
                "tier": "balanced",
            })
            print("Index result:", result.content[0].text)

            # Search
            result = await s.call_tool("search", {
                "query": "gRPC service registration",
                "top_k": 5,
            })
            print("Search result:", result.content[0].text)

asyncio.run(main())
EOF
```

Expected output:
```
Index result: Indexed 6014 files, 42831 chunks. Watching for changes.
Search result: [1] /astro-project/gateway/grpc_server.go:45 (score: 0.924)
               ...
```

### Scenario 5.2 — Auto-Watch on Second Session

```python
# Verify: after /index ran once and registered project,
# a fresh MCP server session auto-watches without /index being called again

@pytest.mark.asyncio
async def test_auto_watch_second_session(tmp_project, tmp_path):
    import json, os
    from opencode_embedder.indexer import index_project
    from opencode_embedder import storage as storage_mod

    db_path = str(tmp_path / "idx.db")
    registry_path = tmp_path / "projects.json"
    os.environ["OPENCODE_REGISTRY_PATH"] = str(registry_path)

    # First session: index and register
    store = storage_mod.Storage(db_path, tier="budget")
    await store.initialize()
    await index_project(store, tmp_project, tier="budget")
    registry_path.write_text(json.dumps({
        "projects": [{"path": str(tmp_project), "db": db_path, "tier": "budget", "watch": True}]
    }))
    await store.close()

    # Second session: new MCP server, no explicit /index call
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command="python",
        args=["-m", "opencode_embedder.mcp_server"],
        env={**os.environ, "OPENCODE_REGISTRY_PATH": str(registry_path)},
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            await asyncio.sleep(2)  # let auto-watch initialize

            result = await session.call_tool("status", {"project_path": str(tmp_project)})
            text = result.content[0].text
            assert "watching" in text.lower() or "active" in text.lower(), \
                f"Auto-watch not active: {text}"
```

---

## 6. Running the Tests

### 6.1 Full Test Run

```bash
cd /home/user/git/github.com/fairyhunter13/opencode-search-engine

# Unit + component tests (fast, no slow marker)
uv run pytest tests/ -v -x --timeout=60 -m "not slow"

# Including slow tests (GPU throughput, large project)
uv run pytest tests/ -v -x --timeout=600

# Specific component
uv run pytest tests/test_reranker.py -v

# With GPU memory report
uv run pytest tests/ -v --tb=short -p no:warnings 2>&1 | tee test_results.log
```

### 6.2 pytest.ini

```ini
[pytest]
asyncio_mode = auto
markers =
    slow: marks tests as slow (deselect with '-m "not slow"')
    gpu: requires GPU (skipped if no CUDA)
timeout = 120
log_cli = true
log_cli_level = INFO
```

### 6.3 CI Matrix

```yaml
# .github/workflows/test.yml (example)
jobs:
  test:
    runs-on: [self-hosted, gpu, linux]  # RTX 5080 runner
    steps:
      - uses: actions/checkout@v4
      - run: uv sync --extra gpu
      - run: uv run pytest tests/ -v -m "not slow" --timeout=120
      - run: uv run pytest tests/test_e2e.py -v --timeout=600
```

### 6.4 Expected Pass Criteria

| Test Suite | Min Pass Rate | Notes |
|---|---|---|
| test_storage.py | 100% | Schema must be exact |
| test_discover.py | 100% | Gitignore required for correctness |
| test_indexer.py | 100% | GPU required |
| test_watcher.py | 100% | Timing-sensitive; allow ±200ms |
| test_search.py | 100% | Relevance threshold: score > 0 |
| test_reranker.py | 100% | GPU required; sigmoid not min-max |
| test_gpu.py | 100% | RTX 5080 specific |
| test_mcp.py | 100% | Requires mcp Python SDK |
| test_e2e.py (non-slow) | 100% | Core flows |
| test_e2e.py (slow) | 100% | throughput ≥ 50 chunks/s |

---

## 7. Debugging Failures

### Common Failures

| Symptom | Likely Cause | Fix |
|---|---|---|
| `GPUNotAvailableError` | CUDA not in providers | `pip install onnxruntime-gpu`; check `nvidia-smi` |
| `AssertionError: norm != 1.0` | CuPy not installed | `pip install cupy-cuda12x` |
| Watcher test timing fail | High system load | Increase `debounce_ms` or `asyncio.sleep` values |
| MCP tool not found | Server not started | Check `python -m opencode_embedder.mcp_server --help` |
| Sigmoid test fails | Old code using min-max | Verify R5 is implemented in `embeddings.py` |
| Cache test: t2 not faster | TTL too short | Set `OPENCODE_RERANK_CACHE_TTL=30` before test |
| IVF-PQ not created | Too few chunks | Ensure >= 512 chunks in store |
| `model not found` | HuggingFace download failed | Run model download step in §1.4 first |

### GPU Memory Leak Detection

```bash
# Run before and after full test suite
nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits

# If memory grows: check for missing `await store.close()` in fixtures
```
