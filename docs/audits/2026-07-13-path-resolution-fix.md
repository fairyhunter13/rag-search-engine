# Path-Resolution Bug Fix — Root Cause, Fix, and Live E2E Validation

> **Date:** 2026-07-13
> **Scope:** MCP query/index path resolution (symlink / subdir / trailing-slash paths against
> the project registry) across `search`, `ask`, `graph`, `overview`, `index`, and the
> `/api/suggested_questions` HTTP route. Also covers a phantom-security-feature removal
> (`RSE_BRIDGE_WORKSPACE_ROOT`) found during the same sweep.
> **Trigger:** live use — querying the actually-indexed `inosoft-project` root and its
> federated `cx-be` member via symlink paths returned `"no project available"` even though
> both were fully indexed.
> **Method:** live reproduction via the MCP tools first (not a code-only review), root-cause
> read of every path-accepting entrypoint, a single shared-resolver fix applied consistently,
> then a dedicated live e2e regression suite plus a full re-run of the federation-scoping
> regression suite.
> **Verdict:** CONFORMS after fix — S1/S2/S3 below were real, live-reproduced defects; all
> three are fixed, covered by new regression tests, and re-verified live against the reloaded
> daemon. One phantom security-feature advertisement (unrelated defect found in the same
> sweep) was removed per explicit user decision. One pre-existing operational issue
> (`/api/reload` not reliably triggering `Restart=on-failure`) was re-observed live during
> verification and is recorded as an open finding, not fixed here (§5).
> **Device neutrality:** paths in this report use placeholders (`<root>`, `<member>`) except
> where a concrete repro is needed for evidence; no secrets included.

---

## §1 — Root cause: one defect, three symptoms

Registry entries and `index_dir()` are keyed on **canonical real path strings** — CLI `init`
canonicalizes (`Path(p).expanduser().resolve()`) before registering. But until this fix,
**query-time path handling did not**, so any non-byte-identical path (a symlink, a subdirectory
of a registered project, or a trailing slash) missed the exact-string registry lookup.

- **S1 — `overview`/`ask`/`graph` "no project available" / "not indexed."**
  `expand_federation(raw_path)` → `get_project(raw_path)` exact-miss on a symlink, a subdir of
  a registered project, or a trailing-slash variant. Reproduced live pre-fix with a symlinked
  federation member and a real subdirectory inside it.
- **S2 — `search` silent over-broadening.** The old `_resolve_roots` matched a symlinked
  member *lexically* (`relative_to()` on the raw, unresolved path) to its **enclosing root**
  before any canonicalization happened, so a query scoped to one member fanned out to the
  **entire federation** (reproduced live pre-fix: ~189 projects, 66s wall time, instead of the
  one member).
- **S3 — `index` tool write-path divergence.** The MCP `index` tool stored the **raw**
  unresolved path, diverging from CLI `init`'s canonicalization — a symlink/relative
  registration produced a registry key + index dir that no read path could ever match.

---

## §2 — Fix: one shared resolver at every path entrypoint

Two helpers added to `core/registry.py`:

- `canonicalize_path(path)` — `str(Path(path).expanduser().resolve())`; identity on
  empty/`OSError`.
