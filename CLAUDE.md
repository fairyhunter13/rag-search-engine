# rag-search-engine — Claude Code Instructions

Indexed in rag-search — global MCP doctrine lives in `~/.claude/CLAUDE.md`; project protocol below.

## Mandatory tool-use protocol (no exceptions)

1. **Call `overview(what='projects')` first** — confirm the project is indexed before doing anything else.
2. **Call `search` immediately after** — use a natural-language query describing what you need to find.
3. Do NOT delegate to sub-agents via the `Agent` tool — sub-agents do not inherit these instructions. Answer directly.
4. Only fall back to Bash/Grep/Glob/Read if `search` returns no useful results.

This rule applies to EVERY codebase question, even ones that seem simple. Training-data answers about this codebase are stale and untrustworthy.

## Running tests and quality checks

**One live suite at a time — the suite now enforces this itself.** Multiple agent profiles work in
this checkout (`~/.claude`, `~/.claude-1`, `~/.claude-2`), and two concurrent live suites share one
1-core daemon cgroup, one GPU, one registry and one global sweep pause, so they contaminate each
other's measurements rather than merely running slowly. `pytest_configure` aborts with a
`UsageError` naming the other run's pid and profile (`_contending_live_runs`,
`src/tests/live/conftest.py`). Don't wrap runs in `flock` — that was the previous convention and it
failed silently: on 2026-07-30 two sessions each invented their own lock name
(`/tmp/rse-live.lock` vs `/tmp/rse-live-tests.lock`) and so serialised against nobody. A collision
never announces itself as one; it surfaced as CB3 measuring 0.44 core on an "idle" daemon, a 5 s
`/api/metrics` timeout, 106 pause calls against 4 resumes, two leaked sample-workspace store sets,
and 11 session-setup errors that vanished on re-run — three chased as regressions first. The gate
keys on the contending process, not on a shared lock name, because both parties can honour a
convention to the letter and still collide.

```bash
# Fast smoke check — skips LLM quality tests + browser tests (~5 min)
.venv/bin/pytest src/tests/live/ -m "live and not slow" --ignore=src/tests/live/test_browser.py -x --strict-markers --strict-config -ra -q

# Full live suite — all intents, quality scoring, watcher (~40 min, no browser)
.venv/bin/pytest src/tests/live/ --ignore=src/tests/live/test_browser.py -x --strict-markers --strict-config -ra -q

# Browser / Playwright tests (run separately — conflicts with pytest-asyncio mode=auto)
.venv/bin/pytest src/tests/live/test_browser.py -v --browser chromium

# Code quality
ruff check src/rag_search src/tests
python -m compileall -q src/rag_search
```

**Test markers**:
- `live` — requires daemon at :8765, GPU
- `slow` — LLM-heavy tests (>30s each); skip with `-m "live and not slow"` for fast feedback

**Memory profile**: the live suite loads a real embedder in-process (~1 GB) — intrinsic to the no-mock invariant. Use the fast smoke command above as the default to keep peak RSS lower. Browser tests run in a separate process; don't run them together with the live suite.

**IMPORTANT — run tests foreground only**: never leave the live suite as an unattended background task. The in-process embedder (~1 GB RSS + a full CPU core) stacks on Chrome/Java/Node and can push the machine into swap, freezing the UI. Run pytest in the foreground, serialized, when other heavy apps are not contending.

**Daemon reload** (after code changes): `POST /api/reload` (default, or explicit `?restart=true`) exits non-zero, so the unit's `Restart=on-failure` policy restarts it via systemd in ~1s. `POST /api/reload?restart=false` exits cleanly (0) and intentionally stays down (used by `daemon stop`) — that path needs a manual `systemctl --user restart rag-search-mcp-daemon` to bring it back up. (There is no `daemon reload` CLI subcommand; only `daemon serve/status/ensure/stop/install-global/install-systemd/bridge-stdio` exist.)

**Stream error metrics**: `overview(what="metrics")` returns `chat_stream.stream_error_count` and `chat_stream.error_by_intent`.

**Key env vars** (the generative lane — *rewritten 2026-07-28*):
- `RSE_QUERY_LLM_MODEL` — dashboard-chat model (default `claude-haiku-4-5`), plus
  `RSE_QUERY_LLM_PROVIDER` / `_NUM_CTX` / `_TIMEOUT` (`core/config.py:45-48`).
