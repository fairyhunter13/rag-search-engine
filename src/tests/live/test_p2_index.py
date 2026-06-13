"""P2 index layer: discover, chunker, indexer, search (GPU)."""
import tempfile
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.live


# ── discover ──────────────────────────────────────────────────────────────────

def test_iter_files_finds_source():
    from opencode_search.index.discover import iter_files
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.py").write_text("x = 1")
        (root / "b.ts").write_text("const x = 1;")
        names = {p.name for p in iter_files(root)}
        assert "a.py" in names and "b.ts" in names


def test_iter_files_skips_ignored_dirs():
    from opencode_search.index.discover import iter_files
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
    from opencode_search.index.discover import _SIZE_LIMITS, iter_files
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        big = root / "big.py"
        big.write_bytes(b"x = 1\n" * (_SIZE_LIMITS["code"] // 6 + 1))
        assert not any(p.name == "big.py" for p in iter_files(root))


def test_iter_files_skips_empty():
    from opencode_search.index.discover import iter_files
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "empty.py").write_text("")
        (root / "real.py").write_text("x = 1")
        names = {p.name for p in iter_files(root)}
        assert "empty.py" not in names and "real.py" in names


# ── chunker ──────────────────────────────────────────────────────────────────

def test_chunk_empty_returns_empty():
    from opencode_search.index.chunker import chunk_file
    assert chunk_file(Path("e.py"), "", "python") == []
    assert chunk_file(Path("e.py"), "   \n", "python") == []


def test_chunk_file_python():
    from opencode_search.index.chunker import chunk_file
    code = "\n".join(f"def func_{i}(x): return x + {i}" for i in range(60))
    chunks = chunk_file(Path("t.py"), code, "python")
    assert len(chunks) >= 1
    assert all(c.language == "python" for c in chunks)
    assert all(c.content.strip() for c in chunks)


# ── indexer + search (GPU) ──────────────────────────────────────────────────

def test_indexer_counts(embedder):
    from opencode_search.index.indexer import index_project
    from opencode_search.index.store import VectorStore
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


def test_search_top_result_relevant(embedder):
    from opencode_search.index.indexer import index_project
    from opencode_search.index.store import VectorStore
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
