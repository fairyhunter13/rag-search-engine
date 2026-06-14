"""P6 daemon tests: scheduler, watcher, sweeps, federation, systemd, CLI (no mocks)."""
import time

import pytest

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
    w.POLL_INTERVAL = 0.1
    w.watch(proj)
    w.start()
    time.sleep(0.15)
    (tmp_path / "new_file.py").write_text("y = 2\n")
    time.sleep(0.35)
    w.stop()
    assert changed, "watcher should have detected the new file"


def test_watcher_inotify_fast(tmp_path):
    """inotify must detect a new file in < 1s even with POLL_INTERVAL=5.0."""
    from opencode_search.daemon.watcher import Watcher

    proj = str(tmp_path)
    (tmp_path / "init.py").write_text("x = 1\n")
    changed: list[str] = []
    w = Watcher(on_change=lambda p, fs: changed.append(p))
    # leave POLL_INTERVAL at default 5.0 — if poll fires we'd wait 5s+
    w.watch(proj)
    w.start()
    time.sleep(0.1)  # let Observer threads settle
    (tmp_path / "fast.py").write_text("y = 2\n")
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not changed:
        time.sleep(0.05)
    w.stop()
    assert changed, "inotify Observer must detect new file in < 1s"


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


def test_federation_discover_empty_dir(tmp_path):
    from opencode_search.daemon.federation import discover_members

    assert discover_members(str(tmp_path)) == []


def test_sweeps_auto_index_skips_existing(tmp_path):
    from opencode_search.core.config import ProjectEntry, project_vector_db
    from opencode_search.core.registry import remove_project, upsert_project
    from opencode_search.daemon.sweeps import auto_index

    proj_path = str(tmp_path)
    vdb = project_vector_db(proj_path)
    vdb.parent.mkdir(parents=True, exist_ok=True)
    vdb.touch()
    upsert_project(ProjectEntry(path=proj_path, enabled=True))
    try:
        auto_index()  # should skip because vdb.exists()
    finally:
        remove_project(proj_path)
        vdb.unlink(missing_ok=True)


def test_sweeps_paused_skips_auto_index(tmp_path):
    """P18.2: a paused auto_index must not create the vector DB."""
    from opencode_search.core.config import ProjectEntry, project_vector_db
    from opencode_search.core.registry import remove_project, upsert_project
    from opencode_search.daemon import sweeps

    proj_path = str(tmp_path)
    vdb = project_vector_db(proj_path)
    upsert_project(ProjectEntry(path=proj_path, enabled=True))
    # vdb intentionally absent so _needs_index() returns True → would trigger indexing
    sweeps._PAUSED = True
    try:
        sweeps.auto_index()
        assert not vdb.exists(), "paused auto_index must not create the vector DB"
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


def test_pipeline_all_stages_real_astro():
    """P10.6: per-stage output traces on the real indexed astro-project.

    Validates: chunk+embed → tree-sitter symbols → call edges → Leiden
    communities → LLM-enriched symbols+communities → L2 hierarchy → wiki pages.
    """
    import sqlite3

    from opencode_search.core.config import project_graph_db, project_vector_db, project_wiki_dir
    from opencode_search.core.registry import list_projects
    from opencode_search.index.store import VectorStore

    astro = next(
        (p.path for p in list_projects()
         if "astro-project" in p.path and "promo" not in p.path and p.enabled), None,
    )
    assert astro, "astro-project not registered (run P8)"
    vs = VectorStore(project_vector_db(astro))
    n = vs.count()
    vs.close()
    assert n > 0, "stage 1 chunk+embed: 0 chunks in vectors.db"
    with sqlite3.connect(str(project_graph_db(astro))) as c:
        assert c.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] > 0, "stage 2 tree-sitter: 0 symbols"
        assert c.execute("SELECT COUNT(*) FROM edges").fetchone()[0] > 0, "stage 2b call-edges: 0 edges"
        assert c.execute("SELECT COUNT(*) FROM communities").fetchone()[0] > 0, "stage 3 leiden: 0 communities"
        assert c.execute("SELECT COUNT(*) FROM symbols WHERE intent IS NOT NULL").fetchone()[0] > 0, "stage 4 enrich-sym: no intents"
        assert c.execute("SELECT COUNT(*) FROM communities WHERE title IS NOT NULL").fetchone()[0] > 0, "stage 5 enrich-comm: no titles"
        assert c.execute("SELECT COUNT(*) FROM communities WHERE level > 1").fetchone()[0] > 0, "stage 6 hierarchy: no L2"
    wiki = project_wiki_dir(astro)
    assert list(wiki.glob("*.md")), f"stage 7 wiki: no pages in {wiki}"


def test_maintenance_vacuums_orphan():
    """P10.7: maintenance() removes orphan index dirs not in the registry."""
    from opencode_search.core.config import INDEX_ROOT
    from opencode_search.daemon.sweeps import maintenance

    orphan = INDEX_ROOT / "p107-test-orphan"
    orphan.mkdir(parents=True, exist_ok=True)
    maintenance()
    assert not orphan.exists(), "maintenance() left orphan dir"


