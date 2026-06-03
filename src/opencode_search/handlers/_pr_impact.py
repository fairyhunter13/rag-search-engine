"""_pr_impact.py — PR impact analysis: changed files → communities + nodes affected."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


async def handle_pr_impact(
    project_path: str,
    files: list[str] | None = None,
    base_branch: str = "main",
    *,
    top_n: int = 10,
) -> dict[str, Any]:
    """Compute graph impact of a pull request's changed files.

    Given a list of changed file paths (relative or absolute), returns:
    - communities touched (by ID + title)
    - total nodes affected
    - risk level (low / medium / high) based on community count and cross-community spread
    - top affected nodes (highest-degree nodes in changed files)

    If files is not provided, runs `git diff --name-only <base_branch>...HEAD` automatically.
    """
    from opencode_search.handlers._graph import _open_graph

    def _run() -> dict[str, Any]:
        # Resolve changed files
        changed = list(files) if files else _git_changed_files(project_path, base_branch)

        if not changed:
            return {
                "status": "ok",
                "project_path": project_path,
                "changed_files": [],
                "communities_touched": [],
                "nodes_affected": 0,
                "risk_level": "none",
                "top_affected_nodes": [],
                "note": "No changed files detected",
            }

        # Normalise paths to absolute
        proj_root = Path(project_path).expanduser().resolve()
        abs_changed: set[str] = set()
        for f in changed:
            p = Path(f)
            if p.is_absolute():
                abs_changed.add(str(p))
            else:
                abs_changed.add(str(proj_root / f))

        gs = _open_graph(project_path)
        if gs is None:
            return {"error": f"Graph not built for {project_path}. Run build(action='index') first."}

        try:
            db = gs._db()
            # Pull all nodes with their file, community_id, name, degree
            rows = db.execute("""
                SELECT n.id, n.name, n.qualified_name, n.kind, n.file,
                       n.community_id, c.title, c.node_count
                FROM nodes n
                LEFT JOIN communities c ON n.community_id = c.id
            """).fetchall()
        finally:
            gs.close()

        # Build file → (nodes, communities) index
        file_nodes: dict[str, list[dict]] = {}
        for r in rows:
            nfile = r[4] or ""
            node = {
                "id": r[0], "name": r[1], "qualified_name": r[2],
                "kind": r[3], "file": nfile,
                "community_id": r[5], "community_title": r[6] or f"Community {r[5]}",
            }
            file_nodes.setdefault(nfile, []).append(node)

        # Match changed files against indexed file paths (exact or suffix match)
        affected_nodes: list[dict] = []
        communities_seen: dict[int | None, str] = {}

        for idx_file, nodes in file_nodes.items():
            if _file_matches(idx_file, abs_changed):
                for n in nodes:
                    affected_nodes.append(n)
                    cid = n["community_id"]
                    if cid not in communities_seen:
                        communities_seen[cid] = n["community_title"]

        # Compute degree from edge table for top-node ranking
        if affected_nodes:
            try:
                gs2 = _open_graph(project_path)
                if gs2:
                    db2 = gs2._db()
                    ids = [n["id"] for n in affected_nodes]
                    ph = ",".join("?" * len(ids))
                    edge_rows = db2.execute(
                        f"SELECT from_id, to_id FROM edges WHERE from_id IN ({ph}) OR to_id IN ({ph})",
                        ids + ids,
                    ).fetchall()
                    gs2.close()
                    from collections import Counter
                    degree: Counter[str] = Counter()
                    for e in edge_rows:
                        degree[e[0]] += 1
                        degree[e[1]] += 1
                    for n in affected_nodes:
                        n["degree"] = degree.get(n["id"], 0)
            except Exception:
                for n in affected_nodes:
                    n["degree"] = 0

        top_nodes = sorted(affected_nodes, key=lambda n: n.get("degree", 0), reverse=True)[:top_n]

        comm_list = [
            {"community_id": cid, "title": title}
            for cid, title in communities_seen.items()
            if cid is not None
        ]

        n_comms = len(comm_list)
        n_nodes = len(affected_nodes)
        if n_comms >= 5 or n_nodes >= 50:
            risk = "high"
        elif n_comms >= 2 or n_nodes >= 10:
            risk = "medium"
        elif n_nodes > 0:
            risk = "low"
        else:
            risk = "none"

        return {
            "status": "ok",
            "project_path": project_path,
            "changed_files": sorted(abs_changed),
            "communities_touched": comm_list,
            "community_count": n_comms,
            "nodes_affected": n_nodes,
            "risk_level": risk,
            "top_affected_nodes": top_nodes,
        }

    return await asyncio.to_thread(_run)


def _git_changed_files(project_path: str, base_branch: str) -> list[str]:
    import subprocess
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base_branch}...HEAD"],
            cwd=project_path,
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return [f.strip() for f in result.stdout.splitlines() if f.strip()]
    except Exception:
        pass
    return []


def _file_matches(indexed_file: str, abs_changed: set[str]) -> bool:
    if indexed_file in abs_changed:
        return True
    # suffix match for relative vs absolute
    for changed in abs_changed:
        if indexed_file.endswith(changed) or changed.endswith(indexed_file):
            return True
    return False
