# LLM Driver Doctrine (June 2026)

> **Status:** reference — locked. Every change to the LLM lane assignments requires updating this file + memories + skills.

---

## 1. Lane map (locked)

| Lane | Engine | Used for | CPU fallback? |
|---|---|---|---|
| **Embeddings + rerank** | **GPU** (FastEmbed/ONNX/CUDA) | Vector search (`search`) + cross-encoder rerank ONLY | **Fatal** — raises at startup |
| **Dashboard chat** | **claude-haiku-4-5** via `claude -p` | Interactive chat answers via `/api/chat_stream` | N/A — cloud |

**Two lanes, and that is the whole map.** The doc-tooling lane (`docgen`, `okf`) and the
KB-enrichment lane (DeepSeek narration, wiki pages, BPRE process linkage) were both deleted with
tier 3 on 2026-07-28. `DEEPSEEK_API_KEY` has no reader left anywhere in the repo, and `claude -p`
has exactly one caller: dashboard chat.

No other lane assignments are permitted. In particular:
- **no generative LLM may be added to the index, extraction or retrieval path** — that is what
  the tier-3 deletion bought, and re-adding one is a new lane needing a row here first
- no cloud LLM may serve dashboard chat except `claude -p`; there is no fallback on that path
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
| GPU lane: exactly two residents | `EMBED_MODEL` + `RERANK_MODEL` are the only model ids reaching the ONNX loader — nothing generative is loadable on-device |
| No third lane | no module in `src/rag_search/` opens an LLM URL, and `DEEPSEEK_API_KEY` has no reader (the tier-3 deletion, 2026-07-28) |
| Chat lane: claude-haiku-4-5 only | `QUERY_LLM_MODEL = "claude-haiku-4-5"`; `routes_chat.py` reaches its LLM only via `_CLAUDE, "-p"` — no HTTP client, no fallback engine |
| `claude -p` has exactly one caller | `routes_chat.py`. A second one is a new lane and needs a row in §1 first |
| Profile selection reads usage, not completions | `core/claude_profiles.py` opens `/api/oauth/usage` only |
| CLAUDE_CODE_SAFE_MODE=1 in subprocess | `subprocess_env()` in daemon includes `CLAUDE_CODE_SAFE_MODE=1` |

---

## See also

- `src/rag_search/core/claude_profiles.py` — `PROFILES`, `utilization`, `pick_profile`, `subprocess_env`
- `src/rag_search/server/routes_chat.py` — the sole `claude -p` caller
