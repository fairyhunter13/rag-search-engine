# FEATURES.md — Parity contract for the clean-room rewrite

Single definition of "100% feature parity". Rewrite is complete when every box is `[x]`.
Generated from the live archived engine before wiping `main`.

---

## 1. MCP tools (5)

### 1.1 search(query, scope="code", project_paths=None, top_k=10, include_federation=True)
- [x] scope: code(default) | docs(wiki/md/rst/txt filter) | all(no filter) | similar
- [x] GPU embed + jina-reranker-v1-turbo-en rerank; reranking always on; --no-rerank CLI flag ignored
- [x] project_paths=None searches all registered projects; include_federation fans to members
- [x] response: {results:[{path,start_line,end_line,content,language,score}], total, elapsed_ms, projects_searched}
- [x] runtime_state.note_activity() + note_query() on every call

### 1.2 ask(query, project_path, scope="all", top_k=10, include_federation=True)
- [x] scope: all(hybrid code+communities+wiki) | architecture(communities only) | wiki(pages only) | global(GraphRAG map-reduce over all community summaries) | feature(entry points+call chain+algorithm+rationale) | business(business-classified communities)
- [x] global (MCP ask): assembled context — community map + code chunks, NO LLM synthesis (Phase-100 read-only path via compose_answer); global LLM synthesis (MAP + REDUCE) is the HTTP /api/ask / dashboard path
- [x] runtime_state.note_activity() + note_query() on every call

### 1.3 graph(symbol, project_path, relation="definition", to_symbol=None, depth=5, include_federation=True)
- [x] relation: definition | callers(BFS depth=5) | callees(BFS depth=5) | impact | path(requires to_symbol) | impact_narrative(LLM: risk+domains) | semantic_trace(requires to_symbol)
- [x] path/semantic_trace without to_symbol returns {error:...}
- [x] runtime_state.note_activity() + note_query() on every call

### 1.4 overview(project_path=None, what="structure", max_depth=4, top_k=100, export_format="json", max_nodes=5000, since_hours=None)
- [x] what: structure | communities(by size, top_k=100) | status | projects(no path needed) | metrics(no path needed) | graph_export(json|graphml,max_nodes) | patterns | architecture_domains(top Leiden level) | hierarchy(all levels) | service_mesh | import_cycles(Tarjan SCC) | suggested_questions | surprising_connections(top-20 bridges) | feature_map | business_rules | process_flows
- [x] runtime_state.note_activity() on every call

### 1.5 index(project_path, enabled=True)
- [x] enabled=True: register idempotent; daemon auto-indexes+KB+watches+federation; {status:"flagged"|"already_registered",path,note}
- [x] enabled=False: DESTRUCTIVE — stop watcher + remove registry + delete on-disk index (handle_remove_project delete_index=True)
- [x] project_path expanduser().resolve()'d; runtime_state.note_activity() called

### 1.6 MCP server mechanics
- [x] FastMCP stdio (run_mcp_server) + streamable-HTTP at http://127.0.0.1:8765/mcp (run_mcp_http_server)
- [x] _fastmcp_stub.FastMCPStub fallback if mcp.server.fastmcp not importable
- [x] instructions field includes _global_prompt_text() (CLAUDE.md/codex/hermes injection text)
- [x] GPU guard at startup (synchronous, before event loop): exit 1 on no-CUDA; no CPU fallback ever
- [x] HTTP lifespan: warmup query models -> stale-cleanup task -> resume_watchers -> resume_stalled_pipelines -> sd_notify READY=1 -> on shutdown: cancel tasks + sd_notify STOPPING=1
- [x] WATCHDOG_USEC: sd_notify("WATCHDOG=1") every WATCHDOG_USEC/2_000_000 ticks
- [x] Stale client cleanup: release watches for clients silent > DEFAULT_CLIENT_STALE_S (60s)
- [x] One-shot model idle unload: after DEFAULT_MODEL_IDLE_UNLOAD_S (300s) no inference -> cleanup_models() once; resets on next inference

---

## 2. HTTP routes (59 dashboard.py + 5 mcp.py = 64 total)

### Admin / health (mcp.py)
- [x] GET /healthz -> {ok,service,transport,uptime_s,load_avg:{1m,5m,15m},cpu_count,...snapshot()}
- [x] POST /admin/client/open {client_id,cwd} -> register client, start watcher if project found
- [x] POST /admin/client/heartbeat {client_id} -> refresh last-seen
- [x] POST /admin/client/close {client_id} -> release client
- [x] GET /admin/status -> {ok,...snapshot()}

