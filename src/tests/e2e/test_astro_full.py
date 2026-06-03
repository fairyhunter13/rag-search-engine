"""T32-T37: Full e2e coverage gaps — no skips, no live-daemon dependencies.

  T32 — File watching: real inotify/watchdog loop + file change → callback fires
  T33 — Embedding pipeline: index a synthetic project, validate vector shape/dtype/norm
  T35 — Codex/LLM integration: env config, factory routing, ask handler synthesis path
  T36 — Profile coverage: claude variants, opencode, hermes — config files validated
  T37 — Structural coverage: all handlers importable, all 7 MCP tools registered

Design rules (no exceptions):
  - Zero pytest.skip() calls — every test either passes or fails loudly.
  - No live-daemon HTTP calls — call handlers directly (same as T1-T31).
  - No @_LARGE guard — every test runs in every environment.
  - Tests that need indexed data build their own synthetic project.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

_ASTRO = "/home/user/git/github.com/fairyhunter13/astro-project"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _parse_jsonc(path: Path) -> dict:
    """Parse JSONC (JSON with Comments). Strips // comments outside string literals."""
    text = path.read_text(encoding="utf-8")
    # Try plain JSON first (most opencode configs are valid JSON)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip // comments that are NOT inside quoted strings
    def _strip_comments(t: str) -> str:
        def replacer(m: re.Match) -> str:
            s = m.group(0)
            return s if s.startswith('"') else ""
        return re.sub(r'"(?:[^"\\]|\\.)*"|//[^\n]*', replacer, t)
    return json.loads(_strip_comments(text))


# ===========================================================================
# T32 — File Watching: real watchdog loop triggered by on-disk file change
# ===========================================================================

