#!/usr/bin/env bash
# Install a git pre-commit hook that runs configure_integrations.py --check.
# Idempotent: safe to run multiple times.
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
HOOK_PATH="$REPO_ROOT/.git/hooks/pre-commit"

cat > "$HOOK_PATH" <<'HOOK'
#!/usr/bin/env bash
# opencode-search pre-commit: verify AI client config is up to date.
# Bypass with: git commit --no-verify
set -euo pipefail
REPO_ROOT="$(git rev-parse --show-toplevel)"
exec "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/scripts/configure_integrations.py" --check
HOOK

chmod 755 "$HOOK_PATH"
echo "Installed pre-commit hook → $HOOK_PATH"
echo "Runs: .venv/bin/python scripts/configure_integrations.py --check"
echo "Bypass with: git commit --no-verify"
