# LLM Driver Doctrine (June 2026)

> **Status:** reference — locked. Every change to the LLM lane assignments requires updating this file + memories + skills.

---

## 1. Four-lane map (locked)

| Lane | Engine | Used for | CPU fallback? |
|---|---|---|---|
| **Embeddings + rerank** | **GPU** (FastEmbed/ONNX/CUDA) | Vector search (`search`) + cross-encoder rerank ONLY | **Fatal** — raises at startup |
| **KB enrichment** | **DeepSeek** (cloud, `deepseek-v4-flash`) | Community narration · wiki pages · BPRE process linkage — ON when `DEEPSEEK_API_KEY` present; suppressed naturally when absent | N/A — cloud |
| **Dashboard chat** | **claude-haiku-4-5** | Interactive chat answers via `/api/chat` | N/A — cloud |
| **Doc-tooling** | **`claude -p`** (Haiku 4.5 default / Sonnet 4.6 for synthetic pages) | `docgen` IH authoring + `okf` OKF bundle authoring | N/A — cloud |

No other lane assignments are permitted. In particular:
- DeepSeek MUST NOT be used for doc-tooling (docgen/okf)
- `claude -p` MUST NOT be used for KB enrichment or dashboard chat
- CPU inference for embeddings/rerank is a fatal error — never a silent fallback

---

## 2. `claude -p` headless driver

`claude -p` is the `claude` CLI's headless/non-interactive mode. It reads a prompt from stdin (or `--prompt-file`), runs to completion, prints the result, and exits.

### Key flags

```bash
claude \
  --print \                          # headless mode (alias: -p)
  --output-format json \             # structured output: {result, usage, model, ...}
  --model claude-haiku-4-5 \         # explicit model (or claude-sonnet-4-6 for synthetic pages)
  --allowedTools "Read,Glob,Grep" \  # restrict tool surface (doc-tooling reads; no writes)
  --permission-mode default \        # no interactive permission prompts
  --no-verbose \                     # suppress progress chatter
  < prompt.txt
```

### CLAUDE_CONFIG_DIR profiles

Two account slots supported (primary + secondary, for rate-limit failover):

```python
# vendor/docgen/src/ose_docgen/config.py
CLAUDE_PROFILES = [
    os.path.expanduser("~/.claude"),          # primary account (default)
    os.path.expanduser("~/.claude-account1"), # secondary (failover on 429)
]
```

Pick the profile with the most headroom; retry the next on HTTP 429.

### Subprocess IPC (daemon context)

When spawning `claude -p` as a subprocess from within OSE's daemon:
- **Always set `CLAUDE_CODE_SAFE_MODE=1`** in `subprocess_env()` — prevents nested Claude Code from inheriting the parent's IPC socket and causing deadlock.
- **Do NOT set `SIMPLE=1`** — breaks OAuth token exchange.

### Billing note (post-15 Jun 2026)

Non-interactive Claude usage (`claude -p`) bills against a **separate Agent-SDK credit pool** (not the same quota as interactive chat). Minimize cost with:
- **Idempotent source-sig skip** — if the repo's content signature is unchanged, `generate()` returns immediately with 0 `claude -p` calls
- **Significance-gating** — deep-read the head (high-importance concepts); shallow-treat the tail (structural labels = 0 tokens)
- **Batching** — batch multiple concept pages into one prompt where possible
- **`--output-format json`** — capture `usage.input_tokens` + `usage.output_tokens` per call for cost telemetry

---

## 3. LLM-native doc-tooling law (June 2026, locked)

**IH importance/generality ranking** and **OKF concept-identity** are irreducibly *semantic* judgments that an AST parser cannot make (salience ≠ syntax; a concept ≠ a tree-sitter node). Therefore:

> **`docgen` and `okf` are 100% LLM-native** — `claude -p` reads the repo source directly (its own Read/Grep/Glob tools), identifies what matters most, and authors the hierarchy/concepts. No tree-sitter, no regex, no static/keyword mapping anywhere on the doc-tooling path.

