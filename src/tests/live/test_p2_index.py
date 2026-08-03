"""P2 index layer: discover, chunker, indexer, search (GPU)."""
import tempfile
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.live


# ── discover ──────────────────────────────────────────────────────────────────

def test_iter_files_finds_source():
    from rag_search.index.discover import iter_files
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.py").write_text("x = 1")
        (root / "b.ts").write_text("const x = 1;")
        names = {p.name for p in iter_files(root)}
        assert "a.py" in names and "b.ts" in names


def test_iter_files_skips_ignored_dirs():
    from rag_search.index.discover import iter_files
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".git").mkdir()
        (root / ".git" / "config").write_text("git")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "pkg.js").write_text("module.exports={}")
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text("print('hi')")
        paths = list(iter_files(root))
        assert not any(".git" in p.parts for p in paths)
        assert not any("node_modules" in p.parts for p in paths)
        assert any(p.name == "main.py" for p in paths)


def test_iter_files_skips_oversized():
    from rag_search.index.discover import _SIZE_LIMITS, iter_files
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        big = root / "big.py"
        big.write_bytes(b"x = 1\n" * (_SIZE_LIMITS["code"] // 6 + 1))
        assert not any(p.name == "big.py" for p in iter_files(root))


def test_iter_files_skips_empty():
    from rag_search.index.discover import iter_files
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "empty.py").write_text("")
        (root / "real.py").write_text("x = 1")
        names = {p.name for p in iter_files(root)}
        assert "empty.py" not in names and "real.py" in names


# ── chunker ──────────────────────────────────────────────────────────────────

def test_chunk_empty_returns_empty():
    from rag_search.index.chunker import chunk_file
    assert chunk_file(Path("e.py"), "", "python") == []
    assert chunk_file(Path("e.py"), "   \n", "python") == []


def test_chunk_file_python():
    from rag_search.index.chunker import chunk_file
    code = "\n".join(f"def func_{i}(x): return x + {i}" for i in range(60))
    chunks = chunk_file(Path("t.py"), code, "python")
    assert len(chunks) >= 1
    assert all(c.language == "python" for c in chunks)
    assert all(c.content.strip() for c in chunks)


# ── indexer + search (GPU) ──────────────────────────────────────────────────

def test_indexer_counts(embedder):
    from rag_search.index.indexer import index_project
    from rag_search.index.store import VectorStore
    with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as store_dir:
        root = Path(proj)
        (root / "a.py").write_text("def add(x, y):\n    return x + y\n")
        (root / "b.py").write_text("def mul(x, y):\n    return x * y\n")
        store = VectorStore(Path(store_dir) / "v.db")
        fc, cc = index_project(root, embedder, store, federation_mode=False)
        assert fc == 2
        assert cc >= 2
        assert store.count() == cc
        store.close()


class _FailAfter:
    """The real embedder, until the nth call — then it stops, the way a kill would.

    Not a fake: every call it does serve is the live model, so the zero-fake policy holds. It
    exists to interrupt `index_project` at a batch boundary, which is the only way to observe
    what a restart actually keeps.
    """

    def __init__(self, inner, allow: int):
        self._inner, self._allow, self.calls = inner, allow, 0

    def embed(self, texts, batch_size=None, *, side="document"):
        self.calls += 1
        if self.calls > self._allow:
            raise RuntimeError("simulated crash mid-embed")
        return self._inner.embed(texts, batch_size=batch_size, side=side)


def test_l3_interrupted_index_keeps_the_files_it_finished(embedder):
    """L3: a restart mid-index must lose less than one file of work.

    Read back over a *second* connection on purpose. The writing handle sees its own open
    transaction, so asserting through it would pass just as happily if nothing were ever
    committed — which is exactly the property under test.

    Demonstrated red against the pre-L3 body (6b56451) on 2026-07-29: crashing on the second
    embed batch left **0 chunked paths**, the whole pass rolled back. Same crash after: 32
    paths chunked and hashed, resume finishes with 0 unhashed.
    """
    from rag_search.index.indexer import index_project
    from rag_search.index.store import VectorStore
    with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as store_dir:
        root = Path(proj)
        for i in range(60):
            (root / f"m{i}.py").write_text(f"def f{i}(x):\n    '''doc {i}'''\n    return x + {i}\n")
        db = Path(store_dir) / "v.db"
        store, crashy = VectorStore(db), _FailAfter(embedder, allow=1)
        with pytest.raises(RuntimeError, match="simulated crash"):
            index_project(root, crashy, store, federation_mode=False)
        assert crashy.calls > 1, "never reached a second batch — the gate would be vacuous"

        durable = VectorStore(db, migrate=False)
        chunked = {p for (p,) in durable._con.execute("SELECT DISTINCT path FROM chunks")}
        hashed = {p for (p,) in durable._con.execute("SELECT path FROM file_hashes")}
        durable.close()
        store.close()
        assert chunked, (
            "an interrupted index committed nothing — the whole pass rolled back, which is the "
            "pre-L3 behaviour this gate exists to prevent"
        )
        assert hashed <= chunked, (
            f"hash rows with no chunks: {sorted(hashed - chunked)} — a hash row must never claim "
            "work that is not in the store"
        )
        assert len(chunked - hashed) <= 1, (
            f"{len(chunked - hashed)} files hold chunks but no hash row; at most the one "
            "straddling the commit boundary may"
        )

        # And what survived is resumable: a clean re-run finishes and leaves nothing unhashed —
        # the shape `_index_set_drift` scans for, so the straggler needs no progress state.
        store = VectorStore(db)
        fc, cc = index_project(root, embedder, store, federation_mode=False)
        left = store._con.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT path FROM chunks "
            "EXCEPT SELECT path FROM file_hashes)"
        ).fetchone()[0]
        store.close()
        assert fc == 60 and cc >= 60
        assert left == 0, f"{left} files still unhashed after a completed index"


