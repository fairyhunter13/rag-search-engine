"""MCP configuration verification tests.

Proves that all AI tools (claude, codex, opencode, hermes) have valid
opencode-search MCP entries pointing to the correct bridge-stdio command.

No live daemon required — these tests only inspect config files.
"""
from __future__ import annotations

import json
import subprocess
import typing
from pathlib import Path

import pytest

HOME = Path.home()
PROJECT_ROOT = Path(__file__).resolve().parents[3]
VENV_PYTHON = str(PROJECT_ROOT / ".venv" / "bin" / "python")
EXPECTED_COMMAND_ARGS = ["-m", "opencode_search", "daemon", "bridge-stdio"]
EXPECTED_SERVER_NAME = "opencode-search"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict:
    """Load JSON or JSONC (strips // comments that are NOT inside strings)."""
    text = path.read_text(encoding="utf-8")
    import re
    # Only strip // comments that are NOT inside quoted strings.
    # Strategy: tokenise, remove comment tokens.
    # Simple approach: remove // ... on lines where // is NOT inside a string value.
    cleaned_lines = []
    for line in text.splitlines():
        # If the line contains "//" and appears to not be inside a string,
        # strip the comment portion (but preserve URLs like "https://...")
        stripped = re.sub(r'(?<![:"/])//(?!/).*$', '', line)
        cleaned_lines.append(stripped)
    try:
        return json.loads("\n".join(cleaned_lines))
    except json.JSONDecodeError:
        # Fallback: return raw without comment stripping
        return json.loads(text)


def _venv_python_exists() -> bool:
    return Path(VENV_PYTHON).exists()


# ── Claude Code (~/.claude/settings.json) ────────────────────────────────────

class TestClaudeCodeMcpConfig:
    """Claude Code MCP config at ~/.claude/settings.json."""

    @pytest.fixture(scope="class")
    def settings(self) -> dict:
        p = HOME / ".claude" / "settings.json"
        if not p.exists():
            pytest.skip(f"Claude settings not found: {p}")
        return json.loads(p.read_text(encoding="utf-8"))

    def test_mcp_servers_key_present(self, settings):
        assert "mcpServers" in settings, "settings.json must have mcpServers key"

    def test_opencode_search_registered(self, settings):
        servers = settings.get("mcpServers", {})
        assert EXPECTED_SERVER_NAME in servers, \
            f"opencode-search not in mcpServers, found: {list(servers.keys())}"

    def test_command_is_python(self, settings):
        cfg = settings["mcpServers"][EXPECTED_SERVER_NAME]
        cmd = cfg.get("command", "")
        assert "python" in cmd or "python3" in cmd, f"Expected python command, got: {cmd}"

    def test_args_include_bridge_stdio(self, settings):
        cfg = settings["mcpServers"][EXPECTED_SERVER_NAME]
        args = cfg.get("args", [])
        assert "bridge-stdio" in args, f"bridge-stdio not in args: {args}"

    def test_args_include_module_flag(self, settings):
        cfg = settings["mcpServers"][EXPECTED_SERVER_NAME]
        args = cfg.get("args", [])
        assert "-m" in args and "opencode_search" in args, \
            f"Expected -m opencode_search in args: {args}"

    def test_venv_python_exists(self, settings):
        cfg = settings["mcpServers"][EXPECTED_SERVER_NAME]
        cmd = cfg.get("command", "")
        if cmd.startswith("/"):
            assert Path(cmd).exists(), f"Python at {cmd} does not exist"


# ── Codex (~/.codex/AGENTS.md) ───────────────────────────────────────────────

