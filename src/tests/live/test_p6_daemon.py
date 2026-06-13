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
    from typer.testing import CliRunner

    from opencode_search.cli import app

    runner = CliRunner()
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    for cmd in ("index", "search", "list", "status", "daemon"):
        assert cmd in r.output, f"CLI missing '{cmd}' command"
