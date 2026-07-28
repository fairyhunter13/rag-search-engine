"""sqlite-vec vector store for code chunk embeddings."""
from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

import numpy as np
import sqlite_vec

from rag_search.core.config import EMBED_MAX_TOKENS, EMBED_MODEL

_log = logging.getLogger(__name__)

# Bump by hand whenever chunk *shape* changes (boundaries, headers, overlap) in a
# way that makes old vectors incomparable to new ones.
CHUNKER_REV = "cast-1"


# What this pipeline prepends to text before embedding, per flow. It prepends nothing: the
# current model's own card says "Prefixes for queries/documents: not necessary". But "none" is a
# choice and not an absence — an e5- or bge-style model needs `query: ` / `passage: `, and adding
# one shifts every vector. Recording it is what makes that change invalidate the index instead of
# silently querying a space the stored vectors were never embedded into. Bump by hand.
EMBED_PREFIX_REV = "noprefix-1"

# Identity of the *lexical* index, which is independent of the vector one: it holds no
# embeddings, so changing it costs a re-tokenise and never a re-embed. Bump when the tokenizer
# or column set changes; the guarded `rebuild` in `_open` then backfills each store once.
FTS_REV = "fts5-unicode61-1"

# Stores already reported as lexically unavailable, so the warning is one line per store per
# process instead of one per query against a 189-member federation.
_WARNED_UNMIGRATED: set[str] = set()

# The pooling and prefix every stored index in the fleet was built with. While the pipeline still
# matches these the signature stays in its four-field form, byte-identical to what is stamped
# today — because the two new fields *describe what those runs already did*, and re-deriving them
# would recompute identical vectors at fleet scale for no change in any result. `embed_signature`
# is also folded into `indexer._content_hash`, so a cosmetic change here would defeat the
# byte-identical re-embed skip on every file at once. Change pooling or the prefix for real and
# both fields appear, invalidating stamp and content hashes together — which is the whole job.
_ERA_POOLING = "PooledNormalizedEmbedding"
_ERA_PREFIX_REV = "noprefix-1"


@lru_cache(maxsize=8)
def pooling_id(model: str = EMBED_MODEL) -> str:
    """The pooling + normalisation actually in force, read off fastembed's implementation class.

    Derived, not hand-maintained, and that is the whole point. fastembed picks one of several
    implementations *from the model name* — `PooledNormalizedEmbedding` is mean-pool + L2,
    `OnnxTextEmbedding` is CLS, `PooledEmbedding` is mean-pool unnormalised — so a model swap can
    change the vector space without one line of this repo changing. A constant here would have to
    be remembered at exactly the moment everyone is thinking about something else; this cannot be
    forgotten. Resolution reads the registry only: no ONNX session, no download, no GPU.
    """
    from fastembed import TextEmbedding
    for cls in TextEmbedding.EMBEDDINGS_REGISTRY:
        try:
            if any(m.model == model for m in cls._list_supported_models()):
                return cls.__name__
        except Exception:
            continue
    return "unregistered"


def _compose_signature(dim: int, pooling: str, prefix_rev: str) -> str:
    """Assemble a signature from stated pipeline facts, era clause included.

    Split out from `embed_signature` so the expansion branch can be exercised with a real
    alternative pooling id — read off fastembed's registry, not patched in — since the repo
    bans monkeypatching and the branch is the one that must not be wrong: if it failed to
    expand, an embedder swap would keep the legacy stamp and serve two vector spaces at once.
    """
    sig = f"{EMBED_MODEL}|{EMBED_MAX_TOKENS}|{dim}|{CHUNKER_REV}"
    if pooling == _ERA_POOLING and prefix_rev == _ERA_PREFIX_REV:
        return sig
    return f"{sig}|{pooling}|{prefix_rev}"


