#!/usr/bin/env bash
# PostCompact hook: after compaction, re-inject key project rules as additionalContext
# so Claude doesn't forget them after context reset.

python3 -c "
import json
ctx = '''=== Post-Compaction Reminder ===
You are working on opencode-search-engine (GPU-accelerated code intelligence MCP server).
Critical rules that survived compaction:
1. MANDATORY: call search()/ask()/overview() BEFORE any Bash grep/find/Read for code lookup
2. GPU-only inference — CPU fallback is FORBIDDEN; must raise fatal error, not fall back silently
3. No mocks in tests — all tests use real daemon, real GPU (RTX 5080). No local generative LLM.
4. Push after EVERY commit (zero-unpushed policy — learned from Jun 9 2026 data loss)
5. Never auto-index — only call build() when user explicitly asks
6. LLM lanes: GPU = embeddings + reranking ONLY (FastEmbed/ONNX/CUDA); KB build = cloud DeepSeek-only (crash if no key); dashboard chat = claude-haiku-4-5 primary + DeepSeek fallback
7. astro-project: 20064 files, 97480 chunks, 4599 L1 communities (flat; no L2/L3 since WS-B 2026-06-26)
8. World model: docs/world-model/model.yaml (L1-L4); check: python scripts/check_world_model.py
==='''
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PostCompact',
        'additionalContext': ctx
    }
}))
" 2>/dev/null || echo '{}'
