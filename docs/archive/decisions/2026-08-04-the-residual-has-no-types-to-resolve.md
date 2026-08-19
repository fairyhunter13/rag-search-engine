# The residual has no types to resolve

**2026-08-04.** A second resolution pass over the call sites the extractor drops was proposed, measured,
and **declined on its own gate**. This records the measurement so the tier is not re-opened on the
argument that a "real type engine" would reach what tree-sitter cannot.

## What was asked

After receiver-type + inheritance qualification shipped (see
`2026-08-04-a-receiver-type-dissolves-the-ambiguity.md`), 73.6% of the still-dropped call sites had no
receiver type the structural parser could see. A structural parser cannot reach those by construction.
The open question was whether a real type-resolving engine could, and the standing answer was "surely
yes — that is what type engines are for."

## What was run

`scip-php` (MIT, the SCIP indexer for PHP) on one 402-file Laravel root, copied to scratch so no
working tree acquired a `vendor/`. PHP 8.3.6 and Composer 2.7.1 installed from the distro. The index
was read from Python via bindings generated from the `scip.proto` that ships inside the tool.

Two install notes worth keeping:

- **Use `:dev-main`.** The tagged releases stop at `v0.0.2` (April 2023) and pin a v4 parser; `main` is
  current and resolves `nikic/php-parser` v5.8.0.
- **`--ignore-platform-reqs` is required**, and not as a workaround. Roots declare extensions the host
  need not have (`ext-mongodb`, `ext-gd` here); resolution needs the autoloader and the sources, never
  the runtime.

## The result

Cost was never the problem. `composer install` was 13.7 s and 164 MB; scip-php ran in 2.55 s at 71 MB
peak RSS. **Accuracy was zero: 0 of 209 residual sites narrowed to exactly one candidate, against a 25%
gate.**

The mechanism, measured over every ambiguous call site and split by the receiver's shape:

| tree | ambiguous calls | scip named | on `$var->` / `$this->prop->` receivers |
|---|---:|---:|---:|
| A | 846 | 44.2% | **7.2%** |
| B | 359 | 19.2% | **0.0%** |
| C | 150 | 90.7% | **0.0%** |

scip-php names `self::`, `$this->` and global/vendor calls at a high rate — **precisely the calls that
were never ambiguous** — and is silent on the variable receivers that are the whole problem. Hand-read:
on one line it emits occurrences for `self::formatData()` and `ucfirst()` and nothing whatever for two
`$item->…()` calls on the same line; the receiver carries no type declaration anywhere in the file.

## It replicated on a second root, chosen to falsify it

One root decides nothing in this lineage, and the first result was one root. So the pilot was repeated
on a second, larger root **picked specifically because it was the most likely to disprove the claim**:
754 files, and `declare(strict_types=1)` in 83 of 300 sampled files against 0 and 1 in its neighbours —
the most strictly typed Laravel root in the fleet.

**It read 0 of 509.** The receiver split is cleaner there than on the first root, not weaker:

| tree | ambiguous calls | scip named | on `$var->` receivers |
|---|---:|---:|---:|
| A | 672 | 23.2% | **0.0%** |
| B | 650 | 31.2% | **0.0%** |
| C | 229 | 54.6% | **0.0%** |
| D | 42 | 100.0% | — |

Across both pilots: **718 residual sites, zero recovered.** `strict_types` declares that *scalar
arguments* are checked; it says nothing about whether a local variable's class is annotated, which is
the fact resolution actually needs. Picking the corpus most favourable to the hypothesis and watching
it read zero is what makes this a finding rather than one root's accident.

## Why this is not a tool defect

It is a correct type resolver returning nothing because **there is no type to resolve**.

The obvious confound was checked and excluded. One tree in the pilot draws PSR-4 warnings from Composer,
which would explain a resolver going quiet — so the measurement was re-split by directory. Tree B is
textbook PSR-4 and reads **0.0%**, the worst of the three. The silence tracks the receiver's shape, not
the autoload configuration.

## What it generalises to

Every type engine infers from the same declarations. PHPStan, Psalm, intelephense, phpactor and every
LSP server read what the source declares; where it declares nothing, they have the same nothing to say.
**The residual's ceiling is a property of the corpus, not of the tooling.** No amount of engine
shopping moves it, and the two-pass "tree-sitter for bulk, type engine for residual" architecture —
which is sound in principle — has no fuel on this fleet.

This also corrects an earlier argument rather than confirming it. LSP was once declined here for having
no batch API; that reasoning was wrong (it measured LSP against a workload nobody proposed). The
correct reason to decline is this one, and it is stronger: the residual is untyped at the source.

## What survives

scip-php named **2,453** call sites whose callee our index holds no symbol for at all — `is_null`,
`ucfirst`, `app`, framework internals. That is a **coverage** prize, not a resolution one, and indexing
`vendor/` into `graph.db` reaches it far more cheaply than adopting SCIP. Recorded, not scheduled.

## The rule

**Before adopting an engine to recover missing information, confirm the information exists.** The
residual was described for three drafts as "what tree-sitter cannot see," which quietly implied
something else could. Nothing could. One pilot root and a directory split settled it; the engine survey
that preceded it could not have, at any length.
