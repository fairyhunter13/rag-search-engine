#!/usr/bin/env bash
# PreToolUse: search-first soft gate.
# Deny Grep tool calls and Bash grep/find/rg commands if no opencode-search call
# has run this session yet. After one search (or timeout/fallback), the marker file
# exists and native tools are allowed freely (0 tokens).
# Glob (file enumeration) and Read (named file) are never gated.
set -euo pipefail

deny() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}' \
    "$(echo "$1" | sed 's/"/\\"/g')"
  exit 0
}
allow() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
  exit 0
}

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id','unknown'))" 2>/dev/null || echo "unknown")
TOOL_NAME=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name',''))" 2>/dev/null || echo "")
SAFE="${SESSION_ID//[^a-zA-Z0-9_-]/}"
MARKER="/tmp/ocs-searched-${SAFE:-unknown}"

# Glob = file enumeration (not content search) — never gated
[[ "$TOOL_NAME" == "Glob" ]] && allow

# For Bash: only gate commands that look like content searches
if [[ "$TOOL_NAME" == "Bash" ]]; then
  CMD=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('command',''))" 2>/dev/null || echo "")
  echo "$CMD" | grep -qE '^\s*(grep|find|rg|ag|fgrep|egrep)\b' || allow
fi
# Grep tool: always a content search — fall through to marker check

# Marker present → search already ran this session → allow freely (0 tok)
[[ -f "$MARKER" ]] && allow

# No search yet — deny with pointer (resilience: timeout counts as "searched")
deny "Call opencode-search first (search/ask/graph/overview). If it times out or fails, that counts — native tools are free after that."
