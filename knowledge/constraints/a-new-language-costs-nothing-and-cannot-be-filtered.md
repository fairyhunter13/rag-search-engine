---
type: Constraint
resource: src/coderag/discover.py, src/coderag/filters.py, src/coderag/search.py
title: A new language is indexed with no change, and the price is that it cannot be named in a filter
description: Discovery is a denylist, so a .zig file is chunked, embedded and searchable the day the language exists — but it carries lang="" and is reachable only by leaving the lang filter unset, which used to return an empty result instead of saying so.
tags: [discovery, search, filters, extensibility]
status: stable
generated: { by: claude/opus-5, at: 2026-08-19T23:30:00Z }
---

# The denylist is the feature

`indexable()` rejects: ignored paths, secret-shaped paths, image and binary extensions. It does not
consult `LANGS` at all. `LANGS` supplies a *label*, nothing more — so a file with an extension
nobody has heard of is discovered, chunked, embedded and searchable with no code change, and the
scope header falls to the code arm, where the generic C-family declaration regex picks up most
brace-and-parenthesis languages for free.

That is why there is no parser, no grammar and no per-language table, and it is the half of the
"one chunker" decision that is easiest to lose. A future "add tree-sitter for real boundaries"
proposal is not only the ~4,200 lines the old engine spent there — it converts an allowlist-free
pipeline into one where a language works when someone ships a grammar for it. The 25% of the fleet
carrying no `lang` label is that design already paying.

# The price, and where it used to be paid silently

An unlabeled file gets `lang=""`, and `""` is not a selectable filter value: `search(lang=...)`
matches on the label, so nothing selects the unlabeled group. That is deliberate — offering `""`
would name a group whose only membership rule is "we have no name for it" — but it means those
chunks are reachable only with the filter unset.

# The label was also just wrong for a fifth of the corpus

`lang_of` read `Path(rel).suffix` alone, so every extensionless file classified as `""` **by
construction** — 1,760 of them here, led by `Dockerfile`, `Jenkinsfile`, `artisan` and `Makefile`,
the files that say how a project builds. A `filters.FILENAMES` map is consulted after the suffix and
is three lines. The rest of the 14,639 unlabeled files were `.svg` (8,039, now filtered as images)
and `.groovy` (2,058, now a `LANGS` entry among ~140 added on 2026-08-21).

Adding to either table reaches an already-indexed file only because `index._relang` re-derives the
column each pass. Without it the widening was inert on every file whose bytes had not changed —
see [a derived column was stored and never re-derived](../defects/a-derived-column-was-stored-and-never-re-derived.md).

None of that weakens the paragraph above: the pipeline still consults no allowlist to decide what to
index, and a language nobody has labeled is still indexed. What the map fixes is the *label*, which
is only ever a filter value. A file whose name is not in it loses nothing but the ability to be
named in `lang=`.

The defect was next to it: an unknown `lang` narrowed the corpus to nothing and returned that as an
ordinary empty result. `mode` had refused unknown values from the first commit; `lang` did not, so
`lang="pyton"` and `lang="cobol"` both read to a caller as "this repo has nothing" — the one failure
mode that cannot be told from an honest answer. `_check_lang` now raises with the same difflib
"did you mean" the config gate uses, and `tools.py` returns that string to the agent rather than
raising, so the hint survives the MCP boundary.