### Scope boundary (critical)

| Path | Tree-sitter? | Engine |
|---|---|---|
| `vendor/docgen/` + `vendor/okf/` (doc-tooling) | **NO** — removed | `claude -p` LLM-native |
| `src/rag_search/graph/extractor.py` (core retrieval) | **YES — kept** | tree-sitter (300+ grammars) + DeepSeek semantics |

### Trade-offs (accepted)

| Gain | Cost | Mitigation |
|---|---|---|
| True semantic ranking; any-language reach | ~10× higher token cost per run | Idempotent source-sig skip (unchanged repo = 0 calls) |
| Concept identity = meaning, not syntax | Run-to-run non-determinism | Gate on structure/citation, never exact prose; golden fixture bundles for CI |
| No grammar dependency | Citation hallucination risk | **Deterministic citation-resolution gate**: every `[code: file:line]` must resolve to a real file:line (plain read + content-substring match) |

### Citation-resolution gate

The primary LLM-native guardrail. Applied deterministically (no LLM) post-generation:
1. Parse every `[code: file:line]` reference from the generated output
2. Verify the file exists relative to the project root
3. Verify the line number is within the file's line count
4. Optionally: verify a content substring appears near that line
5. Reject (raise / skip file) if any citation fails to resolve

This gate catches hallucinated structure *before* it enters any KB or index.

---

## 4. Sources (June 2026)

| Source | Key claim |
|---|---|
| arXiv 2603.27277 — Tree-Sitter-KG | Tree-sitter = "necessary prerequisite infrastructure": zero tokens, ~66 languages, grounding substrate for retrieval. Cannot rank salience. |
| ETH Zürich context-file study (2026) | LLM-authored context files outperform static keyword/regex summaries for developer comprehension. |
| NN/g on salience (2026) | "Importance" is a user-relative, semantic judgment — not computable from parse-tree node type alone. |
| OKF reference pipeline (Google Cloud, Jun 2026) | OKF concepts = semantic units identified by reading source semantics, not AST traversal. |

---

## 5. Conformance checklist

| Property | Check |
|---|---|
| GPU lane: no CPU fallback | `assert_cuda_available()` called at startup; `ort.preload_dlls()` forces CUDA binding |
| KB lane: DeepSeek only | `DEEPSEEK_API_KEY` checked at enrichment time; no ollama/qwen/local model references in `src/rag_search/kb/` |
| Chat lane: claude-haiku-4-5 only | `MODEL_CHAT = "claude-haiku-4-5"` in `server/`; no DeepSeek in chat handler |
| Doc-tooling: `claude -p` only | No DeepSeek imports in `vendor/docgen/` or `vendor/okf/`; config.py MODEL_HAIKU/MODEL_SONNET only |
| No tree-sitter on doc-tooling path | `import tree_sitter` absent from `vendor/docgen/` + `vendor/okf/` |
| Idempotent source-sig skip | Unchanged repo re-run produces 0 `claude -p` calls (test: `unchanged_repo_re_run_zero_llm_calls`) |
| Citation-resolution gate | Every `[code: file:line]` resolves; test asserts gate runs on all generated output |
| CLAUDE_CODE_SAFE_MODE=1 in subprocess | `subprocess_env()` in daemon includes `CLAUDE_CODE_SAFE_MODE=1` |

---

## See also

- `vendor/docgen/src/ose_docgen/config.py` — `MODEL_HAIKU`, `MODEL_SONNET`, `CLAUDE_PROFILES`, `_PHASE_MODELS`
- `vendor/docgen/src/ose_docgen/accounts.py` — profile rotation + rate-limit failover
- `docs/reference/information-hierarchy.md` — what IH means (why LLM-native is needed)
- `docs/reference/okf.md` — OKF spec (why concept-identity needs LLM reasoning)
- `docs/reference/world-model.md` — WM definition
