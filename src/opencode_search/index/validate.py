"""Index integrity validator — pure SQL, no inference, no GPU.

Surfaced as overview(what="validate"). Checks: orphan chunks/vectors,
dangling call-graph edges, bad community refs, placeholder L1 titles,
path leakage, process-edge anchoring + confidence in [0.5, 1.0].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def _check_member(member_path: str, root_path: str) -> dict[str, Any]:
    import sqlite_vec  # type: ignore[import-untyped]

    from opencode_search.core.config import project_graph_db, project_vector_db, root_process_db
    from opencode_search.core.registry import get_project
    out: dict[str, Any] = {}
    ep = get_project(member_path)
    out["indexed_at"] = ep.indexed_at if ep else None
    out["indexed_at_fresh"] = bool(ep and ep.indexed_at)
    out["embedding_dim"] = getattr(ep, "dims", 768) if ep else 768
    vdb = project_vector_db(member_path)
    if not vdb.exists():
        out["vector_db_missing"] = True
        return out
    try:
        con = sqlite3.connect(str(vdb), check_same_thread=False)
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        try:
            cc = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            vc = con.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
            out["chunk_count"] = cc
            out["orphan_count"] = abs(cc - vc)
        finally:
            con.close()
    except Exception as exc:
        out["vector_db_error"] = str(exc)
    gdb = project_graph_db(member_path)
    if not gdb.exists():
        out["graph_db_missing"] = True
        return out
    try:
        gcon = sqlite3.connect(str(gdb), check_same_thread=False)
        try:
            out["dangling_edges"] = gcon.execute(
                "SELECT COUNT(*) FROM edges e"
                " WHERE NOT EXISTS(SELECT 1 FROM symbols WHERE sid=e.caller_sid)"
                " OR NOT EXISTS(SELECT 1 FROM symbols WHERE sid=e.callee_sid)"
            ).fetchone()[0]
            out["bad_community_refs"] = gcon.execute(
                "SELECT COUNT(*) FROM symbols WHERE community_id IS NOT NULL"
                " AND community_id NOT IN(SELECT id FROM communities)"
            ).fetchone()[0]
            el1 = gcon.execute(
                "SELECT COUNT(*) FROM communities WHERE level=1 AND summary IS NOT NULL AND summary!=''"
            ).fetchone()[0]
            out["placeholder_communities"] = gcon.execute(
                "SELECT COUNT(*) FROM communities WHERE level=1 AND summary IS NOT NULL AND summary!=''"
                " AND(title LIKE 'Domain %' OR title='' OR title IS NULL)"
            ).fetchone()[0] if el1 > 0 else 0
            rp = str(Path(member_path).resolve())
            out["path_leakage"] = gcon.execute(
                "SELECT COUNT(*) FROM symbols WHERE file IS NOT NULL AND file!=''"
                " AND SUBSTR(file,1,?)!=?", (len(rp), rp)
            ).fetchone()[0]
        finally:
            gcon.close()
    except Exception as exc:
        out["graph_db_error"] = str(exc)
    pdb = root_process_db(root_path)
    if member_path == root_path and pdb.exists():
        try:
            pcon = sqlite3.connect(str(pdb), check_same_thread=False)
            try:
                t = pcon.execute("SELECT COUNT(*) FROM cross_service_edges").fetchone()[0]
                out["process_graph"] = {
                    "edge_count": t,
                    "unanchored": pcon.execute(
                        "SELECT COUNT(*) FROM cross_service_edges"
                        " WHERE caller_file='' OR caller_file IS NULL OR caller_line=0"
                    ).fetchone()[0] if t else 0,
                    "out_of_band": pcon.execute(
                        "SELECT COUNT(*) FROM cross_service_edges"
                        " WHERE confidence<0.5 OR confidence>1.0"
                    ).fetchone()[0] if t else 0,
                }
            finally:
                pcon.close()
        except Exception as exc:
            out["process_graph"] = {"error": str(exc)}
    else:
        out["process_graph"] = None
    return out


def _is_member_valid(c: dict[str, Any]) -> bool:
    if c.get("vector_db_missing") or c.get("graph_db_missing"):
        return False
    if c.get("vector_db_error") or c.get("graph_db_error"):
        return False
    if not c.get("indexed_at_fresh", False):
        return False
    if c.get("chunk_count", 0) == 0:
        return False
    if c.get("embedding_dim", 768) != 768:
        return False
    if any(c.get(k, 0) != 0 for k in (
        "orphan_count", "dangling_edges", "bad_community_refs",
        "placeholder_communities", "path_leakage",
    )):
        return False
    pg = c.get("process_graph")
    if pg is not None:
        if "error" in pg:
            return False
        if pg.get("unanchored", 0) != 0 or pg.get("out_of_band", 0) != 0:
            return False
    return True


def validate_index(project_path: str) -> dict:
    """Validate all index stores for *project_path* (expands federation).

    Returns ``{"verdict":"VALID"|"INVALID","member_count":N,"checks":{…},"members":[…]}``.
    """
    if not project_path:
        return {"verdict": "INVALID", "error": "no project_path provided", "members": []}
    from opencode_search.daemon.federation import expand_federation
    members = list(expand_federation(project_path)) or [project_path]
    reports: list[dict] = []
    all_valid = True
    for mp in members:
        chk = _check_member(mp, project_path)
        ok = _is_member_valid(chk)
        if not ok:
            all_valid = False
        reports.append({"path": mp, "valid": ok, "checks": chk})
    t = lambda k: sum(r["checks"].get(k, 0) for r in reports)  # noqa: E731
    agg: dict[str, Any] = {
        "chunk_count": t("chunk_count"), "orphan_count": t("orphan_count"),
        "embedding_dim": 768, "dangling_edges": t("dangling_edges"),
        "bad_community_refs": t("bad_community_refs"),
        "placeholder_communities": t("placeholder_communities"),
        "path_leakage": t("path_leakage"),
        "indexed_at_fresh": all(r["checks"].get("indexed_at_fresh", False) for r in reports),
    }
    root_r = next((r for r in reports if r["checks"].get("process_graph") is not None), None)
    if root_r:
        agg["process_graph"] = root_r["checks"]["process_graph"]
    return {"verdict": "VALID" if all_valid else "INVALID",
            "member_count": len(reports), "checks": agg, "members": reports}
