import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "live: requires CUDA GPU + daemon at :8765 + Ollama")
    config.addinivalue_line("markers", "slow: LLM-heavy (>30s)")


@pytest.fixture(scope="session")
def cuda_ep():
    import onnxruntime as ort
    if "CUDAExecutionProvider" not in ort.get_available_providers():
        pytest.fail("CUDAExecutionProvider unavailable — CPU fallback is forbidden")


@pytest.fixture(scope="session")
def embedder(cuda_ep):
    from opencode_search.embed.embedder import Embedder
    e = Embedder()
    e.warmup()
    return e


@pytest.fixture(scope="session")
def mini_stores(embedder, tmp_path_factory):
    """Vector + graph store over a 3-file Python mini-project for P4 tests."""
    from opencode_search.graph.community import detect_communities
    from opencode_search.graph.extractor import extract_symbols, symbol_id
    from opencode_search.graph.store import GraphStore
    from opencode_search.index.indexer import index_project
    from opencode_search.index.store import VectorStore

    _PY = {
        "auth.py": "def authenticate(token):\n    return verify_jwt(token)\n\ndef verify_jwt(t):\n    return True\n",
        "db.py": "def get_connection():\n    return ':memory:'\n\ndef run_query(sql):\n    pass\n",
        "cache.py": "def get_cached(key):\n    return _STORE.get(key)\n\ndef set_cached(k, v):\n    _STORE[k]=v\n",
    }
    proj = tmp_path_factory.mktemp("p4proj")
    sd = tmp_path_factory.mktemp("p4stores")
    for fname, content in _PY.items():
        (proj / fname).write_text(content)

    vdb = sd / "vec.db"
    vs = VectorStore(vdb)
    index_project(proj, embedder, vs, federation_mode=False)
    vs.close()

    gdb = sd / "graph.db"
    gs = GraphStore(gdb)
    for fname, content in _PY.items():
        for s in extract_symbols(proj / fname, content, "python"):
            sid = symbol_id(fname, s.name, s.start_line)
            gs.upsert_symbol(sid, s.name, s.qualified_name, s.kind,
                             fname, s.start_line, s.end_line, s.language)
    gs.commit()
    detect_communities(gs)
    gs.close()
    yield {"proj": proj, "vdb": vdb, "gdb": gdb, "sd": sd}
