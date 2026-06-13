# FEATURES.md — Parity contract for the clean-room rewrite

Single definition of "100% feature parity". Rewrite is complete when every box is `[x]`.
Generated from the live archived engine before wiping `main`.

---

## 1. MCP tools (5)

### 1.1 search(query, scope="code", project_paths=None, top_k=10, include_federation=True)
- [ ] scope: code(default) | docs(wiki/md/rst/txt filter) | all(no filter) | similar
- [ ] GPU embed + jina-reranker-v1-turbo-en rerank; reranking always on; --no-rerank CLI flag ignored
- [ ] project_paths=None searches all registered projects; include_federation fans to members
- [ ] response: {results:[{path,start_line,end_line,content,language,score}], total, elapsed_ms, projects_searched}
- [ ] runtime_state.note_activity() + note_query() on every call

### 1.2 ask(query, project_path, scope="all", top_k=10, include_federation=True)
- [ ] scope: all(hybrid code+communities+wiki) | architecture(communities only) | wiki(pages only) | global(GraphRAG map-reduce over all community summaries) | feature(entry points+call chain+algorithm+rationale) | business(business-classified communities)
- [ ] global: qwen3-enrich:1.7b MAP + query-LLM REDUCE
- [ ] runtime_state.note_activity() + note_query() on every call

### 1.3 graph(symbol, project_path, relation="definition", to_symbol=None, depth=5, include_federation=True)
- [ ] relation: definition | callers(BFS depth=5) | callees(BFS depth=5) | impact | path(requires to_symbol) | impact_narrative(LLM: risk+domains) | semantic_trace(requires to_symbol)
- [ ] path/semantic_trace without to_symbol returns {error:...}
- [ ] runtime_state.note_activity() + note_query() on every call

### 1.4 overview(project_path=None, what="structure", max_depth=4, top_k=100, export_format="json", max_nodes=5000, since_hours=None)
- [ ] what: structure | communities(by size, top_k=100) | status | projects(no path needed) | metrics(no path needed) | graph_export(json|graphml,max_nodes) | patterns | architecture_domains(top Leiden level) | hierarchy(all levels) | service_mesh | import_cycles(Tarjan SCC) | suggested_questions | graph_diff(since_hours) | surprising_connections(top-20 bridges) | feature_map | business_rules | process_flows
- [ ] runtime_state.note_activity() on every call

### 1.5 index(project_path, enabled=True)
- [ ] enabled=True: register idempotent; daemon auto-indexes+KB+watches+federation; {status:"flagged"|"already_registered",path,note}
- [ ] enabled=False: DESTRUCTIVE — stop watcher + remove registry + delete on-disk index (handle_remove_project delete_index=True)
- [ ] project_path expanduser().resolve()'d; runtime_state.note_activity() called

### 1.6 MCP server mechanics
- [ ] FastMCP stdio (run_mcp_server) + streamable-HTTP at http://127.0.0.1:8765/mcp (run_mcp_http_server)
- [ ] _fastmcp_stub.FastMCPStub fallback if mcp.server.fastmcp not importable
- [ ] instructions field includes _global_prompt_text() (CLAUDE.md/codex/hermes injection text)
- [ ] GPU guard at startup (synchronous, before event loop): exit 1 on no-CUDA; no CPU fallback ever
- [ ] HTTP lifespan: warmup query models -> stale-cleanup task -> resume_watchers -> resume_stalled_pipelines -> sd_notify READY=1 -> on shutdown: cancel tasks + sd_notify STOPPING=1
- [ ] WATCHDOG_USEC: sd_notify("WATCHDOG=1") every WATCHDOG_USEC/2_000_000 ticks
- [ ] Stale client cleanup: release watches for clients silent > DEFAULT_CLIENT_STALE_S (60s)
- [ ] One-shot model idle unload: after DEFAULT_MODEL_IDLE_UNLOAD_S (300s) no inference -> cleanup_models() once; resets on next inference

---

## 2. HTTP routes (59 dashboard.py + 5 mcp.py = 64 total)

### Admin / health (mcp.py)
- [ ] GET /healthz -> {ok,service,transport,uptime_s,load_avg:{1m,5m,15m},cpu_count,...snapshot()}
- [ ] POST /admin/client/open {client_id,cwd} -> register client, start watcher if project found
- [ ] POST /admin/client/heartbeat {client_id} -> refresh last-seen
- [ ] POST /admin/client/close {client_id} -> release client
- [ ] GET /admin/status -> {ok,...snapshot()}

