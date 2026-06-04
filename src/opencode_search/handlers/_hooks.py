"""Git hook integration: install/uninstall post-commit hooks for auto-reindex."""
from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from typing import Any

_HOOK_HEADER = "# opencode-search managed hook — do not edit this line"

_HOOK_BODY = """\
#!/bin/sh
# opencode-search managed hook — do not edit this line
# Triggers opencode-search incremental re-index on commit.
# Install: overview(what='install_hooks') | Uninstall: overview(what='uninstall_hooks')
if command -v opencode-search >/dev/null 2>&1; then
  opencode-search index --path "$(git rev-parse --show-toplevel)" --incremental >/dev/null 2>&1 &
fi
"""


async def handle_git_hooks(project_path: str, install: bool) -> dict[str, Any]:
    """Install or uninstall the post-commit git hook for a project."""
    def _run() -> dict[str, Any]:
        root = Path(project_path).expanduser().resolve()
        if not root.is_dir():
            return {"error": f"Not a directory: {project_path}"}

        git_dir = root / ".git"
        if not git_dir.is_dir():
            return {"error": f"Not a git repository (no .git dir): {project_path}"}

        hooks_dir = git_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook_path = hooks_dir / "post-commit"

        if install:
            existing = hook_path.read_text() if hook_path.exists() else ""
            if _HOOK_HEADER in existing:
                return {
                    "status": "already_installed",
                    "hook_path": str(hook_path),
                    "project_path": str(root),
                }
            if existing and not existing.endswith("\n"):
                existing += "\n"
            if existing:
                new_content = existing.rstrip("\n") + "\n\n" + _HOOK_BODY
            else:
                new_content = _HOOK_BODY
            hook_path.write_text(new_content)
            hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            return {
                "status": "installed",
                "hook_path": str(hook_path),
                "project_path": str(root),
            }
        else:
            if not hook_path.exists():
                return {
                    "status": "not_installed",
                    "hook_path": str(hook_path),
                    "project_path": str(root),
                }
            content = hook_path.read_text()
            if _HOOK_HEADER not in content:
                return {
                    "status": "not_managed",
                    "message": "Hook exists but was not installed by opencode-search. Not removing.",
                    "hook_path": str(hook_path),
                }
            lines = content.splitlines(keepends=True)
            managed_start = next(
                (i for i, ln in enumerate(lines) if _HOOK_HEADER in ln), None
            )
            if managed_start is not None:
                remaining = "".join(lines[:managed_start]).rstrip("\n")
                if remaining:
                    hook_path.write_text(remaining + "\n")
                else:
                    hook_path.unlink()
            return {
                "status": "uninstalled",
                "hook_path": str(hook_path),
                "project_path": str(root),
            }

    return await asyncio.to_thread(_run)