class TestCodexMcpConfig:
    """Codex global AGENTS.md at ~/.codex/AGENTS.md."""

    @pytest.fixture(scope="class")
    def agents_md(self) -> str:
        p = HOME / ".codex" / "AGENTS.md"
        if not p.exists():
            pytest.skip(f"Codex AGENTS.md not found: {p}")
        return p.read_text(encoding="utf-8")

    def test_has_opencode_search_mention(self, agents_md):
        assert "opencode-search" in agents_md.lower() or "opencode_search" in agents_md.lower(), \
            "AGENTS.md must mention opencode-search"

    def test_has_mcp_instructions(self, agents_md):
        assert "mcp" in agents_md.lower(), "AGENTS.md must contain MCP instructions"

    def test_has_bridge_stdio_or_daemon(self, agents_md):
        assert "bridge-stdio" in agents_md or "daemon" in agents_md.lower(), \
            "AGENTS.md must reference bridge-stdio or daemon command"

    def test_has_global_system_prompt_markers(self, agents_md):
        assert "opencode-search-global-instructions" in agents_md, \
            "AGENTS.md must include global instructions block markers"

    def test_codex_config_has_mcp(self):
        cfg_path = HOME / ".codex" / "config.toml"
        if not cfg_path.exists():
            pytest.skip("~/.codex/config.toml not found")
        content = cfg_path.read_text(encoding="utf-8")
        assert "mcp" in content.lower() or "opencode" in content.lower(), \
            "codex config.toml should reference MCP or opencode-search"


# ── Opencode (~/.config/opencode/opencode.jsonc) ─────────────────────────────

class TestOpencodeMcpConfig:
    """Opencode MCP config at ~/.config/opencode/opencode.jsonc."""

    @pytest.fixture(scope="class")
    def config(self) -> dict:
        candidates = [
            HOME / ".config" / "opencode" / "opencode.jsonc",
            HOME / ".config" / "opencode" / "config.json",
            HOME / ".opencode" / "config.json",
        ]
        for p in candidates:
            if p.exists():
                return _load_json(p)
        pytest.skip("Opencode config not found")

    def test_mcp_key_present(self, config):
        assert "mcp" in config, f"opencode config must have 'mcp' key, got: {list(config.keys())}"

    def test_opencode_search_registered(self, config):
        mcp = config.get("mcp", {})
        assert EXPECTED_SERVER_NAME in mcp, \
            f"opencode-search not registered in opencode MCP config, found: {list(mcp.keys())}"

    def _full_cmd_list(self, mcp_cfg: dict) -> list[str]:
        """Normalise both list-command and command+args formats to a flat list."""
        cmd = mcp_cfg.get("command", "")
        if isinstance(cmd, list):
            return cmd  # opencode list format: ["python", "-m", ...]
        args = mcp_cfg.get("args", [])
        return [cmd, *list(args)]

    def test_command_is_python(self, config):
        mcp_cfg = config["mcp"][EXPECTED_SERVER_NAME]
        full = self._full_cmd_list(mcp_cfg)
        assert any("python" in c for c in full), f"Expected python in command, got: {full}"

    def test_args_include_bridge_stdio(self, config):
        mcp_cfg = config["mcp"][EXPECTED_SERVER_NAME]
        full = self._full_cmd_list(mcp_cfg)
        assert "bridge-stdio" in full, f"bridge-stdio not in command: {full}"

    def test_type_is_local(self, config):
        mcp_cfg = config["mcp"][EXPECTED_SERVER_NAME]
        t = mcp_cfg.get("type", "")
        assert t in ("local", "stdio", ""), f"Expected local type, got: {t}"

    def test_venv_python_exists_in_command(self, config):
        mcp_cfg = config["mcp"][EXPECTED_SERVER_NAME]
        full = self._full_cmd_list(mcp_cfg)
        python_cmd = next((c for c in full if "python" in c), "")
        if python_cmd.startswith("/"):
            assert Path(python_cmd).exists(), f"Python at {python_cmd} does not exist on disk"


# ── Hermes (~/.hermes/config.yaml) ───────────────────────────────────────────

