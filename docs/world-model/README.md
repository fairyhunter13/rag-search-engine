# RSE World Model

> RSE *fulfills* this model. Code is a side-effect; this is the normative target.

## What this is

A **structured, queryable, partially-executable** representation of what RSE is building, why, and how it MUST be built. Its defining property is **action-conditioned prediction**: given the current codebase state + a candidate change, `check_world_model.py` predicts whether the next state still satisfies the laws (L1 invariants) and behavior specs (L3 HRs).

This is a development-governance artifact. It lives in `docs/` and `scripts/`; it has no MCP surface, no FEATURES.md entry, no `src/rag_search/` code.

## Four-layer schema (agentic-coding, June 2026)

| Layer | What | Where in RSE |
|-------|------|-------------|
| **L1** | Architecture invariants (laws — what changes are permitted) | §1a + `model.yaml` P0–P18 in `federation-and-search-engine.md` |
| **L2** | Components — capability→module→operation map | `src/rag_search/{core,embed,index,graph,kb,query,server,daemon}/` |
| **L3** | Behavior specs — HRs, invariants, workflows | §13b HR1–HR40 in `federation-ops-and-invariants.md` |
| **L4** | Code patterns & generation rules | `model.yaml` L4_patterns; enforced by `test_no_code_semantic_regex.py` |

`model.yaml` in this directory is the machine-readable instantiation.

## Key invariants (L1 summary)

| ID | Law |
|----|-----|
| P0 | GPU-only inference; CPU fallback fatal |
| P1 | No local generative LLM; chat=claude-haiku-4-5 via `claude -p`, the one generative lane left |
| P2 | MCP query path: embed+rerank only (no LLM) |
| P3 | Federation = query-time union; no cross-repo edges |
| P4 | Event-driven indexing; the heavy pass runs only on code-source-fingerprint drift; no periodic sweeps |
| P5 | Two-stage retrieval: hybrid recall (dense + FTS5 BM25, RRF-fused) → cross-encoder rerank |
| P6 | No heuristics: tree-sitter only, package-wide, outside four intrinsic-mechanism files |
| ~~P7~~ | *retired 2026-07-28* — it scoped hygiene to the artifact writers, and RSE writes no artifacts now; P18 is the whole-tree facet |
| P8 | No mocks in tests |
| P9 | Flat-L1 communities only (WS-B 2026-06-26) — igraph `community_fastgreedy`, no LLM |
| P10 | Every line of code is a liability |
| P11 | Push after every commit |
| ~~P12~~, ~~P13~~, ~~P15~~ | *retired 2026-07-28 with tier 3* — all three governed doc-tooling (OKF's LLM-native rule, its manual-trigger-only rule, and the two kill-switches) |
| P14 | Two LLM lanes: GPU=embed+rerank (non-generative); claude-haiku-4-5=chat via `claude -p`, its only caller |
| P16 | Idle frugality — < 1 % CPU idle, models unload, active work cgroup-capped at 1 core |
| P17 | File-watching is OS-notification driven; no hand-rolled poll loop |
| P18 | Public-release & device-neutrality: no secrets, no real device paths, permanent brand lock |

## Tools

```bash
# Check working-tree conformance (GPU-free, daemon-free):
.venv/bin/python scripts/check_world_model.py

# Check a specific diff:
.venv/bin/python scripts/check_world_model.py --base HEAD~1 --head HEAD

# Regenerate skills from this model:
.venv/bin/python scripts/gen_world_model_skills.py
```

## Relationship to §1a/§13b

The §1a principles register in `federation-and-search-engine.md` is the prose form; `model.yaml` is the machine-readable form. They must stay in sync — `check_world_model.py` cross-references both.

The §13b HR table in `federation-ops-and-invariants.md` is the full normative spec; `model.yaml` L3_specs is the subset relevant to action-conditioned checking.