### UI / static (dashboard.py)
- [ ] GET / -> redirect to /dashboard
- [ ] GET /dashboard -> dashboard HTML (5 views)
- [ ] GET /static/{path:path} -> static assets

### Project management
- [ ] GET /api/projects ?project=
- [ ] GET /api/overview ?project=
- [ ] GET /api/communities ?project=&top_k=
- [ ] POST /api/start_watching {project_path}
- [ ] POST /api/stop_watching {project_path}
- [ ] POST /api/projects/register {project_path}
- [ ] POST /api/remove_project {project_path,delete_index?}
- [ ] POST /api/index {path,watch?,force?,follow_symlinks?}

### Wiki
- [ ] GET /api/wiki ?project=
- [ ] GET /api/wiki/page ?project=&page=
- [ ] GET /api/wiki_lint ?project=

### Query / search
- [ ] GET /api/suggested_questions ?project=
- [ ] GET /api/ask ?project=&query=&scope=
- [ ] GET /api/feature ?project=&query=
- [ ] GET /api/search ?project=&query=&top_k=
- [ ] GET /api/patterns ?project=
- [ ] POST /api/analyze_patterns {project_path}

### Business / semantic
- [ ] GET /api/feature_map ?project=
- [ ] GET /api/business_rules ?project=
- [ ] GET /api/process_flows ?project=
- [ ] GET /api/ask_business ?project=&query=

### Graph
- [ ] GET /api/graph ?project=&symbol=&relation=&depth=
- [ ] GET /api/graph_export ?project=&format=&max_nodes=
- [ ] GET /api/service_mesh ?project=
- [ ] GET /api/impact_narrative ?project=&symbol=&depth=
- [ ] GET /api/semantic_trace ?project=&from=&to=
- [ ] GET /api/import_cycles ?project=
- [ ] GET /api/graph_diff ?project=&since_hours=
- [ ] GET /api/callflow_html ?project=&symbol=
- [ ] GET /api/surprising_connections ?project=

### Enrichment / pipeline
- [ ] POST /api/build_hierarchy {project_path}
- [ ] POST /api/enrich_hierarchy {project_path,level?}
- [ ] POST /api/enrich_project {project_path,level?,scope?}
- [ ] POST /api/enrich_symbols {project_path}
- [ ] GET /api/symbol_intent ?project=&symbol=

### Chat / intents
- [ ] POST /api/classify {message} -> {intent,confidence,reason}
- [ ] POST /api/chat {message,project_path?,conversation_id?} -> non-streaming response
- [ ] POST /api/chat_stream {message,project_path?,conversation_id?} -> SSE streaming; codex/gpt-5.4-mini + haiku-4.5 fallback (ONLY surface permitted)

### Health / metrics / ops
- [ ] GET /api/kb_health ?project= -> enrichment % per level + DONE|PENDING verdict
- [ ] GET /api/storage_health ?project=
- [ ] GET /api/git_hooks ?project= / POST /api/git_hooks {project_path,action}
- [ ] POST /api/reload -> SSE close -> SIGTERM -> systemd restarts ~1s
- [ ] POST /api/sweeps/pause
- [ ] POST /api/sweeps/resume
- [ ] GET /api/metrics -> {search:{...},chat_stream:{stream_error_count,error_by_intent,...}}
- [ ] GET /api/metrics/history ?hours=
- [ ] GET /api/auto_pipeline_status
- [ ] GET /api/federation ?project=
- [ ] GET /api/events/stream -> SSE daemon-wide events (pipeline progress, sweep events)
- [ ] GET /api/alerts / POST /api/alerts {level,message,...}
- [ ] GET /api/system_status -> GPU temp, VRAM, Ollama status, daemon uptime
- [ ] GET /api/integrations_status -> Claude Code / Codex / Hermes MCP config state
- [ ] GET /api/jobs
- [ ] GET /api/jobs/{job_id}
- [ ] POST /api/jobs/{job_id}/cancel

---

## 3. Dashboard (5 views, single-page Starlette HTML app)

- [ ] Pulse: KPI tiles (chunks, communities, wiki pages, enrichment %, stream errors) + sparklines + daemon-status dot + SSE live feed + op-log + auto-pipeline tile
- [ ] Chat: message input + send button + SSE streaming display + intent indicator + source chips + multi-turn (conversation_id) + toast notifications; uses codex/gpt-5.4-mini only here
- [ ] Admin: project list (watch status) + KB health per project + storage health + action buttons (enrich/rebuild/vacuum) + reload + sweeps pause/resume + jobs list with cancel
- [ ] Wiki: page list per project + markdown renderer + lint results
- [ ] Graph: Sigma.js WebGL render + community hierarchy tree + import cycles + service mesh + surprising connections + export download (JSON/GraphML)
- [ ] Nav: top navbar (Pulse/Chat/Admin/Wiki/Graph) + Ctrl+K command palette