- **These four are the only LLM vars in the repo.** `RSE_DEEPSEEK_MODEL` and `DEEPSEEK_API_KEY`
  were deleted with tier 3 on 2026-07-28 and have **no reader anywhere in `src/`** — that absence
  is asserted, not assumed (`test_deepseek_api_key_has_no_reader`, `test_inference_lanes.py`).
  There is no LLM lane left to switch on or off: `claude -p` on `POST /api/chat_stream` is the
  whole of it, and no MCP tool reaches it.

**CI**: `.github/workflows/ci.yml` — **owner-triggered only** (quality → tests → contracts → property
tests). Because this is a public repo whose GPU jobs run on a **self-hosted** runner (this device),
there is deliberately **no `pull_request` trigger** — a fork PR must never reach the runner. Triggers
are `push` to main (owner) and manual `workflow_dispatch` (owner). There is deliberately **no
`schedule` trigger**: a nightly cron fired both live jobs unattended and drained the owner's Claude
session quota mid-workday. Fork-PR workflow approval is set to `all_external_contributors`, so
nothing external runs without explicit owner approval.

**`live-fast` vs `live-slow`**: every push runs `live-fast` (`-m "live and not slow"`, <5 min); the
full `@slow` sweep (`live-slow`, ~15–30 min) is **manual only** — `gh workflow run CI`, or the "Run
workflow" button. There is no commit-message trigger: `contains()` matches the whole message
including the body, so a commit that merely mentioned the tag in prose fired a 60-minute real-model
run. **The convergence prescription that used to sit here is retired (2026-07-28).** It named
`test_converge_smoke_standalone` and `test_kb_state_ready_all_projects` guarding
`graph/enrich.py`'s enrich→converge loop; all three left with tier 3, and the property they
measured — how far DeepSeek narration had got — cannot vary now that structural labelling fills
every summary in one deterministic pass (see HR7). The surviving cascade gate is HR38's
FCG1–FCG4 in `test_idle_stability.py`, which runs in `live-fast` on every push, so no
manual-dispatch rule replaces it.

## GPU-only enforcement

**CPU fallback is forbidden.** All inference runs on GPU (NVIDIA CUDA). Since 2026-07-28 that is
exactly **two** residents — the embedder and the cross-encoder reranker; the parenthetical used to
read "embeddings + LLMs" and there is no LLM on this device to include.
Any CPU fallback must raise a fatal error — never fall back silently.

## Efficiency invariants (P16/P17, HR32/HR33/HR35/HR36/HR37/HR38/HR39/HR40)

**Idle CPU < 1 %, RAM minimal & constant, GPU maximized.** The graph-lane cascade (symbol re-derive
+ structural community labelling — *was "KB cascade (enrich/wiki/federation/BPRE)" until 2026-07-28*)
in `daemon/sweeps.py:on_change` runs only when `_code_source_fingerprint` (code-only, HR38) detects
real source drift — never on metadata-only, non-indexed-file, or non-code (docs/config/image)
events. With no drift the daemon reaches true idle and `_idle_unload` (300 s) frees the
embedder/reranker + ORT CUDA arena. **File-watching is event-driven via `watchfiles` (Rust `notify`) —
never manual polling.** `daemon/watcher.py` runs a single `watchfiles.watch()` generator in one thread
across ALL watched roots (one inotify instance total, not one per root); Rust-side debounce/step
coalesces storms before crossing into Python, and `watch_filter` reuses the same `is_ignored_path`
(HR35) resolver as the drift gate, so a churn storm in a hidden/gitignored dir never reaches
`on_change`. There is no hand-rolled Python poll loop — polling, if ever needed (NFS/SMB), is the Rust
library's own `force_polling` path. See `docs/info-hierarchy.md` "Compute-spend doctrine" and
`model.yaml` P16/P17/HR32/HR33/HR37/HR38.

**The drift gate's input must itself be gitignore/hidden-dir-aware (HR35).** `_source_fingerprint` and
the watcher's `is_ignored_path` both route through one shared resolver in `index/discover.py`, applied
in strict order: RSE `.rse-index.yaml` `exclude` (drop) > RSE `include` (force-keep, wins over
`.gitignore`) > default hidden-dir/`IGNORED_DIRS` policy (drop) > `.gitignore` (drop, supplementary,
gated by `respect_gitignore`, cached per-mtime) > keep. This closes the root-cause found 2026-07-01: a
live `vite dev`/Playwright-MCP session continuously rewriting git-ignored tool-cache dirs
(`.svelte-kit`, `.playwright-mcp`) was flipping the fingerprint on every write and re-triggering the
full cascade every ~5 min, pinning a CPU core indefinitely. `.rse-index.yaml` now supports
`index.include` (force-keep globs) and `index.respect_gitignore` (default `true`) alongside the
existing `index.exclude`.

