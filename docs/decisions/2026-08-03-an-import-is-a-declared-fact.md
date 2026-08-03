# An import is a declared fact, and a specifier is not code semantics

**Date:** 2026-08-03 · **Stamp:** `EXTRACTOR_REV` e10 → e11 · **Table:** `file_imports`

## What shipped

A file-to-file import relation, extracted alongside call sites and resolved alongside call edges.
`extract_import_specs` reads the module specifier each import statement declares;
`_ImportResolver` in `sweeps.py` turns that string into a file, or drops it.
`overview(what="import_cycles")` now reads the table whose name it has always used.

## Why the gap was measured before it was built

The plan justified this item with RepoGraph's CrossCodeEval Identifier-Match EM 16.7 → 36.1%. That
number is for a system whose retriever reads the graph, and §1e established that this repo's does
not — `query/search.py` mentions neither `edges` nor `community_id`. #12 (personalized PageRank)
was built on the strength of a similarly borrowed citation and reverted in the same session.

So the local marginal value was measured first: **how many file pairs does an import edge add that
the call graph does not already induce?** If A imports B, A usually calls into B, and that pair is
already there.

| store | import pairs | new pairs | gain over the call graph | already covered by calls |
|---|---:|---:|---:|---:|
| this repo (python) | 455 | 92 | +16.2% | 79.8% |
| a go service | 2,456 | 1,668 | +47.8% | 32.1% |
| a typescript app | 1,151 | 775 | +59.1% | 32.7% |
| a js/vue frontend | 89 | 89 | **+81.7%** | **0.0%** |
| a PSR-4 php service | 173 | 172 | **+220.5%** | ~0% |
| two CodeIgniter-3 php repos | 0 | 0 | +0% | — |

The two extremes are the finding. **A vue component is imported and used in the *template*, never
called** — so the call graph induces none of its pairs and the import edge is the only relation
those files will ever have. And php splits by framework rather than by language: PSR-4 repos gain
the most of anything measured, CodeIgniter 3 gains exactly nothing, because CI3 loads classes
through a framework registry and writes no `use` statement to read. Nothing in the published
evidence would have predicted either.

## Why a separate table and not rows in `edges`

`edges` is `(caller_sid, callee_sid)`, and every consumer assumes both endpoints are symbols —
`purge_dangling_edges` deletes any row whose sid is gone, `graph(relation=…)` walks sid to sid.
An import statement sits at file scope, **inside no symbol, so it has no caller sid to name.**

Giving it one would mean inventing a file-level pseudo-symbol, which changes what a graph node *is*
for every reader of `symbols` — the `symbol_count` denominator, the community partition, the hollow
check, `search`'s symbol lane. A separate table costs one migration and no invariant. That is the
same trade `file_extraction` made for the same reason.

It also costs a duplicated sweeper: `prune_imports_to` mirrors `prune_edges_to` because the row a
re-derive has to retract is the one whose two files both still exist, with the `import` line
between them deleted. `purge_dangling_edges` structurally cannot see that, and `file_imports` has
no dangling concept at all — the resolver only writes a row whose target is already in the index.

## P6 / HR15: is resolving a specifier "code semantics"?

The plan (§5a, Tier 3) flagged this as a call to make deliberately rather than by accident, so:
**resolving a module specifier to a file is not code semantics, and here is the line.**

A specifier is **a string the source declares**. Reading it is the same standing as `_is_call_node`
reading a node type or e10's receiver arm reading a `type` field — structure, not vocabulary. What
happens next is the part that needed deciding, and every rule shipped satisfies two conditions:

1. **Its input is either path arithmetic or a checked-in declarative file** — `go.mod`'s `module`
   line, `composer.json`'s `autoload.psr-4` map. Never a table of framework knowledge this repo
   maintains, which is the thing HR15 exists to keep out. Adding a language means reading that
   language's manifest, not extending a map.
2. **Its answer is falsifiable against the filesystem.** A candidate is emitted only if the file
   exists *and* is already in this index. A specifier that resolves to nothing is dropped in
   silence — there is no fuzzy tier, no name match, no fallback. This is what separates it from
   the same-directory tier struck in `2026-07-31-an-edge-is-a-resolved-call.md`: that one guessed
   and could not be checked; this one computes and can be.

What is *not* shipped, and is the boundary: **Tier 3 template path maps** (Blade's
`@extends('layouts.app')` → `resources/views/layouts/app.blade.php`). That map is a framework
convention with no manifest declaring it, so implementing it means writing the convention down in
this codebase. Rule 1 refuses it. Revisit only with a manifest to read.

## What the go rule gives up, deliberately

A Go import names a **package**, which is a directory. The resolver names its first non-test file.
Fanning out to every file in the package would multiply one declared fact into N asserted ones; a
single edge is a truthful statement that the importer depends on that package, and understates
rather than overstates. Recorded because it is the one lossy rule and it should not be "fixed"
into a fan-out without a measurement.

## The consumer, and why it is a repair rather than a feature

`_find_import_cycles` ran Tarjan over `SELECT DISTINCT s1.file, s2.file FROM edges …` — the
file-level projection of the **call** graph — under the name `import_cycles`. It answered a
different question than the one asked, and the two disagree in both directions: the js/vue row
above is 0% overlap, and a call can cross files that import each other transitively rather than
directly. It now reads `file_imports` and returns nothing on a store that predates e11, because an
empty answer is honest and silently substituting a different relation is the failure this ends.

That consumer also satisfies **SC6** (no write-only columns), alongside `import_count()` and
`list_imports()`.

## Stamp discipline

`sweeps.py` is deliberately not fingerprinted, so `_ImportResolver` living there moves no stamp on
its own — `EXTRACTOR_REV` was bumped in the same commit, per `_pipeline_algo_version`'s standing
instruction. The lease was taken **before the first edit** to `extractor.py`, which restamps on
save.
