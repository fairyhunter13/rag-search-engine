# OSE World Model

> OSE *fulfills* this model. Code is a side-effect; this is the normative target.

## What this is

A **structured, queryable, partially-executable** representation of what OSE is building, why, and how it MUST be built. Its defining property is **action-conditioned prediction**: given the current codebase state + a candidate change, `check_world_model.py` predicts whether the next state still satisfies the laws (L1 invariants) and behavior specs (L3 HRs).

This is a development-governance artifact. It lives in `docs/` and `scripts/`; it has no MCP surface, no FEATURES.md entry, no `src/opencode_search/` code.

## Four-layer schema (agentic-coding, June 2026)

| Layer | What | Where in OSE |
|-------|------|-------------|
| **L1** | Architecture invariants (laws — what changes are permitted) | §1a + `model.yaml` P0–P15 in `federation-and-search-engine.md` |
| **L2** | Components — capability→module→operation map | `src/opencode_search/{core,embed,index,graph,kb,query,server,daemon}/` |
| **L3** | Behavior specs — HRs, invariants, workflows | §13b HR1–HR31 in `federation-ops-and-invariants.md` |
| **L4** | Code patterns & generation rules | `model.yaml` L4_patterns; enforced by `test_no_code_semantic_regex.py` |

`model.yaml` in this directory is the machine-readable instantiation.

## Key invariants (L1 summary)

| ID | Law |
|----|-----|
| P0 | GPU-only inference; CPU fallback fatal |
| P1 | No local generative LLM; KB=DeepSeek; chat=claude-haiku-4-5 |
| P2 | MCP query path: embed+rerank only (no LLM) |
| P3 | Federation = query-time union; no cross-repo edges |
| P4 | Event-driven indexing; no periodic sweeps |
| P5 | Two-stage retrieval: vector recall → cross-encoder rerank |
| P6 | No heuristics: tree-sitter + LLM only |
| P7 | Public-repo hygiene: no absolute paths in artifacts |
| P8 | No mocks in tests |
| P9 | Flat-L1 communities only (WS-B 2026-06-26) |
| P10 | Every line of code is a liability |
| P11 | Push after every commit |
| P12 | Doc-tooling (docgen + OKF) is LLM-native via `claude -p`; no tree-sitter on the doc-tooling path |
| P13 | Docgen + OKF = manual-trigger only; never from auto-sweep or MCP tools |
| P14 | LLM lanes: GPU=embed+rerank; DeepSeek=KB-enrichment; claude-haiku-4-5=chat; claude-p=doc-tooling |
| P15 | Kill-switches (OSE_DOCGEN=0, OSE_OKF=0) → no output; no deterministic skeleton fallback |

## Tools

```bash
# Check working-tree conformance (GPU-free, daemon-free):
python scripts/check_world_model.py

# Check a specific diff:
python scripts/check_world_model.py --base HEAD~1 --head HEAD

# Regenerate skills from this model:
python scripts/gen_world_model_skills.py
```

## Relationship to §1a/§13b

The §1a principles register in `federation-and-search-engine.md` is the prose form; `model.yaml` is the machine-readable form. They must stay in sync — `check_world_model.py` cross-references both.

The §13b HR table in `federation-ops-and-invariants.md` is the full normative spec; `model.yaml` L3_specs is the subset relevant to action-conditioned checking.
