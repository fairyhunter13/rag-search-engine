"""WS-E: one-shot purge of L2/L3 community rows from all registered projects' graph.dbs.

Runs DELETE FROM communities WHERE level != 1  +  UPDATE parent_id=NULL on L1 rows  +
clears stale meta keys.  Idempotent — safe to run again.
Never opens a repo file; only touches data-dir graph.dbs.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Stale meta keys written by the deleted kb/hierarchy.py / kb/federation_hierarchy.py
_STALE_META_KEYS = (
    "l3_theme_sigs",
    "l3_algo",
    "HIER_VERSION",
    "hierarchy_algo",
    "hierarchy_version",
)


def _purge_one(db_path: Path) -> dict:
    if not db_path.exists():
        return {"skipped": True, "reason": "no graph.db"}
    con = sqlite3.connect(str(db_path), timeout=30)
    try:
        con.execute("PRAGMA busy_timeout = 30000")  # wait up to 30s for locks
        con.execute("PRAGMA journal_mode = WAL")
        before = con.execute("SELECT COUNT(*) FROM communities").fetchone()[0]
        deleted = con.execute("DELETE FROM communities WHERE level != 1").rowcount
        con.execute("UPDATE communities SET parent_id = NULL WHERE level = 1")
        remaining = con.execute("SELECT COUNT(*) FROM communities").fetchone()[0]
        # Clear stale meta keys if meta table exists
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        meta_deleted = 0
        if "meta" in tables:
            for key in _STALE_META_KEYS:
                meta_deleted += con.execute("DELETE FROM meta WHERE key=?", (key,)).rowcount
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    return {
        "before": before,
        "deleted": deleted,
        "remaining": remaining,
        "meta_cleared": meta_deleted,
    }


def main() -> None:
    import json

    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import list_projects

    projects = list_projects()
    total_deleted = 0
    errors: list[str] = []

    print(f"Purging L2/L3 communities from {len(projects)} registered project(s)…\n")
    for p in projects:
        db = project_graph_db(p.path)
        try:
            result = _purge_one(db)
        except Exception as exc:
            errors.append(f"{p.path}: {exc}")
            print(f"  ERROR {p.path}: {exc}")
            continue
        if result.get("skipped"):
            print(f"  SKIP   {Path(p.path).name} — {result['reason']}")
        else:
            d = result["deleted"]
            r = result["remaining"]
            m = result["meta_cleared"]
            flag = " (had L2/L3)" if d else ""
            print(f"  OK     {Path(p.path).name:<50}  deleted={d:>4}  L1={r:>4}  meta_cleared={m}{flag}")
            total_deleted += d

    print(f"\nDone. Total L2/L3 rows deleted: {total_deleted}.")
    if errors:
        print(f"\n{len(errors)} error(s):")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
