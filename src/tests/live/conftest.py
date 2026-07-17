import contextlib

import pytest
import requests

from tests.live._sample_workspace import (
    SampleWorkspace,
    build_sample_workspace,
    teardown_sample_workspace,
)

_DAEMON = "http://127.0.0.1:8765"


def pytest_configure(config):
    config.addinivalue_line("markers", "live: requires CUDA GPU + daemon at :8765")
    config.addinivalue_line("markers", "slow: LLM-heavy (>30s)")


_session_exitstatus: int | None = None


def pytest_sessionfinish(session, exitstatus):
    # Stash pytest's real exit code; the hard-exit itself happens in
    # pytest_unconfigure (below) so the terminal summary — printed by the
    # terminalreporter's own sessionfinish hookwrapper, i.e. AFTER this hook — still
    # lands before we skip finalization.
    global _session_exitstatus
    _session_exitstatus = int(exitstatus)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    """Dodge the onnxruntime/CUDA teardown abort by skipping CPython finalization.

    The CUDA EP frees device memory from C++ static destructors that run during
    interpreter shutdown, after the CUDA runtime is unloaded (cudaErrorCudartUnloading)
    -> abort() (exit 134), AFTER a clean pass summary. os._exit skips all finalization
    so that teardown never runs. unconfigure runs after sessionfinish (summary printed)
    and after session-scoped fixture teardown (registry cleanup, pause_sweeps resume),
    so nothing is skipped; the stashed status is pytest's real code, so a genuine
    failure still fails CI. Gated on onnxruntime actually being imported so
    pure-CPU/non-GPU runs are unaffected.
    """
    import sys
    if _session_exitstatus is None or "onnxruntime" not in sys.modules:
        return
    sys.stdout.flush()
    sys.stderr.flush()
    import os
    os._exit(_session_exitstatus)


@pytest.fixture(scope="session")
def live_client():
    """Thin HTTP client targeting the live daemon at :8765.

    HARD-FAILS (never skips) if the daemon is not reachable — skipping is
    forbidden by the P15 real-integration invariant.  Every happy-path HTTP
    test must drive the production create_app() surface through this fixture.
    """
    class _C:
        BASE = _DAEMON
        def get(self, path, **kw):
            return requests.get(self.BASE + path, **kw)
        def post(self, path, **kw):
            return requests.post(self.BASE + path, **kw)
        def request(self, method, path, **kw):
            return requests.request(method, self.BASE + path, **kw)

    try:
        requests.get(f"{_DAEMON}/healthz", timeout=3)
    except Exception as exc:
        pytest.fail(
            f"Live daemon not reachable at {_DAEMON} — start it with "
            f"`rag-search daemon serve` before running these tests. ({exc})"
        )
    return _C()


@pytest.fixture(scope="session", autouse=True)
def _purge_leaked_test_state():
    """Self-heal any test state a *killed* prior session leaked, before this run starts.

    Every live fixture that registers a temp project builds it under
    ~/.local/share/rse-test-dirs and deregisters it in teardown. A run that is killed
    (CI timeout-minutes kill, SIGKILL, crash, Ctrl-C) never runs teardown, so those
    registrations survive — and registry._migrate() can't drop them because the dir
    still exists on disk. The next session's IS2 guard
    (test_no_junk_paths_in_live_registry) then fails on that leaked junk.

    At session START the current run hasn't built its own workspace yet, so anything
    under rse-test-dirs belongs to a dead prior session: purge every such registry
    entry and stale child dir. Idempotent, and the fix's whole point is that it runs
    regardless of how the prior session died.
    """
    import os
    import shutil

    from rag_search.core.registry import list_projects, remove_project
    from tests.live._projects import _SAFE_BASE

    for e in list_projects():
        if e.path == str(_SAFE_BASE) or e.path.startswith(str(_SAFE_BASE) + os.sep):
            with contextlib.suppress(Exception):
                remove_project(e.path)
    if _SAFE_BASE.exists():
        for child in _SAFE_BASE.iterdir():
            with contextlib.suppress(Exception):
                shutil.rmtree(child, ignore_errors=True)
    yield


@pytest.fixture(scope="session", autouse=True)
def pause_sweeps():
    """Pause background sweeps for the whole session to avoid GPU contention."""
    with contextlib.suppress(Exception):
        requests.post(f"{_DAEMON}/api/sweeps/pause", timeout=5)
    yield
    with contextlib.suppress(Exception):
        requests.post(f"{_DAEMON}/api/sweeps/resume", timeout=5)


@pytest.fixture(scope="session")
def cuda_ep():
    import onnxruntime as ort
    if "CUDAExecutionProvider" not in ort.get_available_providers():
        pytest.fail("CUDAExecutionProvider unavailable — CPU fallback is forbidden")


@pytest.fixture(scope="session")
def embedder(cuda_ep):
    from rag_search.embed.embedder import Embedder
    e = Embedder()
    e.warmup()
    return e


@pytest.fixture(scope="session")
def project_with_communities(sample_workspace: SampleWorkspace) -> str:
    """Sample promo-svc (7 L1 communities) — used for community diversity tests.

    Returns a sample workspace member so tests never touch a real device project.
    promo-svc has 7 L1 communities (≥3 floor) including business_rule + test types.
    """
    return sample_workspace.promo


@pytest.fixture(scope="session")
def federation_root_path(sample_workspace: SampleWorkspace) -> str:
    return sample_workspace.fed_root


@pytest.fixture(scope="session")
def standalone_project_path(sample_workspace: SampleWorkspace) -> str:
    return sample_workspace.ledger


@pytest.fixture(scope="session")
def service_member_path(sample_workspace: SampleWorkspace) -> str:
    return sample_workspace.promo


@pytest.fixture()
def safe_tmp_path():
    """Temporary directory outside /tmp and ~/.cache — safe for registry registration tests."""
    import contextlib
    import shutil
    import tempfile
    from pathlib import Path

    from rag_search.core.registry import list_projects, remove_project
    safe_base = Path.home() / ".local" / "share" / "rse-test-dirs"
    safe_base.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=safe_base))
    yield d
    prefix = str(d) + "/"
    for e in list_projects():
        if e.path.startswith(prefix) or e.path == str(d):
            with contextlib.suppress(Exception):
                remove_project(e.path)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="session")
def mini_stores(embedder, tmp_path_factory):
    """Vector + graph store over a 3-file Python mini-project for P4 tests."""
    from rag_search.graph.community import detect_communities
    from rag_search.graph.extractor import extract_symbols, symbol_id
    from rag_search.graph.store import GraphStore
    from rag_search.index.indexer import index_project
    from rag_search.index.store import VectorStore

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


@pytest.fixture(scope="session")
def sample_workspace(_purge_leaked_test_state) -> SampleWorkspace:
    """Session-scoped sample workspace: GPU-indexed fixture projects + replayed enrichment golden.

    Builds shop-federation (cart/checkout/promo) + ledger-standalone under
    ~/.local/share/rse-test-dirs. DeepSeek is suppressed; enrichment.json
    goldens are replayed from src/tests/fixtures/sample_projects/.
    Teardown removes all registry entries and the temp directory.
    """
    ws = build_sample_workspace()
    yield ws
    teardown_sample_workspace(ws)
