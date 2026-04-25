#!/bin/bash
# Configure git to use tracked hooks from .githooks/
# Run once after clone: bash .githooks/setup.sh

set -e
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo '.')"
cd "$REPO_ROOT"
git config core.hooksPath .githooks
echo "Git hooks configured: core.hooksPath = .githooks"