---

## 4. Chat intents (7, _chat_router)

- [ ] search -- vector search narrative
- [ ] graph_callers -- callers narrative
- [ ] graph_callees -- callees narrative
- [ ] graph_impact -- impact narrative; fallback to feature when no graph data
- [ ] architecture -- community context assembly
- [ ] global -- GraphRAG map-reduce synthesis
- [ ] feature -- entry points + call chain + algorithm + design rationale
- [ ] LLM intent classifier used internally (POST /api/classify)
- [ ] SSE stream: each composer is a streaming generator
- [ ] stream_error_count + error_by_intent tracked; exposed via /api/metrics

---

## 5. KB pipeline

- [ ] Chunking (chonkie) -> GPU embed (FastEmbed-GPU + onnxruntime-gpu, jina-v2-base-code 768d, float16) -> LanceDB
- [ ] Graph extraction: tree-sitter + tree-sitter-language-pack -> AST -> nodes/edges in SQLite (graph.db)
- [ ] Community detection: leidenalg + igraph -> L1 communities in graph.db
- [ ] LLM enrichment via qwen3-enrich:1.7b (Ollama GPU): symbol intent (20/call batch) + community summary + semantic type
- [ ] Recursive hierarchy: L2+ community-of-communities (Leiden meta-graph)
- [ ] Wiki generation: community summaries -> wiki/ dir pages
- [ ] Answer-cache warming: pre-compute common ask queries
- [ ] Federation-first (Phase 102): external symlink members are separate registry entries; iter_files prunes external symlinks from root; watcher skips them
- [ ] Incremental on watch: file change -> debounced re-embed -> graph delta -> community re-detect if significant
- [ ] Stalled-pipeline resume: on daemon startup re-queue projects with file_count>0 but 0 communities or incomplete KB
- [ ] _project_needs_hierarchy gated on has_cross_community_edges() -- no futile builds for zero-edge projects

---

## 6. Daemon

### 6.1 Lifecycle
- [ ] Singleton at 127.0.0.1:8765 (OPENCODE_MCP_DAEMON_HOST, OPENCODE_MCP_DAEMON_PORT)
- [ ] ensure_daemon_running() defers to systemd; idempotent bind; no split-brain / second supervisor
- [ ] GPU guard: exit 1 on no-CUDA; CPU fallback forbidden + fatal always
- [ ] _DAEMON_LOOP + _DAEMON_LOOP_READY published so background threads can post coroutines to the live loop
- [ ] Client tracking via runtime_state; stale release after 60s

### 6.2 systemd integration
- [ ] User service: ~/.config/systemd/user/opencode-search.service
- [ ] Failure-notify service (desktop notification on crash)
- [ ] Thermal drop-in: opencode-search.service.d/thermal-max.conf -- 80C/72C ceiling for RTX 5080 Laptop (no HW thermal protection)
- [ ] sd_notify("READY=1") + sd_notify("STOPPING=1") + WATCHDOG_USEC support
- [ ] daemon install-global: writes MCP block to ~/CLAUDE.md, codex config, hermes config

### 6.3 Four sweep monitors (background threads)
- [ ] _shutdown_monitor: exits after DEFAULT_IDLE_SHUTDOWN_S (900s) no note_activity()
- [ ] _kb_sweep_monitor: every ~6h; per project: thermal-gate (skip if GPU>=80C) -> L1 drain -> L2+ enrich -> answer-cache warm; gated by has_cross_community_edges()
- [ ] _auto_index_monitor: polls ~60s; schedules auto-pipeline for registered-but-unindexed projects; auto_pipeline_enabled() gate
- [ ] _maintenance_monitor: deep vacuum ~6h; WAL checkpoint; stale tier-dir removal

### 6.4 Graceful reload
- [ ] POST /api/reload -> SSE close frame to all clients -> SIGTERM -> systemd restarts ~1s

### 6.5 Global-prompt injection
- [ ] _global_prompt_text() injected into FastMCP instructions at all times
- [ ] daemon install-global writes block to ~/CLAUDE.md + codex config + hermes config

