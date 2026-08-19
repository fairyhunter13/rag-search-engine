# The prefix-free escape hatch is not there, and two of the three backlog items were mis-founded

**2026-08-01** · P5 · research only — no production code changed

Three items had been carried as the remaining backlog after
[the graph-export and eval-harness fixes](2026-08-01-an-exported-edge-must-name-an-exported-node.md):
a same-directory resolution tier, a `gte-modernbert-base` embedder arm, and a structural chunk
header. Researched against the tree and the fleet, **two should be struck and the third is now
answered negatively.** Nothing here changes a line of `src/`; the point is that three scheduled
GPU-hours of work turn out not to be owed.

## The arm: a prefix-free challenger that ties the incumbent

`2026-07-31-two-levers-additive-and-the-bill.md` left the switch to **nomic+prefixes @512** fully
evidenced (8 of 8 metrics, two projects) and unshipped, blocked not by evidence but by price: *"the
prefixes are the whole margin"*, so shipping it requires an asymmetric embed path across four call
sites, on top of a ~2.4 h fleet rebuild.

That makes exactly one question worth a new arm — **can a prefix-free model reach the prefixed
challenger?** If one could, the switch would lose its API change and become a pure rebuild. The
prefix-free arm already on the record, `bge-base-en-v1.5 @512`, answers *not quite*: it takes this
repo but ties jina on recall@1 on the larger project (0.2350) while nomic reaches 0.3000.
`gte-modernbert-base` is a much newer backbone and was the plausible next candidate.

This repo, 130 queries, dense lane, both arms built on the same day's corpus at `max_tokens=768`:

| arm | recall@1 | MRR | recall@10 | recall@50 |
|---|---:|---:|---:|---:|
| jina-v2-base-code — *incumbent, rebuilt control* | **0.5923** | **0.6920** | **0.8769** | **0.9615** |
| gte-modernbert-base, prefix-free | 0.5615 | 0.6561 | 0.8692 | 0.9462 |
| nomic+prefixes @768 — *recorded, for the bar* | 0.6615 | 0.7375 | 0.8769 | — |

McNemar's exact on the 40 discordant pairs: **22–18 to the incumbent, p = 0.636.** A tie — the
challenger is statistically indistinguishable from the model it was meant to displace, and sits
**−0.100 recall@1** from the prefixed challenger in the same token bracket. The escape hatch is not
there.

The control validates the comparison rather than being assumed: rebuilt on today's corpus it
reproduces the recorded incumbent's 0.5923 exactly, with the same `python 118, go 11, html 1` query
set. That is the rule from the previous round — *a control recorded against a different corpus is
not a control* — satisfied by re-running rather than by hoping.

**The larger project was not run, and that is a judgement, not an omission.** This repo is 91%
Python, which flatters a code-specialised incumbent, and the lineage's own finding is that the
incumbent *"loses by more, not less, once the questions stop being Python"*. So a tie here is a
mildly positive signal there. But the bar on that project is nomic's 0.3000 against jina's 0.1900,
and an arm that cannot beat jina here has ~0.11 to make up on a relative shift measured at +0.013 for
nomic. Not worth 585 s of GPU on that arithmetic. Stated so the next reader can overrule it cheaply.

### Two fastembed defects found on the way, recorded so nobody pays for them twice

1. **fastembed 0.8.0 ships no `gte-modernbert-base` embedder.** Its 30-model registry has
   `thenlper/gte-base` and `gte-large` only — and `gte-base` is the model already recorded as
   failing inside its tokenizer. The arm needs a real `TextEmbedding.add_custom_model` with an
   explicit `PoolingType.CLS` + `normalization=True` (the GTE line pools CLS; the jina incumbent
   pools MEAN), which `index/store.py::pooling_id` then reads back — get it wrong and every stored
   vector is quietly wrong rather than loudly broken.
2. **The tokenizer sentinel overflows.** `preprocessor_utils.load_tokenizer` computes
   `min(model_max_length, max_length)` and hands it to `enable_truncation`. This repo ships the HF
   sentinel `1000000000000000019884624838656` and no `max_length` at all, so the min *is* the
   sentinel and the load dies with `OverflowError: int too big to convert`. Supplying any finite
   `max_length` fixes it; the value does not matter, because `Embedder._init:113` re-truncates to
   `EMBED_MAX_TOKENS` immediately after load for its own reasons.