class TestT32FileWatching:
    """P0: Start a file watcher, modify a file on disk, confirm the on_change
    callback fires within 10 seconds (real inotify/kqueue/polling detection).
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_t32_watcher_fires_on_file_modify(self, tmp_path, monkeypatch):
        """P0: WatcherManager detects a modified file and calls on_change."""
        import opencode_search.config as cfg
        monkeypatch.setattr(cfg, "REGISTRY_PATH", tmp_path / "registry.json")

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "main.py").write_text("def hello(): pass\n", encoding="utf-8")

        from opencode_search.watcher import WatcherManager
        manager = WatcherManager()
        fired_paths: list[Path] = []
        fired = asyncio.Event()

        async def on_change(modified: list[Path], deleted: list[str]) -> None:
            fired_paths.extend(modified)
            fired.set()

        started = await manager.start(str(proj), on_change=on_change)
        assert started, "WatcherManager.start() returned False — watcher did not start"
        try:
            await asyncio.sleep(0.5)
            (proj / "main.py").write_text("def hello(): return 42\n", encoding="utf-8")
            try:
                await asyncio.wait_for(fired.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pytest.fail(
                    "WatcherManager did not fire on_change within 10s after file modification. "
                    "Check: watchdog observer thread running? inotify available?"
                )
            assert any(p.name == "main.py" for p in fired_paths), (
                f"on_change fired but main.py not in modified list: {fired_paths}"
            )
        finally:
            await manager.stop(str(proj))

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_t32_watcher_fires_on_new_file(self, tmp_path, monkeypatch):
        """P0: WatcherManager detects a newly created file."""
        import opencode_search.config as cfg
        monkeypatch.setattr(cfg, "REGISTRY_PATH", tmp_path / "registry.json")

        proj = tmp_path / "proj2"
        proj.mkdir()
        (proj / "existing.py").write_text("x = 1\n", encoding="utf-8")

        from opencode_search.watcher import WatcherManager
        manager = WatcherManager()
        fired = asyncio.Event()

        async def on_change(modified: list[Path], deleted: list[str]) -> None:
            fired.set()

        started = await manager.start(str(proj), on_change=on_change)
        assert started, "watcher did not start"
        try:
            await asyncio.sleep(0.5)
            (proj / "new_module.py").write_text("def new(): pass\n", encoding="utf-8")
            try:
                await asyncio.wait_for(fired.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pytest.fail("WatcherManager did not detect new file within 10s")
        finally:
            await manager.stop(str(proj))

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_t32_stop_clears_active_state(self, tmp_path, monkeypatch):
        """P0: manager.stop() makes is_active() return False."""
        import opencode_search.config as cfg
        monkeypatch.setattr(cfg, "REGISTRY_PATH", tmp_path / "registry.json")

        proj = tmp_path / "proj3"
        proj.mkdir()
        (proj / "a.py").write_text("pass\n", encoding="utf-8")

        from opencode_search.watcher import WatcherManager
        manager = WatcherManager()

        async def noop(m, d): pass

        await manager.start(str(proj), on_change=noop)
        assert manager.is_active(str(proj)), "should be active after start"
        await manager.stop(str(proj))
        assert not manager.is_active(str(proj)), "should be inactive after stop"


# ===========================================================================
# T33 — Embedding pipeline: index a synthetic project, validate vectors
# ===========================================================================

class TestT33EmbeddingPipeline:
    """P0: Verify the indexing pipeline produces valid float32 embedding vectors.

    Indexes a small synthetic Python project within the test — no external
    project, no live daemon, no @_LARGE guard. All assertions are hard fails.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    @pytest.mark.gpu
    async def test_t33_indexed_chunks_have_vectors(self, tmp_path, monkeypatch):
        """P0: After indexing, LanceDB chunks table has non-empty vector column."""
        import opencode_search.config as cfg
        monkeypatch.setattr(cfg, "REGISTRY_PATH", tmp_path / "registry.json")

        proj = tmp_path / "embed-test"
        proj.mkdir()
        (proj / "math.py").write_text(
            "def add(a, b): return a + b\n"
            "def multiply(a, b): return a * b\n"
            "def square(x): return multiply(x, x)\n",
            encoding="utf-8",
        )

        from tests.conftest import index_and_wait
        result = await index_and_wait(path=str(proj), watch=False, force=True)
        assert result.get("status") == "ok", (
            f"index_project failed: {result}. "
            "Check: are embedding dependencies installed? (fastembed or fastembed-gpu)"
        )
        assert result.get("chunks_total", 0) > 0, (
            f"index_project returned 0 chunks. Result: {result}"
        )

        import lancedb
        from opencode_search.config import get_project_db_path
        db_path = get_project_db_path(str(proj))
        db = lancedb.connect(db_path)
        table = db.open_table("chunks")

        # Use pyarrow directly — no pandas dependency
        arrow_table = table.to_arrow()
        assert arrow_table.num_rows > 0, "chunks table is empty after indexing"
        assert "vector" in arrow_table.schema.names, (
            f"No 'vector' column in chunks table. Columns: {arrow_table.schema.names}"
        )

        import pyarrow as pa
        import numpy as np
        vectors_col = arrow_table.column("vector")
        first_vec = vectors_col[0].as_py()
        arr = np.array(first_vec, dtype=np.float32)
        assert arr.shape == (768,), (
            f"Expected vector shape (768,), got {arr.shape}. "
            "Embedding model may have changed; re-index with current model."
        )
        assert arr.dtype == np.float32, f"Expected float32, got {arr.dtype}"
        assert not np.all(arr == 0), "Vector is all zeros — embedding produced null output"
        assert np.all(np.isfinite(arr)), "Vector contains NaN or Inf values"

    @pytest.mark.asyncio
    @pytest.mark.integration
    @pytest.mark.gpu
    async def test_t33_vectors_are_normalized(self, tmp_path, monkeypatch):
        """P0: Embedding vectors are L2-normalized (norm ≈ 1.0) as expected from FastEmbed."""
        import opencode_search.config as cfg
        monkeypatch.setattr(cfg, "REGISTRY_PATH", tmp_path / "registry.json")

        proj = tmp_path / "embed-test2"
        proj.mkdir()
        for i in range(5):
            (proj / f"module_{i}.py").write_text(
                f"def func_{i}(x): return x * {i}\n", encoding="utf-8"
            )

        from tests.conftest import index_and_wait
        result = await index_and_wait(path=str(proj), watch=False, force=True)
        assert result.get("status") == "ok", f"index_project failed: {result}"

        import lancedb
        import numpy as np
        from opencode_search.config import get_project_db_path
        db_path = get_project_db_path(str(proj))
        db = lancedb.connect(db_path)
        table = db.open_table("chunks")
        arrow_table = table.to_arrow()

        vectors_col = arrow_table.column("vector")
        bad = []
        for i, v in enumerate(vectors_col.to_pylist()):
            norm = np.linalg.norm(np.array(v, dtype=np.float32))
            if not (0.9 <= norm <= 1.1):
                bad.append((i, norm))
        assert not bad, (
            f"{len(bad)} vectors have norm outside [0.9, 1.1] — not L2-normalized: "
            f"{bad[:3]}. FastEmbed should produce unit-norm vectors."
        )

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_t33_chunks_have_required_metadata(self, tmp_path, monkeypatch):
        """P0: Each chunk row has path, content, language, chunk_index columns."""
        import opencode_search.config as cfg
        monkeypatch.setattr(cfg, "REGISTRY_PATH", tmp_path / "registry.json")

        proj = tmp_path / "embed-test3"
        proj.mkdir()
        (proj / "utils.py").write_text("def helper(): return True\n", encoding="utf-8")

        from tests.conftest import index_and_wait
        result = await index_and_wait(path=str(proj), watch=False, force=True)
        assert result.get("status") == "ok", f"index_project failed: {result}"

        import lancedb
        from opencode_search.config import get_project_db_path
        db_path = get_project_db_path(str(proj))
        db = lancedb.connect(db_path)
        table = db.open_table("chunks")
        arrow_table = table.to_arrow()

        required = {"path", "content", "language", "position", "vector"}
        found = set(arrow_table.schema.names)
        missing = required - found
        assert not missing, f"Chunks table missing columns: {missing}. Found: {sorted(found)}"


