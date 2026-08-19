# A private key was searchable, and a CSV export was waking the graph

**2026-07-31** · P6 / HR34 / HR38 · guards: `test_asset_exclusion.py` ASSET1–3,
`test_secret_exclusion.py` SEC1–3, `test_generated_drift_exclusion.py` GEN1, GEN4,
`test_language_coverage_universal.py`

Four predicates in `index/discover.py` and one per-project config took **56,978 chunks — 13.42 %
of the fleet** — out of the vector stores. None of it had ever answered a question. Two of the
four were fixing something worse than bulk.

## What was in there

Measured across every searchable store, then purged with `scripts/purge_unindexable.py`. The
dry-run total and the applied total agreed to the chunk.

| bucket | files | chunks | % | mechanism |
|---|---:|---:|---:|---|
| `.svg` + `.drawio` | 9,173 | 21,059 | 4.96 % | new `is_image_path` |
| generated-XML test tooling, three directories in one project | 4,883 | 19,047 | 4.49 % | per-project `.rse-index.yaml` |
| `csv` | 107 | 12,612 | 2.97 % | `_DATA_LANGS` |
| lockfiles, minified bundles, sourcemaps | 70 | 2,738 | 0.64 % | `_GENERATED_SUFFIXES` |
| `po` | 9 | 1,510 | 0.36 % | `_DATA_LANGS` |
| keys and credential stores | 5 | 12 | — | `is_secret_path` |
| **total** | **14,187** | **56,978** | **13.42 %** | |

## The two that were not about mass

**A live PEM private key was indexed** — a `privkey.pem` whose first line is `-----BEGIN` —
together with three `htpasswd` files. Five files, twelve chunks. The mass is not the argument. A
chunk in the store is a chunk `search` returns and a chunk the dashboard pastes into a `claude -p`
prompt, which is the same exposure argument the dotenv exclusion was written for a day earlier.

`is_secret_path` therefore grows `.pem .key .p12 .pfx .jks .keystore` and `htpasswd`. `.pem` and
`htpasswd` are the measured instances — that docstring's standing evidence bar — and the rest are
formats *defined* to carry key material, so a file with one of those names has nothing else it
could be. `.crt`, `.cer` and `.pub` are deliberately absent: those are the public halves,
published on purpose, and dropping them would be exclusion by association with the word
"certificate".

**`is_code_language("csv")` was `True`.** The grammar pack ships csv and po, so both answered yes
to "is this code" and took the 500 kB *code* size cap instead of the 100 kB data cap — and, worse,
fed `_code_source_fingerprint`. A data export was waking the graph re-derive. HR38 exists to stop
exactly that.

The fix is one word in `_DATA_LANGS`, and it is a **classification** change rather than an
exclusion. At the data cap 96 % of the csv mass and 100 % of the po mass falls out while a
forty-line hand-written test fixture stays indexed — a legitimately searchable thing that an
extension rule would have lost. 580 chunks across 59 small files survived, as intended.

## Text-encoded images: the rule that could not see them

SVG and `.drawio` are the largest bucket after HTML, and they reached the index because the rule
that drops every *other* image cannot see them. `_has_text_bytes` screens for a NUL byte — git's
own binary test — and SVG really is text, so it passed, a grammar parsed it, and it was chunked
and embedded like source. Its content is path data, transform matrices and base64 blobs.

`is_image_path` matches on the *whole* suffix, and that turned out to matter immediately: a
post-purge sweep written with a substring test flagged eight surviving `.drawio.md` files, which
are Markdown design documents that merely have `.drawio` in the stem. The predicate was right and
the sweep was wrong. A substring rule would have deleted them.

## Provenance from names only

`_GENERATED_SUFFIXES` grows `.lock`, `.min.js`/`.min.css`, `.js.map`/`.css.map`, plus `go.sum` and
`package-lock.json` matched **whole** in a new `_GENERATED_NAMES` — `endswith` on a bare `sum` or
`json` would take real source with it. Each of these has its input versioned beside it, so
indexing it stores the same information twice.

Bare `.map` was rejected as wider than the evidence: every sourcemap found was one of the two
compound forms. And provenance is read from the *name* only, never from line geometry — the
inference that a machine-shaped file can be recognised by its line lengths deleted 54.4 % of the
graph three days earlier.

## The extension bootstrap is not a language gate

`test_no_new_hardcoded_lang_or_ext_allowlist_in_core` fails on any new module-level
`frozenset({".x"…})` in this file. All four new lists are **tuples**, and that is a decision
recorded in the guard's own docstring rather than a container-type accident.

`discover.py` is the extension bootstrap P6 names as exempt: the point where bytes first get a
category, upstream of any language question. What the guard protects is the other thing — that
once a file *has* a language, nothing gates on a hand-written list of which languages count as
code. A frozenset of extensions still fails there, as it always did.

## Knock-on, stated because it moves a published number

116 files leave H1's coverage denominator through `graph/store.py`. That ratio changes without the
graph having changed. More honest, and worth saying out loud rather than discovering later as a
regression.

## The knock-on nobody predicted: changing what counts as code re-derives the whole fleet

`_code_source_fingerprint` (HR38) is a pure function of the *code* file set. Reclassifying csv and
po therefore changes the fingerprint of **every project that contains one**, and the purge moved
file sets besides — so the daemon woke into a fleet-wide graph re-derive, one project every 20-30 s,
pegged at its full 1-core quota (`percent_core 0.9995`, 2,174 throttled periods) for roughly an hour.

Nothing is wrong. The pool's own counters say so: `parse_timeout_count 0`, `parse_crash_count 0` —
every parse completed, and the single long-lived worker looked stuck only because the pool is
*persistent* by design and was running back-to-back tasks. The work is also largely redundant in
content: csv and po almost never yield symbols, so most of those graphs re-derived to what they
already were. Special-casing the fingerprint to avoid it would be worse than paying it — a
fingerprint that is not a pure function of its inputs is the failure HR38 names.

Two things follow, and they are the transferable part:

- **Price the re-derive into any future edit of `_DATA_LANGS` or `is_code_language`.** The one-word
  change is one word; its cost is a fleet-wide re-extraction. Schedule it accordingly.
- **P16's idle gate cannot be measured while it runs.** CB3 read 0.9967 of a core and asserted, and
  it was *right* to: sweeps were paused, but pausing prevents new passes rather than stopping one in
  flight, and CB3's contamination probe reads `/api/watcher` only — so sweeps-driven work is
  invisible to it. That is a real gap in the probe, distinct from the watcher-side one. Re-take the
  window after the wave drains; do not touch the threshold.

## Per-project exclusion beats an extension rule

The generated-XML bucket is one project's test tooling, which keeps three trees of machine-written
XML — run configuration, case stubs, and one file per UI element — while the real logic lives in a
fourth directory as ordinary source. That fourth directory stays indexed; the three go out via a
project-local `.rse-index.yaml`.

Each directory is listed **twice**, bare and with `/*`: `is_excluded` fnmatches the root-relative
path *and* the bare name, so the bare form prunes the directory during the walk and the `/*` form
drops any file that reaches the per-file check. `fnmatch`'s `*` crosses `/`, so one `/*` covers
the subtree.

The tempting alternative was an extension rule. It would have been silently wrong: the
element-record files use `.rs`, and a rule keyed on that would drop every Rust file the fleet ever
indexes.

**The transferable lesson:** three of these five were not bulk problems. A file being large or
numerous is what makes you look; what justifies the change is that the content cannot answer the
question the store exists to answer — or, twice here, that it should never have been in a
searchable store at all.
