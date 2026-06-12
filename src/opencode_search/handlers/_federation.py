"""Federation handlers: discover, register, and index groups of related projects."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def _discover_symlink_members(root: Path) -> list[str]:
    """Return resolved real paths of symlinked directories found in the project.

    Scans two levels deep: the project root AND each immediate non-symlink
    subdirectory (e.g. 'repositories-ubuntu/', 'packages/') that itself contains
    only or mostly symlinks. This covers the common monorepo pattern where all
    sub-repo symlinks live in a dedicated container directory.
    """
    seen: set[str] = set()
    members: list[str] = []

    def _scan_dir(directory: Path) -> None:
        try:
            for item in sorted(directory.iterdir()):
                if item.is_symlink():
                    target = item.resolve()
                    t = str(target)
                    if target.is_dir() and t != str(root) and t not in seen:
                        seen.add(t)
                        members.append(t)
        except PermissionError:
            pass

    # Level 1: root itself
    _scan_dir(root)

    # Level 2: real (non-symlink) subdirectories of root — catch containers like
    # 'repositories-ubuntu/', 'packages/', 'services/', etc.
    try:
        for sub in sorted(root.iterdir()):
            if sub.is_dir() and not sub.is_symlink():
                _scan_dir(sub)
    except PermissionError:
        pass

    return members


def _discover_go_work_members(root: Path) -> list[str]:
    """Parse go.work 'use' directives via tree-sitter gowork grammar and return resolved member paths."""
    go_work = root / "go.work"
    if not go_work.exists():
        return []
    members: list[str] = []
    try:
        from opencode_search.handlers._graph import _parse_with_grammar
        result = _parse_with_grammar(go_work, "gowork")
        if result is None:
            return []
        root_node, src = result

        def _text(node) -> str:
            return src[node.start_byte():node.end_byte()].decode("utf-8", errors="replace")

        def _walk(node) -> None:
            if node.kind() == "use_spec":
                for i in range(node.child_count()):
                    child = node.child(i)
                    if child.kind() == "file_path":
                        p = _text(child).strip().strip("\"'")
                        if p and not p.startswith("//"):
                            candidate = (
                                (root / p).resolve()
                                if not Path(p).is_absolute()
                                else Path(p).resolve()
                            )
                            if candidate.is_dir() and str(candidate) != str(root):
                                members.append(str(candidate))
                return
            for i in range(node.child_count()):
                _walk(node.child(i))

        _walk(root_node)
    except OSError:
        pass
    return members


def _discover_pnpm_members(root: Path) -> list[str]:
    """Parse pnpm-workspace.yaml 'packages' entries."""
    import glob as _glob
    ws_file = root / "pnpm-workspace.yaml"
    if not ws_file.exists():
        return []
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(ws_file.read_text(encoding="utf-8"))
        patterns = data.get("packages", []) if isinstance(data, dict) else []
    except Exception:
        return []
    members = []
    for pattern in patterns:
        for match in _glob.glob(str(root / pattern)):
            candidate = Path(match).resolve()
            if candidate.is_dir() and str(candidate) != str(root):
                members.append(str(candidate))
    return members


def _discover_package_json_members(root: Path) -> list[str]:
    """Parse package.json 'workspaces' field."""
    import glob as _glob
    import json
    pkg = root / "package.json"
    if not pkg.exists():
        return []
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
        ws = data.get("workspaces", [])
        if isinstance(ws, dict):
            ws = ws.get("packages", [])
    except Exception:
        return []
    members = []
    for pattern in ws:
        for match in _glob.glob(str(root / pattern)):
            candidate = Path(match).resolve()
            if candidate.is_dir() and str(candidate) != str(root):
                members.append(str(candidate))
    return members


def _expand_with_federation(
    project_paths: list[str],
    registry: dict,
    only_indexed: bool = True,
) -> list[str]:
    """Return project_paths + federation members for each path that has them.

    Args:
        only_indexed: If True (default), only include members that have been indexed
                      (entry.indexed_at is not None). Unindexed members would return
                      0 results anyway, so including them wastes I/O.
    """
    expanded = list(project_paths)
    seen = set(project_paths)
    for path in project_paths:
        entry = registry.get(path)
        if not entry:
            continue
        for member in entry.federation:
            if member in seen:
                continue
            member_entry = registry.get(member)
            if member_entry is None:
                continue
            if only_indexed and member_entry.indexed_at is None:
                continue
            expanded.append(member)
            seen.add(member)
    return expanded


# ---------------------------------------------------------------------------
# Public handlers
# ---------------------------------------------------------------------------

async def handle_discover_federation(project_path: str) -> dict[str, Any]:
    """Auto-discover federation members from symlinks and workspace files.

    Scans the project directory for:
    - Top-level symlinks pointing to other directories (most common for monorepos)
    - go.work 'use' directives
    - pnpm-workspace.yaml 'packages' entries
    - package.json 'workspaces' entries
    """
    root = Path(_resolve(project_path))
    if not root.is_dir():
        return {"error": f"Not a directory: {project_path}"}

    symlinks = _discover_symlink_members(root)
    go_work = _discover_go_work_members(root)
    pnpm = _discover_pnpm_members(root)
    pkg_json = _discover_package_json_members(root)

    # Merge deduplicated
    seen: set[str] = set()
    all_members: list[str] = []
    for m in symlinks + go_work + pnpm + pkg_json:
        if m not in seen:
            seen.add(m)
            all_members.append(m)

    return {
        "project_path": str(root),
        "discovered": all_members,
        "total": len(all_members),
        "sources": {
            "symlinks": symlinks,
            "go_work": go_work,
            "pnpm_workspace": pnpm,
            "package_json": pkg_json,
        },
    }


async def handle_list_federation(project_path: str) -> dict[str, Any]:
    """List federation members registered for a project."""
    from opencode_search.config import load_registry

    root = _resolve(project_path)
    registry = load_registry()
    entry = registry.get(root)
    if entry is None:
        return {"error": f"Project not registered: {project_path}", "members": []}

    members = entry.federation
    indexed_count = sum(
        1 for m in members
        if registry.get(m) and registry[m].indexed_at is not None
    )
    member_details = []
    for m in members:
        m_entry = registry.get(m)
        member_details.append({
            "path": m,
            "indexed": m_entry.indexed_at is not None if m_entry else False,
            "file_count": m_entry.file_count if m_entry else 0,
        })

    return {
        "root": root,
        "members": member_details,
        "total_members": len(members),
        "indexed_count": indexed_count,
    }


async def handle_add_federation_member(root_path: str, member_path: str) -> dict[str, Any]:
    """Add a project path as a federation member of the root project.

    Also registers the member in the registry (if not already present) so it
    can be indexed later. Does NOT trigger indexing.
    """
    from opencode_search.config import (
        ProjectEntry,
        get_project_db_path,
        load_registry,
        save_registry,
    )

    root = _resolve(root_path)
    member = _resolve(member_path)

    if not Path(root).is_dir():
        return {"error": f"Root path is not a directory: {root_path}"}
    if not Path(member).is_dir():
        return {"error": f"Member path is not a directory: {member_path}"}
    if root == member:
        return {"error": "Root and member cannot be the same path"}

    registry = load_registry()

    # Ensure root is in registry
    if root not in registry:
        registry[root] = ProjectEntry(
            path=root,
            db_path=get_project_db_path(root),
        )

    # Ensure member is in registry (pre-registration, no indexing yet)
    if member not in registry:
        registry[member] = ProjectEntry(
            path=member,
            db_path=get_project_db_path(member),
        )

    # Add member to federation (deduplicated)
    if member not in registry[root].federation:
        registry[root].federation.append(member)

    save_registry(registry)

    return {
        "status": "ok",
        "root": root,
        "member": member,
        "total_members": len(registry[root].federation),
    }


async def handle_remove_federation_member(root_path: str, member_path: str) -> dict[str, Any]:
    """Remove a federation member from a root project."""
    from opencode_search.config import load_registry, save_registry

    root = _resolve(root_path)
    member = _resolve(member_path)

    registry = load_registry()
    entry = registry.get(root)
    if entry is None:
        return {"error": f"Project not registered: {root_path}"}

    if member not in entry.federation:
        return {"error": f"{member_path} is not a federation member of {root_path}"}

    entry.federation.remove(member)
    save_registry(registry)

    return {
        "status": "ok",
        "root": root,
        "removed": member,
        "total_members": len(entry.federation),
    }


async def handle_index_federation(
    root_path: str,
    watch: bool = False,
) -> dict[str, Any]:
    """Index all federation members of the root project as separate projects.

    Runs sequentially (not concurrent) to avoid GPU contention during embedding.
    Members that are already indexed and up-to-date are skipped automatically
    by the underlying indexer (hash-based change detection).
    """
    from opencode_search.config import load_registry
    from opencode_search.handlers._index import handle_index_project

    root = _resolve(root_path)
    registry = load_registry()
    entry = registry.get(root)
    if entry is None:
        return {"error": f"Project not registered: {root_path}", "indexed": [], "failed": []}

    members = entry.federation
    if not members:
        return {
            "status": "ok",
            "root": root,
            "indexed": [],
            "failed": [],
            "total": 0,
            "note": "No federation members registered. Use add_federation_member first.",
        }

    t0 = time.perf_counter()
    indexed: list[str] = []
    failed: list[dict[str, str]] = []

    for member in members:
        if not Path(member).is_dir():
            failed.append({"path": member, "reason": "directory does not exist"})
            continue
        try:
            result = await handle_index_project(path=member, watch=watch)
            if "error" in result:
                failed.append({"path": member, "reason": result["error"]})
            else:
                indexed.append(member)
                log.info("federation: started indexing %s", member)
        except Exception as exc:
            failed.append({"path": member, "reason": str(exc)})
            log.warning("federation: failed to index %s: %s", member, exc)

    return {
        "status": "ok",
        "root": root,
        "indexed": indexed,
        "failed": failed,
        "total": len(members),
        "elapsed_s": round(time.perf_counter() - t0, 2),
    }
