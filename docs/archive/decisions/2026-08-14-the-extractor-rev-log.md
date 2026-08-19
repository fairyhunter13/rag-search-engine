# The extractor revision log

**Stamp:** `EXTRACTOR_REV` in `src/rag_search/graph/extractor.py`. Any change to what
extraction *emits* — that module, or `sweeps._extract_graph`'s call resolution, which no
fingerprint covers — bumps it in the same commit, and the bump is what invalidates every
stored graph in the fleet.

The log lived as a 121-line comment above the constant until 2026-08-14. It is history, not
instruction, so it moved here; the rule above stayed in the code. Six of these revs are cited
by name from live code and tests (`e5`, `e6`, `e7`, `e10`, `e11`, `e13`), which is why the
text is kept verbatim rather than summarised.

## `e1` — 2026-07-28

S8 family-gated call resolution; S1 grammar-decided names; S4 token-matched call nodes (+`macro`
field, `*_signature` excluded); S7 shebang fallback.

## `e2` — 2026-07-29

S0 same-file call edges restored in `sweeps._extract_graph` — the resolution loop discarded every
call whose target was defined in the same file, so ccw's stored graph held 0 same-file edges out
of 1,934. Bundled deliberately with community.py's ALGO_VERSION fg1->fg2 (structural labels): both
invalidate every stored graph, and they compose into ONE `_pipeline_algo_version` string, so
shipping them together re-derives the 160-graph fleet once instead of twice.

## `e3` — 2026-07-30

Rung 4: `_highlight_walk` extracts from the grammar's own highlights.scm when process() and the
generic walk both come back empty. Reaches 237 of 306 languages against `tags`' 48, which is why
the ladder skips a tags rung entirely — measured, `tags` is empty for every language that gets
this far.

## `e4` — 2026-07-30

Rung 5 (`_data_walk`, `data_extraction=True` -> `ProcessResult.data`) gives yaml/json/toml/hcl
their top-level structure instead of a blank row, and the X2 rung `language_mismatch` records a
file whose bytes do not parse as the language its extension claims. Both fire only where every
earlier rung came back empty, so no file that already extracted changes.

## `e5` — 2026-07-30

