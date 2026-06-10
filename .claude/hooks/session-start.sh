#!/usr/bin/env bash
# SessionStart hook: set session title + inject context as additionalContext.

PROJ="opencode-search-engine"
if curl -sf http://localhost:8765/healthz 2>/dev/null | grep -q '"ok":true'; then
    DAEMON_UP="up"
else
    DAEMON_UP="DOWN"
fi
GPU=$(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | awk -F', ' '{printf "%s%% GPU %sMB/%sMB", $1, $2, $3}' || echo "unavailable")
BRANCH=$(git -C /home/user/git/github.com/fairyhunter13/opencode-search-engine rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")
UNPUSHED=$(git -C /home/user/git/github.com/fairyhunter13/opencode-search-engine log --oneline @{u}.. 2>/dev/null | wc -l | tr -d ' ')
TITLE="${PROJ} | ${BRANCH} | daemon:${DAEMON_UP}"

python3 - <<PYEOF
import json
ctx = """=== Session Context (${PROJ}) ===
Branch: ${BRANCH} | Daemon: ${DAEMON_UP} | GPU: ${GPU}
Unpushed commits: ${UNPUSHED}
Rules: push after every commit; no mocks; GPU-only inference; no CPU fallback
MCP: call search/ask/overview BEFORE bash grep/find/Read
==="""
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "sessionTitle": "${TITLE}",
        "additionalContext": ctx.strip()
    }
}))
PYEOF
