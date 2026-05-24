#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
  OPENCODE_BIN="$ROOT/.venv/bin/opencode-search"
else
  PYTHON_BIN="python3"
  OPENCODE_BIN="opencode-search"
fi

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

export OPENCODE_REGISTRY_PATH="$TMPDIR/registry.json"
export OPENCODE_BIN
# Optional structural weights (no keyword-based heuristics): prefer implementation
# code over docs/scripts for fact-finding queries.
export OPENCODE_WEIGHT_SRC="2.0"
export OPENCODE_WEIGHT_DOCS="0.1"
export OPENCODE_WEIGHT_SCRIPTS="0.1"
export OPENCODE_WEIGHT_DOCUMENT_LANGUAGE="0.1"

PROJECT_DIR="$TMPDIR/project"
mkdir -p "$PROJECT_DIR"

cat > "$PROJECT_DIR/app.py" <<'PY'
SMOKE_CLI_INITIAL = "cli_alpha_unique"

def smoke_initial():
    return SMOKE_CLI_INITIAL
PY

# Regression guard: stale docs must not outrank implementation code for
# question-like queries.
mkdir -p "$PROJECT_DIR/src" "$PROJECT_DIR/docs"
cat > "$PROJECT_DIR/src/config.py" <<'PY'
# Source of truth (current implementation)
REGISTRY_PATH = "~/.local/share/opencode-search/projects.json"
PY
cat > "$PROJECT_DIR/docs/MIGRATION_PLAN.md" <<'MD'
# Migration Plan (stale)

The project registry is stored at `~/.opencode/projects.json`.
MD

"$OPENCODE_BIN" health --json > "$TMPDIR/health.json"
"$PYTHON_BIN" - <<'PY' "$TMPDIR/health.json"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

assert data["gpu_ok"] is True, data
PY

"$OPENCODE_BIN" index "$PROJECT_DIR" --tier budget --json > "$TMPDIR/index.json"
"$PYTHON_BIN" - <<'PY' "$TMPDIR/index.json"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

assert data["status"] == "ok", data
assert data["files_indexed"] >= 1, data
PY

"$OPENCODE_BIN" search "cli_alpha_unique" --project "$PROJECT_DIR" --no-rerank --json > "$TMPDIR/search-initial.json"
"$PYTHON_BIN" - <<'PY' "$TMPDIR/search-initial.json"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

assert data["results"], data
assert any("cli_alpha_unique" in row["content"] for row in data["results"]), data
PY

"$OPENCODE_BIN" search "Where is the registry of indexed projects stored and what format is it?" --project "$PROJECT_DIR" --no-rerank --json > "$TMPDIR/search-stale-docs.json"
"$PYTHON_BIN" - <<'PY' "$TMPDIR/search-stale-docs.json"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

assert data["results"], data
paths = [row["path"] for row in data["results"]]
config_idx = next((i for i, p in enumerate(paths) if p.endswith("src/config.py")), None)
assert config_idx is not None, data
doc_idx = next((i for i, p in enumerate(paths) if p.endswith("docs/MIGRATION_PLAN.md")), None)
assert doc_idx is None or config_idx < doc_idx, data
PY

"$OPENCODE_BIN" status "$PROJECT_DIR" --json > "$TMPDIR/status.json"
"$PYTHON_BIN" - <<'PY' "$TMPDIR/status.json" "$PROJECT_DIR"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

assert data["indexed"] is True, data
assert data["path"] == sys.argv[2], data
assert data["chunks"] >= 1, data
PY

"$OPENCODE_BIN" list --json > "$TMPDIR/list.json"
"$PYTHON_BIN" - <<'PY' "$TMPDIR/list.json" "$PROJECT_DIR"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

assert any(row["path"] == sys.argv[2] for row in data["projects"]), data
PY

"$PYTHON_BIN" - <<'PY'
import os
import subprocess
import sys
import time

cmd = [os.environ["OPENCODE_BIN"], "mcp"]
proc = subprocess.Popen(
    cmd,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
try:
    time.sleep(2.0)
    if proc.poll() is not None:
        stderr = proc.stderr.read() if proc.stderr else ""
        raise AssertionError(f"MCP command exited early: {stderr}")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
PY

"$OPENCODE_BIN" watch "$PROJECT_DIR" --tier budget > "$TMPDIR/watch.log" 2>&1 &
WATCH_PID=$!

"$PYTHON_BIN" - <<'PY' "$OPENCODE_REGISTRY_PATH" "$PROJECT_DIR"
import json
import sys
import time

registry_path, project_dir = sys.argv[1:3]
deadline = time.time() + 20
while time.time() < deadline:
    try:
        with open(registry_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        time.sleep(0.5)
        continue
    entry = data.get(project_dir)
    if entry and entry.get("watch") is True:
        break
    time.sleep(0.5)
else:
    raise AssertionError("watch command never persisted watch=true")
PY

sleep 1

cat > "$PROJECT_DIR/app.py" <<'PY'
SMOKE_CLI_UPDATED = "cli_beta_unique"

def smoke_updated():
    return SMOKE_CLI_UPDATED
PY

"$PYTHON_BIN" - <<'PY' "$OPENCODE_BIN" "$PROJECT_DIR"
import json
import subprocess
import sys
import time

opencode_bin, project_dir = sys.argv[1:3]
cmd = [opencode_bin, "search", "cli_beta_unique", "--project", project_dir, "--no-rerank", "--json"]
deadline = time.time() + 20
while time.time() < deadline:
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    data = json.loads(proc.stdout)
    if any("cli_beta_unique" in row["content"] for row in data.get("results", [])):
        break
    time.sleep(0.5)
else:
    raise AssertionError("watcher never reindexed updated content")
PY

"$OPENCODE_BIN" stop-watching "$PROJECT_DIR" --json > "$TMPDIR/stop.json"
"$PYTHON_BIN" - <<'PY' "$TMPDIR/stop.json"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

assert data["status"] == "stopped", data
PY

"$PYTHON_BIN" - <<'PY' "$WATCH_PID"
import os
import signal
import sys
import time

pid = int(sys.argv[1])
deadline = time.time() + 20
while time.time() < deadline:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        break
    time.sleep(0.5)
else:
    os.kill(pid, signal.SIGTERM)
    raise AssertionError("watch process did not exit after stop-watching")
PY

cat > "$PROJECT_DIR/app.py" <<'PY'
SMOKE_CLI_STOPPED = "cli_gamma_unique"

def smoke_stopped():
    return SMOKE_CLI_STOPPED
PY

sleep 2
"$OPENCODE_BIN" search "cli_gamma_unique" --project "$PROJECT_DIR" --no-rerank --json > "$TMPDIR/search-stopped.json"
"$PYTHON_BIN" - <<'PY' "$TMPDIR/search-stopped.json"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

assert not any("cli_gamma_unique" in row.get("content", "") for row in data.get("results", [])), data
PY
