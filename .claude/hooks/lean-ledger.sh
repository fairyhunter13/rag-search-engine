#!/usr/bin/env bash
# PostToolUse audit — appends one JSONL line per Edit/Write to lean-ledger.jsonl,
# capturing the ack reason so every accepted change leaves a why-trail. Never blocks.
set -euo pipefail
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
LEDGER="${REPO_ROOT}/.claude/lean-ledger.jsonl"
ACK_FILE="${REPO_ROOT}/.claude/.lean-ack.json"
INPUT=$(cat)
echo "$INPUT" | python3 -c "
import sys, json, datetime, os
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
ti = d.get('tool_input', {})
reason = ''
try:
    reason = json.load(open('${ACK_FILE}')).get('reason', '')
except Exception:
    pass
rec = {
    'ts': datetime.datetime.now().isoformat(timespec='seconds'),
    'tool': d.get('tool_name', ''),
    'file': ti.get('file_path', '') or ti.get('path', ''),
    'reason': reason,
}
with open('${LEDGER}', 'a') as fh:
    fh.write(json.dumps(rec) + os.linesep)
" 2>/dev/null || true
exit 0
