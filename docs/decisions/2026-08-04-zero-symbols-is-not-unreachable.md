# Zero symbols is not the same as unreachable

**2026-08-04.** CSS and SQL extraction was the one coverage gap an all-languages sweep found:
1,906 css/scss/less files and 274 sql files that parse cleanly and produce **0 symbols**. It is
**declined on measurement**, not on the absence of evidence it was previously resting on.

## The question the "0 symbols" framing hid

Every draft described these files as dark, and "dark" quietly implies *unreachable*. But symbols are
not how a file becomes retrievable here — chunks are. CSS and SQL files are chunked and embedded and
indexed in FTS5 like anything else; extracting a `rule_set` selector or a SQL `statement` as a
*symbol* changes the graph, not the corpus.

So the question is not "do these files have symbols" (they don't) but **"if the names a symbol arm
would emit are used as queries, does the file containing them come back?"** If yes, the symbol adds
nothing to reachability and the whole item rests on a ranking claim nobody has evidence for.

## The measurement

Names pulled with the same tree-sitter grammars R7 would use — the first `rule_set` selector line
for css/scss/less, the first `statement` head for sql — then issued as a query against the store the
file lives in, `scope="all"`, top-10. 60 files per store, seeded sample. No labels, no new
extraction code, nothing written.

| store | files sampled | rank 1 | in top-10 | a file of this kind in the top-10 |
|---|---:|---:|---:|---:|
| a css/scss/less-heavy store | 60 | 29 (48%) | **53 (88%)** | 60 / 60 |
| a sql-heavy store | 60 | 51 (85%) | **60 (100%)** | 60 / 60 |

Per language: css 25/28 in top-10, scss 23/26, less 5/6, **sql 60/60**.

**These files are already retrievable by exactly the names the symbol arm would index.** For SQL it
is perfect and for CSS it is 88%, against a corpus where they hold zero symbols. The premise that
extraction would make them findable is false; they are findable now.

## What this does and does not settle

It is a deliberately **best-case** arm: the selector or table name is verbatim in the file's own
text, so this is the leakage `eval_retrieval.py --queries-from commit` exists to avoid. That makes it
decisive in one direction only — and that is the direction it landed. A file already returned at
rank 1 by its own identifier cannot be made more reachable by indexing that identifier again.

It does **not** measure whether these symbols would improve ranking under natural-language queries.
That claim remains untested, and it is also the claim for which no published evidence exists in
either direction, and for which GitHub, Sourcegraph and Zoekt all point the other way by indexing
config and markup as plain text. An unevidenced ranking gain, on 2.8% of files that are already
retrievable, is not worth an extraction arm and a stamp bump.

## The rule

**"Zero symbols" is a statement about the graph, not about retrieval.** Before proposing extraction
to make a population findable, check whether it is already found. Two 60-file samples and no new
code settled an item that had survived three drafts on the strength of a number that was measuring
something else.
