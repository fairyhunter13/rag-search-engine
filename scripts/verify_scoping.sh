#!/usr/bin/env bash
set -euo pipefail

# Focused verification for the guarantees we care about:
# - MCP stdio bridge workspace scoping (prevents cross-workspace indexing/search)
# - Federated search (explicit project_paths) and symlink indexing
# - Watcher behavior for symlinked directories (real path translated back to symlink path)
#
# Run from repo root in a venv that has the dev deps installed:
#   python3 -m venv .venv
#   source .venv/bin/activate
#   pip install -e "src/[dev]"
#
# Notes:
# - This script is strict: it will fail on any test failure or skip.

if [[ ! -x ".venv/bin/pytest" ]]; then
  echo "ERROR: .venv/bin/pytest not found. Create a venv and install deps first:" >&2
  echo "  python3 -m venv .venv" >&2
  echo "  .venv/bin/python -m pip install -e \"src[dev]\"" >&2
  exit 1
fi

.venv/bin/pytest -q -rs \
  src/tests/test_mcp_bridge_scoping.py \
  src/tests/test_index_flow_integration.py \
  src/tests/test_watcher.py
