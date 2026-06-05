#!/usr/bin/env python3
"""Configure all system integrations for opencode-search MCP tools.

Writes/verifies configs for: codex, claude code, opencode.
Skips hermes (binary not installed).

Usage:
    .venv/bin/python scripts/configure_integrations.py           # configure + verify
    .venv/bin/python scripts/configure_integrations.py --check   # verify only (no writes)
    .venv/bin/python scripts/configure_integrations.py --json    # JSON output

Exit codes: 0 = all configured, 1 = some missing
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO = Path(__file__).parent.parent
_VENV_PYTHON = _REPO / ".venv" / "bin" / "python"
_MCP_COMMAND = str(_VENV_PYTHON)
_MCP_ARGS = ["-m", "opencode_search", "daemon", "bridge-stdio"]

AGENTS_MD_CONTENT = """\
# Code Intelligence Tools (opencode-search)

You have access to the **opencode-search** MCP server for deep code intelligence.
Use these tools for ALL code exploration tasks — they are faster and more accurate than grep or file reading.

## Tools Available

- **search(query)** — Find specific code, files, functions. Use BEFORE grep/find/Read.
- **ask(query, project_path)** — Answer 'how does X work?', architecture, business logic.
- **graph(symbol, project_path, relation)** — Call graph: callers, callees, impact, path tracing.
- **overview(project_path, what)** — Project structure, languages, communities, patterns.
- **build(project_path, action)** — Index or rebuild KB. Only when user explicitly asks.
- **federation(root_path)** — Manage multi-repo monorepo federation.
- **manage(project_path, action)** — Stop watching, wiki lint.

## Decision Guide

| Task | Tool |
|------|------|
| Find payment handler | `search("payment handler")` |
| How does auth work? | `ask("how does auth work", project_path)` |
| What calls ProcessOrder? | `graph("ProcessOrder", project_path, relation="callers")` |
| Tell me about this project | `overview(project_path, what="structure")` |
| Index this project | `build(project_path, action="pipeline")` |

## Rules