**~~HR36 — BPRE's own reuse stamp must be code-only and discovery-unified too.~~ Retired 2026-07-28
with BPRE itself**; `kb/bpre.py`, `bpre_source_sig`, `_member_scan_sig`, `_bpre_code_sig` and the
BPS1–BPS4 quartet are all deleted. **The incident it was written for is kept, because the lesson
outlived the subsystem**: on 2026-07-01, on a 170-member federation root, concurrent docs/config/
image edits and `.claude/*.js` tool-cache churn flipped the *all-files* stamp faster than the
~5 min federation rebuild it triggered could finish, pinning a CPU core continuously even after
HR35 shipped. The generalisable rule — **a reuse stamp must be hashed from the same code-only,
HR35-resolved file set as the walk it gates, and written once from the sig taken at start rather
than recomputed at the end against a moving target** — survives in HR38 below, which is now the
whole gate rather than the second half of a pair.

**The `on_change` cascade gate must be code-only (HR38).** *(The "unified with the HR36 BPRE stamp"
clause came out on 2026-07-28 — HR36 is retired, so this is the whole gate, not one of a matched
pair.)* `daemon/sweeps.py` has `_code_source_fingerprint` — same coarse-cache-plus-`iter_files`-walk
shape as `_source_fingerprint`, filtered through `is_code_language`. It backs `_graph_stale`'s
`source_sig` comparison, both `set_meta("source_sig", ...)` stamp sites, and `on_change`'s
cascade-gate comparison — so non-code churn (docs/config/image) can no longer spuriously wake the
labelling cascade or force a graph re-derive. The vector-index/doc-search reindex step runs before
this gate and is unaffected. Guarded by `test_idle_stability.py` FCG1-FCG4, which now carry the
four properties BPS1–BPS4 used to assert for the BPRE half.

