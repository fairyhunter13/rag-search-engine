#!/usr/bin/env bash
set -u

# Run as many test suites as possible:
# - Do NOT skip tests (treat skips as failures where supported)
# - Continue even if a suite fails
# - Print a summary at the end
#
# Usage:
#   scripts/run_all_tests_noskip_keepgoing.sh
#
# Notes:
# - Some E2E tests require external services (GPU embedder/indexer/daemon). When
#   those are not available, tests may fail (by design when OPENCODE_FAIL_ON_SKIP=1).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="${ROOT_DIR}/.venv/bin/python"

if [[ ! -x "${VENV_PY}" ]]; then
  echo "ERROR: venv python not found at ${VENV_PY}"
  echo "Create it with: python3 -m venv .venv && .venv/bin/pip install -r requirements..."
  exit 2
fi

export PYTHONPATH="${ROOT_DIR}/src"
export OPENCODE_FAIL_ON_SKIP=1

failures=0

_embedder_pid=""
_indexer_pid=""

cleanup() {
  set +e
  if [[ -n "${_embedder_pid}" ]]; then
    kill "${_embedder_pid}" 2>/dev/null || true
  fi
  if [[ -n "${_indexer_pid}" ]]; then
    kill "${_indexer_pid}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

start_services() {
  # Start embedder compat server (HTTP :9998)
  "${VENV_PY}" -m opencode_embedder.server >/tmp/opencode_embedder_compat.log 2>&1 &
  _embedder_pid="$!"

  # Start indexer shim (abstract socket @opencode-indexer)
  "${VENV_PY}" "${ROOT_DIR}/scripts/opencode-indexer-shim.py" >/tmp/opencode_indexer_shim.log 2>&1 &
  _indexer_pid="$!"

  # Give servers a moment to bind sockets/ports.
  sleep 0.5
}

run_suite() {
  local name="$1"
  shift
  echo
  echo "=== ${name} ==="
  set +e
  "$@"
  local rc=$?
  set -e
  if [[ $rc -ne 0 ]]; then
    echo "!!! ${name} FAILED (exit=${rc})"
    failures=$((failures + 1))
  else
    echo ">>> ${name} OK"
  fi
}

set -e

start_services

run_suite "Unit/Integration (src/tests)" "${VENV_PY}" -m pytest -q src/tests
run_suite "E2E (tests/)" "${VENV_PY}" -m pytest -q tests
run_suite "Deterministic MCP Harness" "${VENV_PY}" scripts/mcp_stdio_harness.py

echo
if [[ $failures -ne 0 ]]; then
  echo "SUMMARY: ${failures} suite(s) failed."
  exit 1
fi
echo "SUMMARY: all suites passed."
