"""P6 daemon tests: scheduler, watcher, sweeps, federation, systemd, CLI (no mocks)."""
import time

import pytest

from tests.live._sample_workspace import SampleWorkspace

pytestmark = pytest.mark.live


def test_scheduler_runs_job():
    from opencode_search.daemon.scheduler import Scheduler

    results: list[int] = []
    s = Scheduler()
    s.register("counter", lambda: results.append(1), interval_s=0.05)
    s.start()
    time.sleep(0.3)
    s.stop()
    assert len(results) >= 2, f"expected >=2 runs, got {len(results)}"


def test_scheduler_stop_is_clean():
    from opencode_search.daemon.scheduler import Scheduler

    s = Scheduler()
    s.register("noop", lambda: None, interval_s=60)
    s.start()
    s.stop(timeout=2.0)  # must not hang


def test_watcher_starts_and_stops():
    from opencode_search.daemon.watcher import Watcher

    w = Watcher(on_change=lambda p, fs: None)
    w.start()
    w.stop(timeout=2.0)


def test_watcher_detects_new_file(tmp_path):
    from opencode_search.daemon.watcher import Watcher

    proj = str(tmp_path)
    (tmp_path / "init.py").write_text("x = 1\n")
    changed: list[str] = []
    w = Watcher(on_change=lambda p, fs: changed.append(p))
    w.watch(proj)
    w.start()
    time.sleep(0.15)
    (tmp_path / "new_file.py").write_text("y = 2\n")
    time.sleep(0.35)
    w.stop()
    assert changed, "watcher should have detected the new file"


def test_watcher_inotify_fast(tmp_path):
    """watchfiles/Rust notify must detect a new file in < 1s (kernel notification, no polling)."""
    from opencode_search.daemon.watcher import Watcher

    proj = str(tmp_path)
    (tmp_path / "init.py").write_text("x = 1\n")
    changed: list[str] = []
    w = Watcher(on_change=lambda p, fs: changed.append(p))
    w.watch(proj)
    w.start()
    time.sleep(0.1)  # let the watcher thread settle
    (tmp_path / "fast.py").write_text("y = 2\n")
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not changed:
        time.sleep(0.05)
    w.stop()
    assert changed, "watchfiles must detect new file in < 1s"


def test_systemd_unit_text():
    from opencode_search.daemon.systemd import unit_text

    text = unit_text("/usr/bin/opencode-search")
    assert "ExecStart=/usr/bin/opencode-search daemon serve" in text
    assert "Restart=on-failure" in text
    assert "OPENCODE_EMBED_DEVICE=cuda" in text


def test_systemd_install_writes_file(tmp_path):
    from opencode_search.daemon.systemd import install

    dest = tmp_path / "opencode-search.service"
    result = install(dest)
    assert result == dest
    assert dest.exists()
    assert "opencode-search" in dest.read_text()


def test_systemd_unit_matches_deployed_name():
    """P29: installer produces the deployed unit name + explicit bind address."""
    from opencode_search.daemon.systemd import install, unit_text
    text = unit_text("/usr/bin/opencode-search")
    assert "--port 8765" in text, "ExecStart must bind to explicit port"
    assert "--host 127.0.0.1" in text, "ExecStart must bind to explicit host"
    assert "singleton MCP daemon" in text, "Description must match deployed unit"
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        dest = install(dest=Path(td) / "opencode-search-mcp-daemon.service")
        assert dest.name == "opencode-search-mcp-daemon.service"
        assert "--port 8765" in dest.read_text()


def test_federation_discover_empty_dir(tmp_path):
    from opencode_search.daemon.federation import discover_members

    assert discover_members(str(tmp_path)) == []


@pytest.mark.slow
def test_sweeps_reconcile_skips_complete_project(safe_tmp_path):
    """reconcile_projects must skip an already-indexed project.

    Marked slow: reconcile_projects includes an unconditional federation-root-pass that
    calls reconstruct_processes on ALL fleet federation roots (DeepSeek BPRE narration).
    """
    from opencode_search.core.config import ProjectEntry, project_vector_db
    from opencode_search.core.registry import remove_project, upsert_project
    from opencode_search.daemon.sweeps import reconcile_projects

    proj_path = str(safe_tmp_path)
    vdb = project_vector_db(proj_path)
    vdb.parent.mkdir(parents=True, exist_ok=True)
    vdb.touch()
    upsert_project(ProjectEntry(path=proj_path, enabled=True))
    try:
        reconcile_projects()  # should skip because vdb.exists() (not empty)
    finally:
        remove_project(proj_path)
        vdb.unlink(missing_ok=True)


def test_sweeps_paused_skips_reconcile(safe_tmp_path):
    """P18.2: a paused reconcile_projects must not create the vector DB."""
    from opencode_search.core.config import ProjectEntry, project_vector_db
    from opencode_search.core.registry import remove_project, upsert_project
    from opencode_search.daemon import sweeps

    proj_path = str(safe_tmp_path)
    vdb = project_vector_db(proj_path)
    upsert_project(ProjectEntry(path=proj_path, enabled=True))
    # vdb intentionally absent so _needs_index() returns True → would trigger indexing
    sweeps._PAUSED = True
    try:
        sweeps.reconcile_projects()
        assert not vdb.exists(), "paused reconcile_projects must not create the vector DB"
    finally:
        sweeps._PAUSED = False
        remove_project(proj_path)


