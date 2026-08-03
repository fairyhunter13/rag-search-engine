# The embedding track: nomic + task prefixes shipped, the breadcrumb declined

**Date:** 2026-08-03 · **Stamp:** `EMBED_PREFIX_REV` `noprefix-1` → `nomic-task-1`, `CHUNKER_REV`
unchanged at `cast-1` · **Items:** #5 (embedder switch) and #7's chunker half (structural breadcrumb)

Both re-embed every store, so they were sized as one rebuild window. One shipped and one did not,
and the one that did not was declined on a measurement that took building an honest instrument
first — because the harness this repo A/Bs retrieval with **structurally cannot see a breadcrumb.**

## What shipped: `nomic-embed-text-v1.5` + task prefixes at 512 tokens

Four call sites, one new keyword argument. `Embedder.embed` takes `side="document" | "query"` and
prepends `EMBED_DOC_PREFIX` / `EMBED_QUERY_PREFIX`; `index/indexer.py` passes `document` at both
batch drains, `query/search.py` passes `query` at both embed-once sites. An unknown side raises
rather than quietly embedding a query into the document space.

Three details that are decisions rather than mechanics:

- **The prefixes are two constants, not a model→prefix table.** nomic's card asks for these two
  exact strings and the model scores below its published numbers without them; FastEmbed supplies
  neither (`query_embed`/`passage_embed` fall through to `embed` unchanged for every text model), so
  the pipeline has to. A table keyed by model name is the shape HR15 bans and is also the shape of
  the 15 dead env knobs deleted this week. These are the prefixes *this* configuration uses; an
  `EMBED_MODEL` override owns its own consequences.
- **The chunker budget deliberately does not reserve room for the prefix.** The measured arm
  prefixed *after* chunking and let the embedder truncate at 512, so the shipped code reproduces
  that exactly. Reserving the ~4 tokens would have been tidier and would have made the shipped
  pipeline something other than the one the numbers came from. `CHUNKER_REV` stays `cast-1`.
- **`Embedder.dim` had a latent break the switch triggered.** fastembed describes a built-in model
  with a `DenseModelDescription` dataclass and a custom-registered one with a plain dict; the code
  read only `meta.get("dim", 768)`, so it raised `AttributeError` the moment the configured model
  became a built-in. Which shape comes back is a property of how the model got into the registry,
  not of the model.

**The era clause is deleted with it.** `_compose_signature` used to suppress the pooling and prefix
fields while they matched what the fleet was already stamped with — those two fields arrived
*describing what the existing runs already did*, and emitting them would have re-embedded 160
indexes into identical vectors. Model and token budget have now both moved, so every store is stale
on the first four fields anyway and the clause protects nothing. A suppression rule that no longer
suppresses is a branch nobody will be thinking about the next time pooling changes. ES3 and ES6 are
the same two claims read the other way round: they asserted the legacy stamp reads *current*, and
now assert it reads *stale*, which is what makes the fleet migrate rather than answer prefixed
queries out of the vector space it was built in.

## What did not: the structural breadcrumb

`index/chunker.py` prepends `# {rel}\n` to each chunk. #7's chunker half was to extend that header
with the enclosing container — the content e10's receiver arm had just made available.

### The instrument had to be built before the arms could be read

`eval_retrieval.build_query_set` composes every query as `f"{qname} {sig}"` — the qualified name
verbatim, plus the definition's first line. **Any header derived from the qualified name therefore
writes part of the query into its own gold chunk.** This is not avoided by using the container only
(`qualified_name.rpartition(".")[0]`): the container is a substring of the qualified name, so it
leaks too. A breadcrumb arm measured on the stock harness would report a win it earned by copying
the query into the answer.

The leak-free form is **signature-only** (`q = sig`): the container appears in neither the query nor
anything derived from it. This is the third time in two days that a retrieval change owed a probe
whose query population is the shape the change is for — the same finding as the tokenizer and the
path-field records, arrived at from a third direction.

### The corpus was chosen for container richness, not convenience

This repo's query set is 93% python with almost no containers, so it cannot detect the change at
all. The fleet was ranked by the share of symbols carrying a container and the strongest store
picked: a 727-file java store, 6,162 symbols, **79.4%** with `qualified_name != name`. In the
breadcrumb arm **73.4% of chunks (4,445 of 6,056) carried a crumb**, so the instrument was not
blind to it.

### The result, 300 queries, both forms

| query form | arm | recall@1 | MRR | recall@10 | McNemar |
|---|---|---:|---:|---:|---|
| **signature-only (honest)** | baseline | **0.4867** | 0.5542 | 0.7233 | A wins 7-3, **p = 0.34** |
| | breadcrumb | 0.4733 | 0.5473 | 0.7200 | 10 discordant |
| qualified-name (leaky) | baseline | 0.5433 | — | — | B wins 6-3, **p = 0.51** |
| | breadcrumb | 0.5533 | — | — | 9 discordant |

Null on the honest instrument, and **null on the leaky one too** — the form that was built to
flatter the arm did not flatter it either. Reverted; `chunker.py` is byte-identical to before.

The external citation behind the item (BLAgent, 87.3% vs 70.3% Top-10) is not contradicted so much
as not transferred: it is a different retriever on a different corpus, and this is the fourth
imported headline this quarter (#12's PageRank, #8's RepoGraph EM, #11's "41% JSON", #2's 17,701)
that had to be measured locally before it meant anything about this fleet. Three of the four did not
survive.

## What to re-open this on

Not another corpus — this one was chosen as the fleet's best case for the change. Re-open it only if
the *retriever* changes such that a chunk header is read differently: the dense lane embeds the
header as part of the chunk text, so a breadcrumb competes for the same 512 tokens the code needs.
A lexical-side or field-weighted use of the container (BM25F's symbol field, now unblocked by e10)
is a different mechanism and owes its own measurement, not this one's.
