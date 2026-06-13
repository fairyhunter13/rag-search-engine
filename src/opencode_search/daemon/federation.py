"""Federation: discover symlinked sub-repos and register them."""
from __future__ import annotations

from pathlib import Path


def discover_members(root_path: str) -> list[str]:
    """Return resolved paths of direct symlink children that look like repos."""
    root = Path(root_path)
    members: list[str] = []
    try:
        for item in root.iterdir():
            if not (item.is_symlink() and item.is_dir()):
                continue
            target = item.resolve()
            if any(target.glob("*.py")) or (target / "src").exists() or (target / "go.mod").exists():
                members.append(str(target))
    except OSError:
        pass
    return members


def index_members(root_path: str) -> int:
    """Register all discovered federation members. Returns count newly registered."""
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import get_project, upsert_project

    registered = 0
    for m in discover_members(root_path):
        if get_project(m) is None:
            upsert_project(ProjectEntry(path=m, enabled=True))
            registered += 1
    return registered
