# World Model

OSE fulfills the four-layer world model defined in `docs/world-model/model.yaml`.
Use this skill to check conformance and understand OSE's governing laws.

## Quick reference — L1 Invariants

| ID | Principle |
|----|-----------|
| P0 | GPU-only inference — embed+rerank on CUDA; CPU fallback raises fatally. |
| P1 | No local generative LLM. KB=DeepSeek-only. Chat=claude-haiku-4-5 only. |
| P2 | MCP query path (search/ask/graph/overview) runs NO generative LLM — only embed+rerank. |
| P3 | Federation = query-time union; no cross-repo edges; members have independent stores. |
| P4 | Indexing is event-driven (watcher/inotify); heavy KB cascade (enrich/wiki/BPRE) runs ONLY on source-fingerprint drift — never on metadata-only or non-indexed-file events; no periodic sweeps or timers. |
| P5 | Two-stage retrieval: vector recall (sqlite-vec) → cross-encoder rerank (GPU). Results ordered by rerank_score. |
| P6 | No heuristics — tree-sitter + LLM only. No re, no static/dynamic keyword list, no mapping table may substitute for structural analysis in Category A paths. Regex is grepped here; keyword/mapping-table debt is enforced by name in test_no_code_semantic_regex.py (a dict/frozenset literal ban can't be soundly grepped) — the registry is empty as of 2026-07-01: the last entry, bpre_spec._LANG_SPECS/_DEFAULT_SPEC (15 per-language HTTP method-name tables), was retired in favor of ONE universal structural classifier (URL-path anchor + _has_handler_arg handler-shape + _V verb ground-truth + gRPC proto-binding + _SCHEMES receiver-text provenance for non-verb client idioms) covering every tree-sitter code grammar by construction. Protocol/framework codegen-contract naming bound to structural facts (e.g. protoc New*Client/Register*Server scoped to .pb.go output, Spring *Mapping annotations, _GRP_SFXS gated on a discovered proto_services match) is ground truth, not a heuristic — reclassified 2026-07-01, same class as the closed HTTP-verb set _V and the closed protocol/URI-scheme set _SCHEMES. _provenance (bpre_generic.py) was generalized 2026-07-01 (Part C1) from receiver-text-only to also resolve def-use type bindings (build_type_use, valueflow.py) and import-map aliases (_scan_imports, bpre_ast.py) against the same closed _SCHEMES set — still zero library-name vocabulary; unresolved bare library-name idioms (e.g. requests/axios on a non-scheme absolute URL) remain accounted residue-ladder territory, never silently dropped. |
| P7 | Public-repo hygiene — absolute device paths never in wiki/docgen/OKF artifacts. |
| P8 | No mocks in tests — real daemon at :8765, real GPU, real embedder. |
| P9 | Flat-L1 communities only — no L2/L3 hierarchy in OSE KB (WS-B 2026-06-26). |
| P10 | Every line of code is a liability — prefer no change, then deletion, then smallest diff. |
| P11 | Push after every commit — zero unpushed is the policy. |
| P12 | Doc-tooling (docgen + OKF) is LLM-native via claude -p; no tree-sitter on the doc-tooling path. |
| P13 | Docgen + OKF = manual-trigger only; never called from auto-sweep (_enrich_project) or MCP tools. |
| P14 | LLM lanes: GPU=embed+rerank; DeepSeek=KB-enrichment; claude-haiku-4-5=chat; claude-p=doc-tooling. No cross-lane calls. |
| P15 | Kill-switches (OSE_DOCGEN=0, OSE_OKF=0) → no output; no deterministic skeleton fallback. |
| P16 | Idle frugality — with no query and no source drift the daemon holds < 1 % CPU and a constant RAM floor; models unload after OPENCODE_MODEL_IDLE_UNLOAD_S (300 s default); GPU is the only inference engine (maximized; CPU fallback fatal). |
| P17 | File-watching is event-driven via OS filesystem notifications (watchfiles/Rust notify) — one inotify instance + one thread for all watched roots, Rust-side event coalescing; manual per-file polling is a last-resort fallback only (NFS/SMB, handled internally by the Rust notify crate's force_polling), never a hand-rolled Python poll loop. |
| P18 | Public-release & device-neutrality (whole-repo facet of P7) — the tracked tree is publishable: no secrets, no real device paths, no company/project names. Every machine-specific value (storage paths, host, port, models, GPU device) is env-driven with XDG defaults (core/config.py); no hardcoded absolute paths/usernames/hostnames in tracked source, tests, docs, or generated artifacts. Device-specific name bans (company/codename/device-id lists) never ship in the public tree — they live only in the private ose-live-audit repo. |

## L2 Components

| Package | Module | Key ops |
|---------|--------|---------|
| core | `opencode_search.core` | config · registry · types |
| embed | `opencode_search.embed` | get_embedder · get_reranker |
| index | `opencode_search.index` | index_project · VectorStore |
| graph | `opencode_search.graph` | extract_symbols · detect_communities · GraphStore |
| kb | `opencode_search.kb` | enrich_communities_batch · build_wiki · reconstruct_processes · run_docgen · run_okf |
| query | `opencode_search.query` | search · compose_answer |
| server | `opencode_search.server` | mcp · routes_pipeline · routes_project · _overview |
| daemon | `opencode_search.daemon` | sweeps · watcher · federation · scheduler |

## How to check conformance

```bash
# GPU-free, daemon-free
python scripts/check_world_model.py

# Check a specific range
python scripts/check_world_model.py --base HEAD~1 --head HEAD

# Regenerate these skill files after editing model.yaml or info-hierarchy.md
python scripts/gen_world_model_skills.py
```

## Authoritative sources

- `docs/world-model/model.yaml` — machine-readable (L1–L4 schema)
- `docs/world-model/README.md` — human reference
- `docs/architecture/federation-and-search-engine.md` — §1a prose form
- `docs/architecture/federation-ops-and-invariants.md` — §13b HR specs

_Generated by scripts/gen_world_model_skills.py — do not hand-edit._