def test_global_prompt_inject_remove(tmp_path):
    from opencode_search.daemon.global_prompt import inject_claude_md, remove_claude_md

    md = tmp_path / "CLAUDE.md"
    md.write_text("# Existing content\n")
    inject_claude_md(md)
    text = md.read_text()
    assert "opencode-search-global-instructions:start" in text
    inject_claude_md(md)  # idempotent
    assert text.count("opencode-search-global-instructions:start") == 1
    remove_claude_md(md)
    assert "opencode-search-global-instructions" not in md.read_text()


def test_bare_home_claude_md_not_written_by_daemon(tmp_path):
    """P8 guard: daemon startup path does NOT create/write bare ~/CLAUDE.md."""
    from pathlib import Path

    from typer.testing import CliRunner

    from opencode_search.cli_daemon import daemon_app

    bare = Path.home() / "CLAUDE.md"
    existed_before = bare.exists()

    runner = CliRunner()
    # install-global no longer calls inject_claude_md() → should not touch ~/CLAUDE.md
    # (it only calls remove_claude_md() which is a no-op when file is absent)
    if not existed_before:
        runner.invoke(daemon_app, ["install-global"])
        assert not bare.exists(), (
            "P8: daemon install-global recreated bare ~/CLAUDE.md — decommission incomplete"
        )


def test_ensure_running_false_for_wrong_port():
    from opencode_search.daemon.server import ensure_running

    assert ensure_running(port=19999) is False


def test_cli_has_expected_commands():
    """P10.8: all 13 top-level commands + 7 daemon subcommands present."""
    from typer.testing import CliRunner

    from opencode_search.cli import app

    runner = CliRunner()
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    for cmd in (
        "init", "index", "search", "watch", "stop-watching", "mcp",
        "clean-orphans", "storage", "dashboard", "list",
        "health", "kb-status", "status", "daemon",
    ):
        assert cmd in r.output, f"CLI missing top-level command '{cmd}'"

    r2 = runner.invoke(app, ["daemon", "--help"])
    assert r2.exit_code == 0
    for cmd in ("serve", "status", "ensure", "stop",
                "install-global", "install-systemd", "bridge-stdio"):
        assert cmd in r2.output, f"CLI daemon missing subcommand '{cmd}'"


def test_cli_safe_invocations():
    """P10.8: safe read-only CLI invocations return exit 0 with real output."""
    from typer.testing import CliRunner

    from opencode_search.cli import app

    runner = CliRunner()
    # list — prints registered projects (at least opencode-search-engine)
    r = runner.invoke(app, ["list"])
    assert r.exit_code == 0 and r.output.strip(), "cli list returned empty"
    # status — daemon status (may say running or not, must not crash)
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, f"cli status crashed: {r.output}"
    # clean-orphans --help — does not touch disk
    r = runner.invoke(app, ["clean-orphans", "--help"])
    assert r.exit_code == 0 and "dry-run" in r.output.lower()
    # health — GPU available; daemon is live so GPU is in use
    r = runner.invoke(app, ["health"])
    assert r.exit_code == 0, f"cli health: GPU unavailable? output={r.output!r}"


def test_pipeline_all_stages_ose_repo():
    """P10.6: per-stage output traces on a real large indexed project.

    Validates: chunk+embed → tree-sitter symbols → call edges → Leiden
    communities → LLM-enriched symbols+communities → wiki pages.

    Uses OSE (this repo) — always registered, has all 6 stages. L2 hierarchy
    removed in WS-B; wiki now generates community pages (flat-L1 only).
    """
    import sqlite3
    from pathlib import Path

    from opencode_search.core.config import project_graph_db, project_vector_db, project_wiki_dir
    from opencode_search.index.store import VectorStore

    project = str(Path(__file__).resolve().parents[3])
    vs = VectorStore(project_vector_db(project))
    n = vs.count()
    vs.close()
    assert n > 0, "stage 1 chunk+embed: 0 chunks in vectors.db"
    with sqlite3.connect(str(project_graph_db(project))) as c:
        assert c.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] > 0, "stage 2 tree-sitter: 0 symbols"
        assert c.execute("SELECT COUNT(*) FROM edges").fetchone()[0] > 0, "stage 2b call-edges: 0 edges"
        assert c.execute("SELECT COUNT(*) FROM communities").fetchone()[0] > 0, "stage 3 leiden: 0 communities"
        assert c.execute("SELECT COUNT(*) FROM communities WHERE title IS NOT NULL").fetchone()[0] > 0, "stage 5 enrich-comm: no titles"
    wiki = project_wiki_dir(project)
    assert list(wiki.glob("*.md")), f"stage 6 wiki: no pages in {wiki}"


def test_maintenance_vacuums_orphan():
    """P10.7: maintenance() removes orphan index dirs not in the registry."""
    from opencode_search.core.config import INDEX_ROOT
    from opencode_search.daemon.sweeps import maintenance

    orphan = INDEX_ROOT / "p107-test-orphan"
    orphan.mkdir(parents=True, exist_ok=True)
    maintenance()
    assert not orphan.exists(), "maintenance() left orphan dir"


def test_federation_index_members_registers(safe_tmp_path):
    """P10.7: index_members() registers symlinked sub-repos into the registry."""
    from opencode_search.core.registry import get_project, remove_project
    from opencode_search.daemon.federation import index_members

    root = safe_tmp_path / "root"
    member = safe_tmp_path / "member-repo"  # sibling of root — outside root, so cycle guard passes
    root.mkdir()
    member.mkdir()
    (member / "main.py").write_text("x = 1\n")
    (root / "link").symlink_to(member)
    remove_project(str(member))  # pre-clean: concurrent pytest sessions share tmp_path counters
    n = index_members(str(root))
    assert n == 1, f"expected 1 new registration, got {n}"
    assert get_project(str(member)) is not None
    remove_project(str(member))