### UI / static (dashboard.py)
- [x] GET / -> redirect to /dashboard
- [x] GET /dashboard -> dashboard HTML (5 views)
- [x] GET /static/{path:path} -> static assets

### Project management
- [x] GET /api/projects ?project=
- [x] GET /api/overview ?project=
- [x] GET /api/communities ?project=&top_k=
- [x] POST /api/start_watching {project_path}
- [x] POST /api/stop_watching {project_path}
- [x] POST /api/projects/register {project_path}
- [x] POST /api/remove_project {project_path,delete_index?}
- [x] POST /api/index {path,watch?,force?,follow_symlinks?}

### Wiki
- [x] GET /api/wiki ?project=
- [x] GET /api/wiki/page ?project=&page=
- [x] GET /api/wiki_lint ?project=

### Query / search
- [x] GET /api/suggested_questions ?project=
- [x] GET /api/ask ?project=&query=&scope=
- [x] GET /api/feature ?project=&query=
- [x] GET /api/search ?project=&query=&top_k=
- [x] GET /api/patterns ?project=
- [x] POST /api/analyze_patterns {project_path}

### Business / semantic
- [x] GET /api/feature_map ?project=
- [x] GET /api/business_rules ?project=
- [x] GET /api/process_flows ?project=
- [x] GET /api/ask_business ?project=&query=

### Graph
- [x] GET /api/graph ?project=&symbol=&relation=&depth=
- [x] GET /api/graph_export ?project=&format=&max_nodes=
- [x] GET /api/service_mesh ?project=
- [x] GET /api/impact_narrative ?project=&symbol=&depth=
- [x] GET /api/semantic_trace ?project=&from=&to=
- [x] GET /api/import_cycles ?project=
- [x] GET /api/callflow_html ?project=&symbol=
- [x] GET /api/surprising_connections ?project=

### Enrichment / pipeline
- [x] POST /api/build_hierarchy {project_path}
- [x] POST /api/enrich_hierarchy {project_path,level?}
- [x] POST /api/enrich_project {project_path,level?,scope?}
- [x] POST /api/enrich_symbols {project_path}
- [x] GET /api/symbol_intent ?project=&symbol=

### Chat / intents
- [x] POST /api/classify {message} -> {intent,confidence,reason}
- [x] POST /api/chat {message,project_path?,conversation_id?} -> non-streaming response
- [x] POST /api/chat_stream {message,project_path?,conversation_id?} -> SSE streaming; claude-haiku-4-5 (Claude Code CLI) ONLY surface; codex removed

### Health / metrics / ops
- [x] GET /api/kb_health ?project= -> enrichment % per level + DONE|PENDING verdict
- [x] GET /api/storage_health ?project=
- [x] GET /api/git_hooks ?project= / POST /api/git_hooks {project_path,action}
- [x] POST /api/reload -> SSE close -> SIGTERM -> systemd restarts ~1s
- [x] POST /api/sweeps/pause
- [x] POST /api/sweeps/resume
- [x] GET /api/metrics -> {search:{...},chat_stream:{stream_error_count,error_by_intent,...}}
- [x] GET /api/metrics/history ?hours=
- [x] GET /api/auto_pipeline_status
- [x] GET /api/federation ?project=
- [x] GET /api/events/stream -> SSE daemon-wide events (pipeline progress, sweep events)
- [x] GET /api/alerts / POST /api/alerts {level,message,...}
- [x] GET /api/system_status -> GPU temp, VRAM, embedder status, daemon uptime
- [x] GET /api/integrations_status -> Claude Code / Codex / Hermes MCP config state
- [x] GET /api/jobs
- [x] GET /api/jobs/{job_id}
- [x] POST /api/jobs/{job_id}/cancel

---

## 3. Dashboard (5 views, single-page Starlette HTML app)

- [x] Pulse: KPI tiles (chunks, communities, wiki pages, enrichment %, stream errors) + sparklines + daemon-status dot + SSE live feed + op-log + auto-pipeline tile
- [x] Chat: message input + send button + SSE streaming display + intent indicator + source chips + multi-turn (conversation_id) + toast notifications; uses claude-haiku-4-5 (Claude Code) only here
- [x] Admin: project list (watch status) + KB health per project + storage health + action buttons (enrich/rebuild/vacuum) + reload + sweeps pause/resume + jobs list with cancel
- [x] Wiki: page list per project + markdown renderer + lint results
- [x] Graph: Sigma.js WebGL render + community hierarchy tree + import cycles + service mesh + surprising connections + export download (JSON/GraphML)
- [x] Nav: top navbar (Pulse/Chat/Admin/Wiki/Graph) + Ctrl+K command palette

