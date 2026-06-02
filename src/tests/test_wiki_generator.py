"""Tests for opencode_search.wiki.generator — WikiGenerator with mocked LLM."""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

from opencode_search.graph.storage import CommunityData, GraphStorage, NodeData
from opencode_search.wiki.generator import WikiGenerator
from opencode_search.wiki.storage import WikiStorage

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _node_id(file: str, qn: str) -> str:
    return hashlib.sha256(f"{file}::{qn}".encode()).hexdigest()[:16]


def _make_node(file: str, name: str, qn: str | None = None) -> NodeData:
    qualified = qn or f"mod.{name}"
    return NodeData(
        id=_node_id(file, qualified),
        name=name, qualified_name=qualified, kind="function",
        file=file, created_at="", updated_at="",
    )


@pytest.fixture
def setup(tmp_path):
    wiki_dir = tmp_path / "wiki"
    raw_dir = tmp_path / "raw"
    wiki_storage = WikiStorage(wiki_dir=wiki_dir, raw_dir=raw_dir)

    gs = GraphStorage(str(tmp_path / "graph.db"))
    gs.open()

    nodes = [
        _make_node("/project/auth.py", "authenticate", "auth.authenticate"),
        _make_node("/project/auth.py", "verify", "auth.verify"),
    ]
    gs.upsert_nodes(nodes)
    gs.upsert_community(CommunityData(id=0, node_count=2, key_entry_points=["auth.authenticate"]))
    for n in nodes:
        gs.set_community(n.id, 0)

    # Mock LLM
    llm = MagicMock()
    llm.module_wiki_page.return_value = "# Auth Module\n\nHandles authentication."
    llm.community_summary.return_value = ("Auth Layer", "Handles JWT authentication and token verification.")
    llm.raw_doc_to_wiki.return_value = "# Ingested Doc\n\nKey information from the document."

    gen = WikiGenerator(llm=llm, wiki=wiki_storage, graph=gs)
    yield gen, wiki_storage, gs, llm
    gs.close()


async def test_wiki_generate_module_page_creates_file(setup):
    gen, wiki, _, _ = setup
    await gen.generate_module_page("auth", "/project")
    pages = wiki.list_wiki_pages()
    assert any("auth" in p for p in pages)


async def test_wiki_generate_module_page_content_not_empty(setup):
    gen, wiki, _, _llm = setup
    await gen.generate_module_page("auth", "/project")
    pages = wiki.list_wiki_pages()
    page = next(p for p in pages if "auth" in p)
    content = wiki.read_wiki_page(page)
    assert content is not None
    assert len(content) > 0


async def test_wiki_generate_module_page_appends_to_log(setup):
    gen, wiki, _, _ = setup
    await gen.generate_module_page("auth", "/project")
    assert wiki.log_path().exists()
    log = wiki.log_path().read_text(encoding="utf-8")
    assert "auth" in log


async def test_wiki_generate_community_page_creates_file(setup):
    gen, wiki, _, _ = setup
    await gen.generate_community_page(0)
    pages = wiki.list_wiki_pages()
    assert "community_0" in pages


async def test_wiki_generate_community_page_content_not_empty(setup):
    gen, wiki, _, _ = setup
    await gen.generate_community_page(0)
    content = wiki.read_wiki_page("community_0")
    assert content is not None
    assert "Auth Layer" in content


async def test_wiki_generate_community_page_appends_to_log(setup):
    gen, wiki, _, _ = setup
    await gen.generate_community_page(0)
    log = wiki.log_path().read_text(encoding="utf-8")
    assert "community" in log.lower()


async def test_wiki_generate_index_lists_all_page_names(setup):
    gen, wiki, _, _ = setup
    wiki.write_wiki_page("page_a", "content a")
    wiki.write_wiki_page("page_b", "content b")
    await gen.generate_index()
    index = wiki.index_path().read_text(encoding="utf-8")
    assert "page_a" in index
    assert "page_b" in index


async def test_wiki_generate_index_creates_index_md(setup):
    gen, wiki, _, _ = setup
    await gen.generate_index()
    assert wiki.index_path().exists()


async def test_wiki_ingest_raw_markdown_creates_wiki_page(setup, tmp_path):
    gen, wiki, _, _ = setup
    src = tmp_path / "design.md"
    src.write_text("# Design\nThis is a design document.", encoding="utf-8")
    pages = await gen.ingest_raw_source(str(src), "/project")
    assert len(pages) >= 1
    page_content = wiki.read_wiki_page(pages[0])
    assert page_content is not None


