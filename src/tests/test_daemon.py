"""Tests for the singleton MCP daemon helpers."""
from __future__ import annotations

import tomllib
from pathlib import Path

from opencode_search.daemon import (
    _bridge_command,
    _global_prompt_text,
    _install_init_wrapper,
    _render_systemd_service,
    _replace_managed_block,
    _strip_marker_block,
    _update_codex_config_text,
    discover_claude_config_dirs,
    parse_alias_map,
    remove_shell_wrapper_block,
)


def test_parse_alias_map_extracts_target_aliases():
    aliases = parse_alias_map(
        '\n'.join(
            [
                'alias claude="claude --dangerously-skip-permissions"',
                "alias codex='codex --yolo'",
            ]
        )
    )

    assert aliases["claude"] == "claude --dangerously-skip-permissions"
    assert aliases["codex"] == "codex --yolo"


def test_discover_claude_config_dirs_finds_alias_profiles(tmp_path):
    alias_text = '\n'.join(
        [
            'alias claude="claude --dangerously-skip-permissions"',
            'alias claude1="CLAUDE_CONFIG_DIR=~/.claude-account1 claude"',
            'alias claude2="CLAUDE_CONFIG_DIR=~/.claude-account2 claude"',
        ]
    )

    dirs = discover_claude_config_dirs(alias_text, home=tmp_path)

    assert dirs == [
        tmp_path / ".claude-account1",
        tmp_path / ".claude-account2",
    ]


def test_remove_shell_wrapper_block_removes_legacy_shell_hook():
    original = "\n".join(
        [
            "alias codex='codex --yolo'",
            "# >>> opencode-search global singleton MCP >>>",
            "old block",
            "# <<< opencode-search global singleton MCP <<<",
            "",
        ]
    )

    updated = remove_shell_wrapper_block(original)

    assert "old block" not in updated
    assert "alias codex='codex --yolo'" in updated


def test_bridge_command_targets_stdio_bridge():
    command = _bridge_command()

    assert command[-4:] == ["-m", "opencode_search", "daemon", "bridge-stdio"]


def test_render_systemd_service_uses_ensure_oneshot():
    service = _render_systemd_service(Path("/tmp/python"), host="127.0.0.1", port=8765)

    assert "Type=oneshot" in service
    assert "RemainAfterExit=yes" in service
    assert "daemon ensure --host 127.0.0.1 --port 8765" in service


def test_global_prompt_text_requires_explicit_index_and_search_first():
    text = _global_prompt_text()

    assert "Never auto-index a project" in text
    assert "Only call index_project when the user explicitly asks" in text
    assert "use search_code" in text


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
