---
name: world-model
description: "RSE governing laws — the P0–P18 / HR1–HR41 invariant IDs, the component map, and how to check working-tree conformance. Read before changing indexing, extraction, GPU, or CPU-budget behaviour."
---

# World Model

RSE fulfills the four-layer world model defined in `docs/world-model/model.yaml`.
Use this skill to check conformance and understand RSE's governing laws.

## Quick reference — L1 Invariants

| ID | Principle |
|----|-----------|
| P0 | GPU-only inference — embed+rerank on CUDA; CPU fallback raises fatally. |
| P1 | No local generative LLM. Chat=claude-haiku-4-5 via `claude -p` only — the one generative lane left after tier 3 (2026-07-28). |
| P2 | MCP query path (search/graph/overview) runs NO generative LLM — only embed+rerank. The deepseek_* tokens stay in the check after tier 3's deletion: they now match nothing, which is the point — this is the reintroduction guard, not a description of a live call site. |
| P3 | Federation = query-time union; no cross-repo edges; members have independent stores. |
| P4 | Indexing is event-driven (watcher/inotify); the heavy pass (graph re-derive + structural community labelling) runs ONLY on code-source-fingerprint drift — never on metadata-only or non-indexed-file events; no periodic sweeps or timers. The cascade this named until 2026-07-28 (enrich/wiki/BPRE) left with tier 3; the gate did not, because the re-derive it protects is the expensive half that survived. |
| P5 | Two-stage retrieval: hybrid recall — dense (sqlite-vec KNN) and lexical (FTS5 BM25 over chunks_fts), fused by Reciprocal Rank Fusion — then cross-encoder rerank (GPU). Results ordered by rerank_score, never by the RRF or vector score. The lexical lane joined on 2026-07-28; it changes which candidates the cross-encoder sees and nothing about who decides the order. |
| P6 | No heuristics — tree-sitter only. No re, no static/dynamic keyword list, no mapping table may substitute for structural analysis, anywhere in the package except four intrinsic-mechanism files (graph/extractor.py grammar node-kind tables, index/discover.py extension bootstrap, core/registry.py and core/config.py slug plumbing). The doctrine outlived tier 3 and got WIDER, not narrower: the Category-A list this used to enumerate was the five kb/bpre*.py files plus kb/patterns.py and _overview.py's service detection, and every one of them left on 2026-07-28 — so the whole package is now checked instead of seven files. The '+ LLM' half of the old principle went with them; structural analysis has no fallback now, which is why extraction coverage is a first-order concern rather than a nicety. The \\b in the check below is load-bearing at this width: bare `re\\.search` also matches `store.search(` in query/search.py and would report a permanent false AT_RISK — invisible while paths named seven kb/ files that had no such call. Keep the comment inside this string, not above the check: _parse_invariants matches principle and check with `\\s+` between them, so an intervening comment line drops the whole invariant out of the checker silently. |
| P8 | No mocks in tests — real daemon at :8765, real GPU, real embedder. |
| P9 | Flat-L1 communities only — no L2/L3 hierarchy in the graph (WS-B 2026-06-26). Communities are tier 2 (igraph community_fastgreedy + structural labelling, no LLM) and survive tier 3's deletion; only the DeepSeek narration of them was lost. |
| P10 | Every line of code is a liability — prefer no change, then deletion, then smallest diff. |
| P11 | Push after every commit — zero unpushed is the policy. |
| P14 | Two LLM lanes, no cross-lane calls: GPU = embed + rerank (local, non-generative); claude-haiku-4-5 = dashboard chat via `claude -p`, its sole caller. This was four lanes until 2026-07-28 — the doc-tooling lane went with docgen, the DeepSeek KB-enrichment lane with the rest of tier 3. Nothing generative runs on the local GPU and nothing outside routes_chat.py spawns a generative process. |
| P16 | Idle frugality — with no query and no source drift the daemon holds < 1 % CPU and a constant RAM floor; models unload after RSE_MODEL_IDLE_UNLOAD_S (300 s default); GPU is the only inference engine (maximized; CPU fallback fatal). CPU budget is two-tier and kernel-enforced, not merely cooperative (HR40): idle/steady-state < 1 % of one core is a live-measured automated gate; active work (indexing, graph re-derive, parsing — it was 'indexing, BPRE, parsing' until tier 3 left on 2026-07-28, and the ceiling is unchanged because it bounds the cgroup, not a named workload) is bounded ≤ 1 core by a cgroup-v2 CPUQuota on the daemon's systemd unit that it physically cannot exceed. |
| P17 | File-watching is event-driven via OS filesystem notifications (watchfiles/Rust notify) — one inotify instance + one thread for all watched roots, Rust-side event coalescing; manual per-file polling is a last-resort fallback only (NFS/SMB, handled internally by the Rust notify crate's force_polling), never a hand-rolled Python poll loop. Watcher-triggered indexing/parsing work inherits the same kernel CPUQuota ceiling as the rest of the daemon (HR40), including bounded_parse.py's spawn-context workers, which are children of the daemon's own cgroup. |
| P18 | Public-release & device-neutrality (whole-repo facet of P7) — the tracked tree is publishable: no secrets, no real device paths, no company/project names. Every machine-specific value (storage paths, host, port, models, GPU device) is env-driven with XDG defaults (core/config.py); no hardcoded absolute paths/usernames/hostnames in tracked source, tests, docs, or generated artifacts. Permanent brand lock (2026-07-09): the legacy OSE/OPENCODE/ocs branding was fully retired in favor of RSE — no OPENCODE_, OSE_, bare OSE, opencode, ocs[-_]/ose[-_], or opencode-index token may ever reappear, outside a narrow external-product allowlist (the external OpenCode CLI product). Device-specific name bans (company/codename/device-id lists) never ship in the public tree — they live only in the private rse-live-audit repo. |

## L2 Components

| Package | Module | Key ops |
|---------|--------|---------|
| core | `rag_search.core` | config · registry · types |
| embed | `rag_search.embed` | get_embedder · get_reranker |
| index | `rag_search.index` | index_project · VectorStore |
| graph | `rag_search.graph` | extract_symbols · detect_communities · GraphStore |
| query | `rag_search.query` | search · compose_answer · answer_cache |
| server | `rag_search.server` | mcp · routes_pipeline · routes_project · _overview |
| daemon | `rag_search.daemon` | sweeps · watcher · federation · scheduler |

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
