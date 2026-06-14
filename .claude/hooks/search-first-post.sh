#!/usr/bin/env bash
# PostToolUse: after any opencode-search MCP call, set the "searched" marker for
# this session. The PreToolUse search-first gate checks for this marker.
# Fires on ANY result including timeout/fallback — so a down daemon never traps native.
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id','unknown'))" 2>/dev/null || echo "unknown")
SAFE="${SESSION_ID//[^a-zA-Z0-9_-]/}"
touch "/tmp/ocs-searched-${SAFE:-unknown}" 2>/dev/null || true
# No output → 0 tokens