---

## 4. Chat intents (7, _chat_router)

- [x] search -- vector search narrative
- [x] graph_callers -- callers narrative
- [x] graph_callees -- callees narrative
- [x] graph_impact -- impact narrative; fallback to feature when no graph data
- [x] architecture -- community context assembly
- [x] global -- GraphRAG map-reduce synthesis
- [x] feature -- entry points + call chain + algorithm + design rationale
- [x] LLM intent classifier used internally (POST /api/classify)
- [x] SSE stream: each composer is a streaming generator
- [x] stream_error_count + error_by_intent tracked; exposed via /api/metrics

---

## 5. KB pipeline

- [x] Chunking (chonkie) -> GPU embed (FastEmbed-GPU + onnxruntime-gpu, jina-v2-base-code 768d, float16) -> sqlite-vec
- [x] Graph extraction: tree-sitter + tree-sitter-language-pack -> AST -> nodes/edges in SQLite (graph.db)
- [x] Community detection: leidenalg + igraph -> L1 communities in graph.db
- [x] LLM enrichment: symbol intent (20/call) + community/L2 summary + semantic_type via cloud DeepSeek only (crash if no DEEPSEEK_API_KEY); no local generative LLM; daemon reclassify_all=False (no churn), <3-member structural guard
- [x] Recursive hierarchy: L2+ community-of-communities (Leiden meta-graph)
- [x] Wiki generation: community summaries -> wiki/ dir pages
- [x] Answer-cache warming: pre-compute common ask queries
- [x] Federation-first (Phase 102): external symlink members are separate registry entries; iter_files prunes external symlinks from root; watcher skips them
- [x] Incremental on watch: file change -> debounced re-embed -> graph delta -> community re-detect if significant
- [x] Stalled-pipeline resume: on daemon startup re-queue projects with file_count>0 but 0 communities or incomplete KB
- [x] _project_needs_hierarchy gated on has_cross_community_edges() -- no futile builds for zero-edge projects

---

## 6. Daemon

### 6.1 Lifecycle
- [x] Singleton at 127.0.0.1:8765 (OPENCODE_MCP_DAEMON_HOST, OPENCODE_MCP_DAEMON_PORT)
- [x] ensure_daemon_running() defers to systemd; idempotent bind; no split-brain / second supervisor
- [x] GPU guard: exit 1 on no-CUDA; CPU fallback forbidden + fatal always
- [x] _DAEMON_LOOP + _DAEMON_LOOP_READY published so background threads can post coroutines to the live loop
- [x] Client tracking via runtime_state; stale release after 60s

### 6.2 systemd integration
- [x] User service: ~/.config/systemd/user/opencode-search.service
- [x] Failure-notify service (desktop notification on crash)
- [x] Thermal drop-in: opencode-search.service.d/thermal-max.conf -- 80C/72C ceiling for RTX 5080 Laptop (no HW thermal protection)
- [x] sd_notify("READY=1") + sd_notify("STOPPING=1") + WATCHDOG_USEC support
- [x] daemon install-global: writes MCP block to ~/CLAUDE.md, codex config, hermes config

### 6.3 Four sweep monitors (background threads)
- [x] _shutdown_monitor: exits after DEFAULT_IDLE_SHUTDOWN_S (900s) no note_activity()
- [x] _kb_sweep_monitor: every ~6h; per project: thermal-gate (skip if GPU>=80C) -> L1 drain -> L2+ enrich -> answer-cache warm; gated by has_cross_community_edges()
- [x] _auto_index_monitor: polls ~60s; schedules auto-pipeline for registered-but-unindexed projects; auto_pipeline_enabled() gate
- [x] _maintenance_monitor: deep vacuum ~6h; WAL checkpoint; stale tier-dir removal

### 6.4 Graceful reload
- [x] POST /api/reload -> SSE close frame to all clients -> SIGTERM -> systemd restarts ~1s

### 6.5 Global-prompt injection
- [x] _global_prompt_text() injected into FastMCP instructions at all times
- [x] daemon install-global writes block to ~/CLAUDE.md + codex config + hermes config