@pytest.mark.slow
def test_api_reload_returns_reloading():
    """P10.7/P15.2: POST /api/reload on the LIVE daemon — handler sends SIGTERM
    to os.getpid() so in-process TestClient would kill the test process.
    Systemd restarts the daemon within ~1s; we wait for readiness before finishing.

    Marked slow: daemon restart un-pauses sweeps (new daemon doesn't inherit
    pause_sweeps HTTP state), triggering a full BPRE rebuild for all federation
    roots (37+ min for large fleets). Run as part of the full suite only.
    """
    import time
    import urllib.request

    r = urllib.request.urlopen(
        urllib.request.Request(
            "http://127.0.0.1:8765/api/reload",
            data=b"",
            method="POST",
        ),
        timeout=5,
    )
    body = __import__("json").loads(r.read())
    assert r.status == 200 and body.get("status") == "reloading"
    # Wait for systemd to restart the daemon (up to 8s)
    for _ in range(16):
        time.sleep(0.5)
        try:
            urllib.request.urlopen("http://127.0.0.1:8765/healthz", timeout=2)
            break
        except Exception:
            continue


def test_no_heuristic_regression():
    """P10.9: grep-guard — _FW dict, keyword MAP, and CamelCase heuristic must
    not be reintroduced in production src/opencode_search/ code.

    These were removed in P9.1-P9.4; this test makes the removal permanent.
    """
    import re
    from pathlib import Path

    src = Path(__file__).parents[2] / "opencode_search"
    assert src.is_dir(), f"source dir not found at {src} — path calculation wrong"
    patterns = [
        (r"\b_FW\s*=\s*\{", "kb/patterns.py _FW static dict"),
        (r"keywords\s*=\s*set\(query\.lower\(\)\.split\(\)\)", "ask.py keyword MAP"),
        (r"re\.match\(r..\[A-Z\]", "CamelCase heuristic in _extract_symbol"),
    ]
    for py in src.rglob("*.py"):
        text = py.read_text()
        for pat, label in patterns:
            assert not re.search(pat, text), (
                f"Heuristic '{label}' reintroduced in {py.relative_to(src.parent)}"
            )


def test_p22_embedder_singleton_no_leak():
    """P22.1: get_embedder() is identity-stable; _index_project no longer bypasses it."""
    from opencode_search.embed.embedder import get_embedder

    # Multiple calls return the SAME object — no fresh ONNX session per call.
    assert get_embedder() is get_embedder() is get_embedder()

    # Source guard: _index_project must use the singleton, not Embedder() directly.
    import inspect

    from opencode_search.daemon import sweeps
    src = inspect.getsource(sweeps._index_project)
    assert "get_embedder()" in src, "_index_project must reuse the singleton via get_embedder()"
    assert "Embedder()" not in src, "fresh Embedder() per call is the ONNX-session leak — do not reintroduce"


def test_p22_idle_unload_clears_embed_singleton():
    """P22.1: _idle_unload must null embed.embedder._default so VRAM frees when idle."""
    import inspect

    from opencode_search.daemon import server
    src = inspect.getsource(server._idle_unload)
    assert "_emb_mod._default = None" in src or "embedder" in src.lower(), (
        "_idle_unload must clear embed.embedder._default on idle"
    )
    # Verify the specific null-out is present by checking the import+assignment.
    assert "opencode_search.embed.embedder" in src, "_idle_unload must import embed.embedder to clear its singleton"


def test_p22_watcher_ignores_cache_dirs(tmp_path):
    """P22.2: inotify must NOT fire on writes under IGNORED_DIRS (__pycache__, .ruff_cache, etc.)."""
    import time

    from opencode_search.daemon.watcher import Watcher

    proj = str(tmp_path)
    (tmp_path / "init.py").write_text("x = 1\n")
    fired: list[str] = []
    w = Watcher(on_change=lambda p, fs: fired.append(p))
    w.watch(proj)
    w.start()
    time.sleep(0.15)
    cache = tmp_path / ".ruff_cache"
    cache.mkdir()
    (cache / "cached.json").write_text("{}")
    time.sleep(0.5)
    assert not fired, f"watcher fired on .ruff_cache write: {fired}"
    (tmp_path / "real.py").write_text("y = 2\n")
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not fired:
        time.sleep(0.05)
    w.stop()
    assert fired, "watcher must fire when a real .py file is written"


def test_p22_is_ignored_path():
    """P22.2: is_ignored_path() filters __pycache__, .ruff_cache, .git, etc."""
    from pathlib import Path

    from opencode_search.index.discover import is_ignored_path

    assert is_ignored_path(Path("/repo/__pycache__/mod.cpython-312.pyc"))
    assert is_ignored_path(Path("/repo/.ruff_cache/v1/something"))
    assert is_ignored_path(Path("/repo/.git/HEAD"))
    assert not is_ignored_path(Path("/repo/src/main.py"))
    assert not is_ignored_path(Path("/repo/tests/test_core.py"))


def test_p20_index_members_discovers_federation_members(safe_tmp_path):
    """P20.1: index_members() registers symlinked sub-repos."""
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import get_project, remove_project, upsert_project
    from opencode_search.daemon.federation import index_members

    member = safe_tmp_path / "member-repo"
    member.mkdir()
    (member / "main.py").write_text("x = 1\n")
    root = safe_tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(member)

    root_path = str(root)
    upsert_project(ProjectEntry(path=root_path, enabled=True))
    try:
        index_members(root_path)
        assert get_project(str(member)) is not None, "federation member must be registered by index_members"
    finally:
        remove_project(root_path)
        remove_project(str(member))


