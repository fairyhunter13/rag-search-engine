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
            signature TEXT,
            docstring TEXT,
            community_id INTEGER,
            intent TEXT
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
            semantic_type TEXT
        );
    """)
    con.commit()
    # Schema migration: older DBs used node_count, new schema uses member_count.
    _cols = {r[1] for r in con.execute("PRAGMA table_info(communities)")}
    if "node_count" in _cols and "member_count" not in _cols:
        con.execute("ALTER TABLE communities RENAME COLUMN node_count TO member_count")
        con.commit()
    return con


class GraphStore:
    def __init__(self, db_path: Path) -> None:
        self._con = _open(db_path)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._con

    def upsert_symbol(self, sid: str, name: str, qualified_name: str, kind: str,
                      file: str, start_line: int, end_line: int, language: str,
                      signature: str = "", docstring: str = "") -> None:
        self._con.execute(
            """INSERT INTO symbols
               (sid,name,qualified_name,kind,file,start_line,end_line,language,signature,docstring)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(sid) DO UPDATE SET
                 name=excluded.name, qualified_name=excluded.qualified_name,
                 kind=excluded.kind, file=excluded.file,
                 start_line=excluded.start_line, end_line=excluded.end_line,
                 language=excluded.language""",
            (sid, name, qualified_name, kind, file, start_line, end_line, language, signature, docstring),
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
                         member_count: int, semantic_type: str = "") -> None:
        self._con.execute(
            """INSERT INTO communities (id,level,title,summary,member_count,semantic_type)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 title=COALESCE(excluded.title, title),
                 summary=COALESCE(excluded.summary, summary),
                 member_count=excluded.member_count,
                 semantic_type=COALESCE(excluded.semantic_type, semantic_type)""",
            (cid, level, title, summary, member_count, semantic_type),
        )

    def set_intent(self, sid: str, intent: str) -> None:
        self._con.execute("UPDATE symbols SET intent=? WHERE sid=?", (intent, sid))

    def symbol_count(self) -> int:
        return self._con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]

    def community_count(self) -> int:
        return self._con.execute("SELECT COUNT(*) FROM communities").fetchone()[0]

    def list_symbols(self, limit: int = 5000) -> list[dict]:
        rows = self._con.execute(
            "SELECT sid,name,qualified_name,kind,file,start_line,end_line,language,intent "
            "FROM symbols LIMIT ?", (limit,)
        ).fetchall()
        keys = ("sid", "name", "qualified_name", "kind", "file", "start_line", "end_line", "language", "intent")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def has_cross_community_edges(self) -> bool:
        row = self._con.execute(
            """SELECT 1 FROM edges e
               JOIN symbols s1 ON e.caller_sid=s1.sid
               JOIN symbols s2 ON e.callee_sid=s2.sid
               WHERE s1.community_id IS NOT NULL AND s2.community_id IS NOT NULL
                 AND s1.community_id != s2.community_id LIMIT 1"""
        ).fetchone()
        return row is not None

    def commit(self) -> None:
        self._con.commit()

    def close(self) -> None:
        self._con.commit()
        self._con.close()
