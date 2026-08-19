---
type: Decision
resource: src/coderag/chunk.py, pyproject.toml
title: One chunker ships, it is third-party, and the part we wrote is the header
description: Six splitter libraries and four boundary strategies were compared on August 2026 evidence; semantic-text-splitter wins on zero dependencies and offsets, and the measured gain is in the scope header, not the boundary.
tags: [chunking, dependencies, retrieval, evidence]
status: stable
generated: { by: claude/opus-5, at: 2026-08-19T09:40:00Z }
---

# Decision

`chunk.py` wraps `semantic-text-splitter` 0.32 and prepends a scope header. There is **one**
splitter and there will not be a second, because every chunker in the index path costs a full
re-index to compare, doubles the store, and multiplies the eval matrix by its own arity — over the
axis the evidence rates lowest.

This question was asked four times during the rebuild, in four different phrasings ("do we need
every chunker from every library", "what about a semantic chunker", "what about tree-sitter",
"what about cAST"). It will be asked again, which is why the answer is written down rather than
left in the diff.

# What the evidence actually says

- **Structure-aware chunking does not beat a sliding window on code.** [864 controlled
  configurations](https://arxiv.org/abs/2605.04763) over RepoEval and CrossCodeEval: Sliding Window
  leads every retriever it was paired with (27.60 / 28.23 / 29.15 / 28.60 EM) against cAST
  (26.84 / 28.42 / 28.73 / 28.79) and function-level chunking (23.40 / 23.98 / 25.10 / 24.35). All
  six pairwise differences significant at *p* < 0.05; function-vs-anything gives Cliff's δ = −1.0,
  a 3.57–5.64 pp loss. The same paper reports that **chunk size itself has "a weaker, non-monotonic
  effect"** and names no default — so 2,000 is a round number in a flat region, not an optimum.
  [*Practical Code RAG at Scale*](https://arxiv.org/pdf/2510.20609) agrees from the other side:
  line-based chunking matched or slightly exceeded LangChain's own syntax-aware splitter.
- **An embedding-breakpoint semantic chunker is the wrong tool twice.** It loses head-to-head on
  prose (69% for recursive 512-token against 54%, emitting 43-token fragments), and on code its
  *input* is broken: it measures topic drift between sentences produced by splitting on `.` and
  newlines, and `.` is a member-access operator — `pkg.Method` is two sentences before a single
  embedding is computed. It also costs a second GPU pass at ~14× (0.33 MB/s against 4.82), which
  across 61,714 files is the difference between one overnight index and several days.
- **The one contradiction resolves.** [cAST's own paper](https://arxiv.org/html/2506.15655v1)
  reports +1.8 to +4.3 Recall@5 **over naive line-based chunking**, plus +2.67 SWE-bench Pass@1.
  That baseline has no boundary awareness at all; the 864-config study built a better window and
  the gap closed. cAST's delta is mostly the distance between naive and boundary-respecting, which
  is the distance a library with a boundary hierarchy already closes — and stating the baseline is
  what makes the tie legible, rather than citing only the study that produced it.

# Why this library and not the other five

Verified on PyPI in August 2026 — required dependencies and offset support, not stars:

| Library | Required deps | Offsets | Verdict |
|---|---|---|---|
| **`semantic-text-splitter` 0.32.0** | **none** | `chunk_indices()` | **Ships.** Rust, MIT, `from_callback` for a custom size unit |
| `semchunk` 4.1.1 | tqdm | yes | Close second, but token-oriented where this design counts non-whitespace characters |
| `langchain-text-splitters` 1.1.2 | `langchain-core` | yes | Rejected on the dependency. Its recursive splitter is also the exact baseline the second paper matched |
| `chonkie` 1.7.0 | 6, plus tree-sitter | yes | Rejected: `CodeChunker` is AST, flagged experimental, and warns chunks "may significantly exceed specified size limits" — fatal against a 768-token budget |
| `code-splitter`, `chunkipy`, `llama-index-core` | tree-sitter / 28 / 29 | — | Rejected: a grammar per language, or a framework to get a function |

Three API facts made it usable rather than merely small: `from_callback` lets the size unit be
**non-whitespace characters** (the unit cAST defines; the 864-config study explores 2,000 without
endorsing it — see the correction below);
`chunk_indices()` returns each chunk **with its offset**, so `start_line` is a prefix newline count
rather than a re-derivation — and a location the engine had to guess at fails identically to one it
computed; and `overlap`/`trim` are constructor arguments, so measuring overlap stays a one-line arm.

# What we kept, and what we gave up

Kept: the **scope header** — prepended to the embedded and FTS text and never to the stored body.

**The justification given here was wrong and is corrected.** It said the header "is the mechanism
the winning AST chunker actually wins with … cAST has the same parser, lacks the header, and
loses." cAST prepends no header at all. Its gain is from AST boundaries, and its own limitations
section says it works "without explicit contextual awareness" — the authors name the header as
future work, not as their result.

What the evidence does say: every *measured* gain for prepending context to a chunk before
embedding comes either from an LLM-generated blurb (Anthropic's contextual retrieval, 35% fewer
top-20 failures, at one model call per chunk) or from context trained into the embedder
(late chunking, contextual embedding models). **No published result isolates a regex scope header.**
Adjacent work says a *wrong* header is worse than none, since a topically adjacent but incorrect
distractor is the hardest kind.

So the header is kept as a cheap unevidenced bet with a live kill switch: `CHUNK_HEADER=0` is a
bake-off arm, and whichever way it falls is what ships. It is also the single place per-type
knowledge enters this pipeline, which is why it dispatches on language while the splitter does not.

Given up: the library's ladder has no **dedent** rung, so a column-0 `def` is not preferred over an
arbitrary line break. Accepted rather than worked around — blank lines already separate top-level
declarations in this corpus, the evidence rates the boundary axis lowest, and re-adding a regex to
snap boundaries reintroduces the code the library was adopted to delete.

Carried risk with its escape hatch: the size callback is Python called from Rust during a binary
search, O(log n) times per chunk over 61,714 files. If it measures badly, fall back to plain
character capacity at ~2,600 and **record the substitution** — the unit becomes approximate, and
that has to be said rather than absorbed.