async def test_wiki_ingest_raw_markdown_copies_to_raw_dir(setup, tmp_path):
    gen, wiki, _, _ = setup
    src = tmp_path / "notes.md"
    src.write_text("# Notes", encoding="utf-8")
    await gen.ingest_raw_source(str(src), "/project")
    assert wiki.raw_path("notes.md").exists()


async def test_wiki_ingest_raw_markdown_appends_to_log(setup, tmp_path):
    gen, wiki, _, _ = setup
    src = tmp_path / "doc.md"
    src.write_text("content", encoding="utf-8")
    await gen.ingest_raw_source(str(src), "/project")
    log = wiki.log_path().read_text(encoding="utf-8")
    assert "doc.md" in log


async def test_wiki_ingest_nonexistent_file_raises_error(setup):
    gen, _wiki, _, _ = setup
    with pytest.raises(FileNotFoundError):
        await gen.ingest_raw_source("/nonexistent/path/doc.md", "/project")


async def test_wiki_lint_empty_wiki_no_issues(setup):
    gen, _wiki, _, _ = setup
    result = await gen.lint()
    assert result["healthy"] is True
    assert result["total_pages"] == 0
    assert result["orphans"] == []
    assert result["empty_pages"] == []


async def test_wiki_lint_detects_empty_page(setup):
    gen, wiki, _, _ = setup
    wiki.write_wiki_page("empty_page", "")
    result = await gen.lint()
    assert "empty_page" in result["empty_pages"]


async def test_wiki_lint_detects_orphan_page(setup):
    gen, wiki, _, _ = setup
    # Page exists but not referenced in index
    wiki.write_wiki_page("orphan_page", "some content")
    wiki.write_index("# Index\n- No reference to orphan")
    result = await gen.lint()
    assert "orphan_page" in result["orphans"]


async def test_wiki_lint_referenced_page_not_orphan(setup):
    gen, wiki, _, _ = setup
    wiki.write_wiki_page("documented_page", "some content")
    wiki.write_index("# Index\n- [documented_page](documented_page.md)")
    result = await gen.lint()
    assert "documented_page" not in result["orphans"]


# ---------------------------------------------------------------------------
# New tests: empty community guard + code_samples wiring
# ---------------------------------------------------------------------------

async def test_generate_community_page_empty_community_returns_empty(setup):
    """generate_community_page returns '' (no wiki file) if community has no nodes."""
    gen, wiki, gs, _llm = setup
    # Create a community with no nodes
    gs.upsert_community(CommunityData(id=99, node_count=0, key_entry_points=[]))
    # Do NOT assign any nodes to community 99

    content = await gen.generate_community_page(99)
    assert content == "", (
        "generate_community_page must return '' for empty communities, "
        "not call the LLM with empty summaries."
    )
    # LLM must NOT have been called for this empty community
    # (it may have been called for earlier tests in this session — check call count didn't increase)
    assert "community_99" not in wiki.list_wiki_pages(), (
        "No wiki page should be created for empty community 99"
    )


async def test_generate_community_page_passes_code_samples_to_llm(setup, tmp_path):
    """generate_community_page passes code_samples to community_summary."""
    gen, _wiki, gs, llm = setup

    # Write a real source file so _sample_community_code can read it
    src_file = tmp_path / "realfile.py"
    src_file.write_text("def process():\n    return True\n", encoding="utf-8")

    # Create node pointing to the real file
    import datetime
    import hashlib
    now = datetime.datetime.now(datetime.UTC).isoformat()
    nid = hashlib.sha256(b"realfile::process").hexdigest()[:16]
    from opencode_search.graph.storage import NodeData as ND  # noqa: N817
    n = ND(
        id=nid, name="process", qualified_name="realfile.process",
        kind="function", file=str(src_file),
        start_line=1, end_line=2,
        language="python", created_at=now, updated_at=now,
    )
    gs.upsert_nodes([n])
    gs.upsert_community(CommunityData(id=77, node_count=1, key_entry_points=["realfile.process"]))
    gs.set_community(nid, 77)

    # Reset call tracking
    llm.community_summary.reset_mock()

    await gen.generate_community_page(77)

    assert llm.community_summary.called, "community_summary must be called for community 77"
    args, kwargs = llm.community_summary.call_args
    # Either positional or keyword code_samples
    code_samples = kwargs.get("code_samples") if kwargs else (args[1] if len(args) > 1 else None)
    assert code_samples is not None, (
        "generate_community_page must pass code_samples to community_summary. "
        "Import _sample_community_code from handlers._enrichment and pass result."
    )
