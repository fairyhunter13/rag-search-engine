# Whole-Engine Conformance Audit + Research-Backed Tier-3 Recommendation + Public-Release Hardening

> **Date:** 2026-07-09
> **Scope:** Whole-engine adversarial audit of every DIKW rung + cross-cutting lane against the
> no-heuristic doctrine (P6, HR15-19) and token-frugality doctrine (HR23), each verdict backed
> by July-2026 research; a decisive build-vs-retire call on Tier-3 (whole-file LLM), incl. token
> cost; public-release/device-neutrality hardening. **Audit + hardening, not a redesign.**
> **Verdict:** CONFORMS end to end. No code defects. Doc-drift fixed (stale `CLAUDE.md` chat/
> Tier-3 claims). Tier-3 whole-file LLM is formally **RETIRED** (research-backed, not deferred).
> `.gitmodules`/CI repo-identity refs audited — already fork-safe, no change needed. New
> runnable-by-anyone guard test added.
> **Device neutrality:** no real names/paths/hostnames/device IDs in this report (P18/HR34).

---

## Part 1 — Research evidence (July 2026)

- **Static/structural extraction beats LLM extraction**: PyCG 93.3%/86.7% vs. fine-tuned
  GPT-3.5's 57.8%/61.9%, ~190× faster (arXiv 2402.17679); HeaderGen 90.9%/91.8% (2410.00603).
- **SEA select-not-author** (2408.04344) — matches Tier-2 (`_llm_link_resolve`) exactly.
- **cAST structural chunking** (2506.15655) — already shipped (`index/chunker.py`); explicit
  design goal "no language-specific heuristics" (P6).
- **Two-stage rerank** is 2026 standard (Cohere 3.5, Voyage rerank-2.5, BGE v2, Jina v2) —
  matches `query/search.py`.
- **Tree-sitter-first cuts token burn** ~37%, 10× fewer tokens at 83% vs 92% quality
  (Codebase-Memory, 2603.27277) — whole-file LLM reintroduces the exact cost this avoids.
- **Code-embedding SOTA** (voyage-code-3/CodeXEmbed) is cloud-API — conflicts with GPU-only
  doctrine; recorded as a trade-off, not adopted.

## Part 2 — Tier-3 recommendation: **RETIRE, do not build**

Whole-file resolution sends each parse-error file's *entire source* as the dynamic tail — no
stable prefix, so DeepSeek prefix-caching cannot apply (~$0.14/M miss vs ~$0.0028/M hit, ~50×
spread). One ~300-line file ≈ 3-4K tokens; a few dozen unparseable files/rebuild, recurring on
every code-drift event (HR38), ≈ 10⁵-10⁶ miss tokens per rebuild — 1-2 orders of magnitude above
Tier-2's cached/capped SEA-select (`bpre.py:743`, `max_tokens=512`). Also **less accurate**:
static extraction beats LLM extraction per Part 1. `_generic_walk` (deterministic, GPU-free)
remains the honest fallback. **Retired**, not deferred, in `model.yaml` HR15,
`federation-and-search-engine.md` §7a/four-lane, `federation-ops-and-invariants.md` HR16,
`CLAUDE.md`. HR19 (dynamic-recall future work) is unrelated and untouched.

## Part 3 — Whole-engine DIKW audit (WS-1)

| Rung | Evidence | Verdict |
|---|---|---|
| Data/embed | cAST header (`chunker.py`, CC1-CC6); GPU-only embed/rerank binding, double-checked post-construction (`core/gpu.py`, `test_p1_smoke.py`) | CONFORMS |
| Information/no-heuristic | `_SEMANTIC_HEURISTIC_DEBT={}` (empty); 13 guard tests; closed ground-truth vocabs distinguished from heuristics | CONFORMS |
| Knowledge/community | Deterministic seeded `community_fastgreedy` (not Leiden/k-core); `test_sc8_*` | CONFORMS |
| Knowledge/narration | Significance-gated (head≥8 or cross-deg≥2); batched/cached/capped/accounted DeepSeek calls | CONFORMS |
| Wisdom | `check_world_model.py --all` → CONFORMS, all P0-P18 | CONFORMS |
| Query/retrieval | `search.py` ANN×3 → GPU rerank sole ranking authority; zero generative calls | CONFORMS |
| Chat lane | Haiku-only, RuntimeError/SSE-error on CLI failure, no DeepSeek fallback (`da070e8`) | CONFORMS (doc was stale, now fixed) |
| Federation #5 (GPU-only) | Re-derived line-by-line: same `core/gpu.py` mechanism, no weaker federation-path variant | CONFORMS |
| Federation #7 (idempotency) | Re-derived line-by-line: existence-guarded registration, anti-flap federation-list overwrite, soft-disable removal, per-project error isolation, defensive dedup in `expand_federation` (`test_gdup_duplicate_symlink_members_deduped`) | CONFORMS |

