---
type: Constraint
resource: src/rag_search/
title: No generative model runs inside the package
description: HR9, HR10, HR12 and HR31 are one lane map — the GPU does embed and rerank only, `claude -p` serves dashboard chat only, and nothing in `src/rag_search/` opens a completion endpoint.
tags: [llm, lanes, mcp, chat, hr9, hr10, hr12, hr31]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# No generative model runs inside the package

The map is two lanes and one crossing rule.

| Lane | What it runs | Where it may be reached from |
|---|---|---|
| GPU (FastEmbed / ONNX / CUDA) | embeddings + cross-encoder rerank, nothing else | anywhere |
| `claude -p` (`claude-haiku-4-5`, headless) | dashboard chat | `server/routes_chat.py` only |

Everything else follows from those two rows.

## Why the MCP surface generates nothing (HR9)

`search`, `graph` and `overview` perform embedding and reranking and return **ranked locations**,
not prose. Generation is the calling agent's job — this is the June-2026 MCP guidance, and it is
also the cheaper contract: an agent that already holds a model does not want a second one's
paraphrase of the file it is about to read.

The repo learned this concretely. `ask` was a fifth MCP tool that returned assembled prose; it was
retired 2026-07-29 and the reasoning is
[the fifth tool returned assembled prose](../../docs/decisions/2026-07-29-the-fifth-tool-returned-assembled-prose.md).
`compose_answer` survives but is reachable only from the CLI and the dashboard, which is what
`test_run_ask_is_llm_free` pins.

## Why chat has no fallback (HR10)

`POST /api/chat_stream` uses `claude -p --model claude-haiku-4-5` and nothing else. When the CLI is
unavailable it emits an SSE `{"type":"error"}` followed by `{"type":"done"}` — a visible failure,
not a quieter model. There is nothing left to fall back *to*: the 2026-07-28 tier-3 purge took
DeepSeek out of the repo entirely, so the clause that used to read "DeepSeek is
KB-enrichment-exclusive" now has no subject. Codex support was removed in the same sweep.

Account selection goes through `core/claude_profiles.py`, which is **the one module allowed to open
a URL** — a usage read, not a completion call, and DK2 asserts that distinction rather than assuming
it.

## Why the guard is a tree scan and not a crossing test (HR12, HR31)

A test that tries to cross the lanes proves only that one crossing is blocked. DK2 in
`test_inference_lanes.py` instead scans the whole tree for completion endpoints, holds an allowlist
of exactly one URL-opening module, and scans for `DEEPSEEK` readers. That shape closes call sites
nobody has written yet — the same reasoning as
[every parse is bounded out of process](every-parse-is-bounded-out-of-process.md), whose guard bans
a call pattern rather than checking known callers.

One consequence worth stating because it looks like a misconfiguration: **a box with no LLM provider
key is the normal configuration.** `RSE_QUERY_LLM_MODEL` is the only LLM environment variable left;
`_PROVIDER`, `_NUM_CTX` and `_TIMEOUT` were parsed, did nothing, and were deleted 2026-07-31, with
SC9 in `test_schema_consistency.py` now blocking a re-add.

## Sources

Rows HR9, HR10, HR12 and HR31 in
[§13b](../../docs/architecture/federation-ops-and-invariants.md).

| Row | Guard | File |
|---|---|---|
| HR9 | `test_e5_mcp_query_path_no_generation` | `test_server.py` |
| HR10 | `test_e6_dashboard_chat_haiku_only`, `test_chat_stream_sse_sends_done` | `test_server.py`, `test_query.py` |
| HR12 | `test_no_local_llm_tokens_anywhere_in_src` and the DK2 family | `test_inference_lanes.py` |
| HR31 | `test_rerank_passages_only_in_gpu_lane`, `test_embedder_never_requests_cpu_ep`, `test_chat_lane_is_haiku_only`, `test_chat_primary_model_is_haiku` | `test_inference_lanes.py` |

Deletion record: [tier-3 retirement](../../docs/decisions/2026-07-28-tier-3-retirement.md).
