import contextlib
import os
import sys

import pytest
import requests

from tests.live._sample_workspace import (
    SampleWorkspace,
    build_sample_workspace,
    teardown_sample_workspace,
)
from tests.live._sweeps import renew_pause_lease, sweeps_state

_DAEMON = "http://127.0.0.1:8765"


def _contending_live_runs() -> list[str]:
    """Other pytest processes running in this checkout, described well enough to go kill one.

    Keyed on the contending *process*, not on whether it took the same lock file. Two sessions
    already agreed to serialise with `flock` and still collided, because each had invented its own
    lock name — a convention can be followed to the letter by both parties and still serialise
    against nobody, which is why identity beats labels here ([[the migration-tracker lesson]]).

    Our own ancestors are excluded, and that exclusion is the whole difficulty: the shell wrapper
    and the `timeout` that launched this run both carry the word "pytest" in their command line, so
    a check that skipped only `getpid()` reported *itself* as a contender and refused every run.
    """
    from pathlib import Path

    def ppid_of(pid: int) -> int:
        # Field 4 of /proc/<pid>/stat, read after the last ')': comm can hold spaces and parens.
        stat = Path("/proc", str(pid), "stat").read_text()
        return int(stat[stat.rindex(")") + 1:].split()[1])

    repo, found = str(Path(__file__).parents[3]), []
    mine, pid = set(), os.getpid()
    while pid > 1 and pid not in mine:
        mine.add(pid)
        try:
            pid = ppid_of(pid)
        except (OSError, ValueError):
            break
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in mine:
            continue
        with contextlib.suppress(OSError):
            argv = [a for a in entry.joinpath("cmdline").read_bytes().split(b"\0") if a]
            # argv[0] must itself be pytest or an interpreter running it — not merely a process
            # whose command line mentions it. `bash -c "... pytest ..."` and `flock lock pytest`
            # match a substring test, and a wrapper shell that outlives its pytest would then
            # block every later run forever. The wrapped run is still caught: its own pytest
            # appears here the moment it starts, and whichever suite starts second sees the first.
            head = os.path.basename(argv[0].decode("utf-8", "replace")) if argv else ""
            is_pytest = head == "pytest" or (
                head.startswith("python")
                and any(os.path.basename(a.decode("utf-8", "replace")) == "pytest" for a in argv)
            )
            if not is_pytest or os.readlink(entry / "cwd") != repo:
                continue
            env = entry.joinpath("environ").read_bytes().decode("utf-8", "replace")
            profile = next(
                (v.split("=", 1)[1] for v in env.split("\0") if v.startswith("CLAUDE_CONFIG_DIR=")),
                "unknown profile",
            )
            cmd = b" ".join(a for a in argv if a).decode("utf-8", "replace")
            found.append(f"  pid {entry.name} [{profile}]: {cmd[:110]}")
    return found


