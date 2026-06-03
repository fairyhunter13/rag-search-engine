"""_vacuum.py — Storage cleanup: remove orphan index tier dirs, report empty projects."""
from __future__ import annotations

from typing import Any


async def handle_vacuum(project_path: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Remove orphan index tier directories and report empty-file projects.

    Orphan dirs: index_budget/, index_balanced/, index_premium/ that exist alongside
    a canonical index/ directory. These accumulate when the embedding tier is upgraded
    and can waste tens of GB.

    dry_run=True: report what would be removed without deleting anything.
    """
    import asyncio
    import shutil

    from opencode_search.config import get_project_index_dir, load_registry

    orphan_tier_names = ("index_budget", "index_balanced", "index_premium")

    def _run() -> dict[str, Any]:
        registry = load_registry()
        removed_dirs: list[str] = []
        freed_bytes: int = 0
        empty_projects: list[str] = []

        projects = list(registry.values()) if isinstance(registry, dict) else registry

        for entry in projects:
            path = entry.get("path") if isinstance(entry, dict) else getattr(entry, "path", None)
            if not path:
                continue

            fc = entry.get("file_count") if isinstance(entry, dict) else getattr(entry, "file_count", -1)
            if fc == 0:
                empty_projects.append(path)

            try:
                container = get_project_index_dir(path)
            except Exception:
                continue

            canonical = container / "index"
            if not canonical.exists():
                continue

            for tier_name in orphan_tier_names:
                orphan = container / tier_name
                if not orphan.exists():
                    continue
                if orphan.resolve() == canonical.resolve():
                    continue

                dir_bytes = sum(f.stat().st_size for f in orphan.rglob("*") if f.is_file())
                freed_bytes += dir_bytes
                removed_dirs.append(str(orphan))

                if not dry_run:
                    shutil.rmtree(orphan, ignore_errors=True)

        key = "orphan_dirs_found" if dry_run else "orphan_dirs_removed"
        return {
            "status": "ok",
            "dry_run": dry_run,
            key: removed_dirs,
            "freed_bytes": freed_bytes,
            "freed_mb": round(freed_bytes / 1024 / 1024, 1),
            "empty_projects": empty_projects,
            "empty_project_count": len(empty_projects),
        }

    return await asyncio.to_thread(_run)