No new code defects found. All findings were doc-drift or hardening additions (below).

## Part 4 — Token-efficiency audit (WS-3, HR23)

All four `deepseek_extract` call sites in the engine, exhaustively enumerated:

| Site | Prefix | Batch/cap | Accounted |
|---|---|---|---|
| `bpre.py:743` `_llm_link_resolve` (Tier-2 edges) | stable | ≤30 items, `max_tokens=512` | `bpre_link` |
| `bpre.py:921` `_generate_narratives_batch` (process narrative) | stable | 20/batch, `≤8192` tok; delta-narration (unchanged carried over) | `bpre` |
| `enrich.py:95` `classify_communities_semantic` | stable | 20/batch, parallel ×3 | `classify` |
| `enrich.py:154` `_enrich_one_batch` (community narration) | stable | 20/batch, `≤4096` tok, 50K budget cap | accumulated (hit/miss/completion/calls) |

All four satisfy all 5 HR23 requirements — **no gap, no code change**. `query/search.py`,
`ask.py`, `graph_handler.py` contain no generative-LLM imports (token-zero read path confirmed).

## Part 5 — Doc-drift fixed (WS-2)

1. `CLAUDE.md` claimed "chat = haiku + DeepSeek fallback" — fallback was removed in `da070e8`;
   fixed to "haiku only, no DeepSeek fallback (HR12)".
2. `CLAUDE.md` listed "Tier-3 whole-file" as an active ON-by-default lane — fixed to state retired.
3. Tier-3 upgraded from DEFERRED to **RETIRED** with token-cost/accuracy rationale across
   `model.yaml`, both architecture docs. HR19's unrelated DEFERRED item untouched.
4. HR14 (BPRE's own Tier-1/2/3 process-reconstruction numbering) vs HR16 (resolution-ladder
   tiers) — two unrelated numbering schemes shared the term "Tier 3." Disambiguated inline in
   both docs so the retired resolution tier isn't confused with BPRE's live D5/D6 narrative tier.

No code changes required — shipped code/tests were already correct; only prose was stale.

## Part 6 — Public-release hardening (WS-4)

- Re-verified every `core/config.py` machine-specific value (storage, host, port, embed/rerank
  models, GPU device, query-LLM provider/model) is `os.environ.get(...)`-driven with an XDG
  default — full-file read, not sampled.
