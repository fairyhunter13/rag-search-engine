"""SQLite-backed graph storage for structural code relationships.

Schema: nodes, edges, communities tables with BFS traversal via recursive CTEs.
Uses WAL journal mode for concurrent read safety.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any


@dataclass
class NodeData:
    id: str
    name: str
    qualified_name: str
    kind: str                   # file|module|class|function|method
    file: str
    start_line: int | None = None
    end_line: int | None = None
    language: str | None = None
    signature: str | None = None
    docstring: str | None = None
    community_id: int | None = None
    intent: str | None = None
    intent_at: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class EdgeData:
    from_id: str
    to_id: str
    kind: str                   # CALLS|IMPORTS|INHERITS|DEFINES
    confidence: float = 1.0
    resolution_strategy: str | None = None


@dataclass
class CommunityData:
    id: int
    title: str | None = None
    summary: str | None = None
    node_count: int = 0
    key_entry_points: list[str] = field(default_factory=list)
    generated_at: str | None = None
    created_at: str = ""
    level: int = 1                        # 1=micro (default), 2+=macro hierarchy levels
    parent_community_id: int | None = None  # ID of the level+1 community this belongs to


@dataclass
class CallChainRow:
    node_id: str
    depth: int
    name: str = ""
    qualified_name: str = ""
    file: str = ""
    kind: str = ""
    confidence: float = 1.0


_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    file TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    language TEXT,
    signature TEXT,
    docstring TEXT,
    community_id INTEGER,
    intent TEXT,
    intent_at TEXT,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS edges (
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    resolution_strategy TEXT,
    PRIMARY KEY (from_id, to_id, kind),
    FOREIGN KEY(from_id) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY(to_id) REFERENCES nodes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS communities (
    id INTEGER PRIMARY KEY,
    title TEXT,
    summary TEXT,
    node_count INTEGER NOT NULL DEFAULT 0,
    key_entry_points TEXT DEFAULT '[]',
    generated_at TEXT,
    created_at TEXT NOT NULL DEFAULT '',
    level INTEGER NOT NULL DEFAULT 1,
    parent_community_id INTEGER REFERENCES communities(id)
);

CREATE INDEX IF NOT EXISTS idx_communities_level  ON communities(level);
CREATE INDEX IF NOT EXISTS idx_communities_parent ON communities(parent_community_id);

CREATE INDEX IF NOT EXISTS idx_nodes_file      ON nodes(file);
CREATE INDEX IF NOT EXISTS idx_nodes_kind      ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_nodes_community ON nodes(community_id);
CREATE INDEX IF NOT EXISTS idx_nodes_name      ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_edges_from      ON edges(from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to        ON edges(to_id);
CREATE INDEX IF NOT EXISTS idx_edges_kind      ON edges(kind);
"""


