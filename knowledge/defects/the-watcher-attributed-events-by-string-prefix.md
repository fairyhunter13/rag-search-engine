---
type: Defect
resource: src/rag_search/daemon/watcher.py
title: The watcher attributed events by string prefix
description: Two registered roots whose paths were string-prefix siblings could receive each other's file events, writing chunks into the wrong project's store while the true owner went stale.
tags: [watcher, federation, index-isolation, defect]
status: resolved
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# The watcher attributed events by string prefix

## Symptom

With two registered roots named such that one path is a string prefix of the other — `foo` and
`foo-bar` — a change under the longer root was sometimes attributed to the shorter one. Chunks
landed in the wrong project's `VectorStore`, and the project that actually owned the file silently
went stale. Which way it went depended on set iteration order, so it was not reproducible from the
outside.

## Root cause

`_owning_root` tested ownership with a raw `path.startswith(proj)` over an unordered set of roots.
`startswith` has no concept of a path boundary: `/x/foo-bar/main.py` genuinely starts with `/x/foo`.

## Why nothing caught it

The watcher tests built their fixtures with non-colliding names — `root` and `member-repo` — so
the colliding case was never exercised. The invariant it violates,
[one absolute path, one index dir](../constraints/a-federation-is-a-query-time-union.md), was fully
tested; only this one route into it was not.

That is the recurring shape: a fixture's incidental naming decided whether a guard could see a whole
class of failure.

## What covers it now

`daemon/watcher.py` resolves the owning root by a boundary-aware longest-`Path.relative_to` match.
The regression guard is `test_wt5_prefix_sibling_roots_no_misattribution` in
`test_idle_stability.py`, which builds exactly the colliding pair.

Found in an adversarial conformance review of the root-plus-member model on 2026-07-09, and fixed
the same session.
