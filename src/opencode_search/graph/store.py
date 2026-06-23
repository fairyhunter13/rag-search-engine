"""SQLite graph store: symbols, edges, communities."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def _open(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS symbols (
            sid TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            qualified_name TEXT,
            kind TEXT,
            file TEXT,
            start_line INTEGER,
            end_line INTEGER,
            language TEXT,
            community_id INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_sym_file ON symbols(file);
        CREATE INDEX IF NOT EXISTS idx_sym_name ON symbols(name);
        CREATE TABLE IF NOT EXISTS edges (
            caller_sid TEXT,
            callee_sid TEXT,
            PRIMARY KEY (caller_sid, callee_sid)
        );
        CREATE TABLE IF NOT EXISTS communities (
            id INTEGER PRIMARY KEY,
            level INTEGER NOT NULL DEFAULT 1,
            title TEXT,
            summary TEXT,
            member_count INTEGER DEFAULT 0,
            semantic_type TEXT,
            narrated INTEGER DEFAULT 0
        );
    """)
    con.commit()
    # Schema migration: older DBs used node_count, new schema uses member_count.
    _cols = {r[1] for r in con.execute("PRAGMA table_info(communities)")}
    if "node_count" in _cols and "member_count" not in _cols:
        con.execute("ALTER TABLE communities RENAME COLUMN node_count TO member_count")
        con.commit()
    # Schema migration: older DBs used edges(from_id,to_id,kind,...) + a nodes table.
    # Those rows are fully orphaned (0 endpoints match current symbols.sid) so we drop
    # and recreate; the current schema is repopulated by the next full re-index.
    _edge_cols = {r[1] for r in con.execute("PRAGMA table_info(edges)")}
    if "caller_sid" not in _edge_cols:
        con.executescript("""
            DROP TABLE IF EXISTS edges;
            DROP TABLE IF EXISTS nodes;
            CREATE TABLE IF NOT EXISTS edges (
                caller_sid TEXT,
                callee_sid TEXT,
                PRIMARY KEY (caller_sid, callee_sid)
            );
        """)
        con.commit()
    # Schema migration: add parent_id for L1→L2 hierarchy links.
    if "parent_id" not in _cols:
        con.execute("ALTER TABLE communities ADD COLUMN parent_id INTEGER")
        con.commit()
    # Schema migration F-G/F-D: drop write-only columns (signature, docstring, intent).
    # SQLite 3.35+ supports ALTER TABLE DROP COLUMN; current system has 3.45.1.
    _sym_cols = {r[1] for r in con.execute("PRAGMA table_info(symbols)")}
    for _dead_col in ("signature", "docstring", "intent"):
        if _dead_col in _sym_cols:
            con.execute(f"ALTER TABLE symbols DROP COLUMN {_dead_col}")
    if any(c in _sym_cols for c in ("signature", "docstring", "intent")):
        con.commit()
    # Schema migration: Phase 2 Information spine — kind (dir/file/community/domain) + path.
    if "kind" not in _cols:
        con.execute("ALTER TABLE communities ADD COLUMN kind TEXT DEFAULT 'community'")
        con.execute("UPDATE communities SET kind='domain' WHERE level>=2")
        con.commit()
    if "path" not in _cols:
        con.execute("ALTER TABLE communities ADD COLUMN path TEXT")
        con.commit()
    # Schema migration Phase 3: narrated flag (0=unnarrated tail/structure, 1=LLM-narrated).
    # Backfill narrated=1 only for communities with LLM evidence (semantic_type IS NOT NULL);
    # templated tails (semantic_type IS NULL) stay narrated=0 per the abstention doctrine.
    if "narrated" not in _cols:
        con.execute("ALTER TABLE communities ADD COLUMN narrated INTEGER DEFAULT 0")
        con.execute(
            "UPDATE communities SET narrated=1 "
            "WHERE level>=1 AND summary IS NOT NULL AND summary!='' "
            "AND kind NOT IN ('dir','file') AND semantic_type IS NOT NULL"
        )
        con.commit()
    # Data migration Phase 4D: correct over-stamped narrated=1 on tail rows.
    # Phase 3 backfill used summary IS NOT NULL; existing DBs may have narrated=1 on
    # tail rows (semantic_type IS NULL). One-time sweep corrects them to narrated=0 so
    # the classify gate and retrieval selectors behave correctly.
    _over = con.execute(
        "SELECT COUNT(*) FROM communities "
        "WHERE narrated=1 AND semantic_type IS NULL AND kind NOT IN ('dir','file')"
    ).fetchone()[0]
    if _over:
        con.execute(
            "UPDATE communities SET narrated=0 "
            "WHERE narrated=1 AND semantic_type IS NULL AND kind NOT IN ('dir','file')"
        )
        con.commit()
    return con


class GraphStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._con = _open(db_path)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._con

    def upsert_symbol(self, sid: str, name: str, qualified_name: str, kind: str,
                      file: str, start_line: int, end_line: int, language: str) -> None:
        self._con.execute(
            """INSERT INTO symbols
               (sid,name,qualified_name,kind,file,start_line,end_line,language)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(sid) DO UPDATE SET
                 name=excluded.name, qualified_name=excluded.qualified_name,
                 kind=excluded.kind, file=excluded.file,
                 start_line=excluded.start_line, end_line=excluded.end_line,
                 language=excluded.language""",
            (sid, name, qualified_name, kind, file, start_line, end_line, language),
        )

    def dedup_symbols(self) -> int:
        """Delete duplicate (name,file,kind) symbols keeping the lowest-rowid entry."""
        cur = self._con.execute(
            "DELETE FROM symbols WHERE rowid NOT IN "
            "(SELECT MIN(rowid) FROM symbols GROUP BY name,file,kind)"
        )
        self._con.commit()
        return cur.rowcount

    def upsert_edge(self, caller_sid: str, callee_sid: str) -> None:
        self._con.execute(
            "INSERT OR IGNORE INTO edges (caller_sid,callee_sid) VALUES (?,?)",
            (caller_sid, callee_sid),
        )

    def assign_community(self, sid: str, community_id: int) -> None:
        self._con.execute("UPDATE symbols SET community_id=? WHERE sid=?", (community_id, sid))

    def upsert_community(self, cid: int, level: int, title: str | None, summary: str,
                         member_count: int, semantic_type: str = "",
                         narrated: int | None = None) -> None:
        self._con.execute(
            """INSERT INTO communities (id,level,title,summary,member_count,semantic_type,narrated)
               VALUES (?,?,?,?,?,?,COALESCE(?,0))
               ON CONFLICT(id) DO UPDATE SET
                 title=COALESCE(excluded.title, title),
                 summary=COALESCE(excluded.summary, summary),
                 member_count=excluded.member_count,
                 semantic_type=COALESCE(excluded.semantic_type, semantic_type),
                 narrated=MAX(narrated, COALESCE(excluded.narrated, 0))""",
            (cid, level, title, summary, member_count, semantic_type, narrated),
        )

    def set_community_parent(self, cid: int, parent_id: int) -> None:
        self._con.execute("UPDATE communities SET parent_id=? WHERE id=?", (parent_id, cid))

    def clear(self) -> None:
        """Wipe symbols/edges/communities before a full re-index so stale rows don't persist."""
        self._con.executescript("DELETE FROM symbols; DELETE FROM edges; DELETE FROM communities;")
        self._con.commit()

    def symbol_count(self) -> int:
        return self._con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]

    def edge_count(self) -> int:
        return self._con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    def community_count(self) -> int:
        """Count semantic communities (level>=1). Excludes structural spine (level=0)."""
        return self._con.execute("SELECT COUNT(*) FROM communities WHERE level>=1").fetchone()[0]

    def list_symbols(self, limit: int = 5000) -> list[dict]:
        rows = self._con.execute(
            "SELECT sid,name,qualified_name,kind,file,start_line,end_line,language "
            "FROM symbols LIMIT ?", (limit,)
        ).fetchall()
        keys = ("sid", "name", "qualified_name", "kind", "file", "start_line", "end_line", "language")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def commit(self) -> None:
        self._con.commit()

    def close(self) -> None:
        self._con.commit()
        self._con.close()
