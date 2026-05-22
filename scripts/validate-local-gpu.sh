#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export OPENCODE_FAIL_ON_SKIP=1

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
  RUFF_BIN="$ROOT/.venv/bin/ruff"
  PYTEST_BIN="$ROOT/.venv/bin/pytest"
else
  PYTHON_BIN="python3"
  RUFF_BIN="ruff"
  PYTEST_BIN="pytest"
fi

"$PYTHON_BIN" - <<'PY'
import importlib
import sys

required = [
    "lancedb",
    "pyarrow",
    "cachetools",
    "typer",
    "mcp",
    "watchdog",
    "chonkie",
    "langchain_text_splitters",
]
missing = []
for name in required:
    try:
        importlib.import_module(name)
    except Exception:
        missing.append(name)

if missing:
    print("Missing required runtime packages:", ", ".join(missing), file=sys.stderr)
    print('Install them with: pip install -e "src/[dev]"', file=sys.stderr)
    sys.exit(1)

from opencode_search.embeddings import assert_gpu_available

try:
    assert_gpu_available()
except Exception as exc:  # noqa: BLE001
    print(f"GPU validation failed: {exc}", file=sys.stderr)
    sys.exit(1)
PY

"$RUFF_BIN" check src/opencode_search src/tests tests
"$PYTHON_BIN" -m compileall -q src/opencode_search src/tests
"$PYTEST_BIN" -q src/tests
"$ROOT/scripts/e2e-smoke.sh"
