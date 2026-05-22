"""Tests for the singleton MCP daemon helpers."""
from __future__ import annotations

from pathlib import Path

from opencode_search.daemon import (
    _bridge_command,
    _render_systemd_service,
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