**Every tree-sitter parse is bounded out-of-process, so no grammar is ever skipped (HR39).**
In-process cancellation is unavailable in this stack (py-tree-sitter 0.25's `progress_callback` never
fires during a stuck parse; `tree_sitter_language_pack`'s bundled parser exposes no callback at all) —
proven this session via a `cobol`-fed-non-cobol-bytes hang that pinned a core unkillable in-process.
`index/bounded_parse.py` routes every parse call-site (`graph/extractor.py` — the only one left
since `kb/bpre_ast.py` went with tier 3 on 2026-07-28) through a persistent **spawn**-context
(never `fork`) worker pool; a
timed-out worker is killed and respawned, `parse_timeout_count` is exposed via
`overview(what="metrics")`, and the timed-out file is logged by path-hash only (never the real path)
and skipped for that pass only — never silently excluded from the language matrix. A guard test
(`test_no_unbounded_parse.py`) bans any direct `get_parser(...).parse(` call outside
`bounded_parse.py`. Workers never import the embedder — GPU-only doctrine is unaffected; overhead is
indexing-time only. Guarded by `test_bounded_parse.py`.

**CPU budget is two-tier and kernel-enforced, not merely cooperative (HR40).** HR32-HR39 stop the
daemon from *spuriously* doing work; HR40 physically bounds it regardless. **Idle tier**:
`daemon/cpu_budget.py` self-measures the daemon's own cgroup-v2 `cpu.stat` usage delta (exposed via
`/healthz` `cpu_percent_core`/`cpu_quota_cores` and `/api/metrics`'s `cpu` block), live-gated by an
automated test asserting < 1 % of one core over a quiescent window. **Active tier**:
`daemon/systemd.py::unit_text()` sets `CPUQuota=100%` **and** `CPUAccounting=yes` explicitly (the
latter is NOT implied by the former — systemd issue #9647) — a cgroup-v2 kernel ceiling the daemon's
entire service cgroup physically cannot exceed, covering `bounded_parse.py`'s spawn-context workers
too since they're children of the same cgroup (`RSE_BOUNDED_PARSE_WORKERS` defaults to `1`
under this quota — two workers would only time-slice one capped core). The proof is `cpu.stat`'s
`nr_throttled`/`throttled_usec` climbing under sustained real load, not just usage staying low — the
canonical cgroup-v2 enforcement signal — cross-checked by a hermetic `systemd-run --user --scope`
self-test independent of the daemon's own unit. Guarded by `test_cpu_budget.py` (CB1-CB6).

**The daemon must be able to give the GPU back on demand (HR41).** HR40 bounds CPU with a kernel
quota; nothing bounded VRAM. ORT's BFC arena only ever grows — it keeps the high-water mark of the
largest batch a session has served until the `InferenceSession` is destroyed, and
`arena_extend_strategy=kSameAsRequested` bounds how *eagerly* it grows, not whether it ever shrinks.
The release lives in `daemon/server.py::release_models()` with three callers — the idle tick,
`_shutdown_exit`, and `POST /api/gpu/release` — precisely because keeping the only copy behind the
300 s idle check made it unreachable when it mattered: **a daemon you are actively working against
never goes idle for 300 s**, so its high-water mark stands for the whole session. Measured: 12.2 GB
of a 16 GB card still held at `active_clients: 0`, 3.5 GB free, which starved the live suite
(~8.4 GB for its own in-process embedder + reranker) into **60 failures inside onnxruntime** —
`CUBLAS failure 3` / `BFCArena` errors naming neither the GPU nor the daemon. Restarting the daemon
turned those same 60 failures into 623 passed with nothing else changed. **If the live suite fails
en masse with ORT allocation errors, check free VRAM before you read the diff.** The suite now
reclaims the GPU in a session fixture and asserts headroom up front (`RSE_TEST_MIN_VRAM_MB`), so a
shortfall says so instead of looking like broken code. Guarded by `test_gpu_budget.py` GB1-GB2.

## Extraction doctrine (P6, HR15)

*(Header was "P6, HR15–HR19, HR23" until 2026-07-28. HR16 (resolution ladder), HR17 (Tier-1.5
value-flow), HR18 (Tier-1.75/2 rerank + token economy) and HR19 (deterministic LLM gating) were all
tier-3 machinery and retired with it. **HR15 is the one that survives, and it got stronger, not
weaker**: the doctrine no longer has an LLM escape hatch to fall through to.)*

**No regex, no static/dynamic keyword list, no mapping table for code-semantic inference** — only
tree-sitter structure. **There is no longer an "and, for residual ambiguity, a capped/cached/batched
DeepSeek call" clause**: the whole write path is deterministic, which is invariant #9 rather than a
gate. Category A (`kb/bpre*.py`, `kb/patterns.py`, `server/_overview.py`) **is empty** now that
`kb/` is gone, so the guard is one scan over `src/rag_search/` with a four-module Category-B
allowlist (`graph.extractor`, `index.discover`, `core.registry`, `core.config` —
`test_no_code_semantic_regex.py:50`); node-kind maps and infra/config ground-truth remain exempt.
Enforced by `src/tests/live/test_no_code_semantic_regex.py` + `model.yaml` P6.

**Retired with tier 3, recorded so it is not re-derived:** the universal structural HTTP classifier
(`bpre_generic.py`/`bpre_paradigms.py`, URL-anchor + handler-shape + `_V` verbs + gRPC binding +
`_SCHEMES` provenance), its import/type-provenance extension over `valueflow.py`/`bpre_ast.py`, and
the DeepSeek escalate/whole-file residue tiers those fell through to. The transferable half is the
doctrine above — **prefer retiring a per-language table over feeding it**, which is how
`bpre_spec._LANG_SPECS` died on 2026-07-01 and why the debt registry was already empty when tier 3
left.

**Embedded-`<script>` sub-parsing (F2, 2026-07-09).** Vue/Svelte/Astro/HTML host grammars parse
`<script>` content as one opaque `raw_text` leaf — structurally blind to embedded JS/TS calls and
symbols. `graph/extractor.py::_iter_script_blocks` *(sole remaining implementation since 2026-07-28
— `kb/bpre_ast.py::_script_blocks` left with tier 3)* locates that leaf plus its `lang` attribute
(node-kind/attribute reads, no vocabulary) and sub-parses it with the js/ts grammar, remapping line
numbers by the block's start row — covering the symbol/call graph for Vue and Svelte SFCs. **This
path is measurably load-bearing**: `.vue` is 92 % covered fleet-wide (2,020 of 2,190 files,
16,477 symbols). Guarded by
`test_embedded_script_extraction.py`.

## Public-release & device-neutrality invariants (P18, HR34)

This repo is **public**. Never commit secrets, real device paths, or company/project names. Every
machine-specific value (storage paths, host, port, models, GPU device) is **env-driven with XDG
defaults** — see `core/config.py:8-46` (`XDG_DATA_HOME`, `RSE_REGISTRY_PATH`,
`RSE_INDEX_ROOT`, `RSE_MCP_DAEMON_HOST/PORT`, `RSE_GPU_DEVICE`, etc.). No hardcoded
absolute paths (`/home/<user>/`, `/root/`, `/Users/<user>/`, `C:\Users\<user>\`), usernames, or
hostnames anywhere in tracked source, tests, docs, scripts, or generated artifacts. Guards:
`test_public_hygiene.py` (whole-tree home-path scan incl. Windows + storage-path env-driven
assertion), `test_no_real_project_in_tests.py` (machine-agnostic test fixtures),
`test_no_mocks_or_fakes.py`, `model.yaml` P7/P18/HR13/HR34. Device-specific *name* bans (real
company/codename/device-id lists) deliberately stay out of this public tree — they live only in the
private `rse-live-audit` repo.

**Runnable-by-anyone contract (hardened 2026-07-09).** Public-release readiness is more than path
hygiene: a fresh clone must run with zero source edits given only env vars and the README setup
steps. `test_public_hygiene.py::test_runtime_config_is_env_driven` asserts every machine/deployment
constant in `core/config.py` — embed/rerank model, embed device, daemon host/port, query LLM
provider/model, GPU device override — is produced by `os.environ.get(...)`, not a hardcoded literal.
This repo has **no submodules** since docgen's deletion (2026-07-28), so a plain `git clone` is
complete and there is no submodule URL left to audit. The CI `live-fast` job's `github.repository`
guard was audited and found already fork-safe by design (it exists specifically so forks lacking a
self-hosted GPU runner skip the job instead of queuing indefinitely — see the comment above that job
in `.github/workflows/ci.yml`) — **no change needed**, recorded here so a future pass doesn't
re-flag it. See `docs/audits/2026-07-09-whole-engine-conformance-and-research.md`.
Self-hosted-runner hardening (2026-07-14): the `pull_request` trigger was removed and the fork-PR
workflow-approval policy tightened to `all_external_contributors`, so a fork PR can no longer trigger
CI on the self-hosted runner — the `github.repository`/ref `if` guards now stand as defense-in-depth.

## Project quick reference

- Entry points: `src/rag_search/server/mcp.py` (MCP server + routes), `src/rag_search/daemon/` (daemon package), `src/rag_search/cli.py` (CLI), `src/rag_search/__main__.py` (bridge-stdio shim)
- Packages: `core/ embed/ index/ graph/ kb/ query/ server/ daemon/` under `src/rag_search/`. Since
  2026-07-28 `kb/` holds **only `answer_cache.py`** (deterministic caching, no LLM — it was always
  tier 2), and `graph/` is `extractor / store / community / quality`; `enrich.py` and `llm.py` are gone
- Registry: `~/.local/share/rag-search/projects.json`
- Tests: `src/tests/live/` (live suite — requires daemon at :8765, GPU; no local generative LLM)
- LLM: GPU = FastEmbed/ONNX/CUDA (embeddings + reranking, and **nothing else** — two residents);
  chat = claude-haiku-4-5 via `claude -p`, dashboard-only, **no fallback** (HR10). *(The "KB build =
  cloud DeepSeek" lane was deleted 2026-07-28 — DeepSeek is not in this repo.)*
- Setup scripts: `scripts/configure_integrations.py`, `scripts/check_system.py`
- Architecture: `docs/architecture/federation-and-search-engine.md` + `docs/architecture/federation-ops-and-invariants.md`

## World model & info-hierarchy

The RSE world model (governing laws, component map, behavior specs) lives in `docs/world-model/`.
The DIKW doctrine ladder lives in `docs/info-hierarchy.md`.
Generated Claude Code skills: `.claude/skills/world-model.md` + `.claude/skills/info-hierarchy.md`.

```bash
# Check working-tree conformance (GPU-free, daemon-free):
python scripts/check_world_model.py

# Regenerate skills after editing model.yaml or info-hierarchy.md:
python scripts/gen_world_model_skills.py
```
