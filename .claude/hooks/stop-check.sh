#!/usr/bin/env bash
# Stop hook: check for unpushed commits and surface as additionalContext.
# Returns non-blocking feedback — never blocks the stop, just informs.

UNPUSHED=$(git -C /home/user/git/github.com/fairyhunter13/opencode-search-engine log --oneline @{u}.. 2>/dev/null)
UNCOMMITTED=$(git -C /home/user/git/github.com/fairyhunter13/opencode-search-engine status --short 2>/dev/null | grep -v "^??" | head -5)
DIRTY=$(git -C /home/user/git/github.com/fairyhunter13/opencode-search-engine status --short 2>/dev/null | wc -l | tr -d ' ')

if [ -n "$UNPUSHED" ] || [ -n "$UNCOMMITTED" ]; then
    MSG=""
    [ -n "$UNCOMMITTED" ] && MSG="${MSG}Uncommitted changes:\n${UNCOMMITTED}\n"
    [ -n "$UNPUSHED" ] && MSG="${MSG}Unpushed commits:\n${UNPUSHED}\n"
    MSG="${MSG}Policy: push after every commit. Consider: git add -A && git commit && git push"
    python3 -c "
import json
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'Stop',
        'additionalContext': '''${MSG}'''
    }
}))
" 2>/dev/null
fi

exit 0
