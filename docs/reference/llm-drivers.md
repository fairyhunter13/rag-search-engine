# LLM Driver Doctrine (June 2026)

> **Status:** reference — locked. Every change to the LLM lane assignments requires updating this file + memories + skills.

---

## 1. Lane map (locked)

| Lane | Engine | Used for | CPU fallback? |
|---|---|---|---|
| **Embeddings + rerank** | **GPU** (FastEmbed/ONNX/CUDA) | Vector search (`search`) + cross-encoder rerank ONLY | **Fatal** — raises at startup |
| **KB enrichment** | **DeepSeek** (cloud, `deepseek-v4-flash`) | Community narration · wiki pages · BPRE process linkage — ON when `DEEPSEEK_API_KEY` present; suppressed naturally when absent | N/A — cloud |
| **Dashboard chat** | **claude-haiku-4-5** via `claude -p` | Interactive chat answers via `/api/chat` | N/A — cloud |

The **doc-tooling lane is gone.** `docgen` and `okf` were deleted with tier 3 (July 2026), so
`claude -p` now has exactly one caller: dashboard chat. The KB-enrichment row leaves with the rest
of tier 3 — when it does, this table is two lanes: **GPU (embed + rerank)** and **`claude -p`
(dashboard chat only)**.

No other lane assignments are permitted. In particular:
- `claude -p` MUST NOT be used for KB enrichment
- DeepSeek MUST NOT be used for dashboard chat — there is no fallback on that path
- CPU inference for embeddings/rerank is a fatal error — never a silent fallback

---

## 2. `claude -p` headless driver

`claude -p` is the `claude` CLI's headless/non-interactive mode. It reads a prompt from stdin (or `--prompt-file`), runs to completion, prints the result, and exits.

### The one live invocation

`server/routes_chat.py` spawns it directly and streams stdout to the SSE response — no
`--output-format json`, no tool allowlist, because the chat lane asks Claude to write prose
over a context RSE has already assembled:

```python
create_subprocess_exec(
    _CLAUDE, "-p", "--model", QUERY_LLM_MODEL, prompt,   # QUERY_LLM_MODEL = claude-haiku-4-5
    stdout=PIPE, stderr=DEVNULL, env=env,                # env from subprocess_env() below
)
```

Empty output is an error, not a fallback: the route raises rather than answering from
anything else.

### CLAUDE_CONFIG_DIR profiles

Two account slots (primary + secondary, for rate-limit failover), read from
`RSE_CLAUDE_PROFILES` as a comma-separated list:

```python
# src/rag_search/core/claude_profiles.py
PROFILES = [~/.claude, ~/.claude-1]   # default; override with RSE_CLAUDE_PROFILES
```

`pick_profile()` scores each slot with `utilization()` — an OAuth **usage** read
(`api.anthropic.com/api/oauth/usage`, 5-minute per-profile cache), never a completion — and
returns the least-used profile that still has headroom, or `None` when all are exhausted.

### Subprocess IPC (daemon context)

When spawning `claude -p` as a subprocess from within RSE's daemon:
- **Always set `CLAUDE_CODE_SAFE_MODE=1`** in `subprocess_env()` — prevents nested Claude Code from inheriting the parent's IPC socket and causing deadlock.
- **Do NOT set `SIMPLE=1`** — breaks OAuth token exchange.
- **Strip `ANTHROPIC_API_KEY`** — `subprocess_env()` drops it so the call bills against the
  chosen subscription profile rather than silently falling through to API billing.

### Billing note (post-15 Jun 2026)

Non-interactive Claude usage (`claude -p`) bills against a **separate Agent-SDK credit pool** (not the same quota as interactive chat). One dashboard chat turn is one call, so the spend here is bounded by user interaction — profile rotation, not batching, is what keeps a single account from being drained.

---

## 3. Conformance checklist

| Property | Check |
|---|---|
| GPU lane: no CPU fallback | `assert_cuda_available()` called at startup; `ort.preload_dlls()` forces CUDA binding |
| KB lane: DeepSeek only | `DEEPSEEK_API_KEY` checked at enrichment time; no ollama/qwen/local model references in `src/rag_search/kb/` |
| Chat lane: claude-haiku-4-5 only | `QUERY_LLM_MODEL = "claude-haiku-4-5"`; `routes_chat.py` reaches its LLM only via `_CLAUDE, "-p"` — no HTTP client, no fallback engine |
| `claude -p` has exactly one caller | `routes_chat.py`. A second one is a new lane and needs a row in §1 first |
| Profile selection reads usage, not completions | `core/claude_profiles.py` opens `/api/oauth/usage` only |
| CLAUDE_CODE_SAFE_MODE=1 in subprocess | `subprocess_env()` in daemon includes `CLAUDE_CODE_SAFE_MODE=1` |

---

## See also

- `src/rag_search/core/claude_profiles.py` — `PROFILES`, `utilization`, `pick_profile`, `subprocess_env`
- `src/rag_search/server/routes_chat.py` — the sole `claude -p` caller
