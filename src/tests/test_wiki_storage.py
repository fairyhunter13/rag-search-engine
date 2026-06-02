"""Tests for opencode_search.wiki.storage — WikiStorage."""
from __future__ import annotations

import pytest

from opencode_search.wiki.storage import WikiStorage


@pytest.fixture
def wiki(tmp_path) -> WikiStorage:
    return WikiStorage(
        wiki_dir=tmp_path / "wiki",
        raw_dir=tmp_path / "raw",
    )


def test_wiki_storage_creates_wiki_dir_on_init(tmp_path):
    wiki_dir = tmp_path / "wiki"
    WikiStorage(wiki_dir=wiki_dir, raw_dir=tmp_path / "raw")
    assert wiki_dir.exists()


def test_wiki_storage_creates_raw_dir_on_init(tmp_path):
    raw_dir = tmp_path / "raw"
    WikiStorage(wiki_dir=tmp_path / "wiki", raw_dir=raw_dir)
    assert raw_dir.exists()


def test_wiki_storage_write_page_creates_file(wiki):
    wiki.write_wiki_page("auth", "# Auth\nAuthentication module.")
    assert wiki.wiki_path("auth").exists()


def test_wiki_storage_write_page_overwrites_existing(wiki):
    wiki.write_wiki_page("auth", "old content")
    wiki.write_wiki_page("auth", "new content")
    content = wiki.read_wiki_page("auth")
    assert content == "new content"


def test_wiki_storage_read_page_returns_content(wiki):
    wiki.write_wiki_page("module_a", "# Module A\nDoes stuff.")
    content = wiki.read_wiki_page("module_a")
    assert content == "# Module A\nDoes stuff."


def test_wiki_storage_read_nonexistent_page_returns_none(wiki):
    assert wiki.read_wiki_page("does_not_exist_xyz") is None


def test_wiki_storage_list_pages_empty_on_new_storage(wiki):
    assert wiki.list_wiki_pages() == []


def test_wiki_storage_list_pages_returns_all_names(wiki):
    wiki.write_wiki_page("auth", "auth content")
    wiki.write_wiki_page("db", "db content")
    wiki.write_wiki_page("api", "api content")
    pages = wiki.list_wiki_pages()
    assert set(pages) == {"auth", "db", "api"}


def test_wiki_storage_list_pages_excludes_index_and_log(wiki):
    wiki.write_wiki_page("real_page", "content")
    wiki.write_index("# Index")
    wiki.append_log("test entry")
    pages = wiki.list_wiki_pages()
    assert "index" not in pages
    assert "log" not in pages
    assert "real_page" in pages


def test_wiki_storage_append_log_creates_file(wiki):
    wiki.append_log("something happened")
    assert wiki.log_path().exists()


def test_wiki_storage_append_log_multiple_entries_appended(wiki):
    wiki.append_log("event 1")
    wiki.append_log("event 2")
    wiki.append_log("event 3")
    content = wiki.log_path().read_text(encoding="utf-8")
    assert "event 1" in content
    assert "event 2" in content
    assert "event 3" in content


def test_wiki_storage_write_index_creates_index_md(wiki):
    wiki.write_index("# Index\n- [auth](auth.md)")
    assert wiki.index_path().exists()
    content = wiki.index_path().read_text(encoding="utf-8")
    assert "Index" in content


def test_wiki_storage_register_raw_source_copies_file(wiki, tmp_path):
    src = tmp_path / "design.md"
    src.write_text("# Design Doc", encoding="utf-8")
    name = wiki.register_raw_source(str(src))
    assert wiki.raw_path(name).exists()
    assert wiki.raw_path(name).read_text() == "# Design Doc"


def test_wiki_storage_register_raw_source_returns_name(wiki, tmp_path):
    src = tmp_path / "notes.txt"
    src.write_text("some notes", encoding="utf-8")
    name = wiki.register_raw_source(str(src))
    assert name == "notes.txt"


def test_wiki_storage_list_raw_sources_empty(wiki):
    assert wiki.list_raw_sources() == []


def test_wiki_storage_list_raw_sources_after_register(wiki, tmp_path):
    for fname in ["a.md", "b.md"]:
        src = tmp_path / fname
        src.write_text("content", encoding="utf-8")
        wiki.register_raw_source(str(src))
    sources = wiki.list_raw_sources()
    assert set(sources) == {"a.md", "b.md"}
