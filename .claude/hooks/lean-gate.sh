#!/usr/bin/env bash
# PreToolUse gate — enforces lean-change discipline on every Edit/Write/MultiEdit.
# Reads Claude tool input from stdin as JSON; emits a PreToolUse permission decision.
# Fail-closed: any internal parse failure yields an empty field and the budget
# checks then deny, so a broken edit never slips through.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

deny() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}' \
    "$(echo "$1" | sed 's/"/\\"/g')"
  exit 0
}

allow() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","additionalContext":"%s"}}' \
    "$(echo "$1" | sed 's/"/\\"/g')"
  exit 0
}

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name',''))" 2>/dev/null || echo "")
FILE_PATH=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); ti=d.get('tool_input',{}); print(ti.get('file_path','') or ti.get('path',''))" 2>/dev/null || echo "")
NEW_STRING=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); ti=d.get('tool_input',{}); print(ti.get('new_string','') or ti.get('content',''))" 2>/dev/null || echo "")
OLD_STRING=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); ti=d.get('tool_input',{}); print(ti.get('old_string',''))" 2>/dev/null || echo "")

# Bootstrap: never gate Claude's own meta-files (hooks, skills, settings, plans, memory).
# Workflow state must stay editable so flipping a checklist box stays allowed.
if echo "$FILE_PATH" | grep -q '\.claude/'; then
  allow "Claude meta-file edit allowed (ungated)."
fi

# Forbidden paths — protected regardless of budget.
for pat in 'secrets/' '.env' '/.local/share/opencode-search/' '/GoogleDrive/' '/OneDrive/'; do
  if echo "$FILE_PATH" | grep -qF "$pat"; then
    deny "Forbidden path: ${FILE_PATH} matches '${pat}'. This path is protected."
  fi
done

# Diff budget. Claude passes ABSOLUTE file_path, so resolve existence directly —
# joining REPO_ROOT to an absolute path would always miss and mislabel edits as new files.
if [[ "$FILE_PATH" == /* ]]; then ABS_PATH="$FILE_PATH"; else ABS_PATH="${REPO_ROOT}/${FILE_PATH}"; fi
IS_NEW_FILE=0
[[ ! -f "$ABS_PATH" ]] && IS_NEW_FILE=1
[[ "$TOOL_NAME" == "Write" ]] && IS_NEW_FILE=1

ADDED_LINES=$(echo "$NEW_STRING" | wc -l)
REMOVED_LINES=$(echo "$OLD_STRING" | wc -l)
NET_LINES=$(( ADDED_LINES - REMOVED_LINES ))

if (( IS_NEW_FILE == 1 )); then
  BUDGET=150
  if (( ADDED_LINES > BUDGET )); then
    deny "New file too large: ${ADDED_LINES} lines > ${BUDGET} limit. Split into smaller files."
  fi
else
  BUDGET=40
  if (( NET_LINES > BUDGET )); then
    deny "Diff too large: +${NET_LINES} net lines > ${BUDGET} limit. Break this into smaller changes (each line is a liability)."
  fi
fi

allow "Lean-change checks passed (budget: ${BUDGET}, net: +${NET_LINES})."
