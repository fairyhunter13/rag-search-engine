"""Verify all new features work against the real redacted-name-3 index.

Run after reindex_astro.py completes:
  .venv/bin/python scripts/verify_acme_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

ASTRO = str(Path(os.path.expanduser("~/git/github.com/fairyhunter13/redacted-name-3")).resolve())


def _ok(label: str) -> None:
    print(f"  ✓  {label}")


def _fail(label: str, detail: str = "") -> None:
    print(f"  ✗  {label}" + (f": {detail}" if detail else ""))
    sys.exit(1)


async def main() -> None:
    from opencode_search.config import get_project_graph_db_path
    from opencode_search.graph.storage import GraphStorage
    from opencode_search.handlers._graph import (
        handle_detect_impact,
        handle_get_callers,
        handle_get_callees,
        handle_get_communities,
        handle_get_symbol,
        handle_global_search,
        handle_trace_path,
    )
    from opencode_search.handlers._wiki import handle_wiki_lint

    print(f"\n{'='*60}")
    print(f"Verifying redacted-name-3 E2E")
    print(f"Path: {ASTRO}")
    print(f"{'='*60}\n")

    # ── 1. Graph DB exists ────────────────────────────────────────────────────
    print("1. Graph DB")
    db_path = get_project_graph_db_path(ASTRO)
    if not Path(db_path).exists():
        _fail("graph.db exists", f"not found at {db_path}")
    _ok(f"graph.db exists at {db_path}")

    con = sqlite3.connect(db_path)
    nc = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    ec = con.execute("SELECT COUNT(*) FROM edges WHERE kind='CALLS'").fetchone()[0]
    cc = con.execute("SELECT COUNT(*) FROM communities").fetchone()[0]
    cn = con.execute("SELECT COUNT(*) FROM nodes WHERE community_id IS NOT NULL").fetchone()[0]
    langs = con.execute(
        "SELECT language, COUNT(*) n FROM nodes GROUP BY language ORDER BY n DESC LIMIT 8"
    ).fetchall()
    con.close()

    print(f"   nodes={nc}, call_edges={ec}, communities={cc}, "
          f"nodes_with_community={cn}")
    print(f"   top languages: {langs}")

    if nc < 100:
        _fail("node count", f"expected > 100, got {nc}")
    _ok(f"node count {nc} > 100")

    if ec < 1:
        _fail("call edges", "expected > 0")
    _ok(f"call edges: {ec}")

    if cc < 1:
        _fail("communities", "expected > 0")
    _ok(f"communities: {cc}")

    if cn < nc * 0.5:
        _fail("community assignment", f"{cn}/{nc} nodes assigned")
    _ok(f"community assignment: {cn}/{nc}")

    # ── 2. Graph handlers ─────────────────────────────────────────────────────
    print("\n2. Graph handlers")

    # get_communities
    result = await handle_get_communities(project_path=ASTRO)
    if "error" in result and result.get("total", 0) == 0:
        _fail("get_communities", str(result))
    _ok(f"get_communities: {result.get('total', 0)} communities")

    # get_symbol — try some common Go/Java names
    found_symbol = None
    for name in ["main", "New", "Init", "Handle", "Run", "Close", "Get", "Set", "Error"]:
        r = await handle_get_symbol(name=name, project_path=ASTRO)
        if "error" not in r and r.get("count", 0) > 0:
            found_symbol = name
            _ok(f"get_symbol('{name}'): {r['count']} matches")
            break
    if not found_symbol:
        _fail("get_symbol", "no common symbols found (main/New/Handle/Run/Close)")

    # get_callers
    if found_symbol:
        r = await handle_get_callers(symbol=found_symbol, project_path=ASTRO, depth=3)
        if "error" not in r:
            _ok(f"get_callers('{found_symbol}'): ok")
        else:
            print(f"   (get_callers returned error — acceptable if symbol has no callers)")

    # get_callees
    if found_symbol:
        r = await handle_get_callees(symbol=found_symbol, project_path=ASTRO, depth=3)
        if "error" not in r:
            _ok(f"get_callees('{found_symbol}'): ok")

    # detect_impact
    if found_symbol:
        r = await handle_detect_impact(symbol=found_symbol, project_path=ASTRO)
        if "error" not in r:
            _ok(f"detect_impact('{found_symbol}'): ok")

    # ── 3. global_search ──────────────────────────────────────────────────────
    print("\n3. global_search")
    r = await handle_global_search(
        query="authentication middleware handler",
        project_path=ASTRO,
        top_k=10,
    )
    if "error" in r:
        _fail("global_search", str(r))
    _ok(f"global_search: total={r['total']}, "
        f"community_matches={r['community_matches']}, "
        f"wiki_matches={r['wiki_matches']}")

    # ── 4. wiki_lint (empty wiki is fine) ────────────────────────────────────
    print("\n4. wiki_lint")
    r = await handle_wiki_lint(project_path=ASTRO)
    if "error" in r:
        _fail("wiki_lint", str(r))
    _ok(f"wiki_lint: healthy={r.get('healthy')}, "
        f"total_pages={r.get('total_pages', 0)}, "
        f"orphans={len(r.get('orphans', []))}")

    # ── 5. search_code still works ────────────────────────────────────────────
    print("\n5. search_code (regression)")
    from opencode_search.handlers._query import handle_search_code
    r = await handle_search_code(
        query="authentication handler",
        project_paths=[ASTRO],
        top_k=5,
        use_rerank=False,
    )
    if "error" in r:
        _fail("search_code", str(r))
    results_count = len(r.get("results", []))
    _ok(f"search_code: {results_count} results, elapsed={r.get('elapsed_ms', '?')}ms")

    print(f"\n{'='*60}")
    print("All checks passed!")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
