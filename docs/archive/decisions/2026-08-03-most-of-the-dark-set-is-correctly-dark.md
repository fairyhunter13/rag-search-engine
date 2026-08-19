# Most of the dark set is correctly dark

**Date:** 2026-08-03 · **Stamp:** `EXTRACTOR_REV` e11 → e12 · **Arm:** S13 `_module_binding_walk`

## What the plan asked for, and what was actually there

The item read: *15,501 files reach a rung and emit zero symbols; start with the two that are fleet
mass — php 7,801 (34.0%) and javascript 2,313 (49.3%).* It was the largest coverage number on the
board, an order of magnitude above anything else left.

It was never asked what is *in* those files. So that was measured first — a reservoir sample of 400
dark files per language, re-parsed with the shipping pack, reporting the node types they are made
of. The headline did not survive it. **The dark set is three different problems and only one of
them is an extraction gap.**

| language | dark files | what they actually are | recoverable |
|---|---:|---|---|
| **php** | 7,801 | top-level `text_interpolation` and `echo_statement`; **zero** def-suffixed nodes carrying a `name` field | **no — correctly dark** |
| **groovy** | 2,065 | Gradle DSL: `command` nodes (method calls on an implicit receiver), **216 of 400 error outright** | **no** |
| **javascript** | 2,313 | module-scope `lexical_declaration` / `export_statement` | **yes** |
| **css** | 1,488 | `rule_set` — which carries **no fields at all** | measurable, and declined below |
| html | 583 | fragments; mostly trivially short | no |

php is the important row. It is 46% of the whole dark population, and §1g had already reached the
same conclusion from the other direction: these are CodeIgniter view templates, they define
nothing, and **the ladder returning nothing is the correct answer**. e9 gave php an `injections.scm`
and its dark rate moved 34.0% → 34.0%, which was the confirmation, not a disappointment. Nothing is
owed there.

So the honest ceiling for this item is **~3,900 files, not 15,501**, and after the css measurement
below it is **~1,240**.

## What shipped: S13, and why it is scoped

`_named_binding_walk` (S5, e7) names a function *value* bound to a name. What stays dark after it is
the other half of the same construct: `const routes = [...]`, `export const config = {...}` — files
that declare only data. S13 names those.

Two limits, both measured rather than chosen, and both silent if got wrong:

**Module scope only.** Over the 400-file javascript sample, a module-scope arm rescues **53.8% of
the files for 1.8 symbols each**. The same arm left unscoped adds 1,489 further names of which
**79.5% are locals**. A local variable is not a definition anyone searches for, and every one of
them widens a call-resolution candidate pool — so an unscoped arm would buy dark-file coverage by
making the call graph worse. Scope is read structurally (the declarator's grandparent is the file
root or the `export` wrapping it), so nothing here names a grammar: java's field declarators sit
under a class and are excluded by the same test that admits javascript's top-level ones.

**Only where nothing else spoke.** S13 runs after `_generic_walk` returns empty, which is exactly
the population it was sized against. Without that gate it would fire on every javascript file in the
fleet and add their constants too — a far larger and entirely unmeasured population.

`kind` is `variable`, not a borrowed `function`. These bindings are overwhelmingly data, and calling
data a function is the confident wrong answer `_generic_walk`'s `field` branch already exists to
avoid. No consumer branches on `kind`, so the new value costs nothing.

The rung stays `generic`. S13 supplements the generic walk exactly as S5 supplements `structure`
without renaming it, and a new rung value would change what every census over `file_extraction.rung`
is counting.

Fleet extrapolation from the sample: **~1,243 files rescued for ~2,214 symbols** (+0.45% on
487,856).

## The css arm was measured and declined

The plan named css as the second arm. `rule_set` turns out to carry **no fields at all** — its
`selectors` child is a node type, not a field — and a selector (`.btn, .btn-primary`, `#nav a`) is
not an identifier, so there is no honest `name` to read from it. The only implementable form emits
the grammar's own `class_name` / `id_name` nodes.

Measured: that rescues **1,365 fleet files for 172,470 symbols — 126 per rescued file, +35% on the
fleet's entire symbol count**, against S13's 1.8. And what it buys is thin: a css class has no call
graph and no container, and its text is already in the lexical lane from the chunk body, so the
marginal gain is the symbol lane alone. 116 of the 399 sampled files also carry parse errors.

**The ratio is the finding, not the possibility.** EL15 pins css as dark so the arm cannot be added
later without re-measuring it.

## Method note

This is the third item this session re-scoped by measuring the local population before building
against a published or planned headline — after #2 (17,701 `anon_dropped` → a ~2,000-symbol
typescript residue) and #8 (RepoGraph's EM number → a locally measured marginal gain). In all three
the headline was directionally real and quantitatively wrong, and in two of the three it named the
wrong language. **A coverage number counts files; it does not say what is in them.**