class GraphStorage:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        for stmt in _DDL.split(";"):
            stmt = stmt.strip()
            if stmt:
                self._conn.execute(stmt)
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Apply additive schema migrations for databases created before hierarchy support."""
        db = self._conn
        assert db is not None
        cols = {r[1] for r in db.execute("PRAGMA table_info(communities)").fetchall()}
        with db:
            if "level" not in cols:
                db.execute("ALTER TABLE communities ADD COLUMN level INTEGER NOT NULL DEFAULT 1")
            if "parent_community_id" not in cols:
                db.execute("ALTER TABLE communities ADD COLUMN parent_community_id INTEGER")
            # Add indexes if they don't exist yet (CREATE INDEX IF NOT EXISTS is idempotent)
            db.execute("CREATE INDEX IF NOT EXISTS idx_communities_level  ON communities(level)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_communities_parent ON communities(parent_community_id)")

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> GraphStorage:
        self.open()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("GraphStorage not open")
        return self._conn

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def upsert_nodes(self, nodes: list[NodeData]) -> None:
        if not nodes:
            return
        db = self._db()
        now = _now()
        with db:
            db.executemany(
                """INSERT INTO nodes
                   (id, name, qualified_name, kind, file, start_line, end_line,
                    language, signature, docstring, community_id, intent, intent_at,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name,
                     qualified_name=excluded.qualified_name,
                     kind=excluded.kind,
                     file=excluded.file,
                     start_line=excluded.start_line,
                     end_line=excluded.end_line,
                     language=excluded.language,
                     signature=excluded.signature,
                     docstring=excluded.docstring,
                     updated_at=excluded.updated_at
                """,
                [
                    (
                        n.id, n.name, n.qualified_name, n.kind, n.file,
                        n.start_line, n.end_line, n.language, n.signature,
                        n.docstring, n.community_id, n.intent, n.intent_at,
                        n.created_at or now, now,
                    )
                    for n in nodes
                ],
            )

    def upsert_edges(self, edges: list[EdgeData]) -> None:
        if not edges:
            return
        db = self._db()
        with db:
            db.executemany(
                """INSERT INTO edges (from_id, to_id, kind, confidence, resolution_strategy)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(from_id, to_id, kind) DO UPDATE SET
                     confidence=excluded.confidence,
                     resolution_strategy=excluded.resolution_strategy
                """,
                [
                    (e.from_id, e.to_id, e.kind, e.confidence, e.resolution_strategy)
                    for e in edges
                ],
            )

    def delete_file(self, file: str) -> None:
        """Delete all nodes (and cascaded edges) for a file."""
        db = self._db()
        with db:
            db.execute("DELETE FROM nodes WHERE file = ?", (file,))

    def set_community(self, node_id: str, community_id: int) -> None:
        db = self._db()
        with db:
            db.execute(
                "UPDATE nodes SET community_id=? WHERE id=?",
                (community_id, node_id),
            )

    def set_community_batch(self, assignments: dict[str, int]) -> None:
        """Write community assignments for many nodes in a single transaction."""
        if not assignments:
            return
        db = self._db()
        with db:
            db.executemany(
                "UPDATE nodes SET community_id=? WHERE id=?",
                [(cid, nid) for nid, cid in assignments.items()],
            )

    def upsert_community(self, community: CommunityData) -> None:
        import json
        db = self._db()
        now = _now()
        with db:
            db.execute(
                """INSERT INTO communities
                   (id, title, summary, node_count, key_entry_points, generated_at, created_at,
                    level, parent_community_id)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     title=excluded.title,
                     summary=excluded.summary,
                     node_count=excluded.node_count,
                     key_entry_points=excluded.key_entry_points,
                     generated_at=excluded.generated_at,
                     level=excluded.level,
                     parent_community_id=excluded.parent_community_id
                """,
                (
                    community.id, community.title, community.summary,
                    community.node_count,
                    json.dumps(community.key_entry_points),
                    community.generated_at,
                    community.created_at or now,
                    community.level,
                    community.parent_community_id,
                ),
            )

    def upsert_communities_batch(self, communities: list[CommunityData]) -> None:
        """Write all community records in a single transaction."""
        import json
        if not communities:
            return
        db = self._db()
        now = _now()
        with db:
            db.executemany(
                """INSERT INTO communities
                   (id, title, summary, node_count, key_entry_points, generated_at, created_at,
                    level, parent_community_id)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     title=excluded.title,
                     summary=excluded.summary,
                     node_count=excluded.node_count,
                     key_entry_points=excluded.key_entry_points,
                     generated_at=excluded.generated_at,
                     level=excluded.level,
                     parent_community_id=excluded.parent_community_id
                """,
                [
                    (
                        c.id, c.title, c.summary, c.node_count,
                        json.dumps(c.key_entry_points),
                        c.generated_at, c.created_at or now,
                        c.level, c.parent_community_id,
                    )
                    for c in communities
                ],
            )

    def set_node_intent(self, node_id: str, intent: str, intent_at: str) -> None:
        db = self._db()
        with db:
            db.execute(
                "UPDATE nodes SET intent=?, intent_at=? WHERE id=?",
                (intent, intent_at, node_id),
            )

    # ------------------------------------------------------------------
    # Read — single node
    # ------------------------------------------------------------------

    def get_node(self, name: str) -> NodeData | None:
        """Look up a node by name (exact) or qualified_name (exact)."""
        db = self._db()
        row = db.execute(
            "SELECT * FROM nodes WHERE qualified_name=? OR name=? LIMIT 1",
            (name, name),
        ).fetchone()
        return _row_to_node(row) if row else None

    def get_nodes_by_name(self, name: str) -> list[NodeData]:
        """Return all nodes matching name or qualified_name."""
        db = self._db()
        rows = db.execute(
            "SELECT * FROM nodes WHERE name=? OR qualified_name=?",
            (name, name),
        ).fetchall()
        return [_row_to_node(r) for r in rows]

    def get_node_by_id(self, node_id: str) -> NodeData | None:
        db = self._db()
        row = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return _row_to_node(row) if row else None

    # ------------------------------------------------------------------
    # Read — graph traversal
    # ------------------------------------------------------------------

    def get_callers(self, node_id: str, depth: int = 5) -> list[CallChainRow]:
        """BFS upstream: who calls this node. Deduplicates by node_id (keeps min depth)."""
        db = self._db()
        rows = db.execute(
            """WITH RECURSIVE callers(id, depth, confidence) AS (
                 SELECT from_id, 1, confidence
                 FROM edges WHERE to_id=? AND kind='CALLS'
                 UNION
                 SELECT e.from_id, c.depth+1, e.confidence
                 FROM edges e
                 JOIN callers c ON e.to_id=c.id
                 WHERE c.depth < ? AND e.kind='CALLS'
               )
               SELECT c.id, MIN(c.depth) AS depth, MAX(c.confidence) AS confidence,
                      n.name, n.qualified_name, n.file, n.kind
               FROM callers c
               LEFT JOIN nodes n ON n.id=c.id
               GROUP BY c.id
               ORDER BY depth, n.qualified_name
            """,
            (node_id, depth),
        ).fetchall()
        return [_row_to_chain(r) for r in rows]

    def get_callees(self, node_id: str, depth: int = 5) -> list[CallChainRow]:
        """BFS downstream: what does this node call. Deduplicates by node_id (keeps min depth)."""
        db = self._db()
        rows = db.execute(
            """WITH RECURSIVE callees(id, depth, confidence) AS (
                 SELECT to_id, 1, confidence
                 FROM edges WHERE from_id=? AND kind='CALLS'
                 UNION
                 SELECT e.to_id, c.depth+1, e.confidence
                 FROM edges e
                 JOIN callees c ON e.from_id=c.id
                 WHERE c.depth < ? AND e.kind='CALLS'
               )
               SELECT c.id, MIN(c.depth) AS depth, MAX(c.confidence) AS confidence,
                      n.name, n.qualified_name, n.file, n.kind
               FROM callees c
               LEFT JOIN nodes n ON n.id=c.id
               GROUP BY c.id
               ORDER BY depth, n.qualified_name
            """,
            (node_id, depth),
        ).fetchall()
        return [_row_to_chain(r) for r in rows]

    def trace_path(self, from_id: str, to_id: str) -> list[str] | None:
        """BFS shortest path between two nodes. Returns ordered node_id list."""
        db = self._db()
        rows = db.execute(
            """WITH RECURSIVE path(id, trail, depth) AS (
                 SELECT ?, CAST(? AS TEXT), 0
                 UNION
                 SELECT e.to_id,
                        path.trail || ',' || e.to_id,
                        path.depth + 1
                 FROM edges e
                 JOIN path ON e.from_id=path.id
                 WHERE path.depth < 20
                   AND INSTR(',' || path.trail || ',', ',' || e.to_id || ',') = 0
               )
               SELECT trail FROM path WHERE id=? LIMIT 1
            """,
            (from_id, from_id, to_id),
        ).fetchone()
        if not rows:
            return None
        return rows[0].split(",")

    def get_community_nodes(self, community_id: int) -> list[NodeData]:
        db = self._db()
        rows = db.execute(
            "SELECT * FROM nodes WHERE community_id=? ORDER BY qualified_name",
            (community_id,),
        ).fetchall()
        return [_row_to_node(r) for r in rows]

    def all_nodes(self) -> list[NodeData]:
        db = self._db()
        rows = db.execute("SELECT * FROM nodes").fetchall()
        return [_row_to_node(r) for r in rows]

    def all_edges(self) -> list[EdgeData]:
        db = self._db()
        rows = db.execute("SELECT * FROM edges").fetchall()
        return [EdgeData(
            from_id=r["from_id"], to_id=r["to_id"], kind=r["kind"],
            confidence=r["confidence"] or 1.0,
            resolution_strategy=r["resolution_strategy"],
        ) for r in rows]

    def get_communities(
        self,
        limit: int | None = None,
        min_node_count: int = 1,
        order_by_size: bool = False,
        level: int | None = None,
    ) -> list[CommunityData]:
        """Return communities from the graph DB.

        Args:
            limit: Maximum number of communities to return. None = no limit.
            min_node_count: Only return communities with at least this many nodes.
                            Use 2 to exclude singletons (isolated symbols).
            order_by_size: If True, order by node_count DESC (largest first).
                           Default False preserves historical id ASC order so
                           existing callers like _enrich_communities are unaffected.
            level: Filter by hierarchy level (1=micro, 2+=macro). None = all levels.
        """
        import json
        db = self._db()
        order = "node_count DESC" if order_by_size else "id"
        conds = ["node_count >= ?"]
        params: list = [min_node_count]
        if level is not None:
            conds.append("level = ?")
            params.append(level)
        where = " AND ".join(conds)
        if limit is not None:
            rows = db.execute(
                f"SELECT * FROM communities WHERE {where} ORDER BY {order} LIMIT ?",
                (*params, limit),
            ).fetchall()
        else:
            rows = db.execute(
                f"SELECT * FROM communities WHERE {where} ORDER BY {order}",
                params,
            ).fetchall()
        result = []
        for r in rows:
            ep = r["key_entry_points"]
            result.append(CommunityData(
                id=r["id"], title=r["title"], summary=r["summary"],
                node_count=r["node_count"],
                key_entry_points=json.loads(ep) if ep else [],
                generated_at=r["generated_at"],
                created_at=r["created_at"] or "",
                level=_col(r, "level", 1),
                parent_community_id=_col(r, "parent_community_id", None),
            ))
        return result

    def get_max_community_level(self) -> int:
        """Return the highest hierarchy level present in this graph (1 if no hierarchy built)."""
        db = self._db()
        row = db.execute("SELECT MAX(level) AS max_level FROM communities").fetchone()
        return int(row["max_level"] or 1)

    def get_community_hierarchy(self, root_level: int | None = None) -> dict[int, list[CommunityData]]:
        """Return all communities grouped by level, from highest (root) to lowest (micro).

        If root_level is None, uses the maximum level present.
        Returns {level: [CommunityData, ...]} ordered level DESC.
        """
        max_level = self.get_max_community_level()
        result: dict[int, list[CommunityData]] = {}
        for lvl in range(max_level, 0, -1):
            result[lvl] = self.get_communities(level=lvl, order_by_size=True)
        return result

    def get_communities_for_files(self, file_paths: list[str]) -> list[int]:
        """Return IDs of communities containing any nodes from the given file paths."""
        if not file_paths:
            return []
        placeholders = ",".join("?" * len(file_paths))
        rows = self._db().execute(
            f"SELECT DISTINCT community_id FROM nodes "
            f"WHERE file IN ({placeholders}) AND community_id IS NOT NULL",
            file_paths,
        ).fetchall()
        return [r[0] for r in rows if r[0] is not None]

    def node_count(self) -> int:
        return self._db().execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

    def edge_count(self) -> int:
        return self._db().execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    def get_god_nodes(self, top_n: int = 10) -> list[dict]:
        """Return the top-N highest-degree nodes (most total edges in+out).

        These are the 'hub' symbols that the rest of the codebase depends on.
        A high in-degree means many callers; a high out-degree means many
        dependencies — together they flag architectural pivot points.
        """
        db = self._db()
        rows = db.execute(
            """
            SELECT n.id, n.qualified_name, n.kind, n.file, n.community_id,
                   COUNT(DISTINCT e_in.from_id)  AS in_degree,
                   COUNT(DISTINCT e_out.to_id)   AS out_degree,
                   COUNT(DISTINCT e_in.from_id) + COUNT(DISTINCT e_out.to_id) AS degree
            FROM nodes n
            LEFT JOIN edges e_in  ON e_in.to_id   = n.id
            LEFT JOIN edges e_out ON e_out.from_id = n.id
            WHERE n.community_id IS NOT NULL
            GROUP BY n.id
            ORDER BY degree DESC
            LIMIT ?
            """,
            (top_n,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "qualified_name": r["qualified_name"],
                "kind": r["kind"],
                "file": r["file"],
                "community_id": r["community_id"],
                "in_degree": r["in_degree"],
                "out_degree": r["out_degree"],
                "degree": r["degree"],
            }
            for r in rows
        ]

    def get_cross_community_bridges(self, top_n: int = 10) -> list[dict]:
        """Return the top-N cross-community edges by combined node degree.

        A bridge is an edge whose endpoints belong to different communities.
        High-degree bridges are 'surprising connections' — tightly-coupled
        symbols that span architectural boundaries.
        """
        db = self._db()
        rows = db.execute(
            """
            SELECT e.from_id, e.to_id, e.kind, e.confidence,
                   nf.qualified_name AS from_name, nf.community_id AS from_comm,
                   nt.qualified_name AS to_name,   nt.community_id AS to_comm
            FROM edges e
            JOIN nodes nf ON nf.id = e.from_id
            JOIN nodes nt ON nt.id = e.to_id
            WHERE nf.community_id IS NOT NULL
              AND nt.community_id IS NOT NULL
              AND nf.community_id != nt.community_id
              AND e.kind IN ('CALLS', 'IMPORTS')
            ORDER BY e.confidence DESC
            LIMIT ?
            """,
            (top_n,),
        ).fetchall()
        return [
            {
                "from": r["from_name"],
                "to": r["to_name"],
                "kind": r["kind"],
                "confidence": r["confidence"],
                "from_community": r["from_comm"],
                "to_community": r["to_comm"],
            }
            for r in rows
        ]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _now() -> str:
    from datetime import datetime
    return datetime.now(UTC).isoformat()


def _col(row: sqlite3.Row, col: str, default: object = None) -> object:
    """Read a column from a sqlite3.Row by name, falling back to default.

    sqlite3.Row.__contains__ checks VALUES not column names, so the common
    `if "col" in row` pattern is always False. Use this helper instead.
    """
    try:
        return row[col]
    except IndexError:
        return default


def _row_to_node(r: sqlite3.Row) -> NodeData:
    return NodeData(
        id=r["id"],
        name=r["name"],
        qualified_name=r["qualified_name"],
        kind=r["kind"],
        file=r["file"],
        start_line=r["start_line"],
        end_line=r["end_line"],
        language=r["language"],
        signature=r["signature"],
        docstring=r["docstring"],
        community_id=r["community_id"],
        intent=r["intent"],
        intent_at=r["intent_at"],
        created_at=r["created_at"] or "",
        updated_at=r["updated_at"] or "",
    )


def _row_to_chain(r: sqlite3.Row) -> CallChainRow:
    return CallChainRow(
        node_id=r["id"],
        depth=r["depth"],
        name=r["name"] or "",
        qualified_name=r["qualified_name"] or "",
        file=r["file"] or "",
        kind=r["kind"] or "",
        confidence=r["confidence"] or 1.0,
    )
