#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
else
  PYTHON_BIN="python3"
fi

LOCK_FILE="$ROOT/requirements-lock-py312-linux-gpu.txt"

"$PYTHON_BIN" - <<'PY'
import sys
from pathlib import Path

expected_major = 3
expected_minor = 12
if sys.version_info[:2] != (expected_major, expected_minor):
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    raise SystemExit(
        "requirements-lock-py312-linux-gpu.txt must be refreshed with "
        f"Python {expected_major}.{expected_minor}; active Python is {version}"
    )

if Path(sys.prefix).name != ".venv":
    raise SystemExit("refresh-lock expects the repo-local .venv to be active or present")
PY

"$PYTHON_BIN" -m pip freeze --exclude-editable | sort > "$LOCK_FILE"
echo "Wrote $LOCK_FILE"
