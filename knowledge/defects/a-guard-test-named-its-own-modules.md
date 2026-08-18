---
type: Defect
resource: src/tests/live/test_no_code_semantic_regex.py
title: A guard test named its own modules
description: A guard screened a hand-written list of module names; when one of those modules was deleted the guard raised `ModuleNotFoundError` at import instead of asserting, and a crashing guard is as silent about its invariant as a passing one.
tags: [guards, test-design, defect]
status: resolved
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# A guard test named its own modules

## Symptom

The no-regex guard stopped checking anything. It raised `ModuleNotFoundError` at collection when a
module it named was deleted — so the invariant went unenforced, and the only way to notice was to
run the suite and read the error, not to grep for it.

## Root cause

The guard held a hand-maintained list of module names and fed them to `importlib.import_module`.
Nothing guarded the list. A module deleted elsewhere in the tree left a name behind that resolved to
nothing.

## Why nothing caught it

**A guard whose membership list is hand-maintained has nothing guarding the list.** And a guard that
crashes and a guard that passes are equally silent about the property they exist to hold — the only
difference is a line in the output somebody has to read.

The originating module was `kb/llm_escalation.py`, itself a second find from the same audit: a
fully-built, fully-unit-tested Tier-2 escalation implementation that **nothing ever called**, while
the docs cited it as the live one. It was deleted rather than wired up. Both halves of that
sentence are the lesson — a module with tests and no caller is not covered, it is decorated.

## What covers it now

`test_no_code_semantic_regex_outside_allowlist` derives its module set by `rglob`-ing every `*.py`
under the package and screening by file content, with the Category-B allowlist as the only
exemption. Deleting a module cannot blind it, and adding one cannot escape it.

`test_category_b_allowlist_has_no_dead_entries` closes the other end: an exemption naming a module
that no longer uses the exempted thing is a hole with nobody watching it.

The general form of this defect recurs across the repo's guard design and is worth checking any new
guard against: **derive the scan set, never enumerate it.** The same reasoning drives
`_citation_sites()` in `test_coverage_map_traceability.py` reading `git ls-files` rather than a
glob list, and the ban-a-call-pattern shape in
[every parse is bounded out of process](../constraints/every-parse-is-bounded-out-of-process.md).