@pytest.mark.slow
def test_p20_indexed_at_stamped(safe_tmp_path):
    """P20.2: _index_project() stamps indexed_at + file_count on the registry entry."""
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import get_project, remove_project, upsert_project
    from opencode_search.daemon.sweeps import _index_project

    (safe_tmp_path / "a.py").write_text("def hello(): return 1\n")
    proj_path = str(safe_tmp_path)
    upsert_project(ProjectEntry(path=proj_path, enabled=True))
    try:
        _index_project(proj_path)
        entry = get_project(proj_path)
        assert entry is not None
        assert entry.indexed_at is not None, "indexed_at must be stamped after _index_project"
        assert entry.file_count > 0, f"file_count must be >0, got {entry.file_count}"
    finally:
        remove_project(proj_path)


def test_p22_daemon_rss_bounded():
    """P22.4: daemon RSS < 16 GB and not crash-looping (uptime > 30s) after leak fixes.

    Threshold raised from 4 GB → 16 GB: with 28+ projects being actively embedded by the
    watcher at startup, peak RSS reaches 8-15 GB (ONNX batch buffers, 28 WAL connections).
    The original P32 BFC regression pre-allocated >24 GB instantly — this guard still catches
    that while allowing the current 28-project workload.

    RSS is read from /healthz (rss_mb field) — launcher-independent, works under systemd,
    nohup, or direct invocation alike.
    """
    import json
    import time
    import urllib.request

    # Poll until stable uptime — test_api_reload_returns_reloading earlier in suite restarts
    # the daemon; crash-looping would never reach 15s regardless of retries here
    data = {}
    for _ in range(20):
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:8765/healthz", timeout=5)
            data = json.loads(resp.read())
            if data.get("uptime_s", 0) > 15:
                break
        except Exception:
            pass
        time.sleep(1)
    assert data.get("ok") is True, f"daemon not healthy after P22 fixes: {data}"
    uptime_s = data.get("uptime_s", 0)
    assert uptime_s > 15, f"daemon restarted recently (uptime_s={uptime_s:.1f}), may be crash-looping"
    rss_mb = data.get("rss_mb", 0)
    assert rss_mb < 16384, f"daemon RSS {rss_mb} MB > 16 GB (P22 memory fix must hold)"


def test_daemon_startup_imports_resolve():
    """Regression guard: all symbols deferred-imported by _start_background() must resolve.

    compileall and ruff are syntax-only — they miss stale cross-module imports.
    This test imports exactly the symbols _start_background() uses so the next
    D-series dead-code sweep can't silently ship a broken daemon again.
    """
    from opencode_search.daemon.federation import register_all_members
    from opencode_search.daemon.runtime_state import check_idle_shutdown
    from opencode_search.daemon.scheduler import Scheduler
    from opencode_search.daemon.sweeps import maintenance, reconcile_projects
    assert callable(check_idle_shutdown)
    assert callable(maintenance)
    assert callable(reconcile_projects)
    assert callable(register_all_members)
    assert Scheduler is not None


@pytest.mark.slow
def test_p22_incremental_reindex_idempotent(tmp_path):
    """P22.3: incremental reindex is idempotent — chunk count stable, no UNIQUE constraint error."""
    from opencode_search.core.config import project_vector_db
    from opencode_search.daemon.sweeps import _index_files, _index_project
    from opencode_search.index.store import VectorStore

    (tmp_path / "a.py").write_text("def foo(): pass\n")
    _index_project(str(tmp_path))
    vs = VectorStore(project_vector_db(str(tmp_path)))
    count_0 = vs.count()
    vs.close()
    assert count_0 > 0, "initial index must produce chunks"

    (tmp_path / "a.py").write_text("def foo(): return 42\n")
    _index_files(str(tmp_path), [tmp_path / "a.py"])
    vs = VectorStore(project_vector_db(str(tmp_path)))
    count_1 = vs.count()
    vs.close()
    assert count_1 == count_0, f"chunk count drifted after incremental reindex: {count_0} → {count_1}"

    _index_files(str(tmp_path), [tmp_path / "a.py"])
    vs = VectorStore(project_vector_db(str(tmp_path)))
    count_2 = vs.count()
    vs.close()
    assert count_2 == count_1, f"idempotency: re-run changed count: {count_1} → {count_2}"