- `.gitmodules`' `vendor/docgen` submodule URL is publicly cloneable (confirmed reachable); CI's
  `github.repository` guard exists specifically so forks without a self-hosted GPU runner skip
  that job instead of hanging (documented in the CI file's own comment) — both already
  fork-safe. **No genericization applied**; nothing needed user reconfirmation since no change
  was warranted.
- New `test_public_hygiene.py::test_runtime_config_is_env_driven` machine-checks the
  runnable-by-anyone contract for `EMBED_MODEL`/`RERANK_MODEL`/`EMBED_DEVICE`/`DAEMON_HOST`/
  `DAEMON_PORT`/`QUERY_LLM_PROVIDER`/`QUERY_LLM_MODEL`/`OPENCODE_GPU_DEVICE`. Wired into HR34.
- `README.md` setup path (install → secrets → daemon → MCP register → index → verify health)
  and `scripts/check_system.py`/`configure_integrations.py` already give a clean first run.

## Gap register

| # | Area | Verdict | Action |
|---|---|---|---|
| 1 | `CLAUDE.md` stale chat-fallback claim | Doc-drift, fixed | Corrected to match `da070e8` |
| 2 | `CLAUDE.md` stale Tier-3-active claim | Doc-drift, fixed | Corrected; marked retired |
| 3 | Tier-3 DEFERRED vs decisive RETIRE | Doc-drift, fixed | Upgraded with rationale |
| 4 | HR14/HR16 "Tier 3" naming collision | Clarity gap, fixed | Disambiguated inline |
| 5 | Runnable-by-anyone contract not machine-checked | Hardening gap, fixed | New test + HR34 wiring |
| 6 | `.gitmodules`/CI repo-identity coupling | Audited, no defect | Confirmed fork-safe, no change |

## Verification

- `python scripts/check_world_model.py --all` → CONFORMS, all checkable L1 invariants satisfied.
- `.venv/bin/pytest src/tests/live/test_public_hygiene.py -v --strict-markers --strict-config -ra -q` → 6 passed.
- `.venv/bin/ruff check src/tests/live/test_public_hygiene.py` → All checks passed.
- `python -m compileall -q src/tests/live/test_public_hygiene.py` → clean.

## Files changed

`CLAUDE.md`, `docs/world-model/model.yaml`, `docs/architecture/federation-and-search-engine.md`,
`docs/architecture/federation-ops-and-invariants.md`, `src/tests/live/test_public_hygiene.py`,
this report (new).

## Addendum (2026-07-09, research refresh)

Follow-up pass re-checking Part 2's Tier-3 retirement and HR19's DEFERRED status against
July-2026 literature, prompted by a request for the two items still worth watching (DeepSeek
pricing as the one input that could change the Tier-3 call; HR19 as legitimately open future
work). No code or spec change resulted — both calls are reaffirmed, one caveat is retired.

**Tier-3 retirement — reaffirmed and strengthened, not weakened.** "Less Is More"
(arXiv 2604.21746, Apr 2026) shows aggressive LLM involvement in static-analysis tasks is both
*least* accurate (15–25% for the most agentic approach, vs 55–58% for structured/constrained) and
*8× costlier* in tokens — whole-file LLM sits at that aggressive end. Static call-graph SOTA is
unchanged (PyCG 93.3/86.7 vs GPT-3.5 57.8/61.9, arXiv 2402.17679; HeaderGen 91.7/93.3 vs
38.8/39.6, arXiv 2410.00603). The 2026 direction for hybrid extraction is structure-first +
LLM-augment (arXiv 2603.24837), i.e. the shipped tree-sitter-first + Tier-2 SEA-select design —
not whole-file LLM. DeepSeek pricing today: V4 Flash $0.14/M miss vs $0.003/M hit (~47×,
api-docs.deepseek.com) — Part 2's cost argument still holds. **Consequence: the retire call is
over-determined** — pricing only affects the cost leg, and the accuracy leg is price-independent,
so a DeepSeek price change cannot flip the decision on its own. The pricing assumption needs no
further tracking.

**HR19 — reaffirmed DEFERRED; design matches 2026 SOTA; sharpened revisit trigger.** Static
microservice architecture recovery has a real, now-quantified ceiling: best single tool F1 0.86,
4-tool ensemble F1 0.91 (arXiv 2412.08352) — roughly 9–14% of inter-service edges are genuinely
unreachable statically. That paper stays static specifically because static integrates into fast
CI/CD, the same posture this engine holds. Modern microservice-dependency graphs explicitly model
static + dynamic edge types, with runtime tracing (servicegraph, gRPC reflection, cross-service
span correlation — e.g. CrossTrace, arXiv 2508.11342) as the recognized complement — matching
HR19's documented design exactly. Building it, however, adds a runtime-trace dependency
(target system running under load, reflection enabled, a trace-ingestion/reconciliation pipeline,
plus the mandatory anti-hallucination gate) to an engine that is deliberately static, GPU-free,
and CI-friendly — an operational-posture shift, not just more code. **Revisit trigger: a measured
cross-service recall miss on a live federation, not a date.**

Sources: arXiv 2402.17679, 2410.00603, 2604.21746, 2603.24837, 2412.08352, 2508.11342;
api-docs.deepseek.com pricing (July 2026).
