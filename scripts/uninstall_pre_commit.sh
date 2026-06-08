#!/usr/bin/env bash
# Remove the opencode-search git pre-commit hook.
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
HOOK_PATH="$REPO_ROOT/.git/hooks/pre-commit"

if [ -f "$HOOK_PATH" ]; then
    rm -f "$HOOK_PATH"
    echo "Removed pre-commit hook: $HOOK_PATH"
else
    echo "No pre-commit hook found at $HOOK_PATH (nothing to remove)"
fi