- Call **search** BEFORE any grep, find, or file read for code lookup.
- Use **ask** for architecture/how questions; **search** for specific code.
- NEVER auto-index. Only call `build` when the user explicitly asks.
"""


@dataclass
class ConfigResult:
    tool: str
    status: str  # "configured" | "already_ok" | "missing" | "skipped" | "error"
    message: str
    path: str = ""


def configure_codex(check_only: bool = False) -> ConfigResult:
    """Write/verify codex MCP server config in ~/.codex/config.toml."""
    config_path = Path.home() / ".codex" / "config.toml"

    # Read existing config
    existing: dict = {}
    if config_path.exists():
        try:
            if sys.version_info >= (3, 11):
                import tomllib
                existing = tomllib.loads(config_path.read_text())
            else:
                try:
                    import tomllib
                    existing = tomllib.loads(config_path.read_text())
                except ImportError:
                    # Manual parse just enough to check if key exists
                    text = config_path.read_text()
                    if "opencode-search" in text:
                        return ConfigResult(
                            tool="codex", status="already_ok",
                            message="opencode-search already present in codex config.toml",
                            path=str(config_path),
                        )
        except Exception as exc:
            return ConfigResult(
                tool="codex", status="error",
                message=f"Failed to read {config_path}: {exc}",
                path=str(config_path),
            )

    # Check if already configured
    mcp_servers = existing.get("mcp_servers", {})
    if "opencode-search" in mcp_servers:
        existing_cmd = mcp_servers["opencode-search"].get("command", "")
        if str(_VENV_PYTHON) in str(existing_cmd) or "opencode_search" in str(existing_cmd):
            return ConfigResult(
                tool="codex", status="already_ok",
                message="opencode-search already configured in codex config.toml",
                path=str(config_path),
            )

    if check_only:
        return ConfigResult(
            tool="codex", status="missing",
            message=f"opencode-search NOT in codex config.toml ({config_path})",
            path=str(config_path),
        )

    # Write new config (merge, don't overwrite)
    try:
        import tomli_w
        existing.setdefault("mcp_servers", {})
        existing["mcp_servers"]["opencode-search"] = {
            "command": _MCP_COMMAND,
            "args": _MCP_ARGS,
        }
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_bytes(tomli_w.dumps(existing).encode())
        return ConfigResult(
            tool="codex", status="configured",
            message=f"Wrote opencode-search MCP entry to {config_path}",
            path=str(config_path),
        )
    except ImportError:
        # tomli_w not installed — write raw TOML manually
        config_path.parent.mkdir(parents=True, exist_ok=True)
        toml_snippet = (
            '\n[mcp_servers.opencode-search]\n'
            f'command = "{_MCP_COMMAND}"\n'
            f'args = {json.dumps(_MCP_ARGS)}\n'
        )
        existing_text = config_path.read_text() if config_path.exists() else ""
        if "opencode-search" not in existing_text:
            config_path.write_text(existing_text + toml_snippet)
        return ConfigResult(
            tool="codex", status="configured",
            message=f"Appended opencode-search MCP entry to {config_path} (manual TOML)",
            path=str(config_path),
        )
    except Exception as exc:
        return ConfigResult(
            tool="codex", status="error",
            message=f"Failed to write codex config: {exc}",
            path=str(config_path),
        )


def configure_codex_agents_md(check_only: bool = False) -> ConfigResult:
    """Write ~/.codex/AGENTS.md with opencode-search system prompt."""
    agents_path = Path.home() / ".codex" / "AGENTS.md"

    if agents_path.exists() and "opencode-search" in agents_path.read_text():
        return ConfigResult(
            tool="codex(AGENTS.md)", status="already_ok",
            message=f"opencode-search instructions already in {agents_path}",
            path=str(agents_path),
        )

    if check_only:
        return ConfigResult(
            tool="codex(AGENTS.md)", status="missing",
            message=f"opencode-search instructions missing from {agents_path}",
            path=str(agents_path),
        )

    try:
        agents_path.parent.mkdir(parents=True, exist_ok=True)
        # Append to existing or create new
        existing = agents_path.read_text() if agents_path.exists() else ""
        if "opencode-search" not in existing:
            agents_path.write_text(existing + "\n" + AGENTS_MD_CONTENT)
        return ConfigResult(
            tool="codex(AGENTS.md)", status="configured",
            message=f"Wrote opencode-search instructions to {agents_path}",
            path=str(agents_path),
        )
    except Exception as exc:
        return ConfigResult(
            tool="codex(AGENTS.md)", status="error",
            message=f"Failed to write AGENTS.md: {exc}",
            path=str(agents_path),
        )


def verify_claude_code(check_only: bool = False) -> ConfigResult:
    """Verify claude code MCP settings and global CLAUDE.md."""
    settings_path = Path.home() / ".claude" / "settings.json"

    if not settings_path.exists():
        return ConfigResult(
            tool="claude-code(settings)", status="missing",
            message=f"~/.claude/settings.json not found",
            path=str(settings_path),
        )

    try:
        settings = json.loads(settings_path.read_text())
        mcp = settings.get("mcpServers", {})
        has_ocs = "opencode-search" in mcp
        if has_ocs:
            return ConfigResult(
                tool="claude-code(settings)", status="already_ok",
                message=f"opencode-search MCP configured in ~/.claude/settings.json",
                path=str(settings_path),
            )
        else:
            return ConfigResult(
                tool="claude-code(settings)", status="missing",
                message=f"opencode-search NOT in ~/.claude/settings.json mcpServers",
                path=str(settings_path),
            )
    except Exception as exc:
        return ConfigResult(
            tool="claude-code(settings)", status="error",
            message=f"Failed to read settings.json: {exc}",
            path=str(settings_path),
        )


def verify_claude_md() -> ConfigResult:
    """Verify ~/.claude/CLAUDE.md contains opencode-search instructions."""
    claude_md = Path.home() / ".claude" / "CLAUDE.md"
    if claude_md.exists() and "opencode-search" in claude_md.read_text():
        return ConfigResult(
            tool="claude-code(CLAUDE.md)", status="already_ok",
            message=f"opencode-search instructions in ~/.claude/CLAUDE.md",
            path=str(claude_md),
        )
    return ConfigResult(
        tool="claude-code(CLAUDE.md)", status="missing",
        message=f"opencode-search instructions missing from ~/.claude/CLAUDE.md",
        path=str(claude_md),
    )


def verify_opencode() -> ConfigResult:
    """Verify ~/.config/opencode/opencode.jsonc has MCP entry."""
    config_path = Path.home() / ".config" / "opencode" / "opencode.jsonc"
    if not config_path.exists():
        return ConfigResult(
            tool="opencode(config)", status="missing",
            message=f"~/.config/opencode/opencode.jsonc not found",
            path=str(config_path),
        )
    try:
        import json
        data = json.loads(config_path.read_text())
        env = data.get("mcp", {}).get("opencode-search", {}).get("env", {})
        has_env = (
            env.get("OPENCODE_LLM_PROVIDER") == "ollama"
            and env.get("OPENCODE_QUERY_LLM_PROVIDER") == "ollama"
        )
        if "opencode-search" in data.get("mcp", {}):
            if has_env:
                return ConfigResult(
                    tool="opencode(config)", status="already_ok",
                    message="opencode-search MCP configured with ollama env vars in opencode.jsonc",
                    path=str(config_path),
                )
            return ConfigResult(
                tool="opencode(config)", status="warning",
                message="opencode-search MCP found but missing OPENCODE_LLM_PROVIDER/OPENCODE_QUERY_LLM_PROVIDER=ollama env vars",
                path=str(config_path),
            )
    except Exception:
        pass
    return ConfigResult(
        tool="opencode(config)", status="missing",
        message="opencode-search NOT in opencode.jsonc",
        path=str(config_path),
    )


def verify_bash_aliases() -> ConfigResult:
    """Verify ~/.bash_aliases has opencode-search shortcuts."""
    aliases_path = Path.home() / ".bash_aliases"
    if aliases_path.exists() and ("ocs=" in aliases_path.read_text() or "opencode_search" in aliases_path.read_text()):
        return ConfigResult(
            tool="bash_aliases", status="already_ok",
            message=f"opencode-search aliases defined in ~/.bash_aliases",
            path=str(aliases_path),
        )
    return ConfigResult(
        tool="bash_aliases", status="missing",
        message=f"opencode-search aliases missing from ~/.bash_aliases",
        path=str(aliases_path),
    )


def verify_hermes() -> ConfigResult:
    """Verify ~/.hermes/config.yaml has opencode-search MCP entry with ollama env vars."""
    config_path = Path.home() / ".hermes" / "config.yaml"
    if not config_path.exists():
        return ConfigResult(tool="hermes", status="skipped", message="~/.hermes/config.yaml not found (hermes not installed)", path=str(config_path))
    content = config_path.read_text(encoding="utf-8", errors="replace")
    if "opencode-search" not in content:
        return ConfigResult(tool="hermes", status="missing", message="opencode-search MCP not found in ~/.hermes/config.yaml", path=str(config_path))
    has_env = "OPENCODE_LLM_PROVIDER: ollama" in content and "OPENCODE_QUERY_LLM_PROVIDER: ollama" in content
    if has_env:
        return ConfigResult(tool="hermes", status="already_ok", message="opencode-search MCP with ollama env vars in ~/.hermes/config.yaml", path=str(config_path))
    return ConfigResult(tool="hermes", status="warning", message="opencode-search MCP found but missing OPENCODE_LLM_PROVIDER/OPENCODE_QUERY_LLM_PROVIDER=ollama in env section", path=str(config_path))


def verify_git_hook(check_only: bool = False) -> ConfigResult:
    """Install a git pre-push hook that runs prerelease.py --fast before push to main."""
    import subprocess
    try:
        result = subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True, text=True, cwd=str(Path(__file__).parent.parent))
        if result.returncode != 0:
            return ConfigResult(tool="git-hook", status="skipped", message="Not in a git repo")
        git_dir = Path(result.stdout.strip())
        hook_path = git_dir / "hooks" / "pre-push"
    except Exception as exc:
        return ConfigResult(tool="git-hook", status="skipped", message=f"git not available: {exc}")

    hook_content = """#!/bin/bash
