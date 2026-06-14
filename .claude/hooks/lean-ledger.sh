#!/usr/bin/env bash
# PostToolUse audit — appends one JSONL line per Edit/Write to lean-ledger.jsonl,
# capturing the ack reason so every accepted change leaves a why-trail. Never blocks.
set -euo pipefail
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
LEDGER="${REPO_ROOT}/.claude/lean-ledger.jsonl"
INPUT=$(cat)
echo "$INPUT" | python3 -c "
import sys, json, datetime, os
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
ti = d.get('tool_input', {})
new = ti.get('new_string', '') or ti.get('content', '')
old = ti.get('old_string', '')
net = len(new.splitlines()) - len(old.splitlines()) if (new or old) else 0
rec = {
    'ts': datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z',
    'tool': d.get('tool_name', ''),
    'file': ti.get('file_path', '') or ti.get('path', ''),
    'netLines': net,
}
with open('${LEDGER}', 'a') as fh:
    fh.write(json.dumps(rec) + os.linesep)
" 2>/dev/null || true
exit 0
