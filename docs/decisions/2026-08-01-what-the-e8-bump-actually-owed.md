# What the `e8` bump actually owed, written after it shipped

**2026-08-01** · P6 · HR15 · `graph/extractor.py`, `index/discover.py`, `graph/community.py`

This document was specified on 2026-07-31 and not written. The plan that deferred three
`EXTRACTOR_REV` items said the next bump "must decide the `data`-rung question first"; `e8` then
shipped in `d392193` without that decision existing anywhere, and the consolidating doc was never
created. An audit on 2026-08-01 found the gap.

Writing it late is worth more than writing it never, but the interesting part is what the audit
found: **two of the three items had already closed themselves in other documents, and the gate they
were waiting on turns out not to be an extractor question at all.** Every number below was
re-derived on 2026-08-01 rather than copied forward.

## The three deferred items

| item | disposition |
|---|---|
| markdown frontmatter keys as symbols | **closed by measurement**, in `2026-07-31-e7-named-bindings-and-headings.md` §"Not done: markdown frontmatter symbols" — 614 files carry frontmatter and **0 are still dark**, so the arm buys nothing |
| the acme-frontmatter call gap | **recorded and consciously deferred**, in `2026-07-31-call-extraction-skips-injections-deliberately.md` §"What is genuinely missing" — 0 fleet files, and it needs a code-family filter on injected languages first |
| the specification-mandated filename table | **closed here** — the measurement below survived in no document until now |

So the consolidating doc was the right idea and two thirds of its contents found better homes than
it would have been. Only the third item was actually lost.

## The filename table buys nothing, and HR15 already refused it

The item was to give extensionless files a language by name, so `Makefile` and `Dockerfile` stop
being `unknown`. `discover.py:261` already refuses this on principle — *"stay unknown, which is the
honest result rather than a filename table"* — and `test_ts7_extensionless_shebang_files_get_a_language`
pins the refusal. The open question was whether the principle was costing anything.

It is not. Every extensionless file in the fleet was parsed with its best candidate grammar and the
real extractor:

| file | fleet files | candidate grammar | symbols |
|---|---:|---|---:|
| `Dockerfile` | 55 | `dockerfile` | **0.00 / file** |
| `Jenkinsfile` | 38 | `groovy` | **0.00 / file** |
| `Makefile` | 25 | `make` | **0.00 / file** |
| `Procfile` | 2 | — none exists | — |
| `Gemfile`, `Rakefile`, `Vagrantfile` | 0 | `ruby` | not present in the fleet |

**118 files, zero symbols.** The `Jenkinsfile` row is the one that had to be run rather than
assumed: `.groovy` is a productive language here — 3,314 chunks fleet-wide — so a Jenkinsfile
mapped to `groovy` might plausibly have yielded something. It yields nothing, because a declarative
pipeline is a nest of DSL blocks and not the class and method definitions the extractor looks for.

The item is **dropped**, on the same footing as markdown frontmatter: not deferred a fourth time,
closed on a measurement. HR15 was not costing recall, which is the only thing that could have
reopened it.

## The `data`-rung gate: the question was asked about the wrong thing

The deferral rested on one figure — *"the `data` rung is now 270,186 of 360,918 symbols, **74.9%**
of the entire fleet graph, up from 57.6% at the audit"* — and on the inference that spending a
re-derive on a 0.07% item while a 74.9% question was open put the bump on the wrong thing. That
inference was sound. The figure was not stable.

Four values for the same quantity exist across the record, and none matches today:

| source | `data` share |
|---|---:|
| the original audit | 53.7% |
| the deferral | **74.9%** (270,186 / 360,918) |
| `graph/community.py` docstring, 2026-07-31 | **28.1%** (62,449 / 222,483) |
| re-derived 2026-08-01 | **45.1%** (170,202 / 377,554) |

Nothing converged because **the fleet-wide `data` share is not a property of the extractor. It is a
property of one repository.** One store holds **108,996 data symbols — 90.6% of its own 120,247, and
64.0% of every data symbol in the fleet**; almost all of them are JSON keys spread over thousands of
files. The next two stores contribute 4.1% and 2.1%. Whether the fleet reads 28% or 75% depends on
whether that one project is indexed, how much of it, and on what day.

The rung itself is a deliberate flood guard with its reasoning inline at `extractor.py`
(`_DATA_MAX_DEPTH = 2`, `_DATA_MAX_SYMBOLS = 500`, *"the cap is a flood guard, not a visibility
one"*) and it is working exactly as specified. **The denominator is what is wrong, and the lever is
that project's `.rse-index.yaml`, not `EXTRACTOR_REV`.** The same lever, in the same week, took
`.html` from 10.2% of the corpus to 0.87% — see
`2026-07-31-html-duplication-is-within-project.md`.

**Decision: the gate is closed and it owes the next bump nothing.** A future extractor change should
be justified by what it extracts, not by this ratio.

## And the ratio the gate was framed in is diluted 2.1×

Re-derived across all 150 live stores:

| kind | symbols | share | in-edges |
|---|---:|---:|---:|
| `data` | 170,202 | 45.1% | **0** |
| `function` | 137,674 | 36.5% | 109,470 |
| `section` | 26,791 | 7.1% | **0** |
| `method` | 21,846 | 5.8% | 11,895 |
| `class` | 18,570 | 4.9% | 1,013 |
| `field` | 1,944 | 0.5% | 98 |
| `module` | 527 | 0.1% | **0** |

`data`, `section` and `module` are **197,520 symbols, 52.3% of the graph, with exactly zero
in-edges** — not few, zero, by construction: none of them can be a call target. Yet they sit in the
denominator of the number this project's plans have used as their exit criterion for months:

| | |
|---|---:|
| edges / symbol, all kinds — **the reported metric** | **0.3244** |
| edges / symbol over kinds that can be called | **0.6803** |

Both are correct and they answer different questions. The reported one is read as "extraction
quality" and is measuring corpus composition at least as much. A future exit criterion should say
which one it means; the gap between them is a factor of 2.1, which is larger than any extraction
change in this lineage has moved either.

## Status

No code change, no re-derive, no stamp move. Three items closed — two elsewhere, one here. The gate
that blocked `e8` is answered and did not need `e8` to answer it.