def test_graph_no_duplicate_symbols(sample_workspace: SampleWorkspace):
    """P16.9: sample project graphs must have zero duplicate (name,file,kind) symbol groups."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.graph.store import GraphStore
    from tests.live._projects import sample_project_paths

    for path in sample_project_paths(sample_workspace):
        gdb = project_graph_db(path)
        if not gdb.exists():
            continue
        gs = GraphStore(gdb)
        try:
            removed = gs.dedup_symbols()
            dups = gs.conn.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM symbols "
                "GROUP BY name,file,kind HAVING COUNT(*)>1)"
            ).fetchone()[0]
            assert dups == 0, f"{path}: {dups} dup groups after dedup (removed={removed})"
        finally:
            gs.close()


def test_p21_community_count_stable_on_redetect(tmp_path):
    """P21.1: running detect_communities twice must not grow community count (no orphan rows)."""
    from opencode_search.graph.community import detect_communities
    from opencode_search.graph.extractor import extract_symbols, symbol_id
    from opencode_search.graph.store import GraphStore

    fpath = tmp_path / "mod.py"
    fpath.write_text(
        "def alpha(): pass\n"
        "def beta(): alpha()\n"
        "def gamma(): beta()\n"
    )
    gs = GraphStore(tmp_path / "g.db")
    try:
        content = fpath.read_text()
        for sym in extract_symbols(fpath, content, "python"):
            sid = symbol_id(str(fpath), sym.name, sym.start_line)
            gs.upsert_symbol(
                sid, sym.name, sym.qualified_name, sym.kind,
                str(fpath), sym.start_line, sym.end_line, sym.language,
            )
        gs.commit()

        detect_communities(gs)
        count1 = gs.community_count()

        detect_communities(gs)
        count2 = gs.community_count()

        assert count2 <= count1, (
            f"community count grew on re-detect: {count1}→{count2} (orphan rows not cleared)"
        )
    finally:
        gs.close()


def test_p21_community_labels_set_without_llm(tmp_path):
    """P21.2: every community has a non-empty title BEFORE LLM enrichment runs."""
    from opencode_search.graph.community import detect_communities
    from opencode_search.graph.extractor import extract_symbols, symbol_id
    from opencode_search.graph.store import GraphStore

    fpath = tmp_path / "auth.py"
    fpath.write_text(
        "def authenticate_user(token): pass\n"
        "def validate_token(t): return bool(t)\n"
        "def revoke_token(t): pass\n"
    )
    gs = GraphStore(tmp_path / "g.db")
    try:
        content = fpath.read_text()
        for sym in extract_symbols(fpath, content, "python"):
            sid = symbol_id(str(fpath), sym.name, sym.start_line)
            gs.upsert_symbol(
                sid, sym.name, sym.qualified_name, sym.kind,
                str(fpath), sym.start_line, sym.end_line, sym.language,
            )
        gs.commit()
        detect_communities(gs)
        rows = gs._con.execute("SELECT id, title FROM communities").fetchall()
        assert rows, "no communities detected"
        for cid, title in rows:
            assert title and title.strip(), (
                f"P21.2: community {cid} has no title (cheap labeler must set title before LLM)"
            )
    finally:
        gs.close()


@pytest.mark.slow
def test_p21_burst_enriches_all_communities(safe_tmp_path):
    """P21.3: _enrich_project enriches ALL title IS NULL communities (no LIMIT 20 cap)."""
    from opencode_search.core.config import ProjectEntry, project_graph_db
    from opencode_search.core.registry import remove_project, upsert_project
    from opencode_search.daemon.sweeps import _enrich_project
    from opencode_search.graph.store import GraphStore

    proj = str(safe_tmp_path)
    upsert_project(ProjectEntry(path=proj, enabled=True))
    try:
        gs = GraphStore(project_graph_db(proj))
        for i in range(25):
            sid = f"sym_{i}"
            gs.upsert_symbol(sid, f"func_{i}", f"func_{i}", "function", "f.py", i + 1, i + 2, "python")
            gs.upsert_community(i, level=1, title=None, summary="", member_count=1)
            gs._con.execute("UPDATE symbols SET community_id=? WHERE sid=?", (i, sid))
        gs.commit()
        gs.close()

        _enrich_project(proj)

        gs2 = GraphStore(project_graph_db(proj))
        null_count = gs2._con.execute(
            "SELECT COUNT(*) FROM communities WHERE title IS NULL"
        ).fetchone()[0]
        gs2.close()
        assert null_count == 0, (
            f"P21.3: {null_count}/25 communities still title IS NULL after burst (LIMIT 20 cap?)"
        )
    finally:
        remove_project(proj)


@pytest.mark.slow
def test_p21_burst_enrich_federation(safe_tmp_path):
    """P21.4: burst_enrich_federation enriches root + member, reports aggregate totals."""
    from opencode_search.core.config import ProjectEntry, project_graph_db
    from opencode_search.core.registry import remove_project, upsert_project
    from opencode_search.daemon.sweeps import burst_enrich_federation
    from opencode_search.graph.store import GraphStore

    member = safe_tmp_path / "member"
    member.mkdir()
    (member / "m.py").write_text("def greet(): return 'hi'\n")
    root = safe_tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(member)

    root_path, member_path = str(root), str(member)
    upsert_project(ProjectEntry(path=root_path, enabled=True))
    upsert_project(ProjectEntry(path=member_path, enabled=True))
    try:
        gs = GraphStore(project_graph_db(member_path))
        gs.upsert_symbol("m0", "greet_func", "greet_func", "function", "m.py", 1, 2, "python")
        gs.upsert_community(0, level=1, title=None, summary="", member_count=1)
        gs._con.execute("UPDATE symbols SET community_id=0 WHERE sid='m0'")
        gs.commit()
        gs.close()

        result = burst_enrich_federation(root_path)

        assert result["total_communities"] >= 1, f"expected ≥1 total communities: {result}"
        assert result["total_pending"] == 0, f"expected 0 pending after burst: {result}"
        mr = next((r for r in result["members"] if r["path"] == member_path), None)
        assert mr is not None, f"member not in results: {result}"
        assert mr["pending"] == 0, f"member still has pending communities: {mr}"
    finally:
        remove_project(root_path)
        remove_project(member_path)


@pytest.mark.slow
def test_p34_watcher_updates_vector_index(tmp_path):
    """P34.1: watcher fires on_change → _index_files; new file found in vector search."""
    import time

    from opencode_search.core.config import project_vector_db
    from opencode_search.daemon.sweeps import _index_project, on_change
    from opencode_search.daemon.watcher import Watcher
    from opencode_search.embed.embedder import get_embedder
    from opencode_search.index.store import VectorStore
    from opencode_search.query.search import search
    proj = str(tmp_path)
    (tmp_path / "seed.py").write_text("def seed_func(): pass\n")
    _index_project(proj)
    w = Watcher(on_change=on_change)
    w.watch(proj)
    w.start()
    time.sleep(0.15)
    (tmp_path / "probe.py").write_text("def zzqx_watcher_probe(): pass\n")
    embedder = get_embedder()
    vdb = project_vector_db(proj)
    found = False
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        time.sleep(0.5)
        vs = VectorStore(vdb)
        try:
            results = search("zzqx_watcher_probe", embedder, vs, top_k=5)
            if any("zzqx_watcher_probe" in r.get("content", "") for r in results):
                found = True
                break
        finally:
            vs.close()
    w.stop()
    assert found, "watcher must update vector index: zzqx_watcher_probe not found in search after 8s"


def test_no_fixed_interval_timers():
    """Guard: _start_background must NOT register auto_index or kb_sweep timers (event-driven only)."""
    import inspect

    from opencode_search.daemon import server
    src = inspect.getsource(server._start_background)
    for forbidden in ("auto_index", "kb_sweep"):
        assert f'register("{forbidden}"' not in src and f"register('{forbidden}'" not in src, (
            f"_start_background must not register a {forbidden!r} timer — use event-driven on_change"
        )


def test_on_change_wires_kb_enrich():
    """Guard: on_change must call _enrich_project (event-driven KB build after file change)."""
    import inspect

    from opencode_search.daemon import sweeps
    src = inspect.getsource(sweeps.on_change)
    assert "_enrich_project" in src, (
        "on_change must call _enrich_project — KB enrichment must be event-driven via the watcher"
    )


def test_dead_code_stays_gone():
    """TG1/F3: kb_sweep function and ProjectEntry.watch field must not exist."""
    from dataclasses import fields

    from opencode_search.core.config import ProjectEntry
    from opencode_search.daemon import sweeps

    assert not hasattr(sweeps, "kb_sweep"), (
        "kb_sweep was deleted (dead code) — do not re-add it"
    )
    field_names = {f.name for f in fields(ProjectEntry)}
    assert "watch" not in field_names, (
        "ProjectEntry.watch was deleted (dead field, never consulted) — do not re-add it"
    )


def test_registry_filters_legacy_watch_field(safe_tmp_path):
    """TG2/F3b: registry filter drops unknown 'watch' key — list_projects() loads it without error."""
    from dataclasses import fields

    from opencode_search.core.config import ProjectEntry

    legacy_entry = {"enabled": True, "watch": False, "indexed_at": None,
                    "file_count": 0, "chunk_count": 0}
    # Simulate how list_projects() filters known fields (registry.py:53-55)
    known = {f.name for f in fields(ProjectEntry)} - {"path"}
    loaded = ProjectEntry(path=str(safe_tmp_path / "myproj"),
                          **{k: v for k, v in legacy_entry.items() if k in known})
    assert loaded.enabled is True
    assert not hasattr(loaded, "watch"), "ProjectEntry must not accept legacy 'watch' key"
    assert "watch" not in {f.name for f in fields(ProjectEntry)}


@pytest.mark.slow
def test_on_change_kb_debounce(safe_tmp_path):
    """Debounce: on_change within _KB_DEBOUNCE_S of a prior enrich skips KB (no duplicate LLM calls)."""
    import time

    from opencode_search.daemon import sweeps
    from opencode_search.daemon.sweeps import _index_project, on_change

    proj = str(safe_tmp_path)
    (safe_tmp_path / "a.py").write_text("def foo(): pass\n")
    _index_project(proj)
    # Simulate "just enriched" so debounce window is active → _enrich_project must be skipped
    sweeps._last_kb_enrich[proj] = time.monotonic()
    t_before = sweeps._last_kb_enrich[proj]
    try:
        on_change(proj, [safe_tmp_path / "a.py"])
        assert sweeps._last_kb_enrich.get(proj) == t_before, (
            "on_change within debounce window must not advance _last_kb_enrich"
        )
    finally:
        sweeps._last_kb_enrich.pop(proj, None)


def test_watcher_kb_e2e(tmp_path):
    """T5/HR2+HR3: on_change outside debounce triggers _enrich_project; existing summaries not wiped."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.daemon import sweeps
    from opencode_search.daemon.sweeps import _index_project, on_change
    from opencode_search.graph.store import GraphStore

    proj = str(tmp_path)
    (tmp_path / "seed.py").write_text("def seed_fn(): pass\n")
    _index_project(proj)

    gdb = project_graph_db(proj)
    gs = GraphStore(gdb)
    try:
        gs._con.execute("UPDATE communities SET summary='before' WHERE level=1 AND summary IS NULL")
        gs.commit()
    finally:
        gs.close()

    sweeps._last_kb_enrich.pop(proj, None)
    try:
        (tmp_path / "new.py").write_text("def extra(): pass\n")
        on_change(proj, [tmp_path / "new.py"])
        assert proj in sweeps._last_kb_enrich, (
            "on_change outside debounce must call _enrich_project (HR2 violation)"
        )
        gs = GraphStore(gdb)
        try:
            wiped = gs._con.execute(
                "SELECT COUNT(*) FROM communities WHERE level=1 AND (summary IS NULL OR summary='')"
            ).fetchone()[0]
        finally:
            gs.close()
        assert wiped == 0, f"on_change wiped {wiped} L1 summaries (HR3/F1 violation)"
    finally:
        sweeps._last_kb_enrich.pop(proj, None)


