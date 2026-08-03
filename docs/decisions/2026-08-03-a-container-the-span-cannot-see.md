# A container the span cannot see, and an ERROR that could not be told from XML

**2026-08-03** · `EXTRACTOR_REV` e9 → e10 · plan items #7 (extractor half) and #10

Two items entered this stamp window and **one shipped**. They are unrelated in mechanism and related
in kind: in both, the fix the plan specified turned out not to work, and the version that survives is
the one the measurement pointed at rather than the one that was written down. #7 shipped as a
different mechanism than proposed; #10 is declined outright.

## #7 — the fix that was proposed recovers nothing, and the one that works is a different shape

The plan's §1d measured `qualified_name` on the structure rung and found it a verbatim copy of
`name`: a Go `func (s *Server) Start()` stored as `Start`, with `query/graph_handler.py:15`
resolving `WHERE name = ? OR qualified_name = ?` against nothing dotted. It proposed recording a
container from the enclosing span.

**That proposal was measured before it was built, and it gains zero symbols.**

| language | files | symbols | already qualified | **gained by span nesting** |
|---|---:|---:|---:|---:|
| go | 265 | 6,278 | 23.2% | **0** |
| python | 345 | 3,388 | 35.6% | **0** |
| java | 250 | 3,116 | 83.9% | **0** |
| javascript | 117 | 2,485 | 0.2% | **0** |
| php | 200 | 1,523 | 74.9% | **0** |
| tsx | 228 | 1,062 | 0.6% | **0** |
| typescript | 139 | 914 | 41.8% | **0** |

The reason is that **e7's containment arm already did it**. `_extract_symbols_from` admits a
generic-walk name whose span nests inside a reported class or module, and `_generic_walk` tracks
its own parent as it descends — so a member that *has* an enclosing container already carries it.
§1d's numbers (go 6.6%, this repo 11.5%) were taken before that arm shipped on 2026-07-31; the same
measurement today reads 23.2% and 35.6%. The gap §1d named had already been half closed by a change
made for a different reason, and nobody re-measured.

**What containment structurally cannot reach is a method declared outside its type.** Go's
`func (s *Server) Start()` is a top-level declaration nested in nothing — there is no enclosing span
to find, at any level of effort, because the relationship is expressed in a *field* rather than in
the tree shape. Measured on a 400-file Go corpus: **2,561 of 5,881 unqualified symbols (43.5%)** are
that shape, and after the change exactly those 2,561 are qualified.

The receiver is a grammar field, read the same way `_generic_walk` reads `name`, so this invents no
vocabulary about source text (P6/HR15) and nothing is keyed on the language being Go — any grammar
spelling a receiver the same way gets it free.

**One detail worth the line it costs.** The first implementation took the receiver's *last*
identifier, which is right on 641 of 641 real receivers in the corpus. It is still wrong:
`(s *Stack[T])` ends in the type **parameter**, so the positional rule names `T` where reading the
`type` field names `Stack`. The corpus could not have caught this — it contains no generic
receivers — so the rule was changed on the counter-example rather than on the sample.

## #10 — built, measured, reverted: the credit that rescues a template also rescues XML

`_error_byte_ratio` adds an ERROR node's whole span to the error total and does not descend into it.
That makes the metric useless in precisely the case it gets asked about: one unparsable construct
near the top of a file makes the **root** an ERROR, so the ratio is 1.0 for a file that parsed almost
perfectly, and X2 fires with the rung `language_mismatch`.

§1g traced the fleet's instance: 746 CodeIgniter `*.tpl.php` templates where PHP 5's
`$_priv{PRIV_DELETE}` offset syntax — removed in PHP 8, absent from the grammar — makes the root
ERROR span all 39,630 bytes, with **108,434 `name`, 44,307 `text_interpolation` and 19,562
`function_call_expression` nodes** correctly parsed inside it. `_overview.py:82` already carried the
suspicion in a comment: the rung "names a cause it cannot actually observe."

The plan's fix was to credit an ERROR for the bytes its well-formed named descendants cover. **It
was built, and the suite failed it in one run — 6 tests, `RB1` across five languages plus `EL10`.**
RB1 is not a synthetic guard: it encodes a real fleet shape, a Katalon project shipping **2,439
`.rs`/`.ts` files that are XML**, and its docstring already warned that a grammar with gentler error
recovery "would fall through to `generic` and read as an empty file again — the exact defect this
rung closes."

Measured across every credit rule worth trying, against RB1's XML fixture and 300 real `*.tpl.php`:

| credit rule | XML-as-go | XML-as-rust | XML-as-java | tpl.php still flagged |
|---|---:|---:|---:|---:|
| none (shipped behaviour) | 0.907 | 1.000 | 1.000 | 110/300 |
| any named child | 0.113 | 0.134 | 0.124 | **0/300** |
| children with named children | 0.144 | 0.289 | 0.928 | **0/300** |
| children with ≥ 2 named children | 0.227 | 0.567 | 1.000 | **0/300** |

**Every rule that rescues the templates drops XML below the 0.85 threshold too.** The reason is a
property of the tool, not of the rule: tree-sitter's error recovery manufactures structurally
plausible children out of arbitrary bytes, so "are this ERROR's descendants well-formed" carries
almost no signal about whether the file is the language it claims. The only discriminator that would
work is naming the node kinds real PHP produces — the mapping table P6/HR15 exists to forbid.

So the item is **costed and declined**, on the same footing as the same-directory and import tiers
in `2026-07-31-an-edge-is-a-resolved-call.md`. What it was buying was small even if it had worked:
`language_mismatch` versus `generic` is a **label** on files that legitimately define no symbols
either way — §1g measured that re-parsing them as an HTML host rescues 2%, 14 of 746 — and the
symbol count could not have moved, because the ratio is consulted only after every rung has already
returned nothing and both branches return `[]`. Trading a working wrong-language detector for a
better name on 1.8% of the fleet fails P10.

The reasoning is now a docstring on `_error_byte_ratio` itself, with the table, so the next reader
who notices the 1.0 does not rebuild it.

## What both of these are instances of

The e9 record two entries up ends with a generated capability table predicting a coverage win that
the census then measured at zero. These two are the same failure caught earlier and cheaper: a plan
item whose evidence was real when collected and no longer described the code it was aimed at.

The two failed differently, and the difference is the useful part:

- **#7's evidence went stale.** §1d's numbers were correct on 2026-07-23 and had been half-repaired
  by e7's containment arm on 2026-07-31 for an unrelated reason. A read-only probe caught it in
  minutes; building it would have cost a fleet re-derive returning zero symbols.
- **#10's evidence was never wrong — the *inference* from it was.** "19,562 nodes parsed cleanly
  inside the ERROR" is true and remains true. It simply does not imply that counting those nodes
  separates this file from XML, and nothing short of building it and running RB1 would have shown
  that.

So one rule does not cover both. **Re-measure the gap immediately before building the thing that
closes it** handles #7. #10 needed the other discipline the suite already enforces: a fix aimed at a
diagnostic must be run against the population the diagnostic exists to *catch*, not only against the
population it currently mislabels. RB1 held 2,439 real files' worth of that population and answered
in one run.
