---
type: Constraint
resource: src/rag_search/index/discover.py
title: One predicate decides what is indexed
description: HR29, HR35 and HR28 — every enumerator routes through `_should_drop`, its five tiers resolve in a fixed order, and `docs/` is ordinary source rather than a generated concept.
tags: [discovery, gitignore, rse-index-yaml, hr28, hr29, hr35]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# One predicate decides what is indexed

Two enumerators exist — the bulk walk (`iter_files`) and the incremental watcher path
(`on_change` → `is_ignored_path`) — and **they cannot disagree**, because both resolve through the
single `_should_drop` predicate in `discover.py`.

That structural claim is narrower than "here is a list of walks that honour the config" and much
stronger, because it holds for walks nobody has written yet. It is also the lesson HR36 left behind
when it retired: a second walker that re-implements discovery with weaker rules is a bug, not an
optimisation.

## The five tiers, in order

| Tier | Source | Verdict |
|---|---|---|
| 1 | `.rse-index.yaml` `exclude` | **drop** — strongest, explicit intent |
| 2 | `.rse-index.yaml` `include` | **keep**, overriding everything below |
| 3 | `IGNORED_DIRS` membership, or a hidden (`.`-prefixed) segment below the root | drop |
| 4 | `.gitignore`, root and nested chains, `pathspec`, cached per file on mtime | drop |
| 5 | — | keep |

Tier 2 is what makes RSE config win over `.gitignore` on conflict. Tier 4 is supplementary and
gated by `cfg.respect_gitignore`, default `True`.

## The churn this closed

A live `vite dev` plus Playwright-MCP session continuously rewriting git-ignored `.svelte-kit` and
`.playwright-mcp` flipped the source fingerprint on every write, so the drift gate reported
"drifted" perpetually and re-triggered the full cascade every five minutes — pinning a core and
blocking idle unload. `iter_files` had neither a hidden-dir skip nor `.gitignore` support.
`IGNORED_DIRS` also gained an explicit tool-cache belt (`.svelte-kit`, `.playwright-mcp`, `.astro`,
`.turbo`, `.parcel-cache`, `.vite`, `.output`, `.vitest`) for non-git projects.

The fix adds no idle cost: gitignore specs compile once per file and cache on mtime; the hidden-dir
and `IGNORED_DIRS` checks are string comparisons.

## A silent loader, closed 2026-08-17

The config *loader* dropped an unknown block, a misspelled key, a non-string list member and a
quoted `"false"` without a word — which is how `watcher.max_pending_files` was parsed, inherited and
reported at the overview surface for months while enforcing nothing.

`load_project_config` now raises `ProjectConfigError` with a `difflib` suggestion.
`effective_config` catches it and **quarantines the one project** with `exclude=["*"]` rather than
taking the watcher down over a typo, and `config_error(root)` carries the reason to
`overview(status)` — the only surface on which a quarantined project is visible at all.

SC11 is the general form: every `ProjectConfig` field must have a reader outside the loader and
outside the reporting surface. A field that only round-trips through its own loader is a field that
does nothing.

## `docs/` is ordinary source (HR28)

There is no *generated docs* concept. `iter_files` walks `docs/` with no opt-in flag,
`index_project` embeds it like any other directory, and `scope="docs"` is purely a language
predicate over `_TEXT_LANGS` — `scope="code"` excludes the same set. A docs write is therefore
re-embedded like any other write, which only holds because the index step runs above the code-only
drift gate; see
[the graph lane wakes only on code drift](the-graph-lane-wakes-only-on-code-drift.md).

## Sources

Rows HR28, HR29 and HR35 in
[§13b](../../docs/architecture/federation-ops-and-invariants.md).

| Row | Guard | File |
|---|---|---|
| HR28 | GG1–GG4 | `test_docs_index.py` |
| HR29 | `test_hh1_full_index_baseline` … `test_hh5_is_code_language_contract`, HH6–HH9, SC11 | `test_config_universality.py`, `test_schema_consistency.py` |
| HR35 | `test_gitignore_respected_root_and_nested`, `test_hidden_dir_skip_tool_caches`, `test_include_overrides_gitignore_exclude_beats_include` | `test_config_universality.py` |

Incident record: [idle CPU root causes](../../docs/decisions/2026-07-01-idle-cpu-root-causes.md).
