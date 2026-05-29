#!/usr/bin/env bash
# run_all_checks.sh — Run the full opencode-search health + test suite.
#
# Usage:
#   ./scripts/run_all_checks.sh            # full run
#   SKIP_SYSTEM_CHECK=1 ./scripts/run_all_checks.sh  # skip check_system.py
#
# Exit codes:
#   0 — all required checks pass (E2E tests may be skipped if deps absent)
#   1 — one or more required checks failed

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"
VENV_PYTEST="${REPO_ROOT}/.venv/bin/pytest"

# Verify the venv exists
if [[ ! -x "${VENV_PYTHON}" ]]; then
    echo "ERROR: .venv not found at ${REPO_ROOT}/.venv"
    echo "       Run: python -m venv .venv && .venv/bin/pip install -e 'src/[dev]'"
    exit 1
fi

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

_run() {
    local label="$1"; shift
    echo ""
    echo "────────────────────────────────────────────────────────────"
    echo "▶  ${label}"
    echo "────────────────────────────────────────────────────────────"
    if "$@"; then
        echo "✓  ${label} — PASSED"
        ((PASS_COUNT++)) || true
    else
        local exit_code=$?
        if [[ ${exit_code} -eq 5 ]]; then
            # pytest exit code 5 = no tests collected (all skipped)
            echo "~  ${label} — SKIPPED (no tests collected)"
            ((SKIP_COUNT++)) || true
        else
            echo "✗  ${label} — FAILED (exit ${exit_code})"
            ((FAIL_COUNT++)) || true
        fi
    fi
}

# ---------------------------------------------------------------------------
# 1. System health checklist
# ---------------------------------------------------------------------------
if [[ "${SKIP_SYSTEM_CHECK:-0}" != "1" ]]; then
    _run "System health checklist" \
        "${VENV_PYTHON}" "${REPO_ROOT}/scripts/check_system.py"
fi

# ---------------------------------------------------------------------------
# 2. MCP tool registration unit tests
# ---------------------------------------------------------------------------
_run "MCP tools registered (test_mcp_tools_registered.py)" \
    "${VENV_PYTEST}" \
        "${REPO_ROOT}/src/tests/test_mcp_tools_registered.py" \
        -v --tb=short

# ---------------------------------------------------------------------------
# 3. MCP bridge tool registration tests
# ---------------------------------------------------------------------------
_run "MCP bridge tools registered (test_mcp_bridge_tools.py)" \
    "${VENV_PYTEST}" \
        "${REPO_ROOT}/src/tests/test_mcp_bridge_tools.py" \
        -v --tb=short

# ---------------------------------------------------------------------------
# 4. E2E: Claude Code CLI (skipped gracefully if claude not installed)
# ---------------------------------------------------------------------------
_run "E2E: ClaudeCodeClient (test_e2e_mcp_claude_code.py)" \
    "${VENV_PYTEST}" \
        "${REPO_ROOT}/src/tests/test_e2e_mcp_claude_code.py" \
        -v --tb=short || true
# Note: `|| true` above prevents set -e from aborting on skip exit code.
# The _run helper already handles the failure accounting.

# ---------------------------------------------------------------------------
# 5. E2E: OpenAI / Anthropic APIs (skipped gracefully if keys not set)
# ---------------------------------------------------------------------------
_run "E2E: OpenAI + Anthropic clients (test_e2e_mcp_openai.py)" \
    "${VENV_PYTEST}" \
        "${REPO_ROOT}/src/tests/test_e2e_mcp_openai.py" \
        -v --tb=short || true

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "════════════════════════════════════════════════════════════"
echo "  SUMMARY: ${PASS_COUNT} passed  |  ${FAIL_COUNT} failed  |  ${SKIP_COUNT} skipped"
echo "════════════════════════════════════════════════════════════"

if [[ ${FAIL_COUNT} -gt 0 ]]; then
    echo "RESULT: FAILED"
    exit 1
fi
echo "RESULT: OK"
exit 0
