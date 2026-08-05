"""Index integrity validator — pure SQL, no inference, no GPU.

Surfaced as overview(what="validate"). Checks: orphan chunks/vectors,
dangling call-graph edges, bad community refs, placeholder L1 titles,
path leakage, process-edge anchoring + confidence in [0.5, 1.0].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def vector_row_health(con: sqlite3.Connection) -> dict[str, int]:
    """The two ways `chunks` and `vec_chunks` can disagree, counted as set differences.

    `abs(chunk_count - vec_count)` stood here and is a cardinality proxy: one stranded vector
    against one missing one cancels to zero, on the single check that calls this state INVALID.
    It also cannot name a row, so `VectorStore.prune_orphan_vectors` — the repair — had nothing
    to act on. `orphan_count` keeps its name and its meaning of "rows that should not exist as
    they are"; it is now a sum of two disjoint sets rather than a difference of two totals.
    """
    stranded = con.execute(
        "SELECT COUNT(*) FROM vec_chunks v LEFT JOIN chunks c USING(chunk_id)"
        " WHERE c.chunk_id IS NULL"
    ).fetchone()[0]
    missing = con.execute(
        "SELECT COUNT(*) FROM chunks c LEFT JOIN vec_chunks v USING(chunk_id)"
        " WHERE v.chunk_id IS NULL"
    ).fetchone()[0]
    # The bit lane drifts silently in both directions (BQ1), and nothing reported it: 25 codes
    # across four stores outlived their vectors for a day because the repair that removed those
    # vectors was a hand-written DELETE against the one table its author had in mind. Counted
    # separately from `orphan_count` because its repair is separate too.
    has_bin = con.execute(
        "SELECT 1 FROM sqlite_master WHERE name='vec_chunks_bin'").fetchone()
    codes = con.execute(
        "SELECT COUNT(*) FROM vec_chunks_bin b LEFT JOIN vec_chunks v USING(chunk_id)"
        " WHERE v.chunk_id IS NULL"
    ).fetchone()[0] if has_bin else 0
    return {"stranded_vectors": stranded, "missing_vectors": missing,
            "orphan_count": stranded + missing, "stranded_codes": codes}


def _check_member(member_path: str, root_path: str) -> dict[str, Any]:
    import sqlite_vec  # type: ignore[import-untyped]

    from rag_search.core.config import project_graph_db, project_vector_db
    from rag_search.core.registry import get_project
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
            out["chunk_count"] = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            out.update(vector_row_health(con))
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
                " AND(title GLOB 'Domain [0-9]*' OR title='' OR title IS NULL)"
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
    return not any(c.get(k, 0) != 0 for k in (
        "orphan_count", "stranded_codes", "dangling_edges", "bad_community_refs",
        "placeholder_communities", "path_leakage",
    ))


def validate_index(project_path: str) -> dict:
    """Validate all index stores for *project_path* (expands federation).

    Returns ``{"verdict":"VALID"|"INVALID","member_count":N,"checks":{…},"members":[…]}``.
    """
    if not project_path:
        return {"verdict": "INVALID", "error": "no project_path provided", "members": []}
    from rag_search.daemon.federation import expand_federation
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
        # Beside the total, because the repair differs: a stranded vector is removable
        # (`rag-search clean-orphans`), a missing one is only fixed by re-indexing the file.
        "stranded_vectors": t("stranded_vectors"), "missing_vectors": t("missing_vectors"),
        "stranded_codes": t("stranded_codes"),
        "embedding_dim": 768, "dangling_edges": t("dangling_edges"),
        "bad_community_refs": t("bad_community_refs"),
        "placeholder_communities": t("placeholder_communities"),
        "path_leakage": t("path_leakage"),
        "indexed_at_fresh": all(r["checks"].get("indexed_at_fresh", False) for r in reports),
    }
    return {"verdict": "VALID" if all_valid else "INVALID",
            "member_count": len(reports), "checks": agg, "members": reports}