### 6.6 Watcher
- [x] watchdog.Observer per project; debounced (OPENCODE_DEBOUNCE_DELAY_MS 1000ms, OPENCODE_MIN_FLUSH_INTERVAL_S 5s)
- [x] On change: re-embed (GPU) -> graph delta -> update indexed_at; external symlink dirs skipped
- [x] resume_watchers() on startup: starts watcher for every file_count>0 entry; fails loud on error

### 6.7 Federation
- [x] Root entry has federation:[member_paths...]; each member is a separate registry entry
- [x] _expand_with_federation() expands roots for cross-federation queries
- [x] Members watched and indexed independently

---

## 7. CLI (opencode-search)

### 7.1 Top-level commands
- [x] init [path="."] [--watch] [--force] [--follow-symlinks/--no] [--raw] [--json]
- [x] index <path> [--watch] [--force] [--follow-symlinks/--no] [--raw] [--json]
- [x] search <query> [--project/-p repeatable] [--top/-k=10] [--no-rerank ignored] [--json]; auto-detects project from CWD
- [x] status [path] [--json]
- [x] list [--json]
- [x] watch <path> -- blocks until registry watch flag cleared
- [x] stop-watching <path> [--json]
- [x] mcp -- stdio MCP server
- [x] clean-orphans [--yes/-y] [--json] -- dry-run by default
- [x] storage [--project/-p] [--json]
- [x] kb-status [--project/-p] [--json] -- DONE/PENDING per level
- [x] dashboard [--no-open]
- [x] health [--json] -- exit 1 if GPU not OK

### 7.2 daemon sub-app
- [x] daemon serve [--host] [--port]
- [x] daemon ensure [--host] [--port] [--json]
- [x] daemon bridge-stdio
- [x] daemon status [--host] [--port] [--json]
- [x] daemon stop [--host] [--port] [--json]
- [x] daemon install-systemd [--host] [--port] [--json]
- [x] daemon install-global [--host] [--port] [--transport=stdio] [--json]

### 7.3 ocs-index wrapper
- [x] Separate entry point in pyproject.toml [project.scripts]
- [x] One-shot onboarding: index -> enrich -> hierarchy -> wiki

---

## 8. Configuration and env knobs

### 8.1 Search / indexing
- [x] OPENCODE_SCHEMA_VERSION "2" | OPENCODE_FTS_THRESHOLD 50 | OPENCODE_IVF_PQ_THRESHOLD 512
- [x] OPENCODE_IVF_NUM_PARTITIONS_MAX 256 | OPENCODE_IVF_NUM_SUB_VECTORS_MAX 96 | OPENCODE_IVF_NPROBES 16 | OPENCODE_IVF_REFINE_FACTOR 3 (N/A: sqlite-vec replaces LanceDB IVF-PQ)
- [x] OPENCODE_STAGE1_VECTOR_K 20 | OPENCODE_STAGE1_RERANK_K 15 | OPENCODE_GLOBAL_RERANK_MAX 100 | OPENCODE_FINAL_TOP_K 10

### 8.2 Watcher / file size / batch
- [x] OPENCODE_DEBOUNCE_DELAY_MS 1000 | OPENCODE_MIN_FLUSH_INTERVAL_S 5
- [x] OPENCODE_DEFAULT_SOURCE_FILE_SIZE_KB 2048 | _TEXT_ 1024 | _UNKNOWN_ 512
- [x] OPENCODE_MAX_INLINE_BYTES 8MB | OPENCODE_EMBED_PASSAGES_MAX_TEXTS 256 | _MAX_BYTES 24MB

### 8.3 Registry / paths
- [x] OPENCODE_REGISTRY_PATH default ~/.local/share/opencode-search/projects.json
- [x] OPENCODE_INDEX_ROOT default ~/.local/share/opencode-search/indexes/
- [x] Registry I/O: atomic write (os.replace on .tmp), fcntl.flock for concurrent safety
- [x] ProjectEntry: path, db_path, dims, indexed_at, file_count, last_active, watch, federation
- [x] Registry migration: legacy per-project path -> centralized root; tier-suffix -> tier-free + null indexed_at

### 8.4 Embedding models
- [x] OPENCODE_EMBED_MODEL default jinaai/jina-embeddings-v2-base-code (768d, ONNX)
- [x] OPENCODE_RERANK_MODEL default jinaai/jina-reranker-v1-turbo-en
- [x] DEFAULT_DIMS=768; vectors stored as float16 (49% savings vs float32)