def embed_signature(dim: int = 768) -> str:
    """Identity of the pipeline that produced a set of vectors.

    A stored vector is only comparable to a query embedded the same way, so any
    change here invalidates the whole index. Recording it is what lets a stale
    index announce itself: without it, a config change leaves old and new chunk
    shapes coexisting silently and forever — which is how a 512-token truncation
    went unnoticed while discarding half of every indexed repo.
    """
    return _compose_signature(dim, pooling_id(), EMBED_PREFIX_REV)


def fts_query(text: str) -> str:
    """A user question rewritten as an FTS5 MATCH expression. Empty when nothing is searchable.

    Raw text cannot be passed through: `-`, `*`, `:`, `(`, `"` and the bare words AND/OR/NOT are
    all FTS5 operators, so a question like "worker-side validation (async)" is a syntax error,
    not a query. Each whitespace-separated word becomes one quoted phrase of its alphanumeric
    runs, and the phrases are OR'd.

    Phrasing per word is what makes this useful on code. unicode61 splits `_content_hash` into
    `content` + `hash`, so a bare-token query would match every chunk mentioning either word
    anywhere; as the phrase "content hash" it matches only where they are adjacent — which is
    the identifier itself, and its call sites. Splitting is still the right tokenizer: it is what
    lets an English question reach a snake_case name it does not spell exactly.

    Split with `str.isalnum` rather than a regex, both because P6 forbids `re` in this package
    and because it is the closer match: unicode61 keeps unicode letters and digits, which a
    `[0-9A-Za-z]` class would drop from the query while the index still held them.
    """
    phrases = [
        '"' + " ".join(t) + '"'
        for w in text.split()
        if (t := "".join(c if c.isalnum() else " " for c in w).split())
    ]
    return " OR ".join(phrases)


