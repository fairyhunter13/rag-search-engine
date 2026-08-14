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
            member_count INTEGER DEFAULT 0
        );
        -- One row per file the derive attempted, whether or not it produced symbols. Without
        -- it, "0 symbols" conflates a grammar the pack does not serve, bytes that do not parse,
        -- a grammar with no structure output, symbols dropped for having no name, and a file
        -- that genuinely defines nothing. `rung` names which path ran; `anon_count` counts the
        -- unnamed drops. Read by overview(what="metrics"); SC6 bans write-only columns.
        CREATE TABLE IF NOT EXISTS file_extraction (
            file TEXT PRIMARY KEY,
            language TEXT,
            rung TEXT,
            symbol_count INTEGER DEFAULT 0,
            anon_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_fx_lang ON file_extraction(language);
        CREATE INDEX IF NOT EXISTS idx_fx_rung ON file_extraction(rung);
        -- S12: file-to-file import edges. Deliberately *not* rows in `edges`, which is
        -- sid-to-sid and whose every consumer assumes both endpoints are symbols. An import
        -- statement sits at file scope, inside no symbol, so it has no caller sid to name and
        -- inventing a file-level pseudo-symbol to give it one would change what a graph node is
        -- for every reader of `symbols`. A separate table costs one migration and no invariant.
        CREATE TABLE IF NOT EXISTS file_imports (
            src_file TEXT NOT NULL,
            dst_file TEXT NOT NULL,
            PRIMARY KEY (src_file, dst_file)
        );
        CREATE INDEX IF NOT EXISTS idx_imp_dst ON file_imports(dst_file);
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
    # Schema migration F-G/F-D: drop write-only columns (signature, docstring, intent).
    # SQLite 3.35+ supports ALTER TABLE DROP COLUMN; current system has 3.45.1.
    _sym_cols = {r[1] for r in con.execute("PRAGMA table_info(symbols)")}
    for _dead_col in ("signature", "docstring", "intent"):
        if _dead_col in _sym_cols:
            con.execute(f"ALTER TABLE symbols DROP COLUMN {_dead_col}")
    if any(c in _sym_cols for c in ("signature", "docstring", "intent")):
        con.commit()
    # Schema migration R2: drop four community columns whose writers left with tier 3, same
    # DROP COLUMN precedent as the symbols sweep above. Each was censused across all 160 fleet
    # graphs before being dropped — `kind` held one value in 8,793 rows, `path` was never written.
    # A DB that never saw the migrations that added them has nothing to drop, hence `in _cols`.
    _dead_community_cols = ("semantic_type", "narrated", "kind", "path")
    for _dead_col in _dead_community_cols:
        if _dead_col in _cols:
            con.execute(f"ALTER TABLE communities DROP COLUMN {_dead_col}")
    if _cols.intersection(_dead_community_cols):
        con.commit()
    # Schema migration: key-value meta store for algo-version + source-fingerprint stamps.
    # Survives GraphStore.clear() (which only deletes symbols/edges/communities).
    con.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
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

    def upsert_community(self, cid: int, level: int, title: str | None, summary: str | None,
                         member_count: int) -> None:
        self._con.execute(
            """INSERT INTO communities (id,level,title,summary,member_count)
               VALUES (?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 title=COALESCE(excluded.title, title),
                 summary=COALESCE(excluded.summary, summary),
                 member_count=excluded.member_count""",
            (cid, level, title, summary, member_count),
        )

    def record_extraction(self, file: str, language: str, rung: str,
                          symbol_count: int, anon_count: int, error_count: int) -> None:
        """Stamp what the derive did to one file. Upsert, so a re-extract overwrites in place."""
        self._con.execute(
            """INSERT INTO file_extraction
               (file,language,rung,symbol_count,anon_count,error_count)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(file) DO UPDATE SET
                 language=excluded.language, rung=excluded.rung,
                 symbol_count=excluded.symbol_count, anon_count=excluded.anon_count,
                 error_count=excluded.error_count""",
            (file, language, rung, symbol_count, anon_count, error_count),
        )

    def extraction_summary(self) -> list[dict]:
        """Per-(language, rung) rollup for overview(what="metrics")'s `extraction` block.

        `files_with_symbols` is counted here rather than inferred by the caller, and that is the
        whole reason it exists. Without it `_extraction_totals` could only ask "does this *group*
        have symbols", which credits every zero-symbol file in a group that any other file carried
        — so a language/rung pair that mostly failed reported as fully covered. Measured
        2026-07-30 on a 192-file store: 111 claimed against 100 real, i.e. 11 dark files invisible
        to the one number the dark set is supposed to be read from. `file` is the primary key, so
        `SUM(symbol_count > 0)` is a file count, not a symbol count.
        """
        rows = self._con.execute(
            "SELECT language, rung, COUNT(*) AS files, SUM(symbol_count) AS symbols, "
            "SUM(symbol_count > 0) AS files_with_symbols, "
            "SUM(anon_count) AS anon, SUM(error_count) AS errors "
            "FROM file_extraction GROUP BY language, rung ORDER BY files DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def code_files_extracted(self) -> tuple[int, int]:
        """`(code files this store attempted, how many yielded a symbol)`.

        The evidence H1's hollow check needs, and the reason it can be evidence-based at all: until
        `file_extraction` existed, "this store has no symbols" and "this store holds nothing that
        could have any" were the same observation. Restricted to code languages because a store
        whose only files are a README and a LICENCE has zero symbols *correctly* — counting those
        as attempts would turn every container directory into a health alarm.

        Distinct rows only: `file` is the primary key, so a COUNT is a file count.
        """
        from rag_search.index.discover import is_code_language
        rows = self._con.execute(
            "SELECT language, COUNT(*) AS files, SUM(symbol_count > 0) AS with_syms "
            "FROM file_extraction GROUP BY language").fetchall()
        code = [r for r in rows if is_code_language(r["language"] or "")]
        return sum(r["files"] for r in code), sum(r["with_syms"] or 0 for r in code)

    def delete_file_symbols(self, file: str) -> int:
        """Drop one file's symbols and the edges *out of* them (incremental re-extract).

        `upsert_symbol` can only add or overwrite, so a renamed or deleted function would
        otherwise linger forever; the caller must run this before re-extracting the file.
        Community membership lives on `symbols.community_id`, so it goes with the row.

        Incoming edges are deliberately left dangling rather than deleted: they were written
        by files this pass is not re-scanning, so deleting them would silently strip an
        untouched caller's edge whenever its callee's file happened to change. `symbol_id` is
        stable for a symbol whose start_line did not move, so most of them are valid again the
        moment the file is re-extracted; call `purge_dangling_edges` afterwards for the rest.
        """
        # Before the early return, not after: a file that produced *no* symbols still has a
        # file_extraction row (that is the whole point of the table), so gating its removal on
        # having had symbols would leak a row for every deleted zero-symbol file — exactly the
        # files the table exists to account for.
        self._con.execute("DELETE FROM file_extraction WHERE file=?", (file,))
        sids = [r[0] for r in self._con.execute("SELECT sid FROM symbols WHERE file=?", (file,))]
        if not sids:
            return 0
        marks = ",".join("?" * len(sids))
        self._con.execute(f"DELETE FROM edges WHERE caller_sid IN ({marks})", sids)
        self._con.execute("DELETE FROM symbols WHERE file=?", (file,))
        return len(sids)

    def purge_dangling_edges(self) -> int:
        """Delete edges whose caller or callee no longer exists. Run after a re-extract."""
        cur = self._con.execute(
            "DELETE FROM edges WHERE caller_sid NOT IN (SELECT sid FROM symbols) "
            "OR callee_sid NOT IN (SELECT sid FROM symbols)"
        )
        return cur.rowcount

    def prune_symbols_to(self, sids: set[str], files: set[str]) -> tuple[int, int]:
        """Drop symbols and `file_extraction` rows a full re-derive did not just re-record.

        The subtraction half of a full re-derive, moved to *after* the pass. It used to be
        `clear()` running before one, and that is what made a re-derive unreadable: `clear()`
        commits, so the empty table was published to every other connection and stayed empty for
        the whole extraction — minutes on a large project — during which `graph()` and
        `overview(what="communities")` answered "no symbols" with full confidence. Measured
        2026-07-31: a second connection reads 50 symbols before `clear()` and 0 immediately after,
        with no extraction having run yet. See `docs/decisions/2026-07-31-atomic-graph-rederive.md`.

        Doing it here inverts that: `upsert_symbol` is keyed on `sid`, so the pass writes into the
        live table and readers see `old | new` throughout — a superset, never a hole — until this
        prunes it to exactly `new`. The write lock is held for these two statements instead of for
        the extraction, which is what keeps the watcher's incremental path (`timeout=30`) off the
        floor on a store that takes minutes to walk.

        Callers must pass what the pass *walked*, not what it *found*: a file that legitimately
        yields no symbols still has a `file_extraction` row, and dropping it would shrink the
        coverage denominator to the files that happened to succeed.
        """
        self._con.executescript(
            "DROP TABLE IF EXISTS temp._keep_sid;"
            "DROP TABLE IF EXISTS temp._keep_file;"
            "CREATE TEMP TABLE _keep_sid (sid TEXT PRIMARY KEY);"
            "CREATE TEMP TABLE _keep_file (file TEXT PRIMARY KEY);"
        )
        self._con.executemany("INSERT OR IGNORE INTO _keep_sid VALUES (?)", ((s,) for s in sids))
        self._con.executemany("INSERT OR IGNORE INTO _keep_file VALUES (?)", ((f,) for f in files))
        n_sym = self._con.execute(
            "DELETE FROM symbols WHERE NOT EXISTS "
            "(SELECT 1 FROM _keep_sid k WHERE k.sid = symbols.sid)"
        ).rowcount
        n_file = self._con.execute(
            "DELETE FROM file_extraction WHERE NOT EXISTS "
            "(SELECT 1 FROM _keep_file k WHERE k.file = file_extraction.file)"
        ).rowcount
        self._con.commit()
        return n_sym, n_file

    def prune_edges_to(self, edges: set[tuple[str, str]]) -> int:
        """Drop edges a full re-derive did not just re-record. Returns how many went.

        `purge_dangling_edges` is not enough on its own here: it only catches edges whose endpoints
        are gone. The edge a full re-derive has to retract is the one whose endpoints both still
        exist at the same lines — same `sid` — because the *call between them* was deleted from the
        source. `clear()` used to catch those by wiping the table; without this they would survive
        every future re-derive.
        """
        self._con.executescript(
            "DROP TABLE IF EXISTS temp._keep_edge;"
            "CREATE TEMP TABLE _keep_edge (caller_sid TEXT, callee_sid TEXT,"
            " PRIMARY KEY (caller_sid, callee_sid));"
        )
        self._con.executemany("INSERT OR IGNORE INTO _keep_edge VALUES (?,?)", edges)
        n = self._con.execute(
            "DELETE FROM edges WHERE NOT EXISTS (SELECT 1 FROM _keep_edge k "
            "WHERE k.caller_sid = edges.caller_sid AND k.callee_sid = edges.callee_sid)"
        ).rowcount
        self._con.commit()
        return n

    def upsert_import(self, src_file: str, dst_file: str) -> None:
        self._con.execute(
            "INSERT OR IGNORE INTO file_imports (src_file,dst_file) VALUES (?,?)",
            (src_file, dst_file),
        )

    def list_imports(self) -> list[tuple[str, str]]:
        """Every (src_file, dst_file) import pair. Read by `_find_import_cycles`."""
        return [(r[0], r[1]) for r in
                self._con.execute("SELECT src_file, dst_file FROM file_imports")]

    def prune_imports_to(self, pairs: set[tuple[str, str]]) -> int:
        """Drop import rows a full re-derive did not just re-record. `prune_edges_to`'s twin.

        Same reason that one exists and `purge_dangling_edges` is not enough: the row a re-derive
        has to retract is the one whose two files both still exist, because the `import` line
        between them was deleted from the source.
        """
        self._con.executescript(
            "DROP TABLE IF EXISTS temp._keep_import;"
            "CREATE TEMP TABLE _keep_import (src_file TEXT, dst_file TEXT,"
            " PRIMARY KEY (src_file, dst_file));"
        )
        self._con.executemany("INSERT OR IGNORE INTO _keep_import VALUES (?,?)", pairs)
        n = self._con.execute(
            "DELETE FROM file_imports WHERE NOT EXISTS (SELECT 1 FROM _keep_import k "
            "WHERE k.src_file = file_imports.src_file AND k.dst_file = file_imports.dst_file)"
        ).rowcount
        self._con.commit()
        return n

    def delete_file_imports(self, file: str) -> int:
        """Drop one file's outgoing imports (the watcher's incremental path).

        Outgoing only, mirroring `delete_file_symbols`: the incoming rows were written by files
        this pass is not re-scanning, and dropping them would strip an untouched importer's row
        because its target happened to change.
        """
        return self._con.execute(
            "DELETE FROM file_imports WHERE src_file=?", (file,)
        ).rowcount

    def clear(self) -> None:
        """Wipe symbols/edges/communities before a full re-index so stale rows don't persist.

        **No production caller since 2026-07-31** — the re-derive path prunes after the pass
        (`prune_symbols_to`/`prune_edges_to`) precisely so it never publishes the empty table this
        leaves behind. Kept for tests and for a deliberate wipe; do not reintroduce it into a
        rebuild path without reading the decision doc those two methods cite.

        `file_extraction` goes too: a full re-derive re-records every file it walks, so keeping
        the old rows would leave entries for files the new pass never saw and make the coverage
        denominator larger than the corpus.
        """
        self._con.executescript(
            "DELETE FROM symbols; DELETE FROM edges; DELETE FROM communities; "
            "DELETE FROM file_extraction; DELETE FROM file_imports;"
        )
        self._con.commit()

    def symbol_count(self) -> int:
        return self._con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]

    def import_count(self) -> int:
        return self._con.execute("SELECT COUNT(*) FROM file_imports").fetchone()[0]

    def edge_count(self) -> int:
        return self._con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    def community_count(self) -> int:
        """Count communities. The `level>=1` scope is inherited, not load-bearing: no writer has
        ever produced a level=0 row — `community.py` writes only level=1 — and the Phase-2
        dir/file nodes the clause was added to exclude were deleted with them. Kept because the
        SQL is harmless and a fleet-wide predicate change is not; the claim that it excludes a
        "structural spine" is what was removed (2026-07-31), with SC3, since it described a
        distinction the schema no longer draws."""
        return self._con.execute("SELECT COUNT(*) FROM communities WHERE level>=1").fetchone()[0]

    def list_symbols(self, limit: int = 5000) -> list[dict]:
        rows = self._con.execute(
            "SELECT sid,name,qualified_name,kind,file,start_line,end_line,language "
            "FROM symbols LIMIT ?", (limit,)
        ).fetchall()
        keys = ("sid", "name", "qualified_name", "kind", "file", "start_line", "end_line", "language")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def get_meta(self, key: str) -> str | None:
        row = self._con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._con.execute(
            "INSERT INTO meta (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def commit(self) -> None:
        self._con.commit()

    def close(self) -> None:
        self._con.commit()
        self._con.close()
