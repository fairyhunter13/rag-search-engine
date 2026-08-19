# Call extraction skips tree-sitter injections deliberately

**Date:** 2026-07-31
**Status:** Decided. `extract_calls_with_lines` keeps `_iter_script_blocks`; do not switch it to
`_embedded_blocks`.

## The bug that isn't

`graph/extractor.py` has two ways to reach embedded code:

- `_iter_script_blocks` — script/style blocks the host grammar names directly.
- `_embedded_blocks` — the above **plus** rung-1 regions from the host grammar's own
  `injections.scm`.

Symbol extraction (`extract_symbols_with_stats`) uses the second. Call extraction
(`extract_calls_with_lines`) — the one `sweeps._extract_graph` actually calls — uses the first.

Read side by side, that asymmetry looks like an oversight worth one line to close: symbols see
injected regions, calls do not, so call edges are being lost. Two separate readings of this file
reached that conclusion before it was measured.

## What the measurement says

Both variants were run over **all 17,945 host-language files in the fleet** and the call-name sets
diffed:

| language | files | call names gained by switching |
|---|---|---|
| html | 53 | **+191 — every one CSS** (`var` ×158, `rgba` ×24, `blur`, `linear-gradient`) |
| markdown | 1,312 | **+6 — every one CSS**, via injected html |
| php, rust, vue, svelte, nix | 16,580 | **0** |

**Zero legitimate code calls were gained anywhere in the fleet.**

The cause is specific and permanent: html's `injections.scm` statically injects **css**, and
tree-sitter's css grammar reports `var(--x)` and `rgba(...)` as call nodes. They are call nodes in
css's own grammar — that is not a grammar defect. Switching would enrol stylesheet functions into
the code call graph as edges, on every html and markdown file in every project. That is a
regression, not a fix, and it would arrive under the label of a cleanup.

## Why symbols and calls differ, and why that is correct

The two extractions have different tolerances for the same input. A CSS custom property becoming a
`symbol` is noise in a symbol table — findable, low-value, inert. The same token becoming a call
**edge** changes the graph's topology: it creates callers and callees that do not exist, which
feeds community detection, `graph(relation="impact")`, and every consumer downstream of them.
Symmetry between the two helpers would be tidier; it would also be wrong.

## What is genuinely missing, and deliberately deferred

An acme-frontmatter gap is real and was proven in isolation: a `main → helper` edge inside astro
frontmatter is lost. It affects **0 fleet files**. Closing it needs a code-family filter on
injected languages — inject `typescript`, do not inject `css` — not a helper swap. Note it, defer
it; the filter is the actual unit of work, and it should be built when a file needs it.

## Related

- `graph/extractor.py` — the deleted `extract_calls()` carried this same injection-walking bug and
  was dead as well, so it was removed rather than kept as a second, wrong implementation.
- `docs/world-model/model.yaml` — P6/HR15: structure only, no regex or keyword tables. The
  code-family filter above must come from grammar identity, not from a list of language names
  someone believes are stylesheets.