Unlike `gte-reranker-modernbert-base`, this model does **not** pin a fixed padding length, so
`_unpin_tokenizer_padding` has no work to do here. Same family, different tokenizer defect — which
is the third distinct one this family has produced.

## The same-directory tier was already declined

The backlog inherited it from a plan that ranked it at *"52.5% of the cap-1 drops"*. That figure is
superseded. `2026-07-31-an-edge-is-a-resolved-call.md` measured it at **+5,697 groups (+4.7%)** and
declined it — a day *after* the plan that proposed it — and not on cost:

> directory proximity is a guess. Under cap 1 a tier that narrows to the **wrong** single candidate
> emits a confidently wrong edge into a table whose entire new invariant is that it holds none.

A decision doc outranks a plan. **Struck.**

The trio it was bundled with is also separable, contra the plan's own argument that the three had to
ship as one stamp:

- The `confidence` column is not the precondition for a *tier*; `sweeps.py:141` makes it the
  precondition for **raising the cap** above 1. Tiers keep cap 1, so precision stays 1.0 by
  construction.
- Adding it alone would in fact be **banned**: there are zero consumers of `confidence` in `graph/`,
  `server/` or `daemon/`, and SC6 forbids a write-only `symbols` column — a rule
  `extractor.py:145-147` extends to the dataclass. It can only ship with the cap raise that reads it.
- An **import** tier is the one defensible rung, since an import is a declared fact recoverable from
  tree-sitter structure rather than a proximity guess. It needs import extraction, which does not
  exist (the import-node grep over `graph/extractor.py` returns 0).

**But the import tier is not the prize.** The same doc names what owns the loss: recall is 0.633 and
*"that 36.7% is not recoverable at the resolution layer — `extract_calls_with_lines` returns bare
`(name, line)` with no receiver"*. Verified at `extractor.py:963`, whose signature is exactly
`-> list[tuple[str, int]]`. Call-site qualification costs the same stamp and the same re-derive as
the import tier and addresses 36.7% rather than 4.7%. That is the successor item.

## The structural chunk header was a misreading

The item claimed `chunker.py:131-134` promises a structural path that `:142` fails to emit. It does
not. The docstring's stated purpose is *"so the embedder knows the **repo context**"*, and
`# {rel}\n` delivers exactly that; `test_cast_chunking.py` is titled "cAST structural-path header
tests" and CC1 asserts the header is literally `# src/main.py\n`, with CC2 pinning the same string on
the fallback path. Module, tests and docstring agree that *structural path* means the repo-relative
file path.

Adding symbol nesting would be a new feature, not a repair — and its bill is not "a re-chunk of
whatever it touches": changing chunk text invalidates every stored vector, which is the same
multi-hour fleet rebuild as a model switch. **Struck as specified.** If it is ever wanted it rides
that rebuild, and it is measured as an eval arm first like everything else here.

## Transferable

- **A backlog item inherits the evidence of the plan that wrote it, and plans go stale faster than
  code.** Two of these three were refuted by a document written the day after they were scheduled,
  and one by reading the test that pins the behaviour it claimed was missing.
- **Ask what a new arm could change, not whether it might win.** The model question was already
  closed; the only live question was whether a prefix-free model could delete an API change from a
  costed switch. That framing is what made a single negative result sufficient to close the item.
- **A tie is a result.** p = 0.636 on 40 discordant pairs is not "inconclusive, run more" — it is a
  challenger that would have had to win by 0.10 to matter, missing by that much in the wrong
  direction.

## Status

No production change. The arm stores were deleted after their metrics were read back. The registration
and the tokenizer cap stayed in a scratch script and deliberately did **not** land in
`embed/embedder.py`: a model that ties the incumbent does not get a vetted-custom-embedder entry, for
the same reason `_CUSTOM_RERANKERS` is an explicit table rather than a passthrough.