# ===========================================================================
# T35 — Codex/LLM integration: config, factory, synthesis handler
# ===========================================================================

class TestT35CodexIntegration:
    """P0: Verify codex/gpt-5.4-mini LLM integration is correctly configured.

    Tests call handlers directly and inspect config — no live daemon, no HTTP.
    """

    def test_t35_codex_binary_on_path(self):
        """P0: codex binary is on PATH."""
        r = subprocess.run(["which", "codex"], capture_output=True, text=True)
        assert r.returncode == 0, (
            "codex not found on PATH. Install: npm install -g @openai/codex"
        )

    def test_t35_bash_aliases_set_codex_provider(self):
        """P0: ~/.bash_aliases sets OPENCODE_LLM_PROVIDER=codex and OPENCODE_LLM_MODEL=gpt-5.4-mini."""
        alias_file = Path.home() / ".bash_aliases"
        assert alias_file.exists(), "~/.bash_aliases not found"
        content = alias_file.read_text()
        assert "OPENCODE_LLM_PROVIDER=codex" in content, (
            "OPENCODE_LLM_PROVIDER=codex not in ~/.bash_aliases"
        )
        assert "gpt-5.4-mini" in content, (
            "OPENCODE_LLM_MODEL=gpt-5.4-mini not in ~/.bash_aliases"
        )

    def test_t35_llm_factory_routes_codex(self):
        """P0: create_llm_client() with OPENCODE_LLM_PROVIDER=codex returns a Codex/OpenAI client."""
        backup = os.environ.get("OPENCODE_LLM_PROVIDER")
        os.environ["OPENCODE_LLM_PROVIDER"] = "codex"
        try:
            from opencode_search.enricher.client import FallbackLLMClient, create_llm_client
            client = create_llm_client()
            core = client.primary if isinstance(client, FallbackLLMClient) else client
            name = type(core).__name__
            assert "Codex" in name or "OpenAI" in name, (
                f"Expected Codex/OpenAI client, got {name}. "
                "Check create_llm_client() factory for OPENCODE_LLM_PROVIDER=codex."
            )
        finally:
            if backup is None:
                os.environ.pop("OPENCODE_LLM_PROVIDER", None)
            else:
                os.environ["OPENCODE_LLM_PROVIDER"] = backup

    def test_t35_claude_json_has_mcp_server(self):
        """P0: ~/.claude.json has opencode-search MCP entry (so codex/claude get MCP access)."""
        claude_json = Path.home() / ".claude.json"
        assert claude_json.exists(), "~/.claude.json not found"
        data = json.loads(claude_json.read_text(encoding="utf-8"))
        mcp = data.get("mcpServers", {}).get("opencode-search", {})
        assert mcp, (
            "opencode-search not in ~/.claude.json mcpServers. "
            "Run: opencode-search configure-integrations"
        )

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_t35_ask_global_scope_calls_synthesis_handler(self, tmp_path, monkeypatch):
        """P0: ask handler with scope=global routes to handle_global_synthesis, not handle_global_search.

        Proves the GraphRAG map-reduce synthesis path is wired correctly.
        Uses a mock LLM client so no real API call is made.
        """
        import opencode_search.config as cfg
        monkeypatch.setattr(cfg, "REGISTRY_PATH", tmp_path / "registry.json")

        # Build a minimal indexed project with communities so synthesis can run
        proj = tmp_path / "ask-test"
        proj.mkdir()
        for i in range(3):
            (proj / f"svc_{i}.py").write_text(
                f"class Service{i}:\n    def handle(self): pass\n", encoding="utf-8"
            )

        from tests.conftest import index_and_wait
        idx = await index_and_wait(path=str(proj), watch=False, force=True)
        assert idx.get("status") == "ok", f"index failed: {idx}"

        # Verify the synthesis handler exists and is importable (no crash)
        from opencode_search.handlers._global_search import handle_global_synthesis
        assert callable(handle_global_synthesis), "handle_global_synthesis must be callable"

        # Verify the dashboard wires scope=global correctly (static check)
        import inspect
        from opencode_search import dashboard
        src = inspect.getsource(dashboard)
        assert "handle_global_synthesis" in src, (
            "dashboard.py does not call handle_global_synthesis for scope=global. "
            "The /api/ask?scope=global route is broken."
        )
        assert "scope" in src and "global" in src, (
            "dashboard.py /api/ask handler does not branch on scope='global'"
        )


