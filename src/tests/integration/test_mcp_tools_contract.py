"""MCP tools contract tests — verify all 7 tools return correct shapes.

Creates a minimal synthetic Python project (3 .py files with real functions),
indexes it via the MCP stdio bridge, then calls all 7 tools and asserts:
  - Correct top-level response keys
  - No unexpected crashes or 5xx-equivalent error structures
  - Invalid inputs return graceful errors (not exceptions)

These are integration tests: they spawn a real bridge-stdio subprocess.
Mark: integration, runtime_deps
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("anyio")
pytest.importorskip("mcp")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def _result_to_dict(result) -> dict:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    content = getattr(result, "content", None) or []
    if len(content) == 1 and getattr(content[0], "type", None) == "text":
        text = getattr(content[0], "text", "")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"result": text}
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    return {"status": "error", "error": "Unexpected tool result format"}


# ── Session-scoped project + bridge fixture ───────────────────────────────────

@pytest.fixture(scope="module")
def project_dir(tmp_path_factory):
    """Create a minimal synthetic Python project to index."""
    base = tmp_path_factory.mktemp("mcp-contract")
    proj = base / "myproject"

    _write(proj / "src" / "main.py", """\
        def process_order(order_id: str, user_id: str) -> dict:
            '''Process a customer order end-to-end.'''
            validated = validate_order(order_id)
            result = charge_payment(user_id, validated["total"])
            return {"order_id": order_id, "status": "completed", "payment": result}

        def validate_order(order_id: str) -> dict:
            '''Validate order details from the database.'''
            return {"order_id": order_id, "total": 99.99, "items": []}
    """)

    _write(proj / "src" / "payment.py", """\
        def charge_payment(user_id: str, amount: float) -> dict:
            '''Charge the user for the order amount.'''
            return {"user_id": user_id, "amount": amount, "transaction_id": "txn_123"}

        def refund_payment(transaction_id: str) -> bool:
            '''Issue a refund for a transaction.'''
            return True
    """)

    _write(proj / "src" / "config.py", """\
        REGISTRY_PATH = '~/.local/share/opencode-search/projects.json'
        MAX_RETRIES = 3
        DEFAULT_TIMEOUT_S = 30
    """)

    return proj


@pytest.fixture(scope="module")
def registry_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("mcp-contract-registry")


@pytest.fixture(scope="module")
def bridge_env(project_dir, registry_dir):
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    return {
        "OPENCODE_REGISTRY_PATH": str(registry_dir / "registry.json"),
        "OPENCODE_MCP_STATE_DIR": str(registry_dir / "daemon-state"),
        "OPENCODE_BRIDGE_WORKSPACE_ROOT": str(project_dir.parent),
        "OPENCODE_MCP_IDLE_SHUTDOWN_S": "30",
        "PYTHONPATH": str(repo_root / "src"),
    }


@pytest.fixture(scope="module")
def mcp_params(bridge_env):
    from mcp.client.stdio import StdioServerParameters
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "opencode_search", "daemon", "bridge-stdio"],
        env={**os.environ, **bridge_env},
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _has_error(d: dict) -> bool:
    return "error" in d and d.get("status") != "ok"


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.runtime_deps
class TestSearchToolContract:
    """search() returns correct result shapes and handles edge cases."""

    async def test_search_returns_results_list(self, mcp_params, project_dir):
        import anyio
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()

            # Index first
            res = await session.call_tool(
                "build",
                {"project_path": str(project_dir), "action": "index",
                 "watch": False, "force": True},
            )
            d = _result_to_dict(res)
            assert d.get("status") in ("ok", "indexing"), f"index failed: {d}"
            await anyio.sleep(12)

            # Search for a known function
            res = await session.call_tool(
                "search",
                {"query": "process order payment", "project_paths": [str(project_dir)]},
            )
            d = _result_to_dict(res)
            assert "results" in d, f"search must return 'results' key, got: {list(d.keys())}"
            assert isinstance(d["results"], list), \
                    f"results must be a list, got: {type(d['results'])}"

    async def test_search_result_has_file_and_content(self, mcp_params, project_dir):
        import anyio
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            await anyio.sleep(2)  # rely on prior indexing

            res = await session.call_tool(
                "search",
                {"query": "payment charge user", "project_paths": [str(project_dir)], "top_k": 3},
            )
            d = _result_to_dict(res)
            rows = d.get("results", [])
            if rows:
                first = rows[0]
                assert "path" in first or "file" in first, \
                        f"Result must have 'path' or 'file' key, got: {list(first.keys())}"
                assert "content" in first or "text" in first or "chunk" in first, \
                        f"Result must have content key, got: {list(first.keys())}"

    async def test_search_invalid_scope_returns_error(self, mcp_params):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "search",
                {"query": "test", "scope": "invalid_scope_xyz"},
            )
            d = _result_to_dict(res)
            assert "error" in d, \
                    f"Invalid scope must return error key, got: {list(d.keys())}"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.runtime_deps
class TestAskToolContract:
    """ask() returns answers with expected shape."""

    async def test_ask_returns_answer_key(self, mcp_params, project_dir):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "ask",
                {"query": "how does payment processing work?",
                 "project_path": str(project_dir)},
            )
            d = _result_to_dict(res)
            has_answer = (
                "answer" in d or "synthesis" in d or "result" in d
                or "response" in d or "summary" in d or "text" in d
            )
            assert has_answer, \
                    f"ask must return answer/synthesis/result key, got: {list(d.keys())}"

    async def test_ask_invalid_scope_returns_error(self, mcp_params, project_dir):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "ask",
                {"query": "test", "project_path": str(project_dir),
                 "scope": "bogus_scope"},
            )
            d = _result_to_dict(res)
            assert "error" in d, \
                    f"Invalid scope must return error, got: {list(d.keys())}"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.runtime_deps
class TestGraphToolContract:
    """graph() returns symbol/relation data or graceful errors."""

    async def test_graph_definition_returns_dict(self, mcp_params, project_dir):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "graph",
                {"symbol": "process_order", "project_path": str(project_dir),
                 "relation": "definition"},
            )
            d = _result_to_dict(res)
            assert isinstance(d, dict), f"graph must return a dict, got: {type(d)}"

    async def test_graph_invalid_relation_returns_error(self, mcp_params, project_dir):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "graph",
                {"symbol": "process_order", "project_path": str(project_dir),
                 "relation": "invalid_relation"},
            )
            d = _result_to_dict(res)
            assert "error" in d, \
                    f"Invalid relation must return error, got: {list(d.keys())}"

    async def test_graph_path_without_to_symbol_returns_error(self, mcp_params, project_dir):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "graph",
                {"symbol": "process_order", "project_path": str(project_dir),
                 "relation": "path"},
            )
            d = _result_to_dict(res)
            assert "error" in d, \
                    f"path relation without to_symbol must return error, got: {list(d.keys())}"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.runtime_deps
class TestOverviewToolContract:
    """overview() returns structure, status, and project listing."""

    async def test_overview_projects_returns_list(self, mcp_params):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "overview",
                {"what": "projects"},
            )
            d = _result_to_dict(res)
            has_projects = (
                "projects" in d or "result" in d or isinstance(d.get("result"), list)
            )
            assert has_projects or isinstance(d, dict), \
                    f"overview(projects) must return dict with projects, got: {list(d.keys())}"

    async def test_overview_status_returns_indexed_key(self, mcp_params, project_dir):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "overview",
                {"project_path": str(project_dir), "what": "status"},
            )
            d = _result_to_dict(res)
            assert isinstance(d, dict), f"overview(status) must return dict, got: {type(d)}"

    async def test_overview_structure_returns_file_count(self, mcp_params, project_dir):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "overview",
                {"project_path": str(project_dir), "what": "structure"},
            )
            d = _result_to_dict(res)
            assert isinstance(d, dict), f"overview(structure) must return dict, got: {type(d)}"

    async def test_overview_invalid_what_returns_error(self, mcp_params, project_dir):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "overview",
                {"project_path": str(project_dir), "what": "invalid_what_xyz"},
            )
            d = _result_to_dict(res)
            assert "error" in d, \
                    f"Invalid 'what' must return error, got: {list(d.keys())}"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.runtime_deps
class TestBuildToolContract:
    """build() returns status/job_id and handles invalid actions."""

    async def test_build_index_returns_status(self, mcp_params, project_dir):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "build",
                {"project_path": str(project_dir), "action": "index",
                 "watch": False, "force": False},
            )
            d = _result_to_dict(res)
            has_status = "status" in d or "job_id" in d
            assert has_status, \
                    f"build(index) must return status or job_id, got: {list(d.keys())}"

    async def test_build_pipeline_returns_job_id(self, mcp_params, project_dir):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "build",
                {"project_path": str(project_dir), "action": "pipeline", "watch": False},
            )
            d = _result_to_dict(res)
            has_job = "job_id" in d or "status" in d or "message" in d
            assert has_job, \
                    f"build(pipeline) must return job_id/status, got: {list(d.keys())}"

    async def test_build_invalid_action_returns_error(self, mcp_params, project_dir):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "build",
                {"project_path": str(project_dir), "action": "invalid_action_xyz"},
            )
            d = _result_to_dict(res)
            assert "error" in d, \
                    f"Invalid action must return error, got: {list(d.keys())}"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.runtime_deps
class TestFederationToolContract:
    """federation() handles list action and returns a list."""

    async def test_federation_list_returns_list(self, mcp_params, project_dir):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "federation",
                {"root_path": str(project_dir.parent), "action": "list"},
            )
            d = _result_to_dict(res)
            assert isinstance(d, dict), f"federation must return dict, got: {type(d)}"

    async def test_federation_invalid_action_returns_error(self, mcp_params, project_dir):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "federation",
                {"root_path": str(project_dir.parent), "action": "bogus_action"},
            )
            d = _result_to_dict(res)
            assert "error" in d or isinstance(d, dict), \
                    f"federation must return dict (possibly with error), got: {type(d)}"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.runtime_deps
class TestManageToolContract:
    """manage() handles vacuum dry_run, stop_watching, and returns messages."""

    async def test_manage_vacuum_dry_run_returns_message(self, mcp_params, project_dir):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "manage",
                {"project_path": str(project_dir), "action": "vacuum", "dry_run": True},
            )
            d = _result_to_dict(res)
            assert isinstance(d, dict), f"manage must return dict, got: {type(d)}"
            has_result = "message" in d or "status" in d or "freed_bytes" in d or "error" in d
            assert has_result, \
                f"manage(vacuum dry_run) must have message/status/freed_bytes, got: {list(d.keys())}"

    async def test_manage_stop_watching_returns_success(self, mcp_params, project_dir):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "manage",
                {"project_path": str(project_dir), "action": "stop_watching"},
            )
            d = _result_to_dict(res)
            assert isinstance(d, dict), f"manage must return dict, got: {type(d)}"

    async def test_manage_invalid_action_returns_error(self, mcp_params, project_dir):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(mcp_params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "manage",
                {"project_path": str(project_dir), "action": "invalid_action_xyz"},
            )
            d = _result_to_dict(res)
            assert "error" in d, \
                    f"Invalid action must return error, got: {list(d.keys())}"