Rung 1: `_injection_blocks` reads embedded regions out of the host grammar's own `injections.scm`,
supplementing `_iter_script_blocks` rather than replacing it — vue's injections query is *empty*,
and vue is the fleet's highest-coverage host grammar, so the replacement the plan proposed would
have been a straight regression. Unlike e4 this *does* change files that already extracted (an
astro file gains its frontmatter's symbols), so it carries its own rev rather than riding e4's re-
derive silently.

## `e6` — 2026-07-30

The `_generic_walk` supplement gets a span-containment arm, so a class that process() reported
without its members no longer loses them the moment the file also holds a top-level function.
Found by W1-A part 5's ground-truth fixtures on the first run — python and typescript both dropped
a method that the identical file *without* an unrelated function extracted fine. Strictly additive
(a union with the old arm), but it changes files that already extract, so it carries its own rev.

## `e7` — 2026-07-31

Two arms, one rev, because one stamp move re-derives 151 graphs and four of them would have cost
four. S5 `_named_binding_walk`: a function *value* bound to a name (`const getUser = () => {}`,
`valueChange: function () {}`) is a definition process() reports as anonymous, so it arrived
`name=None` and was dropped. Gated on `anon`, so a file process() named completely pays nothing.
And `text.title` -> `section` at rung 4, screened by `_is_title_text` rather than `_is_name_text`,
which recovers markdown headings whole instead of only the one-word ones. Measured on one JS-
dominated store: strictly additive — 0 symbols lost, 0 changed, 1,942 gained, and 55 of its 80
dark JS files lit.

## `e8` — 2026-07-31

Call *resolution*, not extraction — and the only rev here that is subtractive. `_extract_graph`
emitted every same-family definition sharing the callee's name, so 2,220,234 of 2,320,130 fleet
edges bound one call site to N definitions (95.7%; 70.2% of them in groups above 32) and `graph()`
presented all N with equal confidence. Now: prefer definitions in the caller's own file, else the
family, and emit only if that tier holds exactly one candidate after dropping the caller
(`_MAX_CALLEE_FANOUT` in `daemon/sweeps.py`). Measured over 155 stores, 193,309 call-site groups:
precision 0.182 -> 1.000 at recall 0.633; **fleet edges fall 2,320,130 -> ~122,324, a 19x drop,
which is the intended result and not a regression.** Median modularity_q rises 0.578 -> 0.769.
`ALGO_VERSION` deliberately stays `fg3`: the partition changes because its input did, not its
algorithm. This rev is mandatory rather than conventional — the resolution lives in `sweeps.py`,
which S3 below explains is deliberately outside `_code_fingerprint`, so nothing else would have
noticed.

## `e9` — 2026-08-03

No extractor code changed at all — the *grammars* did. `tree-sitter-language-pack` 1.12.1 ->
1.14.0, and this is exactly the case S3 below warns about: a dependency version is invisible to
`_code_fingerprint`, which hashes source modules, so without this rev the fleet would keep serving
edges the installed pack no longer produces. Measured by regenerating `docs/reference/grammar-
capability-matrix.md` on both versions and diffing all 306 pre-existing rows: **0 regressions, 0
languages removed, 65 added, 36 gained capability.** The gains land on the fleet's dark mass
rather than the long tail — php gains highlights *and* injections (rung 6 -> rung 1), which is the
`injections.scm` absence §1g measured on 746 `*.tpl.php` templates; typescript and tsx gain tags
and locals (rung 6 -> rung 4); vue gains highlights and static injections. Totals: languages 306
-> 371, highlights 237 -> 319, tags 48 -> 71, injections 110 -> 157 (static 75 -> 111), locals 81
-> 108, no query files 66 -> 51.

## `e10` — 2026-08-03

`qualified_name` on the structure rung, as the *receiver* arm. The specified span-nesting fix was
measured before being built and gains 0 of 18,766 symbols — e6's containment arm already reaches
everything span nesting can. What containment structurally cannot reach is a method declared
outside its type, which is a grammar *field*, not a tree shape. Fleet-wide go 6.6% -> 40.0%
qualified. Reads the receiver's `type` field rather than its last identifier, because `(s
*Stack[T])` ends in the type *parameter*.

## `e11` — 2026-08-03

Import specifiers, as a fourth element of the extraction tuple. An import is a declared fact and
its target is falsifiable against the filesystem, so resolving one is not code semantics — see
`docs/decisions/2026-08-03-an-import-is-a-declared-fact.md`. Emits no symbols and rewrites no
field, so the dark/anon/symbol census is unchanged across the boundary; that invariance is the
correctness check, not a disappointment.

## `e12` — 2026-08-03

S13 `_module_binding_walk`: a module-scope binding, for files that reach the generic rung and emit
nothing. The other half of S5 — S5 names function *values*, this names the rest (`const routes =
[...]`). Scoped to module level on measurement, not taste: unscoped, 79.5% of what it adds are
locals. The CSS arm the plan asked for alongside it was measured and **declined** — 172,470
symbols to rescue 1,365 files (126 per file, +35% on the fleet symbol count) for selectors that
have no graph and whose text is already in the lexical lane. php's and groovy's dark mass is
correctly dark.

## `e13` — 2026-08-04

PHP receiver hints, as a fifth element (`graph/php_receivers.py`), so callee resolution can narrow
an ambiguous pool by the receiver's *declared* type before `_MAX_CALLEE_FANOUT` drops it. The cap
stays 1 and precision stays 1.000: every recovery still leaves exactly one candidate, which is the
bar the cap already enforces — the type does not overrule the ambiguity, it dissolves it. A
written type, a `use` clause, a PSR-4 prefix and an `extends` edge are all declared facts, so no
name is interpreted (HR15). The tier was struck once on measurement from a CodeIgniter 3 store,
which predates every one of those constructs and was guaranteed to read zero — see
`docs/decisions/2026-08-04-a-receiver-type-dissolves-the-ambiguity.md`.

## `e14` — 2026-08-04

`vimdoc` reclassified from code to text in `index/discover.py`. The pack maps the `.txt` extension
to vimdoc, so `requirements.txt` and `CMakeLists.txt` were taking the 500 kB *code* cap and
feeding `_code_source_fingerprint` — editing a pip pin could wake a graph re-derive, the csv/po
incident of 2026-07-31 recurring in a language that fix did not cover. 390 fleet files (354
`generic`, 36 `language_mismatch`), 0 symbols between them. The rev moves by hand because
`index/discover.py` is **not** in `_FINGERPRINT_MODULES` (`daemon/sweeps.py`), so a classification
change moves no stamp on its own — the same trap `_MAX_CALLEE_FANOUT` documents. Note the graph
itself does not change: those files extracted nothing before and extract nothing now. What moves
is the *denominator* — 390 files leave H1's coverage ratio via `graph/store.py`, so the ratio
shifts without the graph having shifted (`index/discover.py` carries the precedent wording). The
40 other languages the fleet holds were audited in the same pass and none moved; see
`docs/decisions/2026-08-04-the-language-axis-was-already-universal.md`.