def _open(db_path: Path, dim: int, migrate: bool) -> tuple[sqlite3.Connection, bool]:
    """Open (creating if absent) and return the connection plus whether the FTS index is usable."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), check_same_thread=False)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    if migrate:
        # A write-path handle only, and `migrate` is the flag that already means exactly that.
        # 128 MB of page cache holds the chunks b-tree and vec0's pages for the whole indexing
        # transaction instead of evicting and re-reading them: measured 5.46-5.77s of statement
        # time down to 2.85-2.94s over 50k chunk-shaped rows, 1.94x, reproducible to +-1.6% across
        # six runs on two different host loads. Commit got faster too, which refutes the obvious
        # worry that a larger cache merely defers the same writes.
        #
        # Never on the query path: this is a per-connection allocation and a federated search opens
        # one handle per member — 189 on the largest federation here, which at 128 MB each would
        # ask for 24 GB to answer one question. It is a ceiling rather than a reservation, and
        # reconcile's brief staleness-check opens touch too few pages to come near it.
        #
        # `mmap_size` was measured alongside and REJECTED: paired with this it ran 3.16-3.28s
        # against 2.85-2.94s for the cache alone, consistently ~12% worse, so it is left off.
        con.execute("PRAGMA cache_size=-131072")
    con.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id   INTEGER PRIMARY KEY,
            path       TEXT NOT NULL,
            start_line INTEGER,
            end_line   INTEGER,
            language   TEXT,
            content    TEXT
        )
    """)
    con.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
            chunk_id  INTEGER PRIMARY KEY,
            embedding FLOAT[{dim}]
        )
    """)
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    # Content hash of each file whose chunks are currently embedded here, so an incremental
    # reindex can skip a file that was rewritten with identical bytes (generators and
    # save-on-format rewrite constantly; embedding them again buys nothing).
    con.execute(
        "CREATE TABLE IF NOT EXISTS file_hashes (path TEXT PRIMARY KEY, hash TEXT NOT NULL)"
    )
    # A scoped search restricts the KNN with `chunk_id IN (SELECT ... WHERE language IN ...)`.
    # Without this index that subquery scans a table holding >100k rows on the larger repos,
    # on every query. Costs one lazy build per existing store; no reindex, no signature change.
    con.execute("CREATE INDEX IF NOT EXISTS idx_chunks_language ON chunks(language)")
    # The lexical lane. External content, so the text lives once in `chunks` and FTS5 stores
    # only the inverted index. `chunks` is maintained by hand at the three sites that write it
    # rather than by triggers: `INSERT OR REPLACE` fires delete triggers only under
    # recursive_triggers, and `clear()` would otherwise re-tokenise every row on its way out.
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
        "USING fts5(content, content='chunks', content_rowid='chunk_id')"
    )
    # Creating the table leaves it EMPTY — an external-content table indexes nothing it did not
    # see written. `rebuild` is FTS5's own backfill and is the only thing that must run against
    # the rows already on disk; the meta guard is what keeps it (and `optimize`, which merges
    # every segment) from running on each of 160 stores at every daemon start.
    #
    # `migrate` is what keeps it off the query path, and that is not a tuning knob. Measured on
    # the live fleet: opening `redacted-name-12` (99 k chunks) inside a query cost 11.31 s before returning
    # a 1.9 ms result, and a federated search opens one store per member — 189 of them on the
    # largest federation, 137 of which still owed a backfill, so the first such query would have
    # paid roughly two minutes serially and timed out. It defaults True because the dangerous
    # direction is the other one: an unmigrated store whose FTS index is empty cannot be
    # maintained incrementally, since deleting a row that was never indexed writes a negative
    # entry. A forgotten call site must therefore land on "slow but correct", never on "fast
    # and silently corrupting".
    owed = con.execute("SELECT value FROM meta WHERE key='fts_rev'").fetchone() != (FTS_REV,)
    if owed and migrate:
        t0 = time.monotonic()
        con.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        con.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
        con.execute("INSERT OR REPLACE INTO meta VALUES ('fts_rev', ?)", (FTS_REV,))
        # 10.8 s + 1.8 s measured on the fleet's largest store (207 k chunks), once. Logged
        # because it happens inside an open, so without this it is an unattributable
        # ten-second stall on a path that is otherwise milliseconds.
        _log.info("fts5 backfill %s: %.1fs", db_path.parent.name, time.monotonic() - t0)
        owed = False
    elif owed:
        # A store the lexical lane cannot serve, announced once per process rather than per
        # query. Silence here would read as "hybrid retrieval is on" while that member was
        # answering from the dense lane alone — the recall regression 2d exists to remove,
        # invisible in every output.
        if db_path.parent.name not in _WARNED_UNMIGRATED:
            _WARNED_UNMIGRATED.add(db_path.parent.name)
            _log.warning("fts5 index not built for %s — lexical lane disabled for this store "
                         "until it is next indexed", db_path.parent.name)
    con.commit()
    return con, not owed


class VectorStore:
    """sqlite-vec backed vector store for code chunk embeddings (float32 ANN)."""

    def __init__(self, db_path: Path, dim: int = 768, *, migrate: bool = True):
        self._con, self._lexical_ready = _open(db_path, dim, migrate)
        self._dim = dim

    @property
    def lexical_ready(self) -> bool:
        """Whether this store's FTS index is built, and so whether the lexical lane can run."""
        return self._lexical_ready

    def stamp(self) -> None:
        """Record which pipeline built the vectors now held here. Call after a full reindex."""
        self._con.execute(
            "INSERT OR REPLACE INTO meta VALUES ('embed_signature', ?)",
            (embed_signature(self._dim),),
        )

    def get_meta(self, key: str) -> str | None:
        row = self._con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Write a meta key and commit it now — callers use this to record when a store was
        last brought up to date, and a value that vanishes on close would re-trigger the
        rebuild it exists to end."""
        self._con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
        self._con.commit()

    def stale_signature(self) -> str | None:
        """The recorded signature, if it disagrees with the running config; else None.

        A populated index with no stamp predates stamping, so it is stale too.
        An empty index is never stale — there is nothing to be inconsistent with.
        """
        row = self._con.execute(
            "SELECT value FROM meta WHERE key='embed_signature'"
        ).fetchone()
        if row is None:
            return "<unstamped>" if self.count() else None
        return None if row[0] == embed_signature(self._dim) else row[0]

    def insert(
        self, chunk_id: int, path: str, start: int, end: int,
        language: str, content: str, vector: np.ndarray,
    ) -> None:
        v = vector.astype(np.float32).tobytes()
        # Replacing a live chunk_id has to be done by hand, in full. `OR REPLACE` covered only
        # `chunks`: an external-content FTS5 index cannot see the row it displaces (and deleting
        # from that index needs the text that *was* indexed, not the text replacing it), and vec0
        # does not implement the conflict clause at all — it raises UNIQUE, so this path has
        # always aborted midway, after `chunks` was already overwritten. The probe is a PK miss
        # in the normal case, since both callers clear or purge the path before inserting.
        old = self._con.execute(
            "SELECT content FROM chunks WHERE chunk_id=?", (chunk_id,)
        ).fetchone()
        if old is not None:
            if self._lexical_ready:
                self._con.execute(
                    "INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES('delete',?,?)",
                    (chunk_id, old[0]),
                )
            self._con.execute("DELETE FROM vec_chunks WHERE chunk_id=?", (chunk_id,))
        self._con.execute(
            "INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?)",
            (chunk_id, path, start, end, language, content),
        )
        # An unbuilt index is left strictly empty rather than partially filled. Maintaining it
        # here would be worse than useless: `delete` against a row the index never saw writes a
        # negative entry, and the next `rebuild` is what makes the store whole in one step.
        if self._lexical_ready:
            self._con.execute(
                "INSERT INTO chunks_fts(rowid, content) VALUES (?,?)", (chunk_id, content)
            )
        self._con.execute(
            "INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?,?)", (chunk_id, v),
        )

    def file_hash(self, path: str) -> str | None:
        """The content hash whose chunks are currently embedded for path, if any."""
        row = self._con.execute(
            "SELECT hash FROM file_hashes WHERE path=?", (path,)
        ).fetchone()
        return row[0] if row else None

    def set_file_hash(self, path: str, digest: str) -> None:
        """Record that path's chunks in this store were built from content hashing to digest.

        Only ever written after that file's chunks are inserted, and dropped by
        delete_by_path/clear, so a hash row always describes what is really stored.
        """
        self._con.execute(
            "INSERT OR REPLACE INTO file_hashes VALUES (?,?)", (path, digest)
        )

    def flush(self) -> None:
        self._con.commit()

    def search(
        self,
        query_vector: np.ndarray,
        top_k: int = 10,
        languages: Sequence[str] | None = None,
    ) -> list[dict]:
        """Nearest `top_k` chunks, restricted to `languages` when given.

        The restriction goes *inside* the vec0 query, not on its output. vec0 returns
        exactly k rows regardless, so filtering afterwards silently shrinks the result:
        a docs query against a code-heavy repo comes back near-empty while the matching
        docs sit just past the cut. Pre-filtering asks for k rows that already qualify.
        """
        if languages is not None and not languages:
            return []
        v = query_vector.astype(np.float32).tobytes()
        params: list = [v, top_k]
        lang_clause = ""
        if languages is not None:
            marks = ",".join("?" * len(languages))
            lang_clause = (
                f" AND v.chunk_id IN (SELECT chunk_id FROM chunks WHERE language IN ({marks}))"
            )
            params.extend(languages)
        rows = self._con.execute(
            f"""
            SELECT c.chunk_id, c.path, c.start_line, c.end_line,
                   c.language, c.content, v.distance
            FROM vec_chunks v
            JOIN chunks c USING (chunk_id)
            WHERE v.embedding MATCH ? AND v.k = ?{lang_clause}
            ORDER BY v.distance
            """,
            params,
        ).fetchall()
        return [
            {"chunk_id": r[0], "path": r[1], "start_line": r[2], "end_line": r[3],
             "language": r[4], "content": r[5], "score": float(1.0 - r[6])}
            for r in rows
        ]

    def search_lexical(
        self,
        query: str,
        top_k: int = 10,
        languages: Sequence[str] | None = None,
    ) -> list[dict]:
        """Best `top_k` chunks by BM25, restricted to `languages` when given.

        The lexical half of hybrid retrieval, and the only lane that can retrieve a chunk the
        embedder never placed near the query: a rare literal identifier is a strong lexical
        signal and a weak semantic one, because an embedding of a name the model has never seen
        is mostly the shape of its neighbours. Returns `bm25` (lower is better) rather than a
        `score`, so nothing downstream can mistake the two lanes' numbers for each other.
        """
        expr = fts_query(query)
        if not self._lexical_ready or not expr:
            return []
        if languages is not None and not languages:
            return []
        params: list = [expr]
        lang_clause = ""
        if languages is not None:
            marks = ",".join("?" * len(languages))
            # Filtered by rowid, not by joining `chunks` — the join is what phase 1 exists to
            # avoid, and `idx_chunks_language` already serves this shape. Same construction the
            # dense lane uses for its own pre-filter.
            lang_clause = (
                f" AND chunks_fts.rowid IN (SELECT chunk_id FROM chunks "
                f"WHERE language IN ({marks}))"
            )
            params.extend(languages)
        params.append(top_k)
        # Two phases, and the split is the difference between reading `top_k` chunk bodies and
        # reading every body that matched. Joining `chunks` in the same SELECT as the MATCH makes
        # sqlite resolve the join *before* the sort — `SEARCH c USING INTEGER PRIMARY KEY` then
        # `USE TEMP B-TREE FOR ORDER BY` — so a three-common-word question over a 207 k-chunk
        # store hydrates 178 k full-text rows to return ten. Phase 1 sorts rowid+score alone;
        # phase 2 hydrates the survivors. Measured 323 ms -> 130 ms on that store, and the
        # memory difference is four orders of magnitude larger than the time difference.
        rows = self._con.execute(
            f"""
            SELECT c.chunk_id, c.path, c.start_line, c.end_line, c.language, c.content, t.rk
            FROM (
                SELECT chunks_fts.rowid AS rid, bm25(chunks_fts) AS rk
                FROM chunks_fts
                WHERE chunks_fts MATCH ?{lang_clause}
                ORDER BY rk
                LIMIT ?
            ) t
            JOIN chunks c ON c.chunk_id = t.rid
            ORDER BY t.rk
            """,
            params,
        ).fetchall()
        return [
            {"chunk_id": r[0], "path": r[1], "start_line": r[2], "end_line": r[3],
             "language": r[4], "content": r[5], "bm25": float(r[6])}
            for r in rows
        ]

    def count(self) -> int:
        return self._con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def clear(self) -> None:
        """Drop all chunk metadata + vectors (for idempotent full reindex)."""
        self._con.execute("DELETE FROM vec_chunks")
        self._con.execute("DELETE FROM chunks")
        # FTS5's own reset. Deleting row by row would re-tokenise the whole store on the way
        # out, and would have to read each `content` back to do it.
        if self._lexical_ready:
            self._con.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('delete-all')")
        self._con.execute("DELETE FROM file_hashes")

    def delete_by_path(self, path: str) -> None:
        """Remove all chunks (metadata + vectors) for a single file path."""
        rows = self._con.execute(
            "SELECT chunk_id, content FROM chunks WHERE path=?", (path,)
        ).fetchall()
        for cid, content in rows:
            self._con.execute("DELETE FROM vec_chunks WHERE chunk_id=?", (cid,))
            if self._lexical_ready:
                self._con.execute(
                    "INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES('delete',?,?)",
                    (cid, content),
                )
        self._con.execute("DELETE FROM chunks WHERE path=?", (path,))
        self._con.execute("DELETE FROM file_hashes WHERE path=?", (path,))

    def close(self) -> None:
        self._con.close()