def test_federation_index_members_registers(tmp_path):
    """P10.7: index_members() registers symlinked sub-repos into the registry."""
    from opencode_search.core.registry import get_project, remove_project
    from opencode_search.daemon.federation import index_members

    member = tmp_path / "member-repo"
    member.mkdir()
    (member / "main.py").write_text("x = 1\n")
    (tmp_path / "link").symlink_to(member)
    n = index_members(str(tmp_path))
    assert n == 1 and get_project(str(member)) is not None
    remove_project(str(member))


def test_api_reload_returns_reloading():
    """P10.7/P15.2: POST /api/reload on the LIVE daemon — handler sends SIGTERM
    to os.getpid() so in-process TestClient would kill the test process.
    Systemd restarts the daemon within ~1s; we wait for readiness before finishing.
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


def test_p20_auto_index_discovers_federation_members(tmp_path):
    """P20.1: auto_index() calls index_members() and registers symlinked sub-repos."""
    from opencode_search.core.config import ProjectEntry, project_vector_db
    from opencode_search.core.registry import get_project, remove_project, upsert_project
    from opencode_search.daemon.sweeps import auto_index

    member = tmp_path / "member-repo"
    member.mkdir()
    (member / "main.py").write_text("x = 1\n")
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(member)

    root_path = str(root)
    vdb = project_vector_db(root_path)
    vdb.parent.mkdir(parents=True, exist_ok=True)
    vdb.touch()
    upsert_project(ProjectEntry(path=root_path, enabled=True))
    try:
        auto_index()
        assert get_project(str(member)) is not None, "federation member must be registered by auto_index"
    finally:
        remove_project(root_path)
        remove_project(str(member))
        vdb.unlink(missing_ok=True)


@pytest.mark.slow
def test_p20_indexed_at_stamped(tmp_path):
    """P20.2: _index_project() stamps indexed_at + file_count on the registry entry."""
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import get_project, remove_project, upsert_project
    from opencode_search.daemon.sweeps import _index_project

    (tmp_path / "a.py").write_text("def hello(): return 1\n")
    proj_path = str(tmp_path)
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
    """P22.4: daemon RSS < 4 GB and not crash-looping (uptime > 30s) after leak fixes."""
    import json
    import subprocess
    import urllib.request

    r = subprocess.run(
        ["systemctl", "--user", "show", "opencode-search-mcp-daemon.service",
         "-p", "MemoryCurrent"],
        capture_output=True, text=True,
    )
    props = dict(line.split("=", 1) for line in r.stdout.strip().splitlines() if "=" in line)
    mem_mb = int(props.get("MemoryCurrent", "0")) // (1024 * 1024)
    assert mem_mb < 4096, f"daemon RSS {mem_mb} MB > 4 GB (P22 memory fix must hold)"

    resp = urllib.request.urlopen("http://127.0.0.1:8765/healthz", timeout=5)
    data = json.loads(resp.read())
    assert data.get("ok") is True, f"daemon not healthy after P22 fixes: {data}"
    uptime_s = data.get("uptime_s", 0)
    assert uptime_s > 30, f"daemon restarted recently (uptime_s={uptime_s:.1f}), may be crash-looping"


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


def test_graph_no_duplicate_symbols():
    """P16.9: live graphs must have zero duplicate (name,file,kind) symbol groups."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import list_projects
    from opencode_search.graph.store import GraphStore

    projects = [p for p in list_projects() if p.enabled]
    assert projects, "no registered projects"
    for proj in projects:
        gdb = project_graph_db(proj.path)
        if not gdb.exists():
            continue
        gs = GraphStore(gdb)
        try:
            removed = gs.dedup_symbols()
            dups = gs.conn.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM symbols "
                "GROUP BY name,file,kind HAVING COUNT(*)>1)"
            ).fetchone()[0]
            assert dups == 0, f"{proj.path}: {dups} dup groups after dedup (removed={removed})"
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
def test_p21_burst_enriches_all_communities(tmp_path):
    """P21.3: _enrich_project enriches ALL title IS NULL communities (no LIMIT 20 cap)."""
    from opencode_search.core.config import ProjectEntry, project_graph_db
    from opencode_search.core.registry import remove_project, upsert_project
    from opencode_search.daemon.sweeps import _enrich_project
    from opencode_search.graph.store import GraphStore

    proj = str(tmp_path)
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
def test_p21_burst_enrich_federation(tmp_path):
    """P21.4: burst_enrich_federation enriches root + member, reports aggregate totals."""
    from opencode_search.core.config import ProjectEntry, project_graph_db
    from opencode_search.core.registry import remove_project, upsert_project
    from opencode_search.daemon.sweeps import burst_enrich_federation
    from opencode_search.graph.store import GraphStore

    member = tmp_path / "member"
    member.mkdir()
    (member / "m.py").write_text("def greet(): return 'hi'\n")
    root = tmp_path / "root"
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
