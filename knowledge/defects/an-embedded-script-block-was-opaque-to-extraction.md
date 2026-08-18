---
type: Defect
resource: src/rag_search/graph/extractor.py
title: An embedded script block was opaque to extraction
description: Vue and Svelte single-file components indexed with zero symbols and zero edges, because those grammars parse the whole `<script>` body as one opaque leaf and the extractor never descended into it.
tags: [tree-sitter, extraction, vue, svelte, defect]
status: resolved
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# An embedded script block was opaque to extraction

## Symptom

Vue and Svelte members indexed with `symbol_hollow: true` and `edges: 0`. Not *few* symbols —
**zero call-like nodes extracted from any single-file component in the repo**.

## Root cause

The `vue` and `svelte` tree-sitter grammars parse the entire `<script>` block as a single
`raw_text` leaf. They are HTML-shaped grammars: the script body is text as far as they are
concerned. `extract_symbols` and the call extractors walked the tree correctly and found nothing,
because there was nothing in the tree to find.

## Why nothing caught it

Two independent reasons, and the second is the durable one.

Extraction tests used single-language files, so no fixture had an embedded block. And **a hollow
graph is indistinguishable from a genuinely sparse repo** — plenty of real repos have few resolved
calls, which is exactly why the edge-free exemption exists in the partition-quality gate (see
[structure is read, never classified](../constraints/structure-is-read-never-classified.md)). The
reconcile self-heal deliberately does not retrigger on hollowness, so nothing upstream complained
either.

## What covers it now

`_iter_script_blocks` in `graph/extractor.py` finds embedded blocks and re-parses their contents
with the inner language, carrying the line offset so reported positions stay true to the outer
file.

The trigger is **structural** — a `script_element` node — not a `{"vue", "svelte"}` language list.
A language list here would have been a mapping table deciding what user code means, which
[HR15](../constraints/structure-is-read-never-classified.md) forbids; it would also have missed
every other grammar with the same shape.

Guards in `test_embedded_script_extraction.py`:
`test_vue_script_symbols_have_correct_line_offset`,
`test_vue_script_calls_have_correct_line_offset`,
`test_svelte_script_symbols_and_calls`. The offset assertions are the load-bearing half — a
re-parse that finds symbols at the wrong lines is worse than one that finds none.

Root-caused 2026-07-09 and deliberately left for a follow-up rather than patched, because it needed
the structural design above. Fixed the same day in the follow-up pass.