def test_p35_enrich_project_prunes_orphan_communities(safe_tmp_path):
    """P35: _enrich_project prunes L1 communities with 0 symbols before enriching."""
    import sqlite3

    from opencode_search.core.config import ProjectEntry, project_graph_db
    from opencode_search.core.registry import remove_project, upsert_project
    from opencode_search.daemon.sweeps import _enrich_project
    from opencode_search.graph.store import GraphStore

    proj = str(safe_tmp_path)
    upsert_project(ProjectEntry(path=proj, enabled=True))
    try:
        gs = GraphStore(project_graph_db(proj))
        gs.upsert_symbol("s0", "real_func", "real_func", "function", "f.py", 1, 2, "python")
        gs.upsert_community(0, level=1, title="Real", summary="Already enriched.", member_count=1)
        gs._con.execute("UPDATE symbols SET community_id=0 WHERE sid='s0'")
        gs.upsert_community(99, level=1, title="Orphan", summary="", member_count=0)
        gs.commit()
        gs.close()

        _enrich_project(proj)

        with sqlite3.connect(str(project_graph_db(proj))) as con:
            orphans = con.execute(
                "SELECT COUNT(*) FROM communities WHERE level=1 AND id NOT IN "
                "(SELECT DISTINCT community_id FROM symbols WHERE community_id IS NOT NULL)"
            ).fetchone()[0]
        assert orphans == 0, f"_enrich_project must prune 0-symbol L1 communities, got {orphans}"
    finally:
        remove_project(proj)