- `resolve_registered_root(path)` — canonicalize first, then exact registry hit, then longest
  enclosing **enabled** root, else the canonicalized string. Canonicalizing *before* the
  enclosing-root match is the crux: it makes a symlinked *member* resolve to its **own**
  registered key (curing S2's over-broadening) while a true subdir of only-the-root still
  falls through to the enclosing root (curing S1).

**Read entrypoints** (S1/S2) now call `resolve_registered_root` at entry:
`server/mcp.py::_resolve_roots` (search), `server/_overview.py::handle_overview`,
`query/ask.py::run_ask`, `query/graph_handler.py::run_graph`,
`server/routes_search.py::_suggested_questions_sync`.

**Write entrypoint** (S3) — `server/mcp.py::index` tool — canonicalizes **only**
(`canonicalize_path`, never enclosing-root resolution), matching CLI `init` and deliberately
never mis-registering a child directory under its parent's key.

**Registry self-heal** — `core/registry.py::_migrate` now re-keys any existing entry whose
`canonicalize_path` differs from its stored key, but only when the canonical target isn't
already a separate entry and actually exists on disk — conservative, non-destructive,
non-clobbering (no attempt to "merge" duplicate entries).

---

## §3 — Disproven hypothesis / unrelated defect found in the same sweep

- **Env-var rebrand drift — disproven.** `config.py` and the rest of `src/` use `RSE_*`
  consistently (75 occurrences); zero leftover `OPENCODE_*` references.
- **`RSE_BRIDGE_WORKSPACE_ROOT` — confirmed phantom security feature, removed.** Both
  `mcp-config/claude-code.json` and `mcp-config/hermes.json` advertised this env var as
  confinement ("pin the workspace root so the bridge cannot access indexes outside the opened
  project"), but it had **zero** implementation anywhere in `src/`. Per explicit user decision,
  the advertisement (the `env` block + comment in both files) was removed rather than
  implemented or merely flagged — nothing in `src/` referenced it, so removal is a pure
  documentation-hygiene fix with no behavior change.

---

## §4 — Live e2e validation

New `src/tests/live/test_path_resolution.py` (5 tests, `pytest.mark.live`, 2 also
`pytest.mark.slow`), built on a local `_federate()`/`_clean()` fixture (root + member +
symlink + member-subdir), matching the existing `test_federation_architecture.py` convention:

- `test_resolver_contract` — `resolve_registered_root`: exact→exact, symlink→self,
  subdir→root, unknown→canonical, empty→empty; `canonicalize_path`: empty→empty.
- `test_migrate_rekeys_raw_symlink_registration` — a raw/unresolved registry key is re-keyed
  to its canonical path on the next `list_projects()` call (self-heal), stale raw key gone.
- `test_s3_index_tool_canonicalizes_symlink` — MCP `index()` on a symlink path registers
  under the canonical key, not the raw one.
- `test_s1_overview_ask_graph_resolve_symlink_subdir_trailing_slash` (slow) — all three
  probe variants (symlink, subdir, trailing-slash) resolve to the member's own KB via
  `overview`/`ask`/`graph`, asserting on `resolved_project` and `matches`/answer content.
- `test_s2_search_symlinked_member_does_not_fanout` (slow) — `search` scoped to a symlinked
  member returns `projects_searched == [that member]`, never the enclosing federation.

**Result:** `pytest src/tests/live/test_path_resolution.py -v --strict-markers
--strict-config -ra -q` → **5 passed**.

**Regression re-run** (proves no federation-scoping regression):
`test_federation_architecture.py` + `test_named_projects_features.py` + `test_p1_smoke.py` +
`test_federation_exclude.py` → **74 passed**, zero failures.

**Public-hygiene guard** (`test_no_real_project_in_tests.py`) initially caught a real gap:
the new self-heal test calls `list_projects()` directly (to trigger `_migrate()`'s re-keying
side effect), which the guard treats as a real-project-picker pattern by default. This is the
same registry-mechanics category as the already-allowlisted `test_index_validity.py` /
`test_idle_stability.py`, so `test_path_resolution.py` was added to
`_LIST_PROJECTS_ALLOWLIST` with a comment explaining why. Guard re-run → **5 passed**.

**Manual live re-probe** (the exact 3 paths that failed pre-fix this session, against the
reloaded daemon, using the real `inosoft-project` → `cx-be` federation):

| Probe | Pre-fix | Post-fix |
|---|---|---|
| `overview(status, <root>/repositories/cx-be/)` (trailing slash, symlink) | `"no project available"` | `resolved_project=<cx-be real path>`, `kb_state="ready"` |
| `overview(status, <cx-be real path>/internal/masterdata)` (subdir) | `"no project available"` | `resolved_project=<cx-be real path>`, `kb_state="ready"` |
| `search(..., [<root>/repositories/cx-be])` (symlink, no fan-out) | 66s, whole ~189-project federation | 5.3s, `projects_searched=[<cx-be real path>]` only |

---

## §5 — Open finding (out of scope this session): `/api/reload` restart reliability

While reloading the daemon to pick up this fix (`POST /api/reload`, default
`restart=true`), the process cleanly shut down (uvicorn's normal graceful-shutdown log
sequence, "Finished server process") but **systemd reported `ExecMainCode=killed,
ExecMainStatus=15` (raw SIGTERM), `Result=success`, `NRestarts=0`** — i.e. `Restart=on-failure`
did not fire, and the daemon stayed down until a manual `systemctl --user restart
rag-search-mcp-daemon.service`.

The 2026-07-09 root-federation audit (`docs/audits/2026-07-09-root-federation-audit.md` §8)
recorded and claimed to fix exactly this failure mode (`routes_ops.py::_api_reload` exiting
non-zero on the default path so `Restart=on-failure` fires). That fix's code **is** present in
the current tree (`_reload_exit_code`/`server.py`'s post-`uvicorn.run()` `sys.exit()` check),
yet the failure re-occurred live this session — `sys.exit(3)` never appears to have been
reached; the process was terminated by the OS's default `SIGTERM` disposition instead. Root
cause not diagnosed further this session (would require instrumenting uvicorn's signal-handler
install path) — **recorded as an open finding**, not fixed, since it is outside this session's
approved path-resolution scope. Flagged to the user for a future session/decision.

---

## §6 — Verification log

```
ruff check src/rag_search src/tests
→ All checks passed! (after ruff --fix on two import-order nits in the new test file)

python scripts/check_world_model.py --all
→ CONFORMS — all checkable L1 invariants satisfied (P0-P18)

python -m compileall -q src/rag_search src/tests/live/test_path_resolution.py
→ clean

.venv/bin/pytest src/tests/live/test_path_resolution.py -v --strict-markers --strict-config -ra -q
→ 5 passed

.venv/bin/pytest src/tests/live/test_federation_architecture.py \
    src/tests/live/test_named_projects_features.py \
    src/tests/live/test_p1_smoke.py \
    src/tests/live/test_federation_exclude.py -v --strict-markers --strict-config -ra -q
→ 74 passed

.venv/bin/pytest src/tests/live/test_no_real_project_in_tests.py -v --strict-markers --strict-config -ra -q
→ 5 passed (after allowlist fix)

Manual re-probe of the 3 originally-failing paths, live against the reloaded daemon:
→ all 3 resolve correctly; search scoped to the symlinked member no longer fans out
  (5.3s vs. the previously-reproduced 66s whole-federation fan-out)
```

---

## Files changed this session

- `src/rag_search/core/registry.py` — `canonicalize_path`/`resolve_registered_root` helpers
  + `_migrate` self-heal re-keying
- `src/rag_search/server/mcp.py` — `_resolve_roots` rewritten to use the shared resolver;
  `index` tool canonicalizes before registering
- `src/rag_search/server/_overview.py` — resolve at entry
- `src/rag_search/query/ask.py` — resolve at entry
- `src/rag_search/query/graph_handler.py` — resolve at entry
- `src/rag_search/server/routes_search.py` — resolve at entry
- `mcp-config/claude-code.json`, `mcp-config/hermes.json` — removed phantom
  `RSE_BRIDGE_WORKSPACE_ROOT` advertisement
- `src/tests/live/test_path_resolution.py` — new, 5 tests
- `src/tests/live/test_no_real_project_in_tests.py` — allowlist entry for the new file's
  registry-mechanics `list_projects()` call
- `docs/audits/2026-07-13-path-resolution-fix.md` — this report (new)

No commits made; all changes remain in the working tree pending explicit request.
