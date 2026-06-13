"""overview() tool implementation — isolated to keep mcp.py within 150 lines."""
from __future__ import annotations

import json


def handle_overview(project_path: str, what: str) -> str:
    from opencode_search.core.registry import list_projects

    if what == "projects":
        return json.dumps({"projects": [
            {"path": p.path, "enabled": p.enabled, "indexed_at": p.indexed_at}
            for p in list_projects()
        ]})
    if not project_path:
        ps = [p for p in list_projects() if p.enabled]
        project_path = ps[0].path if ps else ""
    if what == "patterns" and project_path:
        from pathlib import Path

        from opencode_search.kb.patterns import detect_patterns
        return json.dumps(detect_patterns(Path(project_path)))
    if project_path:
        from opencode_search.core.config import project_graph_db
        from opencode_search.graph.store import GraphStore

        gdb = project_graph_db(project_path)
        if gdb.exists():
            gs = GraphStore(gdb)
            try:
                if what == "communities":
                    rows = gs._con.execute(
                        "SELECT id,title,level FROM communities ORDER BY level,id LIMIT 50"
                    ).fetchall()
                    return json.dumps({"communities": [{"id": r[0], "title": r[1], "level": r[2]} for r in rows]})
                return json.dumps({"path": project_path, "symbols": gs.symbol_count(), "communities": gs.community_count()})
            finally:
                gs.close()
    return json.dumps({"what": what, "status": "no project available"})