### 6.6 Watcher
- [ ] watchdog.Observer per project; debounced (OPENCODE_DEBOUNCE_DELAY_MS 1000ms, OPENCODE_MIN_FLUSH_INTERVAL_S 5s)
- [ ] On change: re-embed (GPU) -> graph delta -> update indexed_at; external symlink dirs skipped
- [ ] resume_watchers() on startup: starts watcher for every file_count>0 entry; fails loud on error

### 6.7 Federation
- [ ] Root entry has federation:[member_paths...]; each member is a separate registry entry
- [ ] _expand_with_federation() expands roots for cross-federation queries
- [ ] Members watched and indexed independently

---

## 7. CLI (opencode-search)

### 7.1 Top-level commands
- [ ] init [path="."] [--watch] [--force] [--follow-symlinks/--no] [--raw] [--json]
- [ ] index <path> [--watch] [--force] [--follow-symlinks/--no] [--raw] [--json]
- [ ] search <query> [--project/-p repeatable] [--top/-k=10] [--no-rerank ignored] [--json]; auto-detects project from CWD
- [ ] status [path] [--json]
- [ ] list [--json]
- [ ] watch <path> -- blocks until registry watch flag cleared
- [ ] stop-watching <path> [--json]
- [ ] mcp -- stdio MCP server
- [ ] clean-orphans [--yes/-y] [--json] -- dry-run by default
- [ ] storage [--project/-p] [--json]
- [ ] kb-status [--project/-p] [--json] -- DONE/PENDING per level
- [ ] dashboard [--no-open]
- [ ] health [--json] -- exit 1 if GPU not OK

### 7.2 daemon sub-app
- [ ] daemon serve [--host] [--port]
- [ ] daemon ensure [--host] [--port] [--json]
- [ ] daemon bridge-stdio
- [ ] daemon status [--host] [--port] [--json]
- [ ] daemon stop [--host] [--port] [--json]
- [ ] daemon install-systemd [--host] [--port] [--json]
- [ ] daemon install-global [--host] [--port] [--transport=stdio] [--json]

### 7.3 ocs-index wrapper
- [ ] Separate entry point in pyproject.toml [project.scripts]
- [ ] One-shot onboarding: index -> enrich -> hierarchy -> wiki

---

## 8. Configuration and env knobs

### 8.1 Search / indexing
- [ ] OPENCODE_SCHEMA_VERSION "2" | OPENCODE_FTS_THRESHOLD 50 | OPENCODE_IVF_PQ_THRESHOLD 512
- [ ] OPENCODE_IVF_NUM_PARTITIONS_MAX 256 | OPENCODE_IVF_NUM_SUB_VECTORS_MAX 96 | OPENCODE_IVF_NPROBES 16 | OPENCODE_IVF_REFINE_FACTOR 3
- [ ] OPENCODE_STAGE1_VECTOR_K 20 | OPENCODE_STAGE1_RERANK_K 15 | OPENCODE_GLOBAL_RERANK_MAX 100 | OPENCODE_FINAL_TOP_K 10

### 8.2 Watcher / file size / batch
- [ ] OPENCODE_DEBOUNCE_DELAY_MS 1000 | OPENCODE_MIN_FLUSH_INTERVAL_S 5
- [ ] OPENCODE_DEFAULT_SOURCE_FILE_SIZE_KB 2048 | _TEXT_ 1024 | _UNKNOWN_ 512
- [ ] OPENCODE_MAX_INLINE_BYTES 8MB | OPENCODE_EMBED_PASSAGES_MAX_TEXTS 256 | _MAX_BYTES 24MB

### 8.3 Registry / paths
- [ ] OPENCODE_REGISTRY_PATH default ~/.local/share/opencode-search/projects.json
- [ ] OPENCODE_INDEX_ROOT default ~/.local/share/opencode-search/indexes/
- [ ] Registry I/O: atomic write (os.replace on .tmp), fcntl.flock for concurrent safety
- [ ] ProjectEntry: path, db_path, dims, indexed_at, file_count, last_active, watch, federation
- [ ] Registry migration: legacy per-project path -> centralized root; tier-suffix -> tier-free + null indexed_at

### 8.4 Embedding models
- [ ] OPENCODE_EMBED_MODEL default jinaai/jina-embeddings-v2-base-code (768d, ONNX)
- [ ] OPENCODE_RERANK_MODEL default jinaai/jina-reranker-v1-turbo-en
- [ ] DEFAULT_DIMS=768; vectors stored as float16 (49% savings vs float32)

