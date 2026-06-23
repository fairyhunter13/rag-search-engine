"""Deterministic structural spine (Information rung): dir/file nodes in communities.

Zero LLM tokens. Idempotent via CRC32-based ids (dir: +2^30, file: +2^31 range).
level=0, kind='dir'/'file'. semantic_type always NULL — structure is Information,
never Knowledge-interpreted (HR22, DIKW doctrine).
"""
from __future__ import annotations

import os
from binascii import crc32 as _crc32
from pathlib import Path

from opencode_search.graph.store import GraphStore
from opencode_search.index.discover import _EXCLUDE

_DIR_BASE: int = 1 << 30   # 1_073_741_824 — well above any L1/L2/L3 id
_FILE_BASE: int = 1 << 31  # 2_147_483_648


def _dir_id(rel: str) -> int:
    return _DIR_BASE + (_crc32(("dir:" + rel).encode()) & 0x3FFFFFFF)


def _file_id(rel: str) -> int:
    return _FILE_BASE + (_crc32(("file:" + rel).encode()) & 0x3FFFFFFF)


def build_structure_tree(store: GraphStore, root: str) -> None:
    """Insert dir/file structural spine into communities (level=0, kind='dir'/'file').

    Called from _enrich_project before Leiden narration. Includes code-less and
    docs-only directories — any dir the walk reaches. Same pruning as iter_files
    (_EXCLUDE + .egg-info). Idempotent: ON CONFLICT updates titles/summaries.
    """
    root_path = Path(root).resolve()

    dir_parents: dict[str, str | None] = {}  # rel_dir -> parent rel (None = root)
    file_parents: dict[str, str] = {}         # rel_file -> rel_dir

    for dirpath_str, dirnames, filenames in os.walk(str(root_path), followlinks=True):
        dp = Path(dirpath_str)
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _EXCLUDE and not d.endswith(".egg-info")
        )

        rel_dir = str(dp.relative_to(root_path)) if dp != root_path else "."
        if rel_dir not in dir_parents:
            if dp == root_path:
                dir_parents[rel_dir] = None
            else:
                parent_dp = dp.parent
                parent_rel = (
                    str(parent_dp.relative_to(root_path))
                    if parent_dp != root_path else "."
                )
                dir_parents[rel_dir] = parent_rel

        for fn in sorted(filenames):
            fp = dp / fn
            rel_file = str(fp.relative_to(root_path))
            file_parents[rel_file] = rel_dir

    # Count direct children per dir
    dir_file_counts: dict[str, int] = {}
    dir_subdir_counts: dict[str, int] = {}
    for _, parent_rel in file_parents.items():
        dir_file_counts[parent_rel] = dir_file_counts.get(parent_rel, 0) + 1
    for _, parent_rel in dir_parents.items():
        if parent_rel is not None:
            dir_subdir_counts[parent_rel] = dir_subdir_counts.get(parent_rel, 0) + 1

    for rel_dir, parent_rel in dir_parents.items():
        did = _dir_id(rel_dir)
        parent_did = _dir_id(parent_rel) if parent_rel is not None else None
        n_files = dir_file_counts.get(rel_dir, 0)
        n_subdirs = dir_subdir_counts.get(rel_dir, 0)
        name = Path(rel_dir).name if rel_dir != "." else root_path.name
        _upsert_struct(
            store, did, "dir", rel_dir, name,
            f"{n_subdirs} subdirectory(ies), {n_files} file(s).",
            n_files + n_subdirs, parent_did,
        )

    for rel_file, parent_rel in file_parents.items():
        fid = _file_id(rel_file)
        parent_did = _dir_id(parent_rel)
        abs_file = str(root_path / rel_file)
        sym_row = store._con.execute(
            "SELECT COUNT(*), language FROM symbols WHERE file=? "
            "GROUP BY language ORDER BY COUNT(*) DESC LIMIT 1",
            (abs_file,),
        ).fetchone()
        member_count = sym_row[0] if sym_row else 0
        language = (sym_row[1] or "unknown") if sym_row else "unknown"
        name = Path(rel_file).name
        _upsert_struct(
            store, fid, "file", rel_file, name,
            f"{member_count} symbol(s) [{language}].",
            member_count, parent_did,
        )

    store.commit()


def _upsert_struct(
    store: GraphStore,
    nid: int,
    kind: str,
    rel_path: str,
    title: str,
    summary: str,
    member_count: int,
    parent_id: int | None,
) -> None:
    store._con.execute(
        """INSERT INTO communities
               (id, level, kind, path, title, summary, member_count, parent_id)
           VALUES (?, 0, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             kind=excluded.kind, path=excluded.path,
             title=excluded.title, summary=excluded.summary,
             member_count=excluded.member_count, parent_id=excluded.parent_id""",
        (nid, kind, rel_path, title, summary, member_count, parent_id),
    )
    # Explicit NULL: structure is Information, never Knowledge-interpreted (HR22).
    store._con.execute("UPDATE communities SET semantic_type=NULL WHERE id=?", (nid,))