def pytest_configure(config):
    config.addinivalue_line("markers", "live: requires CUDA GPU + daemon at :8765")
    config.addinivalue_line("markers", "slow: LLM-heavy (>30s)")
    contenders = _contending_live_runs()
    if contenders:
        raise pytest.UsageError(
            "another pytest run is already working in this checkout:\n"
            + "\n".join(contenders)
            + "\n\nTwo live suites share one 1-core daemon cgroup, one GPU, one registry and one "
            "global sweep pause, so they do not merely run slowly — they corrupt each other's "
            "measurements. On 2026-07-30 an undetected overlap produced CB3 reading 0.44 core on "
            "an 'idle' daemon, a 5s /api/metrics timeout, 106 pause calls against 4 resumes, two "
            "leaked store sets and 11 setup errors that vanished on re-run; three were chased as "
            "regressions before the second suite was found. Wait for it, or kill it, then re-run."
        )


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

    It is also the *last* teardown in the session (first session autouse to set up, so last to
    finalize), which is the only point at which every store this run created is already written.
    That is where the index-dir snapshot below is settled up — see
    `purge_unowned_index_dirs_created_since` for why a listing diff is the only handle left by then.
    """
    import os
    import shutil

    from rag_search.core.registry import list_projects
    from tests.live._projects import _SAFE_BASE
    from tests.live._sample_workspace import (
        index_dir_names,
        purge_index_dirs_under,
        purge_project,
        purge_unowned_index_dirs_created_since,
    )

    for e in list_projects():
        if e.path == str(_SAFE_BASE) or e.path.startswith(str(_SAFE_BASE) + os.sep):
            purge_project(e.path)  # row *and* store: the row is the only handle on the dir name
    if _SAFE_BASE.exists():
        for child in _SAFE_BASE.iterdir():
            with contextlib.suppress(Exception):
                purge_index_dirs_under(child)
                shutil.rmtree(child, ignore_errors=True)
    before = index_dir_names()
    yield
    leftover = purge_unowned_index_dirs_created_since(before)
    if leftover:
        # Reported, not silent: this is the backstop, and a backstop that keeps quiet is how the
        # 19-dir residue went unnoticed. A name here means some earlier teardown missed it.
        print(f"\n[teardown] removed {len(leftover)} unowned index dir(s) this run created: "
              f"{', '.join(leftover)}")


@pytest.fixture(autouse=True)
def _drain_graph_lane():
    """Never let one test's graph pass still be running during the next one.

    on_change used to finish its heavy half before returning, so heavy in-process work could not
    outlive the test that caused it. The dispatch workers and the graph lane both broke that, and a
    pass still extracting symbols holds the GIL in stretches — measured here at 3-5s of delivery
    latency for an inotify callback under in-process contention, against a 0.35s budget in
    test_watcher_detects_new_file. That test failed in 2 of 3 full-suite runs and never once in
    isolation, which is exactly the signature of leaked work rather than a slow watcher.

    Only pays for itself when there is something to drain: with an empty queue and no pass in
    flight the join returns on its first predicate check.

    It also renews the daemon's pause lease, because this is the one hook that already fires after
    every test: the session pause now expires on its own (that is how a killed session stops
    wedging the daemon for hours), and the suite outlives any single lease. See
    `_sweeps.renew_pause_lease` for why it is conditional.
    """
    yield
    sweeps = sys.modules.get("rag_search.daemon.sweeps")
    if sweeps is not None:
        with contextlib.suppress(Exception):
            sweeps._graph_lane_join(timeout=300.0)
    renew_pause_lease()


@pytest.fixture(scope="session", autouse=True)
def pause_sweeps():
    """Pause background sweeps for the whole session to avoid GPU contention.

    Teardown restores the state the daemon was actually in, so a run does not hand back a
    daemon that is sweeping when the operator had deliberately paused it.

    No `contextlib.suppress` here any more: a daemon that cannot be paused is a suite that
    should not start, for the same reason `reclaim_daemon_gpu` below asserts rather than warns.
    Suppressing it ran the whole suite unpaused and let the consequences land somewhere else.
    """
    with sweeps_state(paused=True):
        yield


# Peak VRAM the suite itself needs, measured: a green run started with 15,771 MiB free and
# bottomed out at 7,401 MiB, i.e. ~8.4 GB for its own in-process embedder + reranker on top of
# whatever the daemon holds. The gate sits above that with room to spare, because the failure
# it prevents is not a clean shortfall message — it is ~60 tests dying inside onnxruntime with
# CUBLAS/BFCArena errors that name neither the GPU nor the daemon. Env-driven per P18/HR34.
_MIN_FREE_VRAM_MB = float(os.environ.get("RSE_TEST_MIN_VRAM_MB", "10000"))


@pytest.fixture(scope="session", autouse=True)
def reclaim_daemon_gpu(pause_sweeps):
    """Make the daemon hand back its VRAM before the suite loads its own models.

    Pausing sweeps stops the daemon starting *new* GPU work; it does nothing about memory
    already held. ORT's BFC arena keeps the high-water mark of the largest batch it has served
    until the session is destroyed, and the daemon's only path to destroying it is a 300 s idle
    unload that cannot fire while anyone is working against it. Measured: 12.2 GB of a 16 GB
    card still held at `active_clients: 0`, which starved this suite into 60 failures.

    Ordered after `pause_sweeps` (it takes it as an argument) so nothing reloads the models
    between the release and the first test.
    """
    from rag_search.core.gpu import vram_free_mb

    released = None
    with contextlib.suppress(Exception):
        released = requests.post(f"{_DAEMON}/api/gpu/release", timeout=30).status_code

    free_mb = vram_free_mb()
    assert free_mb >= _MIN_FREE_VRAM_MB, (
        f"only {free_mb:.0f} MiB VRAM free, need {_MIN_FREE_VRAM_MB:.0f} MiB. The live suite "
        f"loads a real embedder + reranker in-process on the same GPU as the daemon, so it "
        f"cannot start from here — without this gate it fails ~60 tests inside onnxruntime "
        f"(CUBLAS failure 3 / BFCArena) that look like broken code. POST /api/gpu/release "
        f"returned {released!r}: if that is 404 the running daemon predates the route, so "
        f"`systemctl --user restart rag-search-mcp-daemon` and re-run; if it is 200 something "
        f"other than the daemon holds the card (check the GPU's own process list)."
    )
    yield


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
    import shutil
    import tempfile
    from pathlib import Path

    from rag_search.core.registry import list_projects
    from tests.live._sample_workspace import purge_index_dirs_under, purge_project
    safe_base = Path.home() / ".local" / "share" / "rse-test-dirs"
    safe_base.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=safe_base))
    yield d
    prefix = str(d) + "/"
    for e in list_projects():
        if e.path.startswith(prefix) or e.path == str(d):
            purge_project(e.path)
    # Then by tree walk, for the tests that already dropped their own row: the row is the only
    # handle on the index dir's <slug>-<sha16> name, and mkdtemp guarantees the next run picks a
    # different path, so anything missed here is unreachable for good.
    purge_index_dirs_under(d)
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
    """Session-scoped sample workspace: GPU-indexed fixture projects.

    Builds shop-federation (cart/checkout/promo) + ledger-standalone under
    ~/.local/share/rse-test-dirs. The four enrichment.json goldens this used to replay
    (so a build could produce community narratives without spending DeepSeek tokens) went
    with tier 3, along with the suppression they existed to make safe — there is no LLM in
    the build path any more, so there is nothing left to suppress or replay.
    Teardown removes all registry entries and the temp directory.
    """
    ws = build_sample_workspace()
    yield ws
    teardown_sample_workspace(ws)
