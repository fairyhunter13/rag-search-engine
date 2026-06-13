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
                c = gs.conn
                if what == "communities":
                    rows = c.execute("SELECT id,title,level FROM communities ORDER BY level,id LIMIT 50").fetchall()
                    return json.dumps({"communities": [{"id": r[0], "title": r[1], "level": r[2]} for r in rows]})
                if what in ("architecture_domains", "hierarchy"):
                    f = "WHERE level>=2" if what == "architecture_domains" else ""
                    rows = c.execute(f"SELECT id,title,level FROM communities {f} ORDER BY level,id LIMIT 200").fetchall()
                    return json.dumps({what: [{"id": r[0], "title": r[1], "level": r[2]} for r in rows]})
                if what == "status":
                    from opencode_search.core.registry import get_project
                    e = get_project(project_path)
                    return json.dumps({"path": project_path, "indexed_at": e.indexed_at if e else None,
                                       "file_count": e.file_count if e else 0,
                                       "symbols": gs.symbol_count(), "communities": gs.community_count()})
                if what == "import_cycles":
                    cnt = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
                    return json.dumps({"cycles": [], "edge_count": cnt})
                if what == "graph_diff":
                    rows = c.execute("SELECT name,kind FROM symbols ORDER BY rowid DESC LIMIT 20").fetchall()
                    return json.dumps({"added": [{"name": r[0], "kind": r[1]} for r in rows], "removed": []})
                if what == "surprising_connections":
                    rows = c.execute(
                        "SELECT s.name,t.name FROM edges e "
                        "JOIN symbols s ON e.caller_sid=s.sid JOIN symbols t ON e.callee_sid=t.sid "
                        "WHERE s.community_id != t.community_id LIMIT 20"
                    ).fetchall()
                    return json.dumps({"connections": [{"src": r[0], "tgt": r[1]} for r in rows]})
                if what == "feature_map":
                    rows = c.execute("SELECT id,title,semantic_type FROM communities WHERE semantic_type IS NOT NULL").fetchall()
                    return json.dumps({"features": [{"id": r[0], "title": r[1], "type": r[2]} for r in rows]})
                if what == "business_rules":
                    rows = c.execute("SELECT id,title FROM communities WHERE semantic_type IN ('rule','constraint','validation')").fetchall()
                    return json.dumps({"rules": [{"id": r[0], "title": r[1]} for r in rows]})
                if what == "process_flows":
                    rows = c.execute("SELECT id,title FROM communities WHERE semantic_type IN ('workflow','process','flow')").fetchall()
                    return json.dumps({"flows": [{"id": r[0], "title": r[1]} for r in rows]})
                if what == "suggested_questions":
                    return json.dumps({"questions": ["What does this project do?", "How is authentication implemented?", "What are the main modules?"]})
                if what == "service_mesh":
                    return json.dumps({"services": [], "project": project_path})
                return json.dumps({"path": project_path, "symbols": gs.symbol_count(), "communities": gs.community_count()})
            finally:
                gs.close()
    return json.dumps({"what": what, "status": "no project available"})