def test_l3_empty_walk_never_purges_a_healthy_store(embedder):
    """A walk that discovers nothing must leave the store alone.

    L3 replaced `clear()` with an end-of-walk purge of what discovery no longer yields, which
    puts a full wipe one bad walk away: a project root not mounted, a permissions blip, an
    `is_ignored_path` regression. The purge is gated on the walk having produced something,
    and that gate is decoration unless something fails without it.

    The pre-L3 body protected this by accident, via an `if not chunks: return 0, 0` that sat
    before `clear()` — so it passes there too and proves nothing about the old code. The red
    that matters is against *this* body with the guard removed: measured 2026-07-29, the store
    went 1 chunk -> 0.
    """
    from rag_search.index.indexer import index_project
    from rag_search.index.store import VectorStore
    with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as store_dir, \
            tempfile.TemporaryDirectory() as empty:
        root = Path(proj)
        (root / "a.py").write_text("def add(x, y):\n    return x + y\n")
        store = VectorStore(Path(store_dir) / "v.db")
        _fc, cc = index_project(root, embedder, store, federation_mode=False)
        assert cc > 0
        assert index_project(Path(empty), embedder, store, federation_mode=False) == (0, 0)
        assert store.count() == cc, (
            "an empty walk purged a healthy store — one unreadable project root would erase "
            "its whole index"
        )
        store.close()


# ── P10.1: search scopes on sample indexed repos ─────────────────────────────

def test_search_code_scope_sample_federation_root(embedder):
    from tree_sitter_language_pack import has_language

    from rag_search.core.config import project_vector_db
    from rag_search.index.store import VectorStore
    from rag_search.query.search import search
    from tests.live._projects import federation_root
    proj = federation_root()
    vs = VectorStore(project_vector_db(proj))
    results = search("component rendering", embedder, vs, scope="code", top_k=5)
    vs.close()
    assert results and all(has_language(r.get("language", "")) for r in results)


def test_search_code_scope_sample_service_member(embedder):
    from rag_search.core.config import project_vector_db
    from rag_search.index.store import VectorStore
    from rag_search.query.search import search
    from tests.live._projects import service_member
    proj = service_member()
    vs = VectorStore(project_vector_db(proj))
    results = search("request handler routing", embedder, vs, scope="code", top_k=5)
    vs.close()
    assert results, "search on service member returned no results"


def test_search_all_scope_returns_results(embedder):
    from rag_search.core.config import project_vector_db
    from rag_search.index.store import VectorStore
    from rag_search.query.search import search
    from tests.live._projects import federation_root
    proj = federation_root()
    vs = VectorStore(project_vector_db(proj))
    results = search("configuration", embedder, vs, scope="all", top_k=10)
    vs.close()
    assert results, "scope=all returned no results on sample federation root"


def test_search_top_result_relevant(embedder):
    from rag_search.index.indexer import index_project
    from rag_search.index.store import VectorStore
    with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as store_dir:
        root = Path(proj)
        (root / "auth.py").write_text(
            "def authenticate_user(token):\n    return verify_jwt(token)\n"
        )
        (root / "db.py").write_text(
            "def get_connection():\n    return sqlite3.connect(':memory:')\n"
        )
        (root / "cache.py").write_text(
            "def invalidate_cache(key):\n    del _store[key]\n"
        )
        store = VectorStore(Path(store_dir) / "v.db")
        index_project(root, embedder, store, federation_mode=False)
        q = embedder.embed(["JWT token authentication"], batch_size=1)[0].astype(np.float32)
        results = store.search(q, top_k=3)
        store.close()
        assert len(results) >= 1
        top = results[0]["path"]
        assert top.endswith("auth.py"), f"auth.py should rank first, got: {[r['path'] for r in results]}"
