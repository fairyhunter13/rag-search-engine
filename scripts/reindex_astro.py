"""Re-index redacted-name-3 with the new 768-dim model + graph build.

Run: .venv/bin/python scripts/reindex_astro.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# Ensure we're using the src package
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


ASTRO = os.path.expanduser("~/git/github.com/fairyhunter13/redacted-name-3")


async def main() -> None:
    from opencode_search.handlers import handle_index_project, _indexing_status

    path = str(Path(ASTRO).resolve())
    print(f"Re-indexing {path} ...")
    t0 = time.monotonic()

    result = await handle_index_project(path=path, force=True)
    print(f"Launched: {result}")

    # Drain all background tasks (embedding + graph build)
    while True:
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pending:
            break
        done, _ = await asyncio.wait(pending, timeout=30)
        elapsed = time.monotonic() - t0
        status = _indexing_status.get(path, {})
        print(f"  [{elapsed:5.0f}s] tasks_remaining={len(pending) - len(done)}"
              f"  status={status.get('status', '?')}"
              f"  chunks={status.get('chunks_total', '?')}")

    elapsed = time.monotonic() - t0
    final = _indexing_status.get(path, result)
    print(f"\nDone in {elapsed:.1f}s: {final}")

    # Check graph
    from opencode_search.config import get_project_graph_db_path
    import sqlite3
    db = get_project_graph_db_path(path)
    if os.path.exists(db):
        con = sqlite3.connect(db)
        nc = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        ec = con.execute("SELECT COUNT(*) FROM edges WHERE kind='CALLS'").fetchone()[0]
        cc = con.execute("SELECT COUNT(*) FROM communities").fetchone()[0]
        cn = con.execute("SELECT COUNT(*) FROM nodes WHERE community_id IS NOT NULL").fetchone()[0]
        langs = con.execute(
            "SELECT language, COUNT(*) FROM nodes GROUP BY language ORDER BY 2 DESC LIMIT 8"
        ).fetchall()
        con.close()
        print(f"\nGraph DB: nodes={nc}, call_edges={ec}, communities={cc}, "
              f"nodes_with_community={cn}")
        print(f"Languages: {langs}")
    else:
        print(f"\nWARNING: graph.db not found at {db}")


if __name__ == "__main__":
    asyncio.run(main())
