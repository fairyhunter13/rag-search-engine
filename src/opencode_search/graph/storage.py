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
    # Graphify-compatible confidence labels: EXTRACTED|INFERRED|AMBIGUOUS
    # EXTRACTED: directly stated in source (import, explicit call). confidence=1.0
    # INFERRED:  reasoned from context (resolved call). confidence < 1.0 with rubric
    # AMBIGUOUS: uncertain, flagged for review
    confidence_label: str = "EXTRACTED"
    confidence_score: float | None = None  # only set for INFERRED edges (0.55–0.95 rubric)


_SEMANTIC_TYPES = frozenset({
    "feature", "business_process", "business_rule",
    "data_model", "api_boundary", "infrastructure", "utility",
})


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
    semantic_type: str | None = None      # Business classification: feature|business_process|business_rule|data_model|api_boundary|infrastructure|utility


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
    confidence_label TEXT NOT NULL DEFAULT 'EXTRACTED',
    confidence_score REAL,
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
    parent_community_id INTEGER REFERENCES communities(id),
    semantic_type TEXT
);

CREATE TABLE IF NOT EXISTS file_graph_hashes (
    file TEXT PRIMARY KEY,
    hash TEXT NOT NULL,
    indexed_at TEXT NOT NULL DEFAULT ''
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
        comm_cols = {r[1] for r in db.execute("PRAGMA table_info(communities)").fetchall()}
        edge_cols = {r[1] for r in db.execute("PRAGMA table_info(edges)").fetchall()}
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        with db:
            if "level" not in comm_cols:
                db.execute("ALTER TABLE communities ADD COLUMN level INTEGER NOT NULL DEFAULT 1")
            if "parent_community_id" not in comm_cols:
                db.execute("ALTER TABLE communities ADD COLUMN parent_community_id INTEGER")
            # Add indexes if they don't exist yet (CREATE INDEX IF NOT EXISTS is idempotent)
            db.execute("CREATE INDEX IF NOT EXISTS idx_communities_level  ON communities(level)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_communities_parent ON communities(parent_community_id)")
            # Edge confidence labels (graphify-compatible: EXTRACTED/INFERRED/AMBIGUOUS)
            if "confidence_label" not in edge_cols:
                db.execute(
                    "ALTER TABLE edges ADD COLUMN confidence_label TEXT NOT NULL DEFAULT 'EXTRACTED'"
                )
            if "confidence_score" not in edge_cols:
                db.execute("ALTER TABLE edges ADD COLUMN confidence_score REAL")
            # Incremental graph extraction cache (Phase 25)
            if "file_graph_hashes" not in tables:
                db.execute("""
                    CREATE TABLE IF NOT EXISTS file_graph_hashes (
                        file TEXT PRIMARY KEY,
                        hash TEXT NOT NULL,
                        indexed_at TEXT NOT NULL DEFAULT ''
                    )
                """)
            # Business semantic classification (Phase 29)
            if "semantic_type" not in comm_cols:
                db.execute("ALTER TABLE communities ADD COLUMN semantic_type TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS idx_communities_semantic_type ON communities(semantic_type)")

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
                """INSERT INTO edges
                   (from_id, to_id, kind, confidence, resolution_strategy,
                    confidence_label, confidence_score)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(from_id, to_id, kind) DO UPDATE SET
                     confidence=excluded.confidence,
                     resolution_strategy=excluded.resolution_strategy,
                     confidence_label=excluded.confidence_label,
                     confidence_score=excluded.confidence_score
                """,
                [
                    (
                        e.from_id, e.to_id, e.kind, e.confidence, e.resolution_strategy,
                        e.confidence_label, e.confidence_score,
                    )
                    for e in edges
                ],
            )

    def delete_file(self, file: str) -> None:
        """Delete all nodes (and cascaded edges) for a file."""
        db = self._db()
        with db:
            db.execute("DELETE FROM nodes WHERE file = ?", (file,))
            db.execute("DELETE FROM file_graph_hashes WHERE file = ?", (file,))

    def get_graph_file_hashes(self) -> dict[str, str]:
        """Return {file_path: hash} for all files recorded in the graph extraction cache."""
        db = self._db()
        rows = db.execute("SELECT file, hash FROM file_graph_hashes").fetchall()
        return {r["file"]: r["hash"] for r in rows}

    def set_graph_file_hashes_batch(self, hashes: dict[str, str]) -> None:
        """Upsert file→hash entries into the graph extraction cache."""
        if not hashes:
            return
        now = _now()
        db = self._db()
        with db:
            db.executemany(
                """INSERT INTO file_graph_hashes (file, hash, indexed_at)
                   VALUES (?,?,?)
                   ON CONFLICT(file) DO UPDATE SET hash=excluded.hash, indexed_at=excluded.indexed_at
                """,
                [(f, h, now) for f, h in hashes.items()],
            )

    def purge_deleted_file_hashes(self, existing_files: set[str]) -> None:
        """Remove hash entries for files that no longer exist on disk."""
        db = self._db()
        all_files = {r["file"] for r in db.execute("SELECT file FROM file_graph_hashes").fetchall()}
        stale = all_files - existing_files
        if stale:
            with db:
                db.executemany(
                    "DELETE FROM file_graph_hashes WHERE file = ?",
                    [(f,) for f in stale],
                )

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

    def set_community_batch_with_null(
        self,
        all_assignments: dict[str, int],
        real_assignments: dict[str, int],
    ) -> None:
        """Write community assignments, NULLing out singleton nodes.

        Nodes in `real_assignments` get their community_id set.
        Nodes in `all_assignments` but NOT in `real_assignments` (singletons)
        get community_id=NULL — they're isolated and don't belong to any cluster.
        """
        db = self._db()
        with db:
            if real_assignments:
                db.executemany(
                    "UPDATE nodes SET community_id=? WHERE id=?",
                    [(cid, nid) for nid, cid in real_assignments.items()],
                )
            singleton_ids = set(all_assignments.keys()) - set(real_assignments.keys())
            if singleton_ids:
                db.executemany(
                    "UPDATE nodes SET community_id=NULL WHERE id=?",
                    [(nid,) for nid in singleton_ids],
                )

    def upsert_community(self, community: CommunityData) -> None:
        import json
        db = self._db()
        now = _now()
        with db:
            db.execute(
                """INSERT INTO communities
                   (id, title, summary, node_count, key_entry_points, generated_at, created_at,
                    level, parent_community_id, semantic_type)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     title=COALESCE(excluded.title, communities.title),
                     summary=COALESCE(excluded.summary, communities.summary),
                     node_count=excluded.node_count,
                     key_entry_points=excluded.key_entry_points,
                     generated_at=excluded.generated_at,
                     level=excluded.level,
                     parent_community_id=excluded.parent_community_id,
                     semantic_type=COALESCE(excluded.semantic_type, communities.semantic_type)
                """,
                (
                    community.id, community.title, community.summary,
                    community.node_count,
                    json.dumps(community.key_entry_points),
                    community.generated_at,
                    community.created_at or now,
                    community.level,
                    community.parent_community_id,
                    community.semantic_type,
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
                    level, parent_community_id, semantic_type)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     title=COALESCE(excluded.title, communities.title),
                     summary=COALESCE(excluded.summary, communities.summary),
                     node_count=excluded.node_count,
                     key_entry_points=excluded.key_entry_points,
                     generated_at=excluded.generated_at,
                     level=excluded.level,
                     parent_community_id=excluded.parent_community_id,
                     semantic_type=COALESCE(excluded.semantic_type, communities.semantic_type)
                """,
                [
                    (
                        c.id, c.title, c.summary, c.node_count,
                        json.dumps(c.key_entry_points),
                        c.generated_at, c.created_at or now,
                        c.level, c.parent_community_id,
                        c.semantic_type,
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

    def has_unenriched_symbols(self) -> bool:
        """Return True if any function/method node has no intent set."""
        db = self._db()
        row = db.execute(
            "SELECT 1 FROM nodes WHERE kind IN ('function','method') "
            "AND (intent IS NULL OR intent='') LIMIT 1"
        ).fetchone()
        return row is not None

    def all_edges(self) -> list[EdgeData]:
        db = self._db()
        rows = db.execute("SELECT * FROM edges").fetchall()
        return [EdgeData(
            from_id=r["from_id"], to_id=r["to_id"], kind=r["kind"],
            confidence=r["confidence"] or 1.0,
            resolution_strategy=r["resolution_strategy"],
            confidence_label=_col(r, "confidence_label", "EXTRACTED"),
            confidence_score=_col(r, "confidence_score", None),
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
                semantic_type=_col(r, "semantic_type", None),
            ))
        return result

    def get_communities_by_semantic_type(self, semantic_type: str) -> list[CommunityData]:
        """Return all enriched communities matching a given semantic_type, largest first."""
        import json
        db = self._db()
        rows = db.execute(
            "SELECT * FROM communities WHERE semantic_type=? AND node_count>=2 ORDER BY node_count DESC",
            (semantic_type,),
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
                semantic_type=r["semantic_type"],
            ))
        return result

    def get_semantic_type_counts(self) -> dict[str, int]:
        """Return count of enriched communities per semantic_type."""
        db = self._db()
        rows = db.execute(
            "SELECT semantic_type, COUNT(*) as cnt FROM communities WHERE semantic_type IS NOT NULL AND node_count>=2 GROUP BY semantic_type ORDER BY cnt DESC"
        ).fetchall()
        return {r["semantic_type"]: r["cnt"] for r in rows}

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

    def vacuum(self) -> dict:
        """Run SQLite VACUUM + WAL checkpoint + prune singleton/orphan communities.

        Singletons (node_count=1) and orphans (no nodes reference them) waste
        space without providing enrichment value. Pruning them, then running
        VACUUM, can reclaim 10-50 MB on large projects.

        Returns before/after file size so the caller can log reclaimed bytes.
        """
        import os as _os
        db = self._db()
        try:
            before = _os.path.getsize(self._db_path)
        except OSError:
            before = 0
        try:
            with db:
                # NULL out community_id for nodes in singleton communities
                db.execute("""
                    UPDATE nodes SET community_id = NULL
                    WHERE community_id IN (
                        SELECT id FROM communities WHERE node_count = 1
                    )
                """)
                # Delete singleton communities
                db.execute("DELETE FROM communities WHERE node_count = 1")
                # Delete orphan communities (no nodes reference them)
                db.execute("""
                    DELETE FROM communities
                    WHERE id NOT IN (
                        SELECT DISTINCT community_id FROM nodes
                        WHERE community_id IS NOT NULL
                    )
                """)
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("VACUUM")
            db.commit()
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        try:
            after = _os.path.getsize(self._db_path)
        except OSError:
            after = before
        saved_mb = round((before - after) / 1024 / 1024, 1)
        return {"status": "ok", "before_mb": round(before / 1024 / 1024, 1), "after_mb": round(after / 1024 / 1024, 1), "saved_mb": saved_mb}

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

    def find_import_cycles(
        self,
        max_cycle_length: int = 8,
        top_n: int = 20,
    ) -> list[dict]:
        """Detect circular import dependencies at the file level.

        Builds a directed file-level import graph from IMPORTS edges, then
        finds simple cycles via iterative DFS (Tarjan-style SCC detection).
        Returns shortest cycles first, deduplicated by rotation.

        Returns list of:
          {"cycle": ["a.go", "b.go", "c.go"], "length": 3, "severity": "high"|"medium"|"low"}
        """
        db = self._db()
        rows = db.execute(
            """
            SELECT nf.file AS from_file, nt.file AS to_file
            FROM edges e
            JOIN nodes nf ON nf.id = e.from_id
            JOIN nodes nt ON nt.id = e.to_id
            WHERE e.kind IN ('IMPORTS', 'INHERITS')
              AND nf.file != nt.file
              AND nf.file != ''
              AND nt.file != ''
            """,
        ).fetchall()

        # Build adjacency list (file → set of files it imports)
        graph: dict[str, set[str]] = {}
        for r in rows:
            ff, tf = r["from_file"], r["to_file"]
            graph.setdefault(ff, set()).add(tf)
            graph.setdefault(tf, set())  # ensure all nodes exist

        if not graph:
            return []

        # Tarjan's SCC — finds all strongly connected components
        index_counter = [0]
        stack: list[str] = []
        lowlinks: dict[str, int] = {}
        index: dict[str, int] = {}
        on_stack: dict[str, bool] = {}
        sccs: list[list[str]] = []

        def strongconnect(v: str) -> None:
            index[v] = index_counter[0]
            lowlinks[v] = index_counter[0]
            index_counter[0] += 1
            stack.append(v)
            on_stack[v] = True

            for w in graph.get(v, set()):
                if w not in index:
                    strongconnect(w)
                    lowlinks[v] = min(lowlinks[v], lowlinks[w])
                elif on_stack.get(w):
                    lowlinks[v] = min(lowlinks[v], index[w])

            if lowlinks[v] == index[v]:
                scc: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    scc.append(w)
                    if w == v:
                        break
                if len(scc) > 1:
                    sccs.append(scc)

        import sys
        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(old_limit, 5000))
        try:
            for v in list(graph.keys()):
                if v not in index:
                    strongconnect(v)
        finally:
            sys.setrecursionlimit(old_limit)

        # Convert SCCs to cycle format, cap at max_cycle_length
        results = []
        seen: set[tuple] = set()
        for scc in sorted(sccs, key=len):
            if len(scc) > max_cycle_length:
                continue
            # Normalize: start from lexicographically smallest element
            min_idx = scc.index(min(scc))
            normalized = tuple(scc[min_idx:] + scc[:min_idx])
            if normalized in seen:
                continue
            seen.add(normalized)
            length = len(normalized)
            severity = "high" if length <= 2 else ("medium" if length <= 4 else "low")
            results.append({
                "cycle": list(normalized),
                "length": length,
                "severity": severity,
            })
            if len(results) >= top_n:
                break

        return results

    def suggest_questions(self, top_n: int = 7) -> list[dict]:
        """Generate questions the graph is uniquely positioned to answer.

        Inspired by graphify's analyze.py suggest_questions(). Uses structural
        signals: isolated nodes, cross-community bridges, large communities,
        god nodes with many outbound edges.

        Returns list of:
          {"type": str, "question": str, "why": str}
        """
        db = self._db()
        questions: list[dict] = []

        # 1. Isolated nodes (no callers, no callees — possible dead code or missing docs)
        isolated = db.execute(
            """
            SELECT n.name, n.file, n.kind
            FROM nodes n
            WHERE n.kind NOT IN ('file', 'module')
              AND n.community_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.from_id = n.id OR e.to_id = n.id)
            LIMIT 10
            """,
        ).fetchall()
        if isolated:
            names = [r["name"] for r in isolated[:3]]
            questions.append({
                "type": "isolated_nodes",
                "question": f"What connects `{'`, `'.join(names)}` to the rest of the system?",
                "why": f"{len(isolated)} nodes have no edges — possible dead code, missing docs, or extraction gaps.",
                "count": len(isolated),
            })

        # 2. God nodes — symbols everything depends on (high in-degree)
        gods = self.get_god_nodes(top_n=3)
        if gods:
            top = gods[0]
            questions.append({
                "type": "god_node",
                "question": f"What would break if `{top['qualified_name']}` changed?",
                "why": f"{top['qualified_name']} has {top['degree']} total edges — it's a critical hub. Changes propagate widely.",
                "count": top["degree"],
            })

        # 3. Cross-community bridges — architectural boundary crossings
        bridges = self.get_cross_community_bridges(top_n=3)
        if bridges:
            b = bridges[0]
            questions.append({
                "type": "bridge",
                "question": f"Why does `{b['from']}` (community {b['from_community']}) depend on `{b['to']}` (community {b['to_community']})?",
                "why": "This edge crosses architectural community boundaries — unexpected coupling worth reviewing.",
            })

        # 4. Communities with very few nodes — possibly misclustered
        tiny = db.execute(
            """
            SELECT id, title, node_count FROM communities
            WHERE node_count = 1 AND level = 1
            LIMIT 5
            """,
        ).fetchall()
        if len(tiny) >= 3:
            questions.append({
                "type": "singleton_communities",
                "question": f"Why are {len(tiny)} communities isolated singletons?",
                "why": f"{len(tiny)} communities have only 1 node — they may represent dead code, entry points, or need deeper graph extraction.",
                "count": len(tiny),
            })

        # 5. Large communities — possibly too broad, should be split
        large = db.execute(
            """
            SELECT id, title, node_count FROM communities
            WHERE node_count > 50 AND level = 1
            ORDER BY node_count DESC
            LIMIT 3
            """,
        ).fetchall()
        if large:
            top_large = large[0]
            label = top_large["title"] or f"Community {top_large['id']}"
            questions.append({
                "type": "large_community",
                "question": f"Should `{label}` be split into smaller, more focused modules?",
                "why": f"Community has {top_large['node_count']} nodes — large communities often hide multiple concerns.",
                "node_count": top_large["node_count"],
            })

        # 6. Symbols with high out-degree but no community — possible missing enrichment
        unenriched = db.execute(
            """
            SELECT n.name, COUNT(*) AS edge_count
            FROM nodes n
            JOIN edges e ON e.from_id = n.id
            WHERE n.community_id IS NULL AND n.kind NOT IN ('file', 'module')
            GROUP BY n.id
            ORDER BY edge_count DESC
            LIMIT 3
            """,
        ).fetchall()
        if unenriched:
            questions.append({
                "type": "unenriched_hubs",
                "question": f"Why is `{unenriched[0]['name']}` highly connected but not assigned to any community?",
                "why": "High-edge nodes without community assignment reduce graph comprehension quality. Run `build(action='pipeline')` to enrich.",
            })

        if not questions:
            return [{
                "type": "no_signal",
                "question": None,
                "why": "Graph is well-structured: no isolated nodes, no oversized communities, no unenriched hubs detected.",
            }]

        return questions[:top_n]

    def graph_diff(self, since_iso: str = "", since_hours: int | None = None) -> dict:
        """Return what changed in the graph since a given ISO timestamp.

        Uses the updated_at column on nodes to find additions and modifications
        since `since_iso`. Useful for "what changed in my codebase?" queries.

        Args:
            since_iso: ISO 8601 timestamp string (e.g. "2026-06-01T00:00:00").
                       If empty, `since_hours` is used. Defaults to 24h when both absent.
            since_hours: Look back this many hours from now (overridden by since_iso).

        Returns:
            {
              "new_nodes": [...],     # nodes with updated_at > since_iso
              "changed_files": [...], # distinct files with changed nodes
              "new_edges": int,       # edges from new nodes (approximate)
              "summary": "N new symbols in M files since ...",
              "since": since_iso,
            }
        """
        if not since_iso:
            from datetime import UTC, datetime, timedelta
            hours = since_hours if since_hours is not None else 24
            since_iso = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()

        db = self._db()
        new_nodes = db.execute(
            """
            SELECT id, name, qualified_name, kind, file, language, community_id, updated_at
            FROM nodes
            WHERE updated_at > ? AND kind NOT IN ('file', 'module')
            ORDER BY updated_at DESC
            LIMIT 200
            """,
            (since_iso,),
        ).fetchall()

        changed_files = sorted({r["file"] for r in new_nodes if r["file"]})
        node_ids = [r["id"] for r in new_nodes]

        new_edge_count = 0
        if node_ids:
            ph = ",".join("?" * len(node_ids))
            new_edge_count = db.execute(
                f"SELECT COUNT(*) FROM edges WHERE from_id IN ({ph})",
                node_ids,
            ).fetchone()[0]

        nodes_out = [
            {
                "id": r["id"],
                "name": r["name"],
                "qualified_name": r["qualified_name"],
                "kind": r["kind"],
                "file": r["file"],
                "language": r["language"],
                "community_id": r["community_id"],
                "updated_at": r["updated_at"],
            }
            for r in new_nodes
        ]

        summary_parts = []
        if nodes_out:
            summary_parts.append(f"{len(nodes_out)} new/changed symbols")
        if changed_files:
            summary_parts.append(f"{len(changed_files)} files")
        summary = (", ".join(summary_parts) + f" since {since_iso}") if summary_parts else f"no changes since {since_iso}"

        return {
            "new_nodes": nodes_out,
            "changed_files": changed_files,
            "new_edge_count": new_edge_count,
            "summary": summary,
            "since": since_iso,
            "total_new": len(nodes_out),
        }

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
