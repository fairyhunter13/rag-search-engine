---
type: Decision
resource: src/coderag/chunk.py, src/coderag/config.py, src/coderag/store.py, tests/test_chunk_header_arms.py, tests/eval.py
title: The scope header is the path, and the derived line is deleted
description: Ablated on docs and on code; flat both times, and below no-header at all on its own. The census refuted the redundancy explanation, which makes the result harder rather than softer.
tags: [chunking, retrieval, evidence, deletion]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T13:40:00Z }
---

# The 2x2, complete

[the-header-dispatches-on-type-the-splitter-does-not](the-header-dispatches-on-type-the-splitter-does-not.md) built the derived line as four arms inside
`scope_header` and closed by naming its own falsification: *"If they lose or tie, the path-only prose
header and the one-splitter decision come back strengthened, having been tested on the corpus they
were never argued over."* They tied. This is that.

openclaw, dense lane, n = 300, `--corpus code`:

| arm | recall@1 | recall@10 | MRR |
|---|---|---|---|
| path ON, derived ON | 0.2700 | 0.6267 | 0.3782 |
| path ON, derived OFF | 0.2700 | **0.6400** | 0.3815 |
| path OFF, derived ON | 0.1267 | 0.4267 | 0.2102 |
| path OFF, derived OFF | 0.1467 | 0.4567 | 0.2362 |

Paired McNemar against the full header, discordant pairs as (full-only / other-only):

| contrast | recall@1 | recall@10 |
|---|---|---|
| path-only | 16/16, **p = 1.000** | 7/11, p = 0.481 |
| derived-only | 48/5, p < 0.001 | 66/6, p < 0.001 |
| no-header | 44/7, p < 0.001 | 56/5, p < 0.001 |

The path arm is the whole effect and it is large: **−0.1233 recall@1 and −0.1700 recall@10** when it
is removed. The derived arm is worth **+0.0000 with path on and −0.0200 with path off** — an exact
16/16 tie in the cell that decides, and mildly *negative* in the cell that does not. On docs it was
already bounded at +0.0067 with 4 discordant pairs. Two corpora, two nulls, both point estimates
pointing the wrong way for keeping it.

Deleted: `CHUNK_HEADER_DERIVED`, its stamp field, the `imports:` / `in: <decl>` / heading-chain /
key-path construction, the five regexes behind them, and the per-type dispatch that existed only to
choose between them. `chunk.py` went 248 → 137 lines. `scope_header` takes one argument now.

# It was not redundancy, and that matters

The obvious explanation — the header restates tokens already in the chunk body, so the embedder sees
nothing new — is wrong, and a census says so. Per chunk, the fraction of the derived line's tokens
already present in the body it labels:

| arm | chunks | no line emitted | redundancy of the rest |
|---|---|---|---|
| `code:imports` (openclaw) | 876 | 44 | **15.3%** |
| `code:in-decl` (openclaw) | 876 | 147 | 38.9% |
| `doc:heading-chain` | 723 | 216 | 55.0% |
| `data:key-path` | 6,880 | 35 | 52.4% |

The `imports:` line was **85% novel tokens** and bought nothing. So the embedder was handed genuinely
new text and did not use it — and in the path-off cell the same text made retrieval *worse* than
emitting no header at all. That is dilution, not redundancy: the tokens compete with the chunk's own
content for a fixed-length representation.

This is the more useful finding, because redundancy would have been a property of these particular
regexes and would have invited a smarter fourth attempt. Dilution is a property of prepending
structural metadata to a dense embedding, and it generalises. It also matches the one field ablation
in the literature: [arXiv:2601.11863](https://arxiv.org/abs/2601.11863) (ECIR 2026) finds identity
metadata carries the whole effect and *"removing section titles yields only a modest drop"* — path is
identity, the derived line was section-title-class, on a different corpus with a different embedder.

# The path arm, and the caveat that is not resolved

Kept, and the external corroboration is direct: +17.0 pp Top-10 on SWE-bench-Lite from prepending the
relative file path to each chunk, same mechanism, same direction
([arXiv:2605.17965](https://arxiv.org/abs/2605.17965), BLAgent). It reports 87.3% against 70.3%.

**But BLAgent's worked examples are query-document string identity and it presents them approvingly.**
Its two cases are a query naming `django.contrib.admin.utils` against a chunk headed
`django/contrib/admin/utils.py`, and a *stack trace* naming `mpl_toolkits/axes_grid1/axes_grid.py`.
The general form is a known evaluation bias in bug localization — Kochhar et al., ASE 2014 — but no
source connects it to path-in-chunk, and neither ContextBench nor CORE-Bench ablates or masks paths.

This repo already measured its own exposure and it is large: **48.6% of docs eval queries have their
heading echoed in the positive's filename** (`config.py`, `CHUNK_HEADER_PATH`). So part of the docs
path effect is a filename shortcut. What rescues the decision is that the *code* corpus has no such
echo — its queries are docstring-derived, not filename-derived — and the path arm is worth −0.1233
recall@1 there anyway. The docs magnitude is soft; the code magnitude stands.

Not done, and worth doing before the path arm is ever cited as a magnitude: re-score the docs path arm
on the low-slug-overlap stratum. Zero GPU, and it is the only open question the header has left.

# Not changed

`CHUNK_MD_SPLITTER` stays off and one splitter still ships — that arm ran and lost separately
(0.8033 against 0.8133, p = 0.51), and its reasoning is unaffected by any of this.

`CHUNK_ALGO` is bumped, because the header text changed and every store built before this reads a
different embedded string than it would today.

The eval keeps a `no-header` arm. The path arm is now the only component, so an ablation that cannot
turn it off is not an ablation, and this is the number the next person will want to re-check.
