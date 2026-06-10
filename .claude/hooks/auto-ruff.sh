#!/usr/bin/env bash
# PostToolUse: run ruff --fix on any .py file just written or edited.
# Reads tool input JSON from stdin. Non-blocking (always exits 0).

INPUT=$(cat)
FILE=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    # Write tool uses file_path; Edit tool uses file_path
    print(d.get('file_path', ''))
except Exception:
    print('')
" 2>/dev/null)

if [[ "$FILE" == *.py ]] && [[ -f "$FILE" ]]; then
    cd /home/user/git/github.com/fairyhunter13/opencode-search-engine
    .venv/bin/ruff check --fix --quiet "$FILE" 2>/dev/null || true
fi

exit 0
