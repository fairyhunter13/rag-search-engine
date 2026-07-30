# status skill

Comprehensive status audit of the rag-search-engine system.

## What to check

### 1. Registry health
- Call `overview(what='projects')` — list all indexed projects
- Flag: projects with 0 communities (graph never derived)
- Flag: stale test artifacts (paths in /tmp or .venv)
- Flag: projects not watching (watching=false)
- Remove any stale entries with `index(project_path=<path>, enabled=False)` (also frees the index dir)

### 2. Canonical test target verification
- `overview(project_path='<TEST_PROJECT_PATH>', what='status')` — confirm watching=true, communities>5000
- `search(query='payment handler', project_paths=[test_path])` — confirm search returns real results
- `ask(query='how does payment flow work', project_path=test_path, scope='all')` — confirm a
  non-empty `## Code` / `## Architecture` assembly. (`scope='feature'` was retired 2026-07-28 along
  with `global` and `business`; the `entry_points` field it once returned left with tier 3.)
- `graph(symbol='PaymentService', project_path=test_path, relation='impact_narrative')` — confirm graph works
- The same `overview(what='status')` call carries `index_state` — confirm `ready`, not `indexing`
  or `degraded`. This replaces `GET /api/kb_health?project=…` (enrichment_pct=100.0), which left
  with tier 3 on 2026-07-28: it scored community summaries against DeepSeek narration, and
  structural labelling now fills every summary, so a surviving endpoint would have reported a
  permanent, meaningless 100 %.

### 3. Dashboard routes
Check all key routes return expected HTTP status:
- `GET /dashboard` → 200
- `GET /healthz` → 200
- `GET /api/projects` → 200
- `GET /api/metrics` → 200
- `POST /api/chat_stream` with valid project → SSE stream starts

### 4. GPU & resource profile
- `nvidia-smi --query-gpu=temperature.gpu,memory.used,memory.total --format=csv,noheader`
- Confirm GPU temp < 85°C, VRAM < 14 GB
- `curl -s http://127.0.0.1:8765/healthz` — confirm embedder loaded (GPU-bound, not CPU)

### 5. Daemon metrics
- `GET /api/metrics` — report stream_success_count, stream_error_count, error_by_intent
- Flag any error_by_intent entries (should be 0)

### 6. Fast test suite
- Run `.venv/bin/pytest src/tests/live/ -m "live and not slow" -q --ignore=src/tests/live/test_browser.py`
- Must be 0 failed. The pass count is not pinned here — it moved with tier 3's deletion and
  pinning it turns every legitimate test addition into a false alarm

## Output format

```
REGISTRY:   28 indexed, 2 removed (stale), 0 without a graph
RETRIEVAL:  search ✓  ask ✓  graph ✓  index_state=ready
DASHBOARD:  /dashboard 200  /healthz 200  /api/projects 200  /api/metrics 200
GPU:        Xº C  X.X GB / 16.3 GB  embedder GPU-bound ✓ (ONNX/CUDA)
METRICS:    success=N  errors=0
FAST TESTS: N passed 0 failed
```

Execute this audit now.
