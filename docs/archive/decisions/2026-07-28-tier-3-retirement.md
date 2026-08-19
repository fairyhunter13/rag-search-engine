# What left with tier 3, recorded so it is not re-derived

**2026-07-28** · P6/HR15 · guard: `src/tests/live/test_no_code_semantic_regex.py`,
`docs/world-model/model.yaml` P6

Tier 3 was the generative-LLM lane: cloud DeepSeek narration over the knowledge base. Deleting it
took `kb/bpre*.py`, `kb/patterns.py`, `graph/enrich.py`, `graph/llm.py`, `kb/bpre_ast.py`,
`valueflow.py` and the docgen vendor submodule with it. `kb/` now holds only `answer_cache.py`
(deterministic caching, no LLM — it was always tier 2).

## The doctrine that survived, and got stronger

**No regex, no static/dynamic keyword list, no mapping table for code-semantic inference — only
tree-sitter structure.** There is no longer an "and, for residual ambiguity, a capped/cached/
batched DeepSeek call" clause: the whole write path is deterministic, which is invariant #9 rather
than a gate. The doctrine no longer has an LLM escape hatch to fall through to.

Category A (`kb/bpre*.py`, `kb/patterns.py`, `server/_overview.py`) **is empty** now that `kb/` is
gone, so the guard is one scan over `src/rag_search/` with a four-module Category-B allowlist
(`graph.extractor`, `index.discover`, `core.registry`, `core.config` —
`test_no_code_semantic_regex.py:50`); node-kind maps and infra/config ground-truth remain exempt.
The guard is **wider, not narrower**: the whole package is now checked instead of seven files.

## Retired sub-doctrines

The header on this section read "P6, HR15–HR19, HR23" until 2026-07-28. HR16 (resolution ladder),
HR17 (Tier-1.5 value-flow), HR18 (Tier-1.75/2 rerank + token economy) and HR19 (deterministic LLM
gating) were all tier-3 machinery and retired with it. HR23's `llm_token_stats` accounting retired
with the calls it accounted for.

## Recorded so it is not re-derived

The universal structural HTTP classifier (`bpre_generic.py`/`bpre_paradigms.py`, URL-anchor +
handler-shape + `_V` verbs + gRPC binding + `_SCHEMES` provenance), its import/type-provenance
extension over `valueflow.py`/`bpre_ast.py`, and the DeepSeek escalate/whole-file residue tiers
those fell through to.

**The transferable half is the doctrine above — prefer retiring a per-language table over feeding
it**, which is how `bpre_spec._LANG_SPECS` died on 2026-07-01 and why the debt registry was
already empty when tier 3 left.

## Embedded-`<script>` sub-parsing (F2, 2026-07-09) — survives

Vue/Svelte/Astro/HTML host grammars parse `<script>` content as one opaque `raw_text` leaf —
structurally blind to embedded JS/TS calls and symbols. `graph/extractor.py::_iter_script_blocks`
(sole remaining implementation since `kb/bpre_ast.py::_script_blocks` left with tier 3) locates
that leaf plus its `lang` attribute (node-kind/attribute reads, no vocabulary) and sub-parses it
with the js/ts grammar, remapping line numbers by the block's start row — covering the symbol/call
graph for Vue and Svelte SFCs.

**Measurably load-bearing**: `.vue` is 92 % covered fleet-wide (2,020 of 2,190 files, 16,477
symbols). Guarded by `test_embedded_script_extraction.py`.

## Env-var fallout

`RSE_DEEPSEEK_MODEL` and `DEEPSEEK_API_KEY` were deleted and have **no reader anywhere in `src/`**
— that absence is asserted, not assumed (`test_deepseek_api_key_has_no_reader`,
`test_inference_lanes.py`). There is no LLM lane left to switch on or off: `claude -p` on
`POST /api/chat_stream` is the whole of it, and no MCP tool reaches it.

The `deepseek_*` tokens stay in the P2 check after the deletion: they now match nothing, which is
the point — that is the reintroduction guard, not a description of a live call site.
