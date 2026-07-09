# rag-search-engine — Claude Code Instructions

Indexed in rag-search — global MCP doctrine lives in `~/.claude/CLAUDE.md`; project protocol below.

## Mandatory tool-use protocol (no exceptions)

1. **Call `overview(what='projects')` first** — confirm the project is indexed before doing anything else.
2. **Call `search` immediately after** — use a natural-language query describing what you need to find.
3. Do NOT delegate to sub-agents via the `Agent` tool — sub-agents do not inherit these instructions. Answer directly.
4. Only fall back to Bash/Grep/Glob/Read if `search` returns no useful results.

This rule applies to EVERY codebase question, even ones that seem simple. Training-data answers about this codebase are stale and untrustworthy.

## Running tests and quality checks

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

**Key env vars** (BPRE resolution ladder):
- `OSE_DEEPSEEK_MODEL` — override DeepSeek model (default `deepseek-v4-flash`; `deepseek-chat` alias deprecates 2026-07-24)
- All LLM lanes (Tier-2 edge linkage, BPRE narrative, wiki L2) are **ON by default**, suppressed only when `DEEPSEEK_API_KEY` is absent. Tier-3 whole-file resolution is **RETIRED** (research-backed 2026-07-09 decision, never built — see `docs/audits/2026-07-09-whole-engine-conformance-and-research.md`).

**CI**: `.github/workflows/ci.yml` — runs on every push (quality → tests → contracts → property tests)

## GPU-only enforcement

**CPU fallback is forbidden.** All inference (embeddings + LLMs) runs on GPU (NVIDIA CUDA).
Any CPU fallback must raise a fatal error — never fall back silently.

## Efficiency invariants (P16/P17, HR32/HR33/HR35/HR36/HR37/HR38/HR39/HR40)

**Idle CPU < 1 %, RAM minimal & constant, GPU maximized.** The KB cascade (enrich/wiki/federation/BPRE)
in `daemon/sweeps.py:on_change` runs only when `_code_source_fingerprint` (code-only, HR38) detects
real source drift — never on metadata-only, non-indexed-file, or non-code (docs/wiki/config/image)
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
in strict order: OSE `.opencode-index.yaml` `exclude` (drop) > OSE `include` (force-keep, wins over
`.gitignore`) > default hidden-dir/`IGNORED_DIRS` policy (drop) > `.gitignore` (drop, supplementary,
gated by `respect_gitignore`, cached per-mtime) > keep. This closes the root-cause found 2026-07-01: a
live `vite dev`/Playwright-MCP session continuously rewriting git-ignored tool-cache dirs
(`.svelte-kit`, `.playwright-mcp`) was flipping the fingerprint on every write and re-triggering the
full cascade every ~5 min, pinning a CPU core indefinitely. `.opencode-index.yaml` now supports
`index.include` (force-keep globs) and `index.respect_gitignore` (default `true`) alongside the
existing `index.exclude`.

**BPRE's own reuse stamp must be code-only and discovery-unified too (HR36).** `kb/bpre.py`'s
federation-wide reuse stamp (`bpre_source_sig`) and per-member scan-cache key (`_member_scan_sig`)
are hashed from `_bpre_code_sig`, which walks `_source_files` — routed through the same HR35
resolver as `iter_files`, gated by `is_code_language` — never the all-files `_source_fingerprint`.
The stamp is written once from the sig computed at rebuild start (no end-of-rebuild recompute
chasing a moving target). This closes a 3rd, distinct root-cause found 2026-07-01: on a 170-member
federation root, concurrent docs/config/image edits and `.claude/*.js` tool-cache churn kept
flipping the all-files stamp faster than the ~5 min BPRE federation rebuild it triggered could
finish, pinning a CPU core continuously even after HR35 shipped. Guarded by `test_idle_stability.py`
BPS1-BPS4 (docs-churn/hidden-dir-churn quiescence, real-code-drift rebuild, convergence).

