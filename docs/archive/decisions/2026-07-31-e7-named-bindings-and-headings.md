# A function bound to a name is a definition, and a heading is not an identifier

**2026-07-31** · P6/HR15 · guard: `test_extraction_ground_truth.py` GT1–GT2, `test_extraction_ladder.py` EL3/EL5

Two extraction arms, one `EXTRACTOR_REV`. They are unrelated as code and inseparable as cost: a
stamp move re-derives every graph in the fleet, so shipping them apart buys the same re-derive
twice. `21d0880` is the precedent — reclassifying csv/po moved `_code_source_fingerprint`, woke a
fleet-wide re-derive at full quota, and made P16 unmeasurable while it ran.

## S5 — the named-binding arm

`process()` reports the *function* node. For `const getUser = () => {}` that node is genuinely
anonymous: the name lives on the binding, not on the function. So the entry arrived with
`name=None`, `sweeps.py`'s `if not sym.name: continue` discarded it, and a file written entirely
in modern JS read exactly like a file with no code in it.

`_named_binding_walk` reads the name off the binding node's own name field. What makes this
structure and not vocabulary (P6/HR15) is that every entry in `_BINDING_NAME_FIELDS` is a *node
kind* and a *field name* — the grammar's spelling of its own shape, the same basis
`_DEF_PARENT_TOKENS` and `_generic_walk`'s kind tests already stand on. Nothing reads an
identifier and decides what it means.

Several name fields per kind, because the grammars disagree about the same construct: a class
field is `public_field_definition`/`name` in typescript and `field_definition`/`property` in
javascript. Measured, not assumed — with only `name` listed, `class K { m = () => {}; }` recovered
under typescript and stayed dark under javascript.

The arm is gated on `anon`, so a file `process()` named completely never pays for the walk, and
keyed on `(name, start_line)` — the identity `symbol_id` already uses — so a recovered symbol can
never shadow a structure entry.

### Equivalence before coverage

Run at the extractor level rather than store-to-store: the same 333 files, twice, with
`_named_binding_walk` stubbed out on one pass. One variable, and no dependence on whatever history
a stored graph was built with.

```
files=333  old_symbols=1410  new_symbols=3352
LOST=0  CHANGED=0  GAINED=1942     (all javascript)
dark js files: 80 before, 55 now lit
```

Strictly additive: nothing lost, nothing changed.

### What 22% of the gain is, and why it stays

427 of the 1,942 are named `default` — `props: { x: { type: Object, default: () => {} } }`, the
Vue prop-factory idiom. They are real function values with a name that carries no identity, and
they are **structurally indistinguishable** from the Vue methods in the same file:
`default: () => {}` and `valueChange: function () {}` are both a `pair` whose value is a function,
at the same nesting depth. Every rule that separates them is a test on the *name*, which is
exactly what P6/HR15 forbids. So they stay, and the measurement is recorded here instead.

The cost was checked rather than assumed. Call resolution keys on `(family, name)`
(`sweeps.py:627`), so the exposure would be a call to `default(...)` binding to all 427 at once —
the fan-out pathology the backlog already tracks. Measured on the same store: **zero call sites
named `default`**, and zero for `handler`, `mounted` and `isInvalid` too. They are inert symbol
rows, not edges.

## The markdown arm

`overview(what="metrics")` read this repo's markdown as **31 files, rung `generic`, 0 symbols,
0.0% coverage**, and the cause was one unmapped capture: markdown's grammar emits exactly
`text.title` and `punctuation.special`, `text.title` was in no map, and `atx_heading` carried no
`_DEF_PARENT_TOKENS` token. Adding `text.title -> section` and `heading` fixes both.

`section` rather than a borrowed `module`, because a heading names a region of a document and call
resolution must never mistake it for something a call could reach. There is no central kind
registry to declare it in; `eval_retrieval.py`'s `_QUERY_KINDS` excludes it by construction.

`section` is added and `section` is *not* added to `_DEF_PARENT_TOKENS`: markdown's heading nodes
are the direct parent of the captured `inline`, so `heading` alone is sufficient, and admitting
`section` would additionally catch whatever `[section]`-shaped grammars name that way, on no
evidence.

### The whitespace decision, made rather than deferred

`_is_name_text` rejects **all** whitespace, deliberately: in an identifier, whitespace is the
signature of an unwrap that fell through to a whole expression. Headings are multi-word. Screening
them with that predicate recovers `Index` and drops `A longer heading here` — markdown would have
gone from 0% to *looking* covered while losing most of it, which is the quiet half-measure this
change exists to avoid.

Relaxing `_is_name_text` was rejected: it would spend a code-wide guarantee to solve a prose
problem. Instead `_is_title_text` is a separate predicate — non-blank, and no newline — and
`_highlight_walk` picks by kind. A heading is one line by construction, so a multi-line hit is the
same fell-through signal the original test was catching.

## `anon_count` is not decremented, and the first cut was wrong to

The arm's first version decremented `anon` per recovered symbol, reasoning that the definition was
"never anonymous, only reported that way". That made EL3 and EL5 fail, and they were right to.

`anon_count` counts *structure entries `process()` could not name*. `_named_binding_walk` is a
different walk over the same tree, and the two do not correspond one-to-one — measured on EL3's own
fixture, `process()` drops one entry and the arm recovers two symbols. Subtracting therefore
reports a number that is neither the drop count nor the recovery count. Coverage is
`symbol_count`; `anon_count` stays a measurement of what `process()` alone can do.

## Two tests changed, one of them a premise

- **`test_extract_unsupported_returns_empty`** used markdown as its stand-in for "unsupported".
  e7 gives markdown symbols, so the test went red on the change that was the point — it was pinning
  the example, not the property. It now asks an unknown language, and still asserts the real
  invariant: unsupported is a return value, not an exception.
- **GT1/GT2 fixtures** gain a `named-binding` javascript case and a `prose` markdown case. GT1 is
  exact set equality in both directions, which is what makes the *negatives* assertions rather than
  hopes: `notFn: 3` is a pair whose value is not a function, and `conf` is a binding whose value is
  an object. Neither may appear. The markdown fixture carries a multi-word heading for the same
  reason.

`handler` in that fixture takes no parameter on purpose. `process()` names an arrow after its sole
parameter — `async (req) => req` yields a `function req` — which predates this arm (it is why EL3's
fixture has always extracted an `x`) and is not what these fixtures are for.

## Not done: markdown frontmatter symbols

Carried across three plans as "fold it into the next bump that happens anyway". This was that bump,
and the headings arm removed its premise: the ladder returns at the first non-empty rung, so a
markdown file that now yields headings never reaches `_data_walk` at all. Making frontmatter
reachable would mean changing the ladder from first-hit to union — for `title`/`date` keys on files
that are no longer dark.

Measured fleet-wide rather than argued:

```
markdown files                                          3595
  lit by headings                                       3538  (98.4%)
  still dark                                              57
  carrying frontmatter                                   614
  frontmatter AND still dark  (what the arm would buy)     0
```

614 files do carry frontmatter, so the premise was not imaginary — it is that **every one of them
already has a heading**, and the ladder stops there. The intersection the arm exists to serve is
empty. The 57 that stay dark are heading-less: link dumps, a bare `CONTRIBUTING` boilerplate, a
pasted meeting note. None of them would be reached by a frontmatter walk either.

The item is dropped on that measurement rather than deferred a fourth time. If the ladder ever
becomes a union rather than first-hit, this is worth re-measuring — but that is the change that
would have to justify itself, not this one.