# Auto-installed by configure_integrations.py — runs fast pre-release check before push
while read local_ref local_sha remote_ref remote_sha; do
  if [[ "$remote_ref" == *"main"* || "$remote_ref" == *"master"* ]]; then
    REPO_ROOT="$(git rev-parse --show-toplevel)"
    VENV="$REPO_ROOT/.venv/bin/python"
    PRERELEASE="$REPO_ROOT/scripts/prerelease.py"
    if [ -f "$VENV" ] && [ -f "$PRERELEASE" ]; then
      echo "🔍 Running fast pre-release check before push to main…"
      "$VENV" "$PRERELEASE" --fast
      if [ $? -ne 0 ]; then
        echo "❌ Pre-release check failed. Push blocked."
        exit 1
      fi
      echo "✅ Pre-release check passed."
    fi
  fi
done
exit 0
"""
    if hook_path.exists():
        existing = hook_path.read_text(encoding="utf-8", errors="replace")
        if "prerelease.py" in existing:
            return ConfigResult(tool="git-hook", status="already_ok", message="pre-push hook already installed", path=str(hook_path))
    if check_only:
        return ConfigResult(tool="git-hook", status="missing", message="pre-push hook not installed", path=str(hook_path))
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(hook_content, encoding="utf-8")
    hook_path.chmod(0o755)
    return ConfigResult(tool="git-hook", status="configured", message="pre-push hook installed", path=str(hook_path))


def run_all(check_only: bool = False) -> list[ConfigResult]:
    results = []
    results.append(configure_codex(check_only=check_only))
    results.append(configure_codex_agents_md(check_only=check_only))
    results.append(verify_claude_code(check_only=check_only))
    results.append(verify_claude_md())
    results.append(verify_opencode())
    results.append(verify_bash_aliases())
    results.append(verify_hermes())
    results.append(verify_git_hook(check_only=check_only))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure opencode-search integrations")
    parser.add_argument("--check", action="store_true", help="Check only, no writes")
    parser.add_argument("--json", dest="json_out", action="store_true", help="JSON output")
    args = parser.parse_args()

    results = run_all(check_only=args.check)

    if args.json_out:
        import dataclasses
        print(json.dumps([dataclasses.asdict(r) for r in results], indent=2))
    else:
        print(f"\n{'='*60}")
        print(f"  Integration Configuration")
        print(f"{'='*60}")
        icons = {"configured": "✅", "already_ok": "✅", "missing": "🔴", "skipped": "⏭️", "error": "❌"}
        for r in results:
            icon = icons.get(r.status, "?")
            print(f"  {icon} [{r.status:<12}] {r.tool:<30} {r.message[:50]}")
        print(f"{'='*60}")
        ok_count = sum(1 for r in results if r.status in ("configured", "already_ok"))
        print(f"  {ok_count}/{len(results)} integrations configured\n")

    missing = [r for r in results if r.status == "missing" and r.tool not in ("bash_aliases",)]
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