class TestHermesMcpConfig:
    """Hermes MCP config at ~/.hermes/config.yaml."""

    @pytest.fixture(scope="class")
    def config(self) -> dict:
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML not installed — cannot parse hermes config")
        p = HOME / ".hermes" / "config.yaml"
        if not p.exists():
            pytest.skip(f"Hermes config not found: {p}")
        import yaml
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    def test_mcp_servers_key_present(self, config):
        assert "mcp_servers" in config, \
            f"hermes config must have mcp_servers key, got: {list(config.keys())}"

    def test_opencode_search_registered(self, config):
        servers = config.get("mcp_servers", {})
        assert EXPECTED_SERVER_NAME in servers, \
            f"opencode-search not in hermes mcp_servers, found: {list(servers.keys())}"

    def test_server_enabled(self, config):
        cfg = config["mcp_servers"][EXPECTED_SERVER_NAME]
        assert cfg.get("enabled", True) is True, "opencode-search server must be enabled"

    def test_command_is_python(self, config):
        cfg = config["mcp_servers"][EXPECTED_SERVER_NAME]
        cmd = cfg.get("command", "")
        assert "python" in cmd or "python3" in cmd, f"Expected python, got: {cmd}"

    def test_args_include_bridge_stdio(self, config):
        cfg = config["mcp_servers"][EXPECTED_SERVER_NAME]
        args = cfg.get("args", [])
        assert "bridge-stdio" in args, f"bridge-stdio not in args: {args}"

    def test_has_global_system_prompt(self, config):
        agent = config.get("agent", {})
        sp = agent.get("system_prompt", "")
        assert "opencode-search" in sp.lower() or "opencode_search" in sp.lower(), \
            "Hermes system_prompt must mention opencode-search"

    def test_system_prompt_has_global_instructions(self, config):
        agent = config.get("agent", {})
        sp = agent.get("system_prompt", "")
        assert "opencode-search-global-instructions" in sp, \
            "Hermes system_prompt must include global instructions block"

    def test_venv_python_exists(self, config):
        cfg = config["mcp_servers"][EXPECTED_SERVER_NAME]
        cmd = cfg.get("command", "")
        if cmd.startswith("/"):
            assert Path(cmd).exists(), f"Python at {cmd} does not exist on disk"


# ── MCP bridge binary sanity ──────────────────────────────────────────────────

