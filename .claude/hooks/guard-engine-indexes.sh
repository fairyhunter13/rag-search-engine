#!/usr/bin/env bash
# PreToolUse guard: block accidental deletion of engine index directories
# Reads tool input JSON from stdin; exits 2 to block, 0 to allow.

INPUT=$(cat)
CMD=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('command', ''))
except Exception:
    print('')
" 2>/dev/null)

# Block rm -rf targeting engine indexes
if echo "$CMD" | grep -qE "rm[[:space:]].*-[a-zA-Z]*r[a-zA-Z]*f|rm[[:space:]].*-[a-zA-Z]*f[a-zA-Z]*r"; then
    if echo "$CMD" | grep -qE "rag-search/(indexes|index_[a-z]+)|local/share/rag-search/(indexes|index_[a-z]+)"; then
        echo '{"decision":"block","reason":"Blocked: use manage(action=vacuum) or ocs clean-orphans to remove engine indexes safely — direct rm bypasses orphan detection."}'
        exit 2
    fi
fi

exit 0
