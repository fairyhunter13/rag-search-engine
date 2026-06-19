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
3. No mocks in tests — all tests use real daemon, real Ollama, real GPU (RTX 5080)
4. Push after EVERY commit (zero-unpushed policy — learned from Jun 9 2026 data loss)
5. Never auto-index — only call build() when user explicitly asks
6. LLM lanes: local qwen3-enrich:1.7b (summaries, think=false) + cloud DeepSeek (classification + wiki); claude-haiku-4-5 for dashboard chat (codex removed)
7. redacted-name-3: 20064 files, 97480 chunks, 4599 communities, 3-level hierarchy — all healthy
==='''
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PostCompact',
        'additionalContext': ctx
    }
}))
" 2>/dev/null || echo '{}'
