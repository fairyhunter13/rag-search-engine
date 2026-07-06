# status skill

Comprehensive status audit of the rag-search-engine system.

## What to check

### 1. Registry health
- Call `overview(what='projects')` — list all indexed projects
- Flag: projects with 0 communities (not enriched)
- Flag: stale test artifacts (paths in /tmp or .venv)
- Flag: projects not watching (watching=false)
- Remove any stale entries with `index(project_path=<path>, enabled=False)` (also frees the index dir)

### 2. Canonical test target verification
- `overview(project_path='<TEST_PROJECT_PATH>', what='status')` — confirm watching=true, communities>5000
- `search(query='payment handler', project_paths=[test_path])` — confirm search returns real results
- `ask(query='how does payment flow work', project_path=test_path, scope='feature')` — confirm structured answer with entry_points
- `graph(symbol='PaymentService', project_path=test_path, relation='impact_narrative')` — confirm graph works
- `GET /api/kb_health?project=<test_path>` — confirm enrichment_pct=100.0

### 3. Dashboard routes
Check all key routes return expected HTTP status:
- `GET /dashboard` → 200
- `GET /api/projects` → 200
- `GET /api/jobs` → 200
- `GET /api/metrics` → 200
- `POST /api/chat_stream` with valid project → SSE stream starts

### 4. GPU & resource profile
- `nvidia-smi --query-gpu=temperature.gpu,memory.used,memory.total --format=csv,noheader`
- Confirm GPU temp < 85°C, VRAM < 14 GB
- `curl -s http://127.0.0.1:8765/api/healthz` — confirm embedder loaded (GPU-bound, not CPU)

### 5. Daemon metrics
- `GET /api/metrics` — report stream_success_count, stream_error_count, error_by_intent
- Flag any error_by_intent entries (should be 0)

### 6. Fast test suite
- Run `.venv/bin/pytest src/tests/live/ -m "live and not slow" -q --ignore=src/tests/live/test_browser.py`
- Must be 330 passed, 0 failed

## Output format

```
REGISTRY:   28 indexed, 2 removed (stale), 0 unenriched
KB:         search ✓  ask ✓  graph ✓  kb_health 100%
DASHBOARD:  /dashboard 200  /api/projects 200  /api/jobs 200
GPU:        Xº C  X.X GB / 16.3 GB  embedder GPU-bound ✓ (ONNX/CUDA)
METRICS:    success=N  errors=0
FAST TESTS: 330 passed 0 failed
```

Execute this audit now.
