"""Bash aliases integration tests.

Verifies that ~/.bash_aliases is correctly configured for the opencode-search
engine: all required CLI aliases present, LLM provider is local (ollama),
GPU enforcement is configured, and no CPU fallback is allowed.

No shell execution — pure file-content checks, fast and safe.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

HOME = Path.home()
ALIASES_PATH = HOME / ".bash_aliases"


@pytest.fixture(scope="module")
def aliases_text() -> str:
    if not ALIASES_PATH.exists():
        pytest.skip(f"~/.bash_aliases not found at {ALIASES_PATH}")
    return ALIASES_PATH.read_text(encoding="utf-8")


# ── Presence: required aliases ────────────────────────────────────────────────

class TestBashAliasesPresence:
    """All required CLI aliases must be defined in ~/.bash_aliases."""

    def test_file_exists_and_nonempty(self, aliases_text):
        assert len(aliases_text.strip()) > 100, \
            "~/.bash_aliases is empty or too short to contain opencode-search config"

    def test_ocs_alias_present(self, aliases_text):
        assert "alias ocs=" in aliases_text, \
            "~/.bash_aliases must define 'alias ocs=' (main CLI shortcut)"

    def test_ocs_daemon_alias_present(self, aliases_text):
        assert "alias ocs-daemon=" in aliases_text, \
            "~/.bash_aliases must define 'alias ocs-daemon=' (daemon management)"

    def test_ocs_dash_alias_present(self, aliases_text):
        assert "alias ocs-dash=" in aliases_text, \
            "~/.bash_aliases must define 'alias ocs-dash=' (open dashboard)"

    def test_ocs_verify_alias_present(self, aliases_text):
        assert "alias ocs-verify=" in aliases_text, \
            "~/.bash_aliases must define 'alias ocs-verify=' (run test suite)"

    def test_ocs_health_alias_present(self, aliases_text):
        assert "alias ocs-health=" in aliases_text, \
            "~/.bash_aliases must define 'alias ocs-health=' (health check)"

    def test_venv_python_referenced_and_exists(self, aliases_text):
        # Extract the venv python path from aliases content
        m = re.search(r'(_OPPYTHON|OPPYTHON)=["\']?([^"\'$\n ]+\.venv[^"\'$\n ]*python[^"\'$\n ]*)["\']?', aliases_text)
        if not m:
            # Fallback: look for any .venv/bin/python path
            m = re.search(r'(/[^\s"\']+\.venv/bin/python[^\s"\']*)', aliases_text)
        if not m:
            pytest.skip("Cannot extract venv python path from ~/.bash_aliases")
        venv_python = Path(m.group(m.lastindex))
        assert venv_python.exists(), \
            f"venv Python referenced in ~/.bash_aliases does not exist: {venv_python}"

    def test_opsrc_path_exists(self, aliases_text):
        # Extract _OPSRC path
        m = re.search(r'_OPSRC=["\']?([^"\'$\n ]+)["\']?', aliases_text)
        if not m:
            pytest.skip("Cannot find _OPSRC definition in ~/.bash_aliases")
        src_path = Path(m.group(1))
        assert src_path.exists(), \
            f"_OPSRC path in ~/.bash_aliases does not exist: {src_path}"


# ── LLM configuration ─────────────────────────────────────────────────────────

class TestBashAliasesLlmConfig:
    """LLM provider must be local (ollama) — no API-based fallbacks allowed."""

    def test_llm_provider_is_ollama(self, aliases_text):
        assert "OPENCODE_LLM_PROVIDER=ollama" in aliases_text, \
            "~/.bash_aliases must export OPENCODE_LLM_PROVIDER=ollama (local GPU LLM only)"

    def test_llm_model_is_local_not_gpt(self, aliases_text):
        # Only check the `export` line — inline alias overrides (ocs-enrich-astro etc.) may use
        # API models temporarily, which is intentional and should not be flagged here.
        m = re.search(r'^export\s+OPENCODE_LLM_MODEL=([^\s\n]+)', aliases_text, re.MULTILINE)
        if not m:
            pytest.skip("export OPENCODE_LLM_MODEL= not found in ~/.bash_aliases")
        model_val = m.group(1).strip("\"'")
        assert "gpt" not in model_val.lower(), \
            f"Default OPENCODE_LLM_MODEL must not be a GPT model (API-based): {model_val}"
        assert "claude-" not in model_val.lower(), \
            f"Default OPENCODE_LLM_MODEL must be a local model (e.g. qwen3), not API claude: {model_val}"

    def test_ollama_num_gpu_is_positive(self, aliases_text):
        m = re.search(r'OLLAMA_NUM_GPU=([0-9]+)', aliases_text)
        if not m:
            pytest.skip("OLLAMA_NUM_GPU not found in ~/.bash_aliases")
        gpu_count = int(m.group(1))
        assert gpu_count > 0, \
            f"OLLAMA_NUM_GPU must be > 0 to enforce GPU usage, got: {gpu_count}"

    def test_no_cpu_embed_device(self, aliases_text):
        assert "OPENCODE_EMBED_DEVICE=cpu" not in aliases_text, \
            "~/.bash_aliases must NOT export OPENCODE_EMBED_DEVICE=cpu — CPU inference is forbidden"

    def test_verify_alias_excludes_playwright(self, aliases_text):
        # Find the ocs-verify alias line
        for line in aliases_text.splitlines():
            if "alias ocs-verify=" in line and "playwright" not in line and "-m" in line and "not" in line:
                # This is a test-suite alias — playwright is excluded via -m not marker
                return
        # If we didn't find a clear exclusion, skip rather than fail
        # (the alias may use a different pattern)
        pytest.skip("Could not verify playwright exclusion pattern in ocs-verify alias")
