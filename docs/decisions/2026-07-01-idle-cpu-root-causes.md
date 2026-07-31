# Four root causes of idle CPU pinning

**2026-07-01** (HR41 addendum 2026-07-29) · P16/P17, HR32/HR33/HR35/HR37/HR38/HR39/HR40 ·
guards: `test_idle_stability.py` FCG1–FCG4, `test_bounded_parse.py`, `test_cpu_budget.py` CB1–CB6

Target: **idle CPU < 1 %, RAM minimal and constant, GPU maximized.** Four distinct causes were
root-caused in sequence; each fix looked complete until the next surfaced.

The graph-lane cascade (symbol re-derive + structural community labelling — it was the "KB cascade
(enrich/wiki/federation/BPRE)" until 2026-07-28) in `daemon/sweeps.py:on_change` runs only when
`_code_source_fingerprint` detects real source drift — never on metadata-only, non-indexed-file,
or non-code (docs/config/image) events. With no drift the daemon reaches true idle and
`_idle_unload` (300 s, `RSE_MODEL_IDLE_UNLOAD_S`) frees the embedder/reranker and the ORT CUDA
arena.

## 1 — The drift gate's input must be gitignore/hidden-dir-aware (HR35)

`_source_fingerprint` and the watcher's `is_ignored_path` both route through one shared resolver
in `index/discover.py`, applied in strict order: RSE `.rse-index.yaml` `exclude` (drop) > RSE
`include` (force-keep, wins over `.gitignore`) > default hidden-dir/`IGNORED_DIRS` policy (drop) >
`.gitignore` (drop, supplementary, gated by `respect_gitignore`, cached per-mtime) > keep.

Root cause: a live `vite dev`/Playwright-MCP session continuously rewriting git-ignored tool-cache
dirs (`.svelte-kit`, `.playwright-mcp`) was flipping the fingerprint on every write and
re-triggering the full cascade every ~5 min, pinning a CPU core indefinitely. `.rse-index.yaml`
now supports `index.include` (force-keep globs) and `index.respect_gitignore` (default `true`)
alongside the existing `index.exclude`.

## 2 — ~~HR36: BPRE's reuse stamp must be code-only and discovery-unified too~~

**Retired 2026-07-28 with BPRE itself**; `kb/bpre.py`, `bpre_source_sig`, `_member_scan_sig`,
`_bpre_code_sig` and the BPS1–BPS4 quartet are all deleted. **The incident is kept, because the
lesson outlived the subsystem**: on a 170-member federation root, concurrent docs/config/image
edits and `.claude/*.js` tool-cache churn flipped the *all-files* stamp faster than the ~5 min
federation rebuild it triggered could finish, pinning a CPU core continuously even after HR35
shipped.

**The generalisable rule — a reuse stamp must be hashed from the same code-only, HR35-resolved
file set as the walk it gates, and written once from the sig taken at start rather than recomputed
at the end against a moving target** — survives in HR38 below, which is now the whole gate rather
than the second half of a pair.

## 3 — The watcher must be event-driven, one instance for all roots (HR37)

`daemon/watcher.py` runs a single `watchfiles.watch()` generator (Rust `notify`) in one thread
across ALL watched roots — one inotify instance total, not one per root. Rust-side debounce/step
coalesces storms before crossing into Python, and `watch_filter` reuses the same HR35
`is_ignored_path` resolver as the drift gate, so a churn storm in a hidden/gitignored dir never
reaches `on_change`. There is no hand-rolled Python poll loop; polling, if ever needed (NFS/SMB),
is the Rust library's own `force_polling` path.

The predecessor scheduled one recursive watchdog `Observer` per federation member — ~278 threads /
139 inotify instances, already over the default `max_user_instances=128` — and filtered ignored
paths post-hoc in Python per raw event, so an unbounded Python-side `InotifyBuffer` grew to
2.45 GB RSS and pinned one dispatch thread at ~104 % CPU for the life of the process, even after
the HR35/HR36 drift-gate fixes made the gates themselves correctly report no drift.

## 4 — The `on_change` cascade gate must be code-only (HR38)

`daemon/sweeps.py` has `_code_source_fingerprint` — same coarse-cache-plus-`iter_files`-walk shape
as `_source_fingerprint`, filtered through `is_code_language`. It backs `_graph_stale`'s
`source_sig` comparison, both `set_meta("source_sig", ...)` stamp sites, and `on_change`'s
cascade-gate comparison — so non-code churn (docs/config/image) can no longer spuriously wake the
labelling cascade or force a graph re-derive. The vector-index/doc-search reindex step runs before
this gate and is unaffected.

FCG1–FCG4 in `test_idle_stability.py` now carry the four properties BPS1–BPS4 used to assert for
the retired BPRE half.

## Bounded parsing (HR39)

**Every tree-sitter parse is bounded out-of-process, so no grammar is ever skipped.** In-process
cancellation is unavailable in this stack: py-tree-sitter 0.25's `progress_callback` never fires
during a stuck parse, and `tree_sitter_language_pack`'s bundled parser exposes no callback at all
— proven via a `cobol`-fed-non-cobol-bytes hang that pinned a core unkillable in-process.

`index/bounded_parse.py` routes every parse call-site (`graph/extractor.py` — the only one left
since `kb/bpre_ast.py` went with tier 3) through a persistent **spawn**-context (never `fork`)
worker pool. A timed-out worker is killed and respawned, `parse_timeout_count` is exposed via
`overview(what="metrics")`, and the timed-out file is logged by path-hash only (never the real
path) and skipped for that pass only — never silently excluded from the language matrix.
`test_no_unbounded_parse.py` bans any direct `get_parser(...).parse(` call outside
`bounded_parse.py`. Workers never import the embedder, so GPU-only doctrine is unaffected;
overhead is indexing-time only.

## Kernel-enforced CPU budget (HR40)

HR32–HR39 stop the daemon from *spuriously* doing work; HR40 physically bounds it regardless.

**Idle tier**: `daemon/cpu_budget.py` self-measures the daemon's own cgroup-v2 `cpu.stat` usage
delta (exposed via `/healthz` `cpu_percent_core`/`cpu_quota_cores` and `/api/metrics`'s `cpu`
block), live-gated by a test asserting < 1 % of one core over a quiescent window.

**Active tier**: `daemon/systemd.py::unit_text()` sets `CPUQuota=100%` **and**
`CPUAccounting=yes` explicitly — the latter is NOT implied by the former (systemd issue #9647) — a
cgroup-v2 kernel ceiling the daemon's entire service cgroup physically cannot exceed, covering
`bounded_parse.py`'s spawn-context workers too since they're children of the same cgroup
(`RSE_BOUNDED_PARSE_WORKERS` defaults to `1` under this quota; two workers would only time-slice
one capped core).

The proof is `cpu.stat`'s `nr_throttled`/`throttled_usec` climbing under sustained real load, not
just usage staying low — the canonical cgroup-v2 enforcement signal — cross-checked by a hermetic
`systemd-run --user --scope` self-test independent of the daemon's own unit.

## VRAM

Bounded separately and later — see [giving the GPU back before idle](2026-07-29-vram-starvation.md).

See also `docs/info-hierarchy.md` "Compute-spend doctrine" and `docs/world-model/model.yaml`
P16/P17/HR32/HR33/HR37/HR38.
