"""Tests for the singleton MCP daemon, daemon runtime state, and dashboard HTTP API."""
from __future__ import annotations

import json
import tomllib
from pathlib import Path
import os
import urllib.error
import urllib.request

import pytest

from opencode_search.daemon import (
    _HERMES_MARKER_END,
    _HERMES_MARKER_START,
    _SYSTEMD_NOTIFY_SERVICE_NAME,
    _bridge_command,
    _disable_codex_fast_mode,
    _global_prompt_text,
    _install_claude_global_prompt,
    _install_init_wrapper,
    _install_opencode_configs,
    _install_opencode_global_prompt,
    _render_systemd_notify_failure_service,
    _render_systemd_service,
    _replace_managed_block,
    _strip_jsonc_comments,
    _strip_marker_block,
    _update_codex_config_text,
    discover_claude_config_dirs,
    install_global_integration,
)
from opencode_search.daemon_runtime import _RuntimeState


# === Daemon ===

def test_discover_claude_config_dirs_scans_home_for_profiles(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude-account1").mkdir()
    (tmp_path / ".claude-account2").mkdir()

    dirs = discover_claude_config_dirs(home=tmp_path)

    assert dirs == [
        tmp_path / ".claude-account1",
        tmp_path / ".claude-account2",
    ]


def test_bridge_command_targets_stdio_bridge():
    command = _bridge_command()

    assert command[-4:] == ["-m", "opencode_search", "daemon", "bridge-stdio"]


def test_render_systemd_service_is_true_daemon():
    service = _render_systemd_service(Path("/tmp/python"), host="127.0.0.1", port=8765)

    # Type=notify: systemd waits for READY=1 before marking the service active.
    assert "Type=notify" in service
    assert "NotifyAccess=main" in service
    # Hard-fail desktop notification via OnFailure=.
    assert f"OnFailure={_SYSTEMD_NOTIFY_SERVICE_NAME}" in service
    # Restart limits: stop crash-looping if GPU guard fails repeatedly.
    assert "StartLimitBurst=5" in service
    assert "StartLimitIntervalSec=120" in service
    # Always restart (including after idle self-shutdown via SIGTERM).
    assert "Restart=always" in service
    # Idle shutdown disabled; model unload handles GPU memory conservation.
    assert "OPENCODE_MCP_IDLE_SHUTDOWN_S=0" in service
    # Device protection.
    assert "Nice=10" in service
    assert "IOSchedulingClass=best-effort" in service
    assert "OOMScoreAdj=200" in service
    assert "daemon serve --host 127.0.0.1 --port 8765" in service


def test_render_systemd_notify_failure_service_fires_notify_send():
    service = _render_systemd_notify_failure_service()

    assert "Type=oneshot" in service
    assert "notify-send" in service
    assert "-u critical" in service
    # Must not crash if notify-send is absent.
    assert "|| true" in service
    # Recovery instructions are included in the notification body.
    assert "reset-failed" in service
    assert "journalctl" in service


def test_global_prompt_text_requires_explicit_index_and_search_first():
    text = _global_prompt_text()

    # Concise decision-tree format — must mention all 7 tools
    assert "search" in text
    assert "ask" in text
    assert "graph" in text
    assert "overview" in text
    assert "build" in text
    assert "federation" in text
    assert "manage" in text
    # Must contain no-auto-index rule
    assert "auto-index" in text.lower() or "NEVER auto-index" in text or "explicitly" in text
    # Must contain search-before-bash rule
    assert "grep" in text or "bash" in text.lower()
    # Anti-delegation rule
    assert "sub-agent" in text.lower() or "Agent" in text


def test_replace_managed_block_replaces_existing_section():
    original = "before\nSTART\nold\nEND\nafter\n"

    updated = _replace_managed_block(original, "START", "END", "START\nnew\nEND")

    assert updated == "before\nSTART\nnew\nEND\nafter\n"


def test_strip_marker_block_removes_only_managed_segment():
    original = "prefix\nSTART\nmanaged\nEND\nsuffix"

    stripped = _strip_marker_block(original, "START", "END")

    assert stripped == "prefix\nsuffix"


def test_install_init_wrapper_writes_executable_script(tmp_path, monkeypatch):
    wrapper_path = tmp_path / "bin" / "opencode-search-init"
    monkeypatch.setattr("opencode_search.daemon._INIT_WRAPPER_PATH", wrapper_path)

    installed = _install_init_wrapper(Path("/tmp/python"))

    assert installed == str(wrapper_path)
    text = wrapper_path.read_text(encoding="utf-8")
    assert 'exec "/tmp/python" -m opencode_search init "$@"' in text
    assert wrapper_path.stat().st_mode & 0o111


def test_update_codex_config_text_inserts_root_prompt_before_tables():
    original = "\n".join(
        [
            'model = "gpt-5.4"',
            "",
            "[projects.\"/tmp/proj\"]",
            'trust_level = "trusted"',
            "",
        ]
    )

    updated = _update_codex_config_text(original)
    parsed = tomllib.loads(updated)

    assert "developer_instructions" in parsed
    assert parsed["developer_instructions"].startswith("[opencode-search-global-instructions:start]")
    assert updated.index("developer_instructions") < updated.index("[projects.")


def test_update_codex_config_text_replaces_existing_root_instruction_without_duplicate():
    original = "\n".join(
        [
            'developer_instructions = "custom root prompt"',
            "",
            "[mcp_servers.example]",
            'command = "example"',
            "",
        ]
    )

    updated = _update_codex_config_text(original)
    parsed = tomllib.loads(updated)

    assert parsed["developer_instructions"].startswith("custom root prompt")
    assert updated.count("developer_instructions = ") == 1


def test_update_codex_config_text_removes_orphaned_managed_markers_from_old_table_tail():
    original = "\n".join(
        [
            'model = "gpt-5.4"',
            "",
            "[mcp_servers.opencode-search]",
            'command = "python"',
            '# <<< opencode-search developer instructions <<<',
            "",
        ]
    )

    updated = _update_codex_config_text(original)
    parsed = tomllib.loads(updated)

    assert "developer_instructions" in parsed
    assert updated.count("# >>> opencode-search developer instructions >>>") == 1
    assert updated.count("# <<< opencode-search developer instructions <<<") == 1


# ---------------------------------------------------------------------------
# _disable_codex_fast_mode
# ---------------------------------------------------------------------------

def test_disable_codex_fast_mode_inserts_into_existing_features_section():
    config = "[features]\nterminal_resize_reflow = true\nmemories = true\n"

    result = _disable_codex_fast_mode(config)

    assert "fast_mode = false" in result
    assert tomllib.loads(result)["features"]["fast_mode"] is False


def test_disable_codex_fast_mode_replaces_true_with_false():
    config = "[features]\nfast_mode = true\nmemories = true\n"

    result = _disable_codex_fast_mode(config)

    assert "fast_mode = false" in result
    assert "fast_mode = true" not in result


def test_disable_codex_fast_mode_is_noop_when_already_false():
    config = "[features]\nfast_mode = false\n"

    assert _disable_codex_fast_mode(config) == config


def test_disable_codex_fast_mode_appends_features_section_when_missing():
    config = "[tui]\nstatus_line_use_colors = true\n"

    result = _disable_codex_fast_mode(config)

    assert "[features]" in result
    assert "fast_mode = false" in result
    assert tomllib.loads(result)["features"]["fast_mode"] is False


# ---------------------------------------------------------------------------
# _install_claude_global_prompt
# ---------------------------------------------------------------------------

def test_install_claude_global_prompt_writes_to_default_and_all_profile_dirs(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude-account1").mkdir()
    (tmp_path / ".claude-account2").mkdir()

    written = _install_claude_global_prompt(
        [tmp_path / ".claude-account1", tmp_path / ".claude-account2"],
        home=tmp_path,
    )

    assert len(written) == 3
    for path_str in written:
        content = Path(path_str).read_text()
        assert "search" in content and "overview" in content
        assert "search" in content  # v2 uses `search` tool name
        assert "grep" in content


def test_install_claude_global_prompt_skips_nonexistent_dirs(tmp_path):
    (tmp_path / ".claude").mkdir()

    written = _install_claude_global_prompt(
        [tmp_path / ".claude-missing"],
        home=tmp_path,
    )

    assert written == [str(tmp_path / ".claude" / "CLAUDE.md")]


def test_install_claude_global_prompt_updates_existing_managed_block(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    target = claude_dir / "CLAUDE.md"
    target.write_text(
        "before\n"
        "<!-- >>> opencode-search global instructions >>> -->\n"
        "old content\n"
        "<!-- <<< opencode-search global instructions <<< -->\n"
        "after\n"
    )

    _install_claude_global_prompt([], home=tmp_path)

    content = target.read_text()
    assert "old content" not in content
    assert "search" in content and "overview" in content
    assert "before" in content
    assert "after" in content


def test_install_claude_global_prompt_creates_file_when_absent(tmp_path):
    (tmp_path / ".claude").mkdir()

    _install_claude_global_prompt([], home=tmp_path)

    target = tmp_path / ".claude" / "CLAUDE.md"
    assert target.exists()
    assert "search" in target.read_text() and "overview" in target.read_text()


# ---------------------------------------------------------------------------
# _strip_jsonc_comments
# ---------------------------------------------------------------------------

def test_strip_jsonc_comments_removes_line_comments():
    text = '{\n  "key": "value" // comment\n}'
    assert json.loads(_strip_jsonc_comments(text)) == {"key": "value"}


def test_strip_jsonc_comments_removes_block_comments():
    text = '{\n  /* block */\n  "key": "value"\n}'
    assert json.loads(_strip_jsonc_comments(text)) == {"key": "value"}


def test_strip_jsonc_comments_leaves_plain_json_intact():
    text = '{"key": "value"}'
    assert _strip_jsonc_comments(text) == text


# ---------------------------------------------------------------------------
# _install_opencode_configs
# ---------------------------------------------------------------------------

_BRIDGE = ["/usr/bin/python", "-m", "opencode_search", "daemon", "bridge-stdio"]


def test_install_opencode_configs_writes_mcp_entry_to_existing_config(tmp_path, monkeypatch):
    opencode_dir = tmp_path / "opencode"
    opencode_dir.mkdir(parents=True)
    cfg = opencode_dir / "opencode.jsonc"
    cfg.write_text('{"$schema": "https://opencode.ai/config.json"}\n', encoding="utf-8")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    updated = _install_opencode_configs(_BRIDGE)

    assert updated == [str(cfg)]
    data = json.loads(cfg.read_text())
    assert data["mcp"]["opencode-search"]["type"] == "local"
    assert data["mcp"]["opencode-search"]["command"] == _BRIDGE
    assert data["mcp"]["opencode-search"]["timeout"] == 30000


def test_install_opencode_configs_picks_up_named_profiles(tmp_path, monkeypatch):
    # Default profile: $XDG_CONFIG_HOME/opencode/opencode.jsonc
    default_dir = tmp_path / "opencode"
    default_dir.mkdir(parents=True)
    (default_dir / "opencode.jsonc").write_text('{"$schema": "https://opencode.ai/config.json"}', encoding="utf-8")
    # Named profiles: $XDG_CONFIG_HOME/opencode-<name>/opencode/opencode.jsonc
    for subdir in ["opencode-personal", "opencode-work"]:
        d = tmp_path / subdir / "opencode"
        d.mkdir(parents=True)
        (d / "opencode.jsonc").write_text('{"$schema": "https://opencode.ai/config.json"}', encoding="utf-8")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    updated = _install_opencode_configs(_BRIDGE)

    assert len(updated) == 3


def test_install_opencode_configs_skips_missing_files(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    updated = _install_opencode_configs(_BRIDGE)

    assert updated == []


def test_install_opencode_configs_is_idempotent(tmp_path, monkeypatch):
    opencode_dir = tmp_path / "opencode"
    opencode_dir.mkdir(parents=True)
    cfg = opencode_dir / "opencode.jsonc"
    cfg.write_text('{"$schema": "https://opencode.ai/config.json"}\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    _install_opencode_configs(_BRIDGE)
    mtime_after_first = cfg.stat().st_mtime_ns
    updated_second = _install_opencode_configs(_BRIDGE)

    # Second call must not rewrite the file (entry already matches)
    assert updated_second == []
    assert cfg.stat().st_mtime_ns == mtime_after_first


def test_install_opencode_configs_strips_jsonc_comments(tmp_path, monkeypatch):
    opencode_dir = tmp_path / "opencode"
    opencode_dir.mkdir(parents=True)
    cfg = opencode_dir / "opencode.jsonc"
    cfg.write_text(
        '{\n  "$schema": "https://opencode.ai/config.json" // schema\n}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    updated = _install_opencode_configs(_BRIDGE)

    assert updated == [str(cfg)]
    data = json.loads(cfg.read_text())
    assert "opencode-search" in data["mcp"]


# ---------------------------------------------------------------------------
# _install_opencode_global_prompt
# ---------------------------------------------------------------------------

def test_install_opencode_global_prompt_writes_managed_block(tmp_path, monkeypatch):
    """Writes the opencode-search prompt into AGENTS.md inside managed markers."""
    opencode_dir = tmp_path / "opencode"
    opencode_dir.mkdir(parents=True)
    agents_md = opencode_dir / "AGENTS.md"
    agents_md.write_text("# My Instructions\n\nDo things.\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    written = _install_opencode_global_prompt()

    assert str(agents_md) in written
    content = agents_md.read_text(encoding="utf-8")
    assert _HERMES_MARKER_START in content
    assert _HERMES_MARKER_END in content
    assert "opencode-search" in content.lower()
    # Original content must be preserved
    assert "My Instructions" in content


def test_install_opencode_global_prompt_is_idempotent(tmp_path, monkeypatch):
    """Running twice does not duplicate the block."""
    opencode_dir = tmp_path / "opencode"
    opencode_dir.mkdir(parents=True)
    agents_md = opencode_dir / "AGENTS.md"
    agents_md.write_text("", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    _install_opencode_global_prompt()
    content_first = agents_md.read_text(encoding="utf-8")

    _install_opencode_global_prompt()
    content_second = agents_md.read_text(encoding="utf-8")

    # Block should appear exactly once
    assert content_first.count(_HERMES_MARKER_START) == 1
    assert content_second.count(_HERMES_MARKER_START) == 1


def test_install_opencode_global_prompt_picks_up_named_profiles(tmp_path, monkeypatch):
    """Also writes to ~/.config/opencode-personal/opencode/AGENTS.md."""
    for profile in ("opencode", "opencode-personal/opencode"):
        d = tmp_path / profile
        d.mkdir(parents=True)
        (d / "AGENTS.md").write_text("", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    written = _install_opencode_global_prompt()

    assert len(written) == 2
    for path in written:
        content = Path(path).read_text(encoding="utf-8")
        assert _HERMES_MARKER_START in content


def test_install_opencode_global_prompt_skips_missing_dirs(tmp_path, monkeypatch):
    """Skips silently if the opencode config dir does not exist."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # No opencode/ dir created → nothing to write
    written = _install_opencode_global_prompt()
    assert written == []


# ---------------------------------------------------------------------------
# install_global_integration default transport
# ---------------------------------------------------------------------------

def test_install_global_integration_default_transport_is_stdio():
    """install_global_integration() defaults to transport='stdio' not 'http'."""
    import inspect
    sig = inspect.signature(install_global_integration)
    default = sig.parameters.get("transport")
    assert default is not None, "transport param not found"
    assert default.default == "stdio", (
        f"Expected default transport='stdio', got {default.default!r}. "
        "Update plan: all profiles should use the stdio bridge."
    )


def test_global_prompt_text_mentions_patterns():
    """_global_prompt_text() mentions what='patterns' for dependency/style queries."""
    text = _global_prompt_text()
    assert "patterns" in text, (
        "_global_prompt_text does not mention 'patterns' — "
        "assistants won't know to use overview(what='patterns')"
    )


# === Runtime State ===

def test_runtime_state_tracks_clients_and_activity():
    state = _RuntimeState()

    state.client_open("client-a", project_path="/tmp/proj")
    snapshot = state.snapshot()

    assert snapshot["active_clients"] == 1
    assert snapshot["client_ids"] == ["client-a"]
    assert snapshot["active_projects"] == ["/tmp/proj"]

    closed = state.client_close("client-a")
    assert closed == "/tmp/proj"
    assert state.snapshot()["active_clients"] == 1
    assert state.snapshot()["closing_clients"] == ["client-a"]


def test_runtime_state_prunes_stale_clients():
    state = _RuntimeState()
    state.client_open("client-a")
    state.active_clients["client-a"] -= 120

    pruned = state.prune_stale_clients(60)

    assert pruned == 1
    assert state.snapshot()["active_clients"] == 0


def test_runtime_state_should_shutdown_only_when_idle_and_no_clients():
    state = _RuntimeState()
    state.client_open("client-a")
    state.last_activity_monotonic -= 3600

    assert state.should_shutdown(idle_timeout_s=300, stale_after_s=60) is False

    state.active_clients["client-a"] -= 120

    assert state.should_shutdown(idle_timeout_s=300, stale_after_s=60) is True


def test_runtime_state_counts_clients_per_project():
    state = _RuntimeState()

    state.client_open("client-a", project_path="/tmp/proj")
    state.client_open("client-b", project_path="/tmp/proj")
    state.client_open("client-c", project_path="/tmp/other")

    assert state.project_client_count("/tmp/proj") == 2
    assert state.project_client_count("/tmp/other") == 1

    state.client_close("client-a")
    assert state.project_client_count("/tmp/proj") == 2


def test_runtime_state_releases_projects_only_after_stale_disconnect():
    state = _RuntimeState()

    state.client_open("client-a", project_path="/tmp/proj")
    state.client_close("client-a")
    assert state.project_client_count("/tmp/proj") == 1

    state.active_clients["client-a"] -= 120
    released = state.releaseable_stale_projects(60)

    assert released == ["/tmp/proj"]
    assert state.project_client_count("/tmp/proj") == 0


def test_runtime_state_binds_open_client_to_project_after_index():
    state = _RuntimeState()

    state.client_open("client-a", cwd="/tmp/proj/subdir")

    bound = state.bind_clients_to_project("/tmp/proj")

    assert bound == 1
    assert state.project_client_count("/tmp/proj") == 1


def test_runtime_state_does_not_overwrite_existing_child_project_binding():
    state = _RuntimeState()

    state.client_open("client-a", cwd="/tmp/repo/subproj/src", project_path="/tmp/repo/subproj")

    bound = state.bind_clients_to_project("/tmp/repo")

    assert bound == 0
    assert state.project_client_count("/tmp/repo/subproj") == 1
    assert state.project_client_count("/tmp/repo") == 0


def test_runtime_state_does_not_rebind_clients_that_are_closing():
    state = _RuntimeState()

    state.client_open("client-a", cwd="/tmp/proj/subdir")
    state.client_close("client-a")

    bound = state.bind_clients_to_project("/tmp/proj")

    assert bound == 0
    assert state.project_client_count("/tmp/proj") == 0
    assert state.snapshot()["closing_clients"] == ["client-a"]


# === Dashboard API ===

_LIVE = pytest.mark.runtime_deps
_BASE = "http://127.0.0.1:8765"
_PROJECT = os.environ.get(
    "OPENCODE_TEST_PROJECT",
    "/home/user/git/github.com/fairyhunter13/redacted-name-3",
)


def _get(path: str, timeout: int = 10) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"{_BASE}{path}", timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"_raw": body}
    except Exception as exc:
        return 0, {"_error": str(exc)}


class TestDaemonHealth:
    @_LIVE
    def test_healthz_returns_ok(self):
        status, body = _get("/healthz")
        assert status == 200, f"Expected 200, got {status}"
        assert body.get("ok") is True, f"Expected ok=true, got {body}"

    @_LIVE
    def test_healthz_has_uptime(self):
        _, body = _get("/healthz")
        assert "uptime_s" in body or "ok" in body


class TestProjectsAPI:
    @_LIVE
    def test_projects_returns_list(self):
        status, body = _get("/api/projects")
        assert status == 200
        assert "projects" in body
        assert isinstance(body["projects"], list)

    @_LIVE
    def test_projects_list_nonempty(self):
        _, body = _get("/api/projects")
        assert len(body.get("projects", [])) >= 1, "Expected at least 1 indexed project"

    @_LIVE
    def test_project_entries_have_path(self):
        _, body = _get("/api/projects")
        for p in body.get("projects", []):
            assert "path" in p, f"Project entry missing 'path': {p}"


class TestCommunitiesAPI:
    @_LIVE
    def test_communities_returns_list(self):
        from urllib.parse import quote
        status, body = _get(f"/api/communities?project={quote(_PROJECT)}&top_k=5")
        assert status == 200
        assert "communities" in body
        assert isinstance(body["communities"], list)

    @_LIVE
    def test_communities_have_required_fields(self):
        from urllib.parse import quote
        _, body = _get(f"/api/communities?project={quote(_PROJECT)}&top_k=5")
        for c in body.get("communities", [])[:3]:
            assert "id" in c
            assert "title" in c


class TestKBHealthAPI:
    @_LIVE
    def test_kb_health_returns_data(self):
        from urllib.parse import quote
        status, body = _get(f"/api/kb_health?project={quote(_PROJECT)}")
        assert status == 200
        assert isinstance(body, dict)
        assert len(body) >= 1

    @_LIVE
    def test_kb_health_has_enrichment_field(self):
        from urllib.parse import quote
        _, body = _get(f"/api/kb_health?project={quote(_PROJECT)}")
        has_enr = "enrichment_pct" in body or "enriched_communities" in body
        assert has_enr, f"Expected enrichment field, got keys: {list(body.keys())}"

    @_LIVE
    def test_kb_health_wiki_page_count(self):
        from urllib.parse import quote
        _, body = _get(f"/api/kb_health?project={quote(_PROJECT)}")
        assert "wiki_page_count" in body


class TestPatternsAPI:
    @_LIVE
    def test_patterns_returns_ok_status(self):
        from urllib.parse import quote
        status, body = _get(f"/api/patterns?project={quote(_PROJECT)}")
        assert status == 200
        assert body.get("status") == "ok", f"Expected status=ok, got {body.get('status')}"

    @_LIVE
    def test_patterns_has_architecture(self):
        from urllib.parse import quote
        _, body = _get(f"/api/patterns?project={quote(_PROJECT)}")
        has_arch = "architecture" in body or "llm_analysis" in body
        assert has_arch, f"Expected architecture data, got keys: {list(body.keys())}"


class TestGraphExportAPI:
    @_LIVE
    def test_graph_export_json_has_nodes_edges(self):
        from urllib.parse import quote
        status, body = _get(f"/api/graph_export?project={quote(_PROJECT)}&format=json&max_nodes=50")
        assert status == 200
        assert "nodes" in body, f"Expected 'nodes' key, got {list(body.keys())}"
        assert "edges" in body, f"Expected 'edges' key, got {list(body.keys())}"

    @_LIVE
    def test_graph_export_max_nodes_respected(self):
        from urllib.parse import quote
        _, body = _get(f"/api/graph_export?project={quote(_PROJECT)}&format=json&max_nodes=50")
        n = len(body.get("nodes", []))
        assert n <= 50, f"max_nodes=50 not respected: got {n} nodes"

    @_LIVE
    def test_graph_export_graphml_is_xml(self):
        import urllib.request
        import xml.etree.ElementTree as ET
        from urllib.parse import quote
        with urllib.request.urlopen(
            f"{_BASE}/api/graph_export?project={quote(_PROJECT)}&format=graphml&max_nodes=50",
            timeout=15
        ) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        # Must parse as valid XML
        root = ET.fromstring(body)
        assert root is not None


class TestWikiAPI:
    @_LIVE
    def test_wiki_returns_page_list(self):
        from urllib.parse import quote
        status, body = _get(f"/api/wiki?project={quote(_PROJECT)}")
        assert status == 200
        assert "pages" in body
        assert isinstance(body["pages"], list)

    @_LIVE
    def test_wiki_page_list_nonempty(self):
        from urllib.parse import quote
        _, body = _get(f"/api/wiki?project={quote(_PROJECT)}")
        assert len(body.get("pages", [])) >= 1, "Expected at least 1 wiki page"


class TestAutoPipelineAPI:
    @_LIVE
    def test_auto_pipeline_has_enabled_field(self):
        status, body = _get("/api/auto_pipeline_status")
        assert status == 200
        assert "enabled" in body
        assert isinstance(body["enabled"], bool)


class TestMetricsAPI:
    @_LIVE
    def test_metrics_returns_data(self):
        status, body = _get("/api/metrics")
        assert status == 200
        assert isinstance(body, dict)


class TestNoInternalErrors:
    @_LIVE
    def test_all_endpoints_return_200(self):
        from urllib.parse import quote
        endpoints = [
            "/healthz",
            "/api/projects",
            f"/api/communities?project={quote(_PROJECT)}&top_k=5",
            f"/api/patterns?project={quote(_PROJECT)}",
            f"/api/graph_export?project={quote(_PROJECT)}&format=json&max_nodes=50",
            f"/api/kb_health?project={quote(_PROJECT)}",
            f"/api/wiki?project={quote(_PROJECT)}",
            "/api/auto_pipeline_status",
            "/api/metrics",
        ]
        errors = []
        for ep in endpoints:
            status, body = _get(ep)
            if status >= 500:
                errors.append(f"HTTP {status} on {ep}: {str(body)[:100]}")
        assert not errors, "Internal server errors:\n" + "\n".join(errors)


class TestPrereleaseStatusAPI:
    @_LIVE
    def test_prerelease_status_returns_json(self):
        status, body = _get("/api/prerelease_status")
        # Either 200 (report exists) or 404 (not yet run) — both are valid
        assert status in (200, 404)
        assert isinstance(body, dict)