class TestMcpBridgeBinary:
    """Verify the bridge-stdio command can be invoked at all."""

    def test_venv_python_exists(self):
        assert Path(VENV_PYTHON).exists(), \
            f"venv Python not found at {VENV_PYTHON}"

    def test_opencode_search_module_importable(self):
        result = subprocess.run(
            [VENV_PYTHON, "-c", "import opencode_search; print('ok')"],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, f"Import failed: {result.stderr}"
        assert "ok" in result.stdout

    def test_daemon_module_invokable(self):
        result = subprocess.run(
            [VENV_PYTHON, "-m", "opencode_search", "--help"],
            capture_output=True, text=True, timeout=15,
        )
        # Should exit 0 or show help — not crash with ImportError
        assert result.returncode in (0, 1, 2), \
            f"Unexpected exit code {result.returncode}: {result.stderr}"
        assert "Error" not in result.stderr or "Usage" in result.stderr or "usage" in result.stderr

    def test_bridge_stdio_command_list_format(self):
        """The bridge-stdio command must be a valid list format (not a shell string)."""
        settings_path = HOME / ".claude" / "settings.json"
        if not settings_path.exists():
            pytest.skip("Claude settings not found")
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        cfg = settings.get("mcpServers", {}).get(EXPECTED_SERVER_NAME, {})
        # The command in settings.json is stored as "command" + "args"
        # or as a single list — verify it can be reassembled
        cmd = cfg.get("command", "")
        args = cfg.get("args", [])
        assert cmd, "command must be non-empty"
        assert isinstance(args, list), "args must be a list"


# ── Global system prompt ──────────────────────────────────────────────────────

class TestGlobalSystemPrompt:
    """Verify global instructions are present in all relevant locations."""

    REQUIRED_PHRASES: typing.ClassVar[list[str]] = [
        "opencode-search",
        "search(",
        "ask(",
        "graph(",
        "overview(",
    ]

    def _check_text(self, text: str, label: str):
        for phrase in self.REQUIRED_PHRASES:
            assert phrase in text, \
                f"{label} missing required phrase: '{phrase}'"

    def test_claude_claude_md_has_instructions(self):
        p = HOME / ".claude" / "CLAUDE.md"
        if not p.exists():
            pytest.skip("~/.claude/CLAUDE.md not found")
        self._check_text(p.read_text(encoding="utf-8"), "~/.claude/CLAUDE.md")

    def test_project_claude_md_has_instructions(self):
        p = PROJECT_ROOT / "CLAUDE.md"
        if not p.exists():
            pytest.skip("project CLAUDE.md not found")
        self._check_text(p.read_text(encoding="utf-8"), "project CLAUDE.md")

    def test_codex_agents_md_has_instructions(self):
        p = HOME / ".codex" / "AGENTS.md"
        if not p.exists():
            pytest.skip("~/.codex/AGENTS.md not found")
        text = p.read_text(encoding="utf-8")
        assert "opencode-search" in text.lower() or "opencode_search" in text.lower()

    def test_hermes_system_prompt_has_instructions(self):
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML not installed")
        p = HOME / ".hermes" / "config.yaml"
        if not p.exists():
            pytest.skip("~/.hermes/config.yaml not found")
        config = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        sp = config.get("agent", {}).get("system_prompt", "")
        if not sp:
            pytest.skip("No system_prompt in hermes config")
        assert "opencode-search" in sp.lower()

    def test_all_7_tool_names_in_claude_md(self):
        p = HOME / ".claude" / "CLAUDE.md"
        if not p.exists():
            pytest.skip("~/.claude/CLAUDE.md not found")
        text = p.read_text(encoding="utf-8")
        for tool in ("search", "ask", "graph", "overview", "build", "federation", "manage"):
            assert tool in text, \
                f"~/.claude/CLAUDE.md must mention the '{tool}' MCP tool"

    def test_quick_decision_guide_present(self):
        p = HOME / ".claude" / "CLAUDE.md"
        if not p.exists():
            pytest.skip("~/.claude/CLAUDE.md not found")
        text = p.read_text(encoding="utf-8")
        assert "QUICK DECISION GUIDE" in text or "DECISION GUIDE" in text, \
            "~/.claude/CLAUDE.md must contain a QUICK DECISION GUIDE section"

    def test_bash_aliases_llm_is_local(self):
        p = HOME / ".bash_aliases"
        if not p.exists():
            pytest.skip("~/.bash_aliases not found")
        text = p.read_text(encoding="utf-8")
        assert "OPENCODE_LLM_PROVIDER=ollama" in text, \
            "~/.bash_aliases must export OPENCODE_LLM_PROVIDER=ollama (not an API-based provider)"

    def test_bash_aliases_no_cpu_fallback(self):
        p = HOME / ".bash_aliases"
        if not p.exists():
            pytest.skip("~/.bash_aliases not found")
        text = p.read_text(encoding="utf-8")
        assert "OPENCODE_EMBED_DEVICE=cpu" not in text, \
            "~/.bash_aliases must NOT export OPENCODE_EMBED_DEVICE=cpu — CPU inference is forbidden"


# ── Claude Code MCP env vars ──────────────────────────────────────────────────

class TestClaudeCodeMcpEnv:
    """Claude Code MCP server env must use local LLM provider."""

    @pytest.fixture(scope="class")
    def settings(self) -> dict:
        import json as _json
        p = HOME / ".claude" / "settings.json"
        if not p.exists():
            pytest.skip(f"Claude settings not found: {p}")
        return _json.loads(p.read_text(encoding="utf-8"))

    def test_mcp_env_uses_local_llm(self, settings):
        cfg = settings.get("mcpServers", {}).get(EXPECTED_SERVER_NAME, {})
        env = cfg.get("env", {})
        if not env:
            pytest.skip("No env block in Claude Code MCP config")
        provider = env.get("OPENCODE_LLM_PROVIDER", "")
        assert provider == "ollama", \
            f"Claude Code MCP env must set OPENCODE_LLM_PROVIDER=ollama, got: {provider!r}"

    def test_mcp_env_query_llm_is_local(self, settings):
        cfg = settings.get("mcpServers", {}).get(EXPECTED_SERVER_NAME, {})
        env = cfg.get("env", {})
        if not env:
            pytest.skip("No env block in Claude Code MCP config")
        query_provider = env.get("OPENCODE_QUERY_LLM_PROVIDER", "")
        if query_provider:
            assert query_provider == "ollama", \
                f"OPENCODE_QUERY_LLM_PROVIDER must be ollama, got: {query_provider!r}"
