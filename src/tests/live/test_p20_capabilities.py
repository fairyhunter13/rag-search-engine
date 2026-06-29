"""P20.6: E2E proof that all 5 wired capabilities work together (no mocks)."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


@pytest.mark.slow
def test_p20_capabilities_e2e(safe_tmp_path):
    """A+B+C+D+E: federation-register, indexed_at stamp, metrics, check, ask context."""
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import get_project, remove_project, upsert_project
    from opencode_search.daemon.federation import index_members
    from opencode_search.daemon.sweeps import _index_project
    from opencode_search.server._overview import handle_overview

    # Setup: tmp root with a symlinked python sub-repo
    member = safe_tmp_path / "member"
    member.mkdir()
    (member / "auth.py").write_text("def authenticate(token): return bool(token)\n")
    root = safe_tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(member)
    (root / "main.py").write_text("def main(): pass\n")

    root_path, member_path = str(root), str(member)
    upsert_project(ProjectEntry(path=root_path, enabled=True))
    try:
        # B: index_project stamps indexed_at + file_count
        _index_project(root_path)
        entry = get_project(root_path)
        assert entry is not None
        assert entry.indexed_at is not None, "B: indexed_at must be stamped after _index_project"
        assert entry.file_count > 0, f"B: file_count must be >0, got {entry.file_count}"

        # A: index_members discovers and registers the federation member
        index_members(root_path)
        assert get_project(member_path) is not None, "A: federation member must be registered"

        # C: overview(what="metrics") returns chat_stream metrics
        metrics_json = handle_overview("", "metrics")
        metrics = json.loads(metrics_json)
        assert "chat_stream" in metrics, f"C: metrics missing chat_stream: {metrics_json}"
        assert "stream_error_count" in metrics["chat_stream"], (
            f"C: chat_stream missing stream_error_count: {metrics}"
        )

        # D: configure_integrations.py --check exits 0 (all 15 targets in sync)
        script = Path(__file__).parents[3] / "scripts" / "configure_integrations.py"
        r = subprocess.run([sys.executable, str(script), "--check"], capture_output=True, text=True)
        assert r.returncode == 0, f"D: --check failed:\n{r.stdout}\n{r.stderr}"

        # E: MCP ask returns non-empty composed context from the indexed project
        from opencode_search.server.mcp import ask as ask_tool
        from tests.live._projects import federation_root
        sample_fed_root = federation_root()  # sample shop-federation, not a real project
        context = asyncio.run(ask_tool("How does authentication work?", sample_fed_root, "all"))
        assert isinstance(context, str) and len(context) > 20, (
            f"E: MCP ask returned empty/tiny context: {context!r}"
        )
    finally:
        remove_project(root_path)
        remove_project(member_path)