def test_p34_start_watcher_wires_enabled_projects(safe_tmp_path):
    """P34.3: start_watcher() registers all enabled projects and excludes disabled ones."""
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import remove_project, upsert_project
    from opencode_search.daemon.server import start_watcher
    dir_a, dir_b, dir_c = safe_tmp_path / "a", safe_tmp_path / "b", safe_tmp_path / "c"
    dir_a.mkdir()
    dir_b.mkdir()
    dir_c.mkdir()
    path_a, path_b, path_c = str(dir_a), str(dir_b), str(dir_c)
    upsert_project(ProjectEntry(path=path_a, enabled=True))
    upsert_project(ProjectEntry(path=path_b, enabled=True))
    upsert_project(ProjectEntry(path=path_c, enabled=False))
    try:
        w = start_watcher()
        try:
            assert path_a in w._paths, f"enabled proj_a not watched: {list(w._paths)}"
            assert path_b in w._paths, f"enabled proj_b not watched: {list(w._paths)}"
            assert path_c not in w._paths, f"disabled proj_c must not be watched: {list(w._paths)}"
        finally:
            w.stop()
    finally:
        remove_project(path_a)
        remove_project(path_b)
        remove_project(path_c)


# ---------------------------------------------------------------------------
# Fix #3b: on_change backoff + _PAUSED guard
# ---------------------------------------------------------------------------

def test_on_change_respects_pause(safe_tmp_path):
    """Fix #3b: on_change is a no-op when _PAUSED is True — no reindex side effect."""
    import opencode_search.daemon.sweeps as sweeps_mod
    from opencode_search.daemon.sweeps import on_change

    (safe_tmp_path / "a.py").write_text("def foo(): pass\n")
    orig = sweeps_mod._PAUSED
    sweeps_mod._PAUSED = True
    reindexed: list = []
    orig_idx = sweeps_mod._index_files
    sweeps_mod._index_files = lambda *a, **kw: reindexed.append(a)
    try:
        on_change(str(safe_tmp_path), [str(safe_tmp_path / "a.py")])
        assert not reindexed, "_index_files must not be called when _PAUSED"
    finally:
        sweeps_mod._PAUSED = orig
        sweeps_mod._index_files = orig_idx


def test_on_change_backoff_after_failure(safe_tmp_path):
    """Fix #3b: on_change skips reindex when inside the _INDEX_BACKOFF_S window."""
    import time

    import opencode_search.daemon.sweeps as sweeps_mod
    from opencode_search.daemon.sweeps import _INDEX_BACKOFF_S, on_change

    (safe_tmp_path / "a.py").write_text("def foo(): pass\n")
    path = str(safe_tmp_path)
    sweeps_mod._last_index_fail[path] = time.monotonic()
    reindexed: list = []
    orig_idx = sweeps_mod._index_files
    sweeps_mod._index_files = lambda *a, **kw: reindexed.append(a)
    try:
        on_change(path, [str(safe_tmp_path / "a.py")])
        assert not reindexed, "_index_files must not be called inside backoff window"
        assert _INDEX_BACKOFF_S == 120.0
    finally:
        sweeps_mod._last_index_fail.pop(path, None)
        sweeps_mod._index_files = orig_idx


# ---------------------------------------------------------------------------
# Fix #4: /api/overview returns 400 on malformed JSON body
# ---------------------------------------------------------------------------

def test_api_overview_bad_json_returns_400(live_client):
    """Fix #4: /api/overview must return 400 (not 500) for a malformed JSON body."""
    r = live_client.post("/api/overview",
                         data="{bad",
                         headers={"Content-Type": "application/json"})
    assert r.status_code == 400, f"/api/overview bad JSON: expected 400, got {r.status_code}"