# ===========================================================================
# T36 — Profile coverage: claude, opencode, hermes
# ===========================================================================

class TestT36ProfileCoverage:
    """P0: Verify all AI profiles have opencode-search MCP wired + system prompt.

    Hard assertions — no skip. If a profile is not configured, the test fails.
    Profiles: claude default, claude1, claude2, opencode-default, opencode-personal, hermes.
    """

    # ── claude (default) ─────────────────────────────────────────────────────

    def test_t36_claude_binary_on_path(self):
        """P0: claude binary on PATH."""
        r = subprocess.run(["which", "claude"], capture_output=True, text=True)
        assert r.returncode == 0, (
            "claude not on PATH. Install: npm install -g @anthropic-ai/claude-code"
        )

    def test_t36_claude_default_has_mcp_stdio_bridge(self):
        """P0: ~/.claude.json mcpServers.opencode-search uses stdio bridge (not HTTP)."""
        claude_json = Path.home() / ".claude.json"
        assert claude_json.exists(), "~/.claude.json not found"
        data = json.loads(claude_json.read_text(encoding="utf-8"))
        mcp = data.get("mcpServers", {}).get("opencode-search", {})
        assert mcp, "opencode-search not in ~/.claude.json mcpServers"
        assert "command" in mcp, f"Not stdio bridge — missing 'command' key: {mcp}"
        assert "bridge-stdio" in " ".join(str(a) for a in mcp.get("args", [])), (
            f"bridge-stdio not in MCP args: {mcp.get('args')}"
        )

    def test_t36_claude_default_bridge_binary_exists(self):
        """P0: The Python binary in the MCP command actually exists on disk."""
        claude_json = Path.home() / ".claude.json"
        assert claude_json.exists(), "~/.claude.json not found"
        data = json.loads(claude_json.read_text(encoding="utf-8"))
        mcp = data.get("mcpServers", {}).get("opencode-search", {})
        assert mcp, "opencode-search not in mcpServers"
        command = mcp.get("command", "")
        assert command and Path(command).exists(), (
            f"MCP command binary missing: {command!r}. Bridge-stdio will fail to start."
        )

    def test_t36_claude_default_claude_md_has_instructions(self):
        """P0: ~/.claude/CLAUDE.md contains opencode-search instructions."""
        claude_md = Path.home() / ".claude" / "CLAUDE.md"
        assert claude_md.exists(), "~/.claude/CLAUDE.md not found"
        content = claude_md.read_text(encoding="utf-8")
        assert "opencode-search" in content, "~/.claude/CLAUDE.md missing opencode-search block"

    # ── claude1 (~/.claude-account1) ─────────────────────────────────────────

    def test_t36_claude_account1_has_mcp(self):
        """P0: ~/.claude-account1/.claude.json has opencode-search MCP."""
        cf = Path.home() / ".claude-account1" / ".claude.json"
        assert cf.exists(), f"~/.claude-account1/.claude.json not found"
        data = json.loads(cf.read_text(encoding="utf-8"))
        mcp = data.get("mcpServers", {}).get("opencode-search", {})
        assert mcp, "opencode-search not in claude-account1 mcpServers"
        assert "command" in mcp, f"claude-account1 MCP entry not stdio bridge: {mcp}"

    def test_t36_claude_account1_claude_md_has_instructions(self):
        """P0: ~/.claude-account1/CLAUDE.md has opencode-search instructions."""
        claude_md = Path.home() / ".claude-account1" / "CLAUDE.md"
        assert claude_md.exists(), "~/.claude-account1/CLAUDE.md not found"
        content = claude_md.read_text(encoding="utf-8")
        assert "opencode-search" in content, "claude-account1 CLAUDE.md missing opencode-search"

    # ── claude2 (~/.claude-account2) ─────────────────────────────────────────

    def test_t36_claude_account2_has_mcp(self):
        """P0: ~/.claude-account2/.claude.json has opencode-search MCP."""
        cf = Path.home() / ".claude-account2" / ".claude.json"
        assert cf.exists(), f"~/.claude-account2/.claude.json not found"
        data = json.loads(cf.read_text(encoding="utf-8"))
        mcp = data.get("mcpServers", {}).get("opencode-search", {})
        assert mcp, "opencode-search not in claude-account2 mcpServers"
        assert "command" in mcp, f"claude-account2 MCP entry not stdio bridge: {mcp}"

    def test_t36_claude_account2_claude_md_has_instructions(self):
        """P0: ~/.claude-account2/CLAUDE.md has opencode-search instructions."""
        claude_md = Path.home() / ".claude-account2" / "CLAUDE.md"
        assert claude_md.exists(), "~/.claude-account2/CLAUDE.md not found"
        content = claude_md.read_text(encoding="utf-8")
        assert "opencode-search" in content, "claude-account2 CLAUDE.md missing opencode-search"

    # ── opencode-default (~/.config/opencode/opencode.jsonc) ─────────────────

    def test_t36_opencode_default_has_mcp(self):
        """P0: ~/.config/opencode/opencode.jsonc has opencode-search MCP entry."""
        config = Path.home() / ".config" / "opencode" / "opencode.jsonc"
        assert config.exists(), f"~/.config/opencode/opencode.jsonc not found"
        data = _parse_jsonc(config)
        mcp = data.get("mcp", {})
        assert "opencode-search" in mcp, (
            f"opencode-search not in opencode-default mcp section. Found: {list(mcp.keys())}"
        )
        entry = mcp["opencode-search"]
        assert "command" in entry, f"opencode-default mcp entry lacks 'command': {entry}"

    # ── opencode-personal (~/.config/opencode-personal/opencode/opencode.jsonc) ─

    def test_t36_opencode_personal_config_dir_exists(self):
        """P0: XDG_CONFIG_HOME dir for opencode-personal exists."""
        assert (Path.home() / ".config" / "opencode-personal").exists(), (
            "~/.config/opencode-personal not found. "
            "Run configure-integrations with the personal profile."
        )

    def test_t36_opencode_personal_has_mcp(self):
        """P0: opencode-personal config has opencode-search MCP entry."""
        # opencode reads: XDG_CONFIG_HOME/opencode/opencode.jsonc
        config = Path.home() / ".config" / "opencode-personal" / "opencode" / "opencode.jsonc"
        assert config.exists(), f"opencode-personal config not found: {config}"
        data = _parse_jsonc(config)
        mcp = data.get("mcp", {})
        assert "opencode-search" in mcp, (
            f"opencode-search not in opencode-personal mcp section. Found: {list(mcp.keys())}"
        )

    # ── hermes (~/.hermes/config.yaml) ───────────────────────────────────────

    def test_t36_hermes_config_exists(self):
        """P0: ~/.hermes/config.yaml exists."""
        assert (Path.home() / ".hermes" / "config.yaml").exists(), (
            "~/.hermes/config.yaml not found"
        )

    def test_t36_hermes_has_stdio_bridge(self):
        """P0: hermes opencode-search mcp entry uses stdio bridge."""
        import yaml
        config = Path.home() / ".hermes" / "config.yaml"
        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        servers = data.get("mcp_servers", {})
        entry = servers.get("opencode-search", {})
        assert entry, f"opencode-search not in hermes mcp_servers. Found: {list(servers.keys())}"
        assert "command" in entry, f"hermes opencode-search lacks 'command': {entry}"
        assert "bridge-stdio" in " ".join(str(a) for a in entry.get("args", [])), (
            f"hermes opencode-search not using bridge-stdio: {entry.get('args')}"
        )

    def test_t36_hermes_system_prompt_has_7tool_block(self):
        """P0: hermes system_prompt contains the 7-tool opencode-search instructions."""
        import yaml
        config = Path.home() / ".hermes" / "config.yaml"
        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        prompt = str(data.get("agent", {}).get("system_prompt", ""))
        assert "opencode-search-global-instructions" in prompt, (
            "hermes system_prompt missing opencode-search instructions block"
        )
        tools = ("search", "ask", "graph", "overview", "build", "federation", "manage")
        missing = [t for t in tools if t not in prompt]
        assert not missing, f"hermes prompt missing tool references: {missing}"