**The `on_change` cascade gate itself must be code-only, unified with the HR36 BPRE stamp (HR38).**
`daemon/sweeps.py` gains `_code_source_fingerprint` — same coarse-cache-plus-`iter_files`-walk shape
as `_source_fingerprint`, filtered through `is_code_language`, mirroring `kb/bpre.py`'s
`_bpre_code_sig` exactly. It now backs `_graph_stale`'s `source_sig` comparison, both
`set_meta("source_sig", ...)` stamp sites, and `on_change`'s cascade-gate comparison — so non-code
churn (docs/wiki/config/image) can no longer spuriously wake the enrich/wiki/BPRE cascade or force a
graph re-derive, closing the gap where BPRE's own stamp (HR36) was already code-only but the gate
feeding it wasn't. The vector-index/doc-search reindex step runs before this gate and is unaffected.
Guarded by `test_idle_stability.py` FCG1-FCG4.

**Every tree-sitter parse is bounded out-of-process, so no grammar is ever skipped (HR39).**
In-process cancellation is unavailable in this stack (py-tree-sitter 0.25's `progress_callback` never
fires during a stuck parse; `tree_sitter_language_pack`'s bundled parser exposes no callback at all) —
proven this session via a `cobol`-fed-non-cobol-bytes hang that pinned a core unkillable in-process.
`index/bounded_parse.py` routes every parse call-site (`graph/extractor.py`, `kb/bpre_ast.py`,
including the Go fast path) through a persistent **spawn**-context (never `fork`) worker pool; a
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
too since they're children of the same cgroup (`OPENCODE_BOUNDED_PARSE_WORKERS` defaults to `1`
under this quota — two workers would only time-slice one capped core). The proof is `cpu.stat`'s
`nr_throttled`/`throttled_usec` climbing under sustained real load, not just usage staying low — the
canonical cgroup-v2 enforcement signal — cross-checked by a hermetic `systemd-run --user --scope`
self-test independent of the daemon's own unit. Guarded by `test_cpu_budget.py` (CB1-CB6).

## Extraction doctrine (P6, HR15–HR19, HR23)

**No regex, no static/dynamic keyword list, no mapping table for code-semantic inference** — only
tree-sitter structure and, for genuine residual ambiguity, a capped/cached/batched DeepSeek call
(SEA select-not-author). Applies to Category A (`kb/bpre*.py`, `kb/patterns.py`,
`server/_overview.py`); node-kind maps and infra/config ground-truth are exempt. **The debt
registry is empty (2026-07-01)**: the last per-language HTTP method-name table
(`bpre_spec._LANG_SPECS`) was retired for ONE universal structural classifier (URL-anchor +
handler-shape + `_V` verb ground-truth + gRPC proto-binding + `_SCHEMES` receiver-text provenance)
covering all 299 tree-sitter code grammars — see `bpre_generic.py`/`bpre_paradigms.py`. Full
5-tier ladder + Category A/B enumeration: `docs/architecture/federation-and-search-engine.md` §7a.
Token-frugality requirement for any new DeepSeek call site (stable prefix, batch, cap, structural
context, feed
`llm_token_stats()`): `docs/info-hierarchy.md` "Extraction / semantic-resolution ladder". Enforced by
`src/tests/live/test_no_code_semantic_regex.py` + `model.yaml` P6.

**`_provenance` (`bpre_generic.py`) is import- and type-provenance-aware, not just receiver-text
(P6/HR15).** Beyond the original `_SCHEMES`-token receiver-text check, it now also resolves the
receiver's def-use type binding (`build_type_use`, `valueflow.py`, gated on `_NEW_KINDS`) and its
import-map-resolved module path (`_scan_imports`, `bpre_ast.py`, gated on a new `_IMPORT_KINDS`
node-kind set in `bpre_spec.py`) against the same closed `_SCHEMES` set — generalizing Go's existing
import check to every language. This closes typed-client idioms (`client = new HttpClient();
client.GetAsync(...)`) with zero new library-name vocabulary. A bare library-name idiom with no
scheme-bearing signal (`requests`/`axios` on a non-scheme absolute URL) still falls through to the
DeepSeek escalate/whole-file residue tiers — recorded, never silently dropped.