### 8.5 Build-tier LLM (KB enrichment; cloud DeepSeek; NEVER for dashboard chat; no local LLM)
- [x] cloud DeepSeek-only (DEEPSEEK_API_KEY required; crash if absent — no local fallback)
- [x] OSE_DEEPSEEK_MODEL env override (default: deepseek-chat)

### 8.6 Query-tier LLM (dashboard chat ONLY; forbidden everywhere else)
- [x] OPENCODE_QUERY_LLM_PROVIDER claude | OPENCODE_QUERY_LLM_MODEL claude-haiku-4-5 (primary; codex removed)
- [x] QUERY_LLM_FALLBACK_MODEL = OSE_DEEPSEEK_MODEL env (default: deepseek-chat) — used when haiku CLI absent or returns empty
- [x] OPENCODE_QUERY_LLM_NUM_CTX 4096 | OPENCODE_QUERY_LLM_TIMEOUT 180s

### 8.7 Daemon constants
- [x] OPENCODE_MCP_DAEMON_HOST 127.0.0.1 | OPENCODE_MCP_DAEMON_PORT 8765
- [x] OPENCODE_MCP_IDLE_SHUTDOWN_S 900s | OPENCODE_MCP_CLIENT_STALE_S 60s | OPENCODE_MODEL_IDLE_UNLOAD_S 300s

---

## 9. GPU and inference invariants

- [x] GPU-only: CPUExecutionProvider forbidden; any CPU fallback must raise fatal error, never silently succeed
- [x] ONNX arena: OPENCODE_ONNX_ARENA_MB 4096MB; arena_extend_strategy=kSameAsRequested; enable_cpu_mem_arena=False; enable_mem_pattern=False
- [x] ONNX batch_size: 8 for >=8GB VRAM, 6 for <8GB (BFC arena OOM fix)
- [x] Thermal guard: OPENCODE_GPU_TEMP_MAX 80C; KB sweeps skip if GPU>=80C
- [x] CuPy where= crash fix: validate zero-length tensors before CuPy normalization ops
- [x] OPENCODE_DISABLE_TENSORRT default 1 for RTX 5080 (Blackwell not yet TensorRT-supported)
- [x] Model idle unload: cleanup_models() after 300s idle; one-shot per idle period; resets on next inference
- [x] FastEmbed cache: ~/.cache/opencode/fastembed; must not be wiped without re-seeding
- [x] _GPU_INFER_LOCK: global lock preventing concurrent GPU inference races

---

## 10. File discovery

- [x] IGNORED_DIRS canonical frozenset in discover.py: .git, node_modules, __pycache__, .venv, venv, .env, dist, build, .next, .nuxt, target, vendor, bower_components, .idea, .vscode, coverage, .nyc_output, .cache, tmp, temp, logs, *.egg-info, and all others in the frozen set
- [x] _REGISTRY_EXCLUDE_SEGMENTS = IGNORED_DIRS + site-packages
- [x] File size limits enforced per category (source / text / unknown)
- [x] follow_symlinks=True default; external symlinks pruned in federation-first mode
- [x] Never index /tmp or ~/.cache/** paths

---

## 11. Test suite (fresh -- written per rewrite layer)

- [x] No mocks -- all tests hit real daemon (localhost:8765), real GPU; no local generative LLM
- [x] No skipped or xfail tests
- [x] Session fixtures: pause sweeps (POST /api/sweeps/pause), cap CPU threads
- [x] Markers: live (daemon+GPU required), slow (LLM-heavy >30s)
- [x] Canonical project: a large multi-repo workspace resolved from the registry (no hardcoded device paths)
- [x] Fast suite (-m "live and not slow"): target <=10 min; full suite: 0 fail/0 skip/0 xfail

---

## 12. Hard invariants

- [x] CPU fallback forbidden -- any attempt must raise a fatal error, never fall back silently
- [x] Dashboard chat (/api/chat_stream): claude-haiku-4-5 primary + DeepSeek fallback (codex removed); KB build = cloud DeepSeek-only; no local generative LLM
- [x] ~/GoogleDrive and ~/OneDrive are rclone mounts -- never touch, index, or delete
- [x] Push after every commit -- zero unpushed at all times
- [x] Registry at ~/.local/share/opencode-search/projects.json; never index /tmp or cache dirs
- [x] No duplicate daemon: ensure_daemon_running defers to systemd; idempotent bind only