### 8.5 Build-tier LLM (KB enrichment; Ollama qwen3 on GPU; NEVER for dashboard chat)
- [ ] OPENCODE_LLM_PROVIDER ollama | OPENCODE_LLM_MODEL qwen3-enrich:1.7b | OPENCODE_LLM_NUM_CTX 4096 | OPENCODE_LLM_TIMEOUT 120s
- [ ] OPENCODE_LLM_CONCURRENCY default = OLLAMA_NUM_PARALLEL default 3

### 8.6 Query-tier LLM (dashboard chat ONLY; forbidden everywhere else)
- [ ] OPENCODE_QUERY_LLM_PROVIDER codex | OPENCODE_QUERY_LLM_MODEL gpt-5.4-mini (+ haiku-4.5 fallback)
- [ ] OPENCODE_QUERY_LLM_NUM_CTX 4096 | OPENCODE_QUERY_LLM_TIMEOUT 180s

### 8.7 Daemon constants
- [ ] OPENCODE_MCP_DAEMON_HOST 127.0.0.1 | OPENCODE_MCP_DAEMON_PORT 8765
- [ ] OPENCODE_MCP_IDLE_SHUTDOWN_S 900s | OPENCODE_MCP_CLIENT_STALE_S 60s | OPENCODE_MODEL_IDLE_UNLOAD_S 300s

---

## 9. GPU and inference invariants

- [ ] GPU-only: CPUExecutionProvider forbidden; any CPU fallback must raise fatal error, never silently succeed
- [ ] ONNX arena: OPENCODE_ONNX_ARENA_MB 4096MB; arena_extend_strategy=kSameAsRequested; enable_cpu_mem_arena=False; enable_mem_pattern=False
- [ ] ONNX batch_size: 8 for >=8GB VRAM, 6 for <8GB (BFC arena OOM fix)
- [ ] Thermal guard: OPENCODE_GPU_TEMP_MAX 80C; KB sweeps skip if GPU>=80C
- [ ] CuPy where= crash fix: validate zero-length tensors before CuPy normalization ops
- [ ] OPENCODE_DISABLE_TENSORRT default 1 for RTX 5080 (Blackwell not yet TensorRT-supported)
- [ ] Model idle unload: cleanup_models() after 300s idle; one-shot per idle period; resets on next inference
- [ ] FastEmbed cache: ~/.cache/opencode/fastembed; must not be wiped without re-seeding
- [ ] _GPU_INFER_LOCK: global lock preventing concurrent GPU inference races

---

## 10. File discovery

- [ ] IGNORED_DIRS canonical frozenset in discover.py: .git, node_modules, __pycache__, .venv, venv, .env, dist, build, .next, .nuxt, target, vendor, bower_components, .idea, .vscode, coverage, .nyc_output, .cache, tmp, temp, logs, *.egg-info, and all others in the frozen set
- [ ] _REGISTRY_EXCLUDE_SEGMENTS = IGNORED_DIRS + site-packages
- [ ] File size limits enforced per category (source / text / unknown)
- [ ] follow_symlinks=True default; external symlinks pruned in federation-first mode
- [ ] Never index /tmp or ~/.cache/** paths

---

## 11. Test suite (fresh -- written per rewrite layer)

- [ ] No mocks -- all tests hit real daemon (localhost:8765), real GPU, real Ollama qwen3-enrich:1.7b
- [ ] No skipped or xfail tests
- [ ] Session fixtures: pin qwen3-enrich:1.7b resident, pause sweeps (POST /api/sweeps/pause), cap CPU threads
- [ ] Markers: live (daemon+GPU+Ollama required), slow (LLM-heavy >30s)
- [ ] Canonical project: /home/user/git/github.com/fairyhunter13/redacted-name-3
- [ ] Fast suite (-m "live and not slow"): target <=10 min; full suite: 0 fail/0 skip/0 xfail

---

## 12. Hard invariants

- [ ] CPU fallback forbidden -- any attempt must raise a fatal error, never fall back silently
- [ ] Dashboard chat (/api/chat, /api/chat_stream) is the ONLY surface for codex/gpt-5.4-mini + haiku-4.5; all other LLM calls use Ollama qwen3 on GPU
- [ ] ~/GoogleDrive and ~/OneDrive are rclone mounts -- never touch, index, or delete
- [ ] Push after every commit -- zero unpushed at all times
- [ ] Registry at ~/.local/share/opencode-search/projects.json; never index /tmp or cache dirs
- [ ] No duplicate daemon: ensure_daemon_running defers to systemd; idempotent bind only