**Embedded-`<script>` sub-parsing (F2, 2026-07-09).** Vue/Svelte/Astro/HTML host grammars parse
`<script>` content as one opaque `raw_text` leaf — structurally blind to embedded JS/TS calls and
symbols. `graph/extractor.py::_iter_script_blocks` and `kb/bpre_ast.py::_script_blocks` locate that
leaf plus its `lang` attribute (node-kind/attribute reads, no vocabulary) and sub-parse it with the
js/ts grammar, remapping line numbers by the block's start row — covering both the symbol/call
graph and BPRE's HTTP-client detection for Vue and Svelte SFCs. Guarded by
`test_embedded_script_extraction.py`.

## Public-release & device-neutrality invariants (P18, HR34)

This repo is **public**. Never commit secrets, real device paths, or company/project names. Every
machine-specific value (storage paths, host, port, models, GPU device) is **env-driven with XDG
defaults** — see `core/config.py:8-46` (`XDG_DATA_HOME`, `OPENCODE_REGISTRY_PATH`,
`OPENCODE_INDEX_ROOT`, `OPENCODE_MCP_DAEMON_HOST/PORT`, `OPENCODE_GPU_DEVICE`, etc.). No hardcoded
absolute paths (`/home/<user>/`, `/root/`, `/Users/<user>/`, `C:\Users\<user>\`), usernames, or
hostnames anywhere in tracked source, tests, docs, scripts, or generated artifacts. Guards:
`test_public_hygiene.py` (whole-tree home-path scan incl. Windows + storage-path env-driven
assertion), `test_no_real_project_in_tests.py` (machine-agnostic test fixtures),
`test_no_mocks_or_fakes.py`, `model.yaml` P7/P18/HR13/HR34. Device-specific *name* bans (real
company/codename/device-id lists) deliberately stay out of this public tree — they live only in the
private `ose-live-audit` repo.

**Runnable-by-anyone contract (hardened 2026-07-09).** Public-release readiness is more than path
hygiene: a fresh clone must run with zero source edits given only env vars and the README setup
steps. `test_public_hygiene.py::test_runtime_config_is_env_driven` asserts every machine/deployment
constant in `core/config.py` — embed/rerank model, embed device, daemon host/port, query LLM
provider/model, GPU device override — is produced by `os.environ.get(...)`, not a hardcoded literal.
`.gitmodules`' `vendor/docgen` submodule URL and the CI `live-fast` job's `github.repository` guard
were audited and found already fork-safe by design (the submodule repo is public; the CI guard exists
specifically so forks lacking a self-hosted GPU runner skip the job instead of queuing indefinitely —
see the comment above that job in `.github/workflows/ci.yml`) — **no change needed**, recorded here so
a future pass doesn't re-flag them. See `docs/audits/2026-07-09-whole-engine-conformance-and-research.md`.

## Project quick reference

- Entry points: `src/rag_search/server/mcp.py` (MCP server + routes), `src/rag_search/daemon/` (daemon package), `src/rag_search/cli.py` (CLI), `src/rag_search/__main__.py` (bridge-stdio shim)
- Packages: `core/ embed/ index/ graph/ kb/ query/ server/ daemon/` under `src/rag_search/`
- Registry: `~/.local/share/rag-search/projects.json`
- Tests: `src/tests/live/` (live suite — requires daemon at :8765, GPU; no local generative LLM)
- LLM: GPU = FastEmbed/ONNX/CUDA (embeddings + reranking only); KB build = cloud DeepSeek; chat = claude-haiku-4-5 only (no DeepSeek fallback, HR12)
- Setup scripts: `scripts/configure_integrations.py`, `scripts/check_system.py`
- Architecture: `docs/architecture/federation-and-search-engine.md` + `docs/architecture/federation-ops-and-invariants.md`

## World model & info-hierarchy

The OSE world model (governing laws, component map, behavior specs) lives in `docs/world-model/`.
The DIKW doctrine ladder lives in `docs/info-hierarchy.md`.
Generated Claude Code skills: `.claude/skills/world-model.md` + `.claude/skills/info-hierarchy.md`.

```bash
# Check working-tree conformance (GPU-free, daemon-free):
python scripts/check_world_model.py

# Regenerate skills after editing model.yaml or info-hierarchy.md:
python scripts/gen_world_model_skills.py
```