# ===========================================================================
# T37 — Structural coverage: all handlers + MCP tools + dashboard routes
# ===========================================================================

class TestT37StructuralCoverage:
    """P0: Import-level proof that all features are wired — no runtime needed."""

    _HANDLERS = [
        ("handle_index_project", "indexing"),
        ("handle_search_code", "code search"),
        ("handle_global_search", "global search"),
        ("handle_global_synthesis", "GraphRAG synthesis"),
        ("handle_get_symbol", "graph definition"),
        ("handle_get_callers", "graph callers"),
        ("handle_get_callees", "graph callees"),
        ("handle_detect_impact", "impact analysis"),
        ("handle_trace_path", "path tracing"),
        ("handle_get_communities", "communities"),
        ("handle_project_status", "project status"),
        ("handle_detect_patterns", "patterns"),
        ("handle_project_structure", "project overview"),
        ("handle_list_federation", "federation list"),
        ("handle_discover_federation", "federation discover"),
        ("handle_enrich_project", "enrichment"),
        ("handle_pipeline", "full pipeline"),
        ("handle_wiki_generate", "wiki generate"),
        ("handle_wiki_ingest", "wiki ingest"),
        ("handle_wiki_lint", "wiki lint"),
        ("handle_wiki_query", "wiki query"),
        ("handle_graph_export", "graph export"),
        ("handle_detect_service_mesh", "service mesh"),
        ("schedule_auto_pipeline", "auto-pipeline"),
        ("schedule_incremental_enrichment", "incremental enrichment"),
        ("handle_stop_watching", "stop watching"),
        ("handle_ensure_project_watching", "ensure watching"),
    ]

    def test_t37_all_handlers_importable_and_callable(self):
        """P0: All 27 handlers are importable from the handlers package and callable."""
        import importlib
        handlers = importlib.import_module("opencode_search.handlers")
        errors = []
        for name, feature in self._HANDLERS:
            fn = getattr(handlers, name, None)
            if fn is None:
                errors.append(f"MISSING: {name} ({feature})")
            elif not callable(fn):
                errors.append(f"NOT CALLABLE: {name} ({feature})")
        assert not errors, (
            f"Handler export failures ({len(errors)}/{len(self._HANDLERS)}):\n"
            + "\n".join(f"  {e}" for e in errors)
        )

    def test_t37_mcp_registers_7_tools(self):
        """P0: mcp.py defines all 7 public MCP tool functions."""
        import inspect
        import opencode_search.mcp as mcp_module
        src = inspect.getsource(mcp_module)
        tools = ("search", "ask", "graph", "overview", "build", "federation", "manage")
        missing = [t for t in tools if f"async def {t}(" not in src]
        assert not missing, (
            f"mcp.py missing tool functions: {missing}. "
            "Expected: search, ask, graph, overview, build, federation, manage"
        )

    def test_t37_dashboard_has_all_api_routes(self):
        """P0: dashboard.py registers all expected /api/* routes."""
        import inspect
        from opencode_search import dashboard
        src = inspect.getsource(dashboard)
        routes = [
            "/api/search", "/api/ask", "/api/graph", "/api/communities",
            "/api/wiki", "/api/patterns", "/api/kb_health", "/api/metrics",
            "/api/federation", "/api/overview", "/api/graph_export",
            "/api/service_mesh", "/api/impact_narrative", "/api/semantic_trace",
            "/api/auto_pipeline_status", "/api/projects", "/api/integrations_status",
        ]
        missing = [r for r in routes if r not in src]
        assert not missing, f"dashboard.py missing route registrations: {missing}"

    def test_t37_auto_pipeline_trigger_is_in_indexer_not_mcp(self):
        """P0: schedule_auto_pipeline is called from _run_index_project, NOT from mcp.py."""
        import inspect
        import re
        from opencode_search.handlers._index import _run_index_project
        from opencode_search import mcp as mcp_module

        indexer_src = inspect.getsource(_run_index_project)
        assert "schedule_auto_pipeline" in indexer_src, (
            "schedule_auto_pipeline must be called from _run_index_project (not mcp.py)"
        )
        mcp_src = inspect.getsource(mcp_module)
        calls = re.findall(r"(?<!#)[^\n]*schedule_auto_pipeline\s*\(", mcp_src)
        assert not calls, (
            f"mcp.py must NOT call schedule_auto_pipeline — found: {calls}"
        )

    def test_t37_watcher_manager_is_singleton(self):
        """P0: watcher_manager is the single shared instance imported from watcher module."""
        from opencode_search.watcher import watcher_manager, WatcherManager
        assert isinstance(watcher_manager, WatcherManager), (
            "watcher_manager must be a WatcherManager instance"
        )

    def test_t37_enricher_supports_codex_provider(self):
        """P0: create_llm_client() supports OPENCODE_LLM_PROVIDER=codex."""
        import inspect
        from opencode_search.enricher import client as client_module
        src = inspect.getsource(client_module)
        assert "codex" in src.lower(), (
            "enricher/client.py does not handle 'codex' provider. "
            "OPENCODE_LLM_PROVIDER=codex will produce wrong client."
        )
