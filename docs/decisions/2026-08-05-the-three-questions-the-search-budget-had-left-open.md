# The three questions the search budget had left open

**2026-08-04.** Three questions were recorded as explicitly unanswered because a session's WebSearch
budget ran out: whether a new code-indexing tool launched June–August 2026, what the current AI
coding tools actually use for codebase understanding, and whether any 2026 paper finally ablates a
code graph against a strong retrieval baseline. All three are now answered, against primary sources
(arXiv's API, the GitHub API, vendor documentation) rather than recall.

**Method note, because it matters more than any single answer:** the search budget was never the
binding constraint. `WebFetch` against a search engine's HTML endpoint, and against `export.arxiv.org`
and `api.github.com` directly, does the same job — and for dates and adoption numbers it does it
better, because it reads the registry rather than a blog about the registry. The rule this repo
already had ("prefer the package registry for dates") generalises: **query the index, not the
commentary.**

## 1. Did a new code-indexing tool launch June–August 2026?

One, with real adoption. Searching the GitHub API for repositories created since 2026-06-01 in this
space returns exactly one above 10 stars — the rest of the field is 1–7 stars, hobby scale.

**CodeSeek** (MIT, Rust, created 2026-06-03, ~818 stars, last push 2026-08-02). Its own description:

> *"Builds call graphs and hybrid semantic search indexes (Dense + Sparse + RRF + Reranker) across
> 7 languages. Ships as native MCP tools."*

**That is this engine's architecture, arrived at independently.** Tree-sitter call graph, dense plus
sparse fused by RRF, a reranker on top, exposed over MCP. It is not an adoption candidate — there is
nothing in it this repo does not already have, and it covers 7 languages against 41 here — but it is
the strongest external evidence yet that the shape is right. Convergence by two teams who did not
talk to each other is worth more than either team's own benchmark.

Also newly checked: a second `CodeGraph` (created 2026-04-04, ~49 stars, 42 MCP tools, 38 languages).
Small; noted for completeness. The 64k-star `codegraph` already surveyed was created 2026-01-18 and
remains outside the June–August window.

## 2. What do the current AI coding tools use?

Read from vendor sources where they exist. The pattern is consistent and it is not what a code-graph
advocate would predict:

| tool | what its own documentation states | a maintained code graph? |
|---|---|---|
| Cursor | Merkle-tree change detection, files split into **syntactic chunks**, chunks embedded, embeddings cached by content hash. *"These chunks are converted into the embeddings that enable semantic search."* | **Not mentioned anywhere** |
| Amp | Shell and tool-driven agentic search; *"runs tools and shell commands on your behalf to inspect code."* No embedding index disclosed. | **No** — and its vendor *owns* SCIP |
| Augment | A semantic index — *"not just grep… a full search engine for your code."* | Not disclosed |
| Windsurf | RAG plus an agentic loop making many tool calls per prompt. | Not disclosed |
| Claude Code | No pre-indexing; the agent is given search tools and decides what to explore. | **No** |

**Nobody in this list advertises a maintained call graph, and the one vendor that owns a resolved-
index format does not use it in its agent.** The industry converged on embeddings plus agentic
search. That does not make a graph wrong — this repo's graph is measured and cheap — but it does mean
**no external pressure justifies expanding it**, and it is consistent with everything else this plan
found: graph structure keeps failing to demonstrate a retrieval win that a strong lexical or hybrid
baseline does not already have.

## 3. Does any 2026 paper ablate a code graph against a strong hybrid + reranker baseline?

**Still no** — the gap this lineage kept hitting is real and remains open at August 2026. Fifty 2026
arXiv papers on repository-level retrieval were enumerated through the arXiv API; the graph papers
benchmark against lexical search, agentic baselines, or classical IR, never against dense+sparse
fusion with a cross-encoder on top.

**But the surrounding literature moved, and three results bear directly on decisions taken here.**

**a. The lexical-versus-graph comparison now exists, and lexical wins.** *Better Call Grep*
(arXiv:2601.23254) asks this plan's question from the other end: how far does index-free lexical
retrieval go before anything more complex is needed? Its finding is that **a baseline where the LLM
just writes `ripgrep` commands performs comparably to sophisticated graph-based baselines.** Then it
adds two things and beats the state of the art by 7.04–15.58% relative exact-match:
**identifier-weighted re-ranking and structure-aware deduplication.**

**Those two additions are a reranker and R5.** This is the first published support for
deduplication in code retrieval, and it arrived independently of the local measurement that shipped
`_diversify` the same week. The earlier claim in this repo — *"there is no published evidence for MMR
or per-file caps in code retrieval at all"* — was true when written and is now **out of date in R5's
favour**. It was still right to ship on the local number.

**b. The static global graph is being argued against on maintenance cost.** *DyRetriever*
(arXiv:2608.01927, submitted the day before this was written) criticises graph retrieval that
*"relies on manually designed rules and static global graphs, leading to limited flexibility and high
construction and maintenance costs,"* and builds partial dependency graphs on demand instead —
reporting **7.4× faster than static-dependency-graph baselines**. A static global graph, incrementally
maintained, is exactly what this repo has. The cost objection does not obviously transfer (this graph
is maintained by a sweep nobody waits on, not built per query), but it is the sharpest published
argument against the architecture and should be answered on measurement if it is ever raised here.

**c. Two cautions worth carrying.** *CoREB* (arXiv:2605.04615) benchmarks eleven embedding models and
five rerankers with graded relevance on contamination-limited data, and finds that **short keyword
queries — the format closest to real developer search — collapse every model to near-zero nDCG@10**,
and that **off-the-shelf rerankers are task-asymmetric, with no baseline net-positive across all
tasks.** The first is independent support for the move to commit-derived queries: query population
dominates. The second says a reranker is not free lift by default — this repo's is justified by its
own measured top-1 change rate, which is the right kind of evidence to have.

**d. One positive graph result, honestly reported.** *LARGER* (arXiv:2605.16352) anchors graph
expansion on lexical matches and reports +13.9 Acc@5 on LocBench over its strongest baseline. Its
baseline is agentic lexical search, not hybrid+reranker, so it does not close the gap — but it is the
best evidence to date that graph structure *does* pay when it starts from lexical hits rather than
replacing them. That is close to how this engine already works, and it is the one paper here worth
re-reading before any future graph-in-retrieval work.

## What changes as a result

Nothing ships from this. No item re-opens: the residual is still untyped, CSS/SQL are still already
reachable, and per-language engines are still immature. What changes is the standing of two claims —
deduplication now has published support, and "no 2026 paper ablates a code graph against a strong
baseline" is now a verified statement rather than an assumed one.

**And the method rule generalises past this session:** an exhausted search budget was treated for a
whole session as a hard stop on three questions. It was not one. The registries were reachable the
entire time, and they gave better answers than a search engine would have.