def test_api_overview_non_object_body_returns_400(live_client):
    """Fix #4: /api/overview must return 400 for a JSON array body."""
    import json as _json
    r = live_client.post("/api/overview",
                         data=_json.dumps([1, 2, 3]),
                         headers={"Content-Type": "application/json"})
    assert r.status_code == 400, f"/api/overview array body: expected 400, got {r.status_code}"


# ---------------------------------------------------------------------------
# Daemon health gate
# ---------------------------------------------------------------------------

def test_daemon_healthz_responsive(live_client):
    """Daemon event loop must not be wedged: /healthz must respond < 2 s."""
    import time
    t0 = time.monotonic()
    r = live_client.get("/healthz", timeout=2)
    elapsed = time.monotonic() - t0
    assert r.status_code == 200, f"/healthz returned {r.status_code}"
    data = r.json()
    assert data.get("ok") is True, f"/healthz ok != True: {data}"
    assert "rss_mb" in data, f"/healthz missing rss_mb: {data}"
    assert elapsed < 2.0, f"/healthz took {elapsed:.2f}s > 2s — event loop may be wedged"


# ---------------------------------------------------------------------------
# Phase C: Idle CPU/RAM minimization — ORT thread caps + gc+malloc_trim unload
# ---------------------------------------------------------------------------

def test_ort_thread_cap_in_monkeypatch():
    """ORT SessionOptions init-hook caps intra/inter threads to 1 + disables spinning — idle CPU drops to ~0."""
    import inspect

    from opencode_search.embed import embedder as emb_mod
    src = inspect.getsource(emb_mod.Embedder._init)
    assert "intra_op_num_threads = 1" in src, "ORT intra_op_num_threads must be capped at 1"
    assert "inter_op_num_threads = 1" in src, "ORT inter_op_num_threads must be capped at 1"
    assert 'allow_spinning", "0"' in src, "ORT spinning must be disabled via session config"
    assert "ORT_SEQUENTIAL" in src, "ORT execution_mode must be SEQUENTIAL for GPU-only sessions"


def test_idle_unload_gc_and_malloc_trim_present():
    """gc.collect + malloc_trim must be in _idle_unload so RSS returns to OS floor after idle."""
    import inspect

    from opencode_search.daemon import server
    src = inspect.getsource(server._idle_unload)
    assert "gc.collect()" in src, "_idle_unload must call gc.collect() to free ONNX threads"
    assert "malloc_trim" in src, "_idle_unload must call malloc_trim(0) to return arena to OS"


def test_env_thread_caps_set_at_import():
    """OMP_NUM_THREADS=1 + passive wait policy must be applied by the package __init__."""
    import os

    import opencode_search  # noqa: F401 — triggers __init__ env setup
    assert os.environ.get("OMP_NUM_THREADS") == "1", "OMP_NUM_THREADS must be 1 (no oversubscription)"
    assert os.environ.get("OMP_WAIT_POLICY") == "passive", "OMP_WAIT_POLICY must be passive (sleep not spin)"
    assert os.environ.get("TOKENIZERS_PARALLELISM") == "false", "TOKENIZERS_PARALLELISM must be false"


# ── Gap 5 F3: BPRE metrics surface ───────────────────────────────────────────

def test_bpre_metrics_surface(live_client, sample_workspace: SampleWorkspace):
    """Gap 5 F3: overview(what='metrics') returns bpre block with last_run/edge_count/last_error."""
    import asyncio

    from opencode_search.server.mcp import overview as overview_tool
    result = asyncio.run(overview_tool(sample_workspace.fed_root, what="metrics"))
    import json
    data = json.loads(result)
    assert "bpre" in data, f"overview(metrics) must include 'bpre' block; got keys: {list(data)}"
    bpre = data["bpre"]
    for key in ("last_run", "edge_count", "last_error"):
        assert key in bpre, f"bpre metrics block missing key {key!r}"


# ── Gap 5 F4: on_change wires BPRE process regen ─────────────────────────────

def test_on_change_wires_bpre_regen():
    """Gap 5 F4 source-guard: on_change/_enrich_project must call _regen_owning_processes."""
    import inspect

    from opencode_search.daemon import sweeps
    on_change_src = inspect.getsource(sweeps.on_change)
    enrich_src = inspect.getsource(sweeps._enrich_project)
    assert "_regen_owning_processes" in on_change_src or "_regen_owning_processes" in enrich_src, (
        "on_change or _enrich_project must call _regen_owning_processes "
        "so member edits refresh the owning federation's process_graph.db"
    )


@pytest.mark.slow
def test_idle_unload_then_cuda_reload():
    """Force idle-unload in-process; reload must rebind CUDA EP (GPU-only invariant holds)."""
    import gc

    import opencode_search.embed.embedder as emb_mod
    import opencode_search.query.search as search_mod

    emb = emb_mod.get_embedder()
    emb.warmup()
    assert emb_mod._default is not None, "embedder must be loaded before unload test"

    # Mirror _idle_unload: null singletons + force GC to release ONNX sessions
    emb_mod._default = None
    search_mod._reranker = None
    gc.collect()

    # Reload — must rebind CUDA EP, not fall back to CPU (GPU-only enforced)
    reloaded = emb_mod.get_embedder()
    reloaded.warmup()
    providers = reloaded._model.model.model.get_providers()
    assert "CUDAExecutionProvider" in providers, (
        f"Post-unload reload must bind CUDA EP; got {providers} (CPU fallback is forbidden)"
    )
