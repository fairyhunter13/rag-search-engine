"""Tests for opencode_search.chunker — file-type routing and safety limits.

All tests run without GPU (chunking is CPU-only).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencode_search.chunker import (
    MAX_TOKENS_PER_CHUNK,
    MIN_TOKENS_PER_CHUNK,
    TARGET_TOKENS_PER_CHUNK,
    Chunk,
    _chunk_fallback,
    _chunk_json,
    _chunk_jsonl,
    _chunk_markdown,
    _enforce_token_limit,
    _merge_tiny,
    _route,
    chunk_file,
    count_tokens,
    split_by_tokens,
)


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------


def test_count_tokens_empty():
    assert count_tokens("") == 0


def test_count_tokens_positive():
    assert count_tokens("hello world") > 0


def test_count_tokens_proportional():
    short = count_tokens("hi")
    long = count_tokens("hi " * 100)
    assert long > short


# ---------------------------------------------------------------------------
# Empty file handling
# ---------------------------------------------------------------------------


def test_chunk_file_empty_content():
    result = chunk_file("", Path("test.py"))
    assert result == []


def test_chunk_file_whitespace_only():
    result = chunk_file("   \n\t  ", Path("test.py"))
    assert result == []


# ---------------------------------------------------------------------------
# Small file — single chunk pass-through
# ---------------------------------------------------------------------------


def test_chunk_file_small_returns_one_chunk():
    content = "def hello():\n    pass\n"
    result = chunk_file(content, Path("test.py"))
    assert len(result) == 1
    assert result[0].content == content


# ---------------------------------------------------------------------------
# Markdown chunking
# ---------------------------------------------------------------------------


def test_chunk_markdown_returns_chunks():
    content = "# Title\n\nSome content.\n\n## Section\n\nMore content here.\n"
    chunks = _chunk_markdown(content)
    assert len(chunks) >= 1
    for c in chunks:
        assert isinstance(c, Chunk)
        assert c.content.strip()


def test_chunk_markdown_preserves_content():
    content = "# Header\n\nParagraph text.\n"
    chunks = _chunk_markdown(content)
    combined = " ".join(c.content for c in chunks)
    assert "Header" in combined or "Paragraph" in combined


# ---------------------------------------------------------------------------
# JSON chunking
# ---------------------------------------------------------------------------


def test_chunk_json_dict():
    data = {"key": "value", "nested": {"a": 1, "b": 2}}
    content = json.dumps(data)
    chunks = _chunk_json(content)
    assert len(chunks) >= 1
    for c in chunks:
        assert c.chunk_type == "json"


def test_chunk_json_large_object():
    data = {f"key_{i}": f"value_{i}" * 10 for i in range(50)}
    content = json.dumps(data)
    chunks = _chunk_json(content)
    assert len(chunks) >= 1


# ---------------------------------------------------------------------------
# JSONL chunking
# ---------------------------------------------------------------------------


def test_chunk_jsonl_lines():
    lines = [json.dumps({"id": i, "text": "content"}) for i in range(10)]
    content = "\n".join(lines)
    chunks = _chunk_jsonl(content)
    assert len(chunks) >= 1
    for c in chunks:
        assert c.chunk_type == "json"


def test_chunk_jsonl_empty_lines_ignored():
    content = '\n{"a": 1}\n\n{"b": 2}\n'
    chunks = _chunk_jsonl(content)
    assert len(chunks) >= 1


# ---------------------------------------------------------------------------
# Fallback chunker
# ---------------------------------------------------------------------------


def test_chunk_fallback_returns_chunks():
    content = "Some random text " * 100
    chunks = _chunk_fallback(content, "text")
    assert len(chunks) >= 1
    for c in chunks:
        assert c.content.strip()


def test_chunk_fallback_chunk_type():
    chunks = _chunk_fallback("hello world", "python")
    assert all(c.chunk_type == "block" for c in chunks)


# ---------------------------------------------------------------------------
# _enforce_token_limit
# ---------------------------------------------------------------------------


def test_enforce_token_limit_passes_small():
    small = Chunk(content="short text", start_line=0, end_line=0, chunk_type="code", language="python")
    result = _enforce_token_limit([small])
    assert len(result) >= 1


def test_enforce_token_limit_splits_huge():
    # Create a chunk that exceeds MAX_TOKENS_PER_CHUNK
    huge_content = "word " * (MAX_TOKENS_PER_CHUNK * 5)
    huge = Chunk(content=huge_content, start_line=0, end_line=0, chunk_type="code", language="python")
    result = _enforce_token_limit([huge])
    # Should be split into multiple chunks, each within limit
    assert len(result) > 1
    for c in result:
        assert count_tokens(c.content) <= MAX_TOKENS_PER_CHUNK


# ---------------------------------------------------------------------------
# _merge_tiny
# ---------------------------------------------------------------------------


def test_merge_tiny_merges_small_chunks():
    tiny = Chunk(content="x", start_line=0, end_line=0, chunk_type="code", language="python")
    result = _merge_tiny([tiny, tiny, tiny])
    # After merging, should have fewer or equal chunks
    assert len(result) <= 3


def test_merge_tiny_preserves_large_chunks():
    large_text = "word " * TARGET_TOKENS_PER_CHUNK
    large = Chunk(content=large_text, start_line=0, end_line=0, chunk_type="code", language="python")
    result = _merge_tiny([large])
    assert len(result) == 1


# ---------------------------------------------------------------------------
# split_by_tokens
# ---------------------------------------------------------------------------


def test_split_by_tokens_empty():
    assert split_by_tokens("", "python") == []


def test_split_by_tokens_small():
    result = split_by_tokens("hello", "python")
    assert len(result) == 1
    assert result[0].content == "hello"


def test_split_by_tokens_respects_limit():
    content = "word " * (MAX_TOKENS_PER_CHUNK * 3)
    chunks = split_by_tokens(content, "python")
    assert len(chunks) > 1
    for c in chunks:
        assert count_tokens(c.content) <= MAX_TOKENS_PER_CHUNK


# ---------------------------------------------------------------------------
# Route dispatch — language detection
# ---------------------------------------------------------------------------


def test_route_markdown():
    content = "# Title\nContent"
    chunks = _route(content, "md", "markdown")
    assert len(chunks) >= 1


def test_route_json():
    content = '{"key": "value"}'
    chunks = _route(content, "json", "json")
    assert len(chunks) >= 1


def test_route_jsonl():
    content = '{"a": 1}\n{"b": 2}'
    chunks = _route(content, "jsonl", "json")
    assert len(chunks) >= 1


# ---------------------------------------------------------------------------
# chunk_file integration
# ---------------------------------------------------------------------------


def test_chunk_file_python():
    content = "def hello():\n    return 'world'\n" * 20
    chunks = chunk_file(content, Path("module.py"))
    assert len(chunks) >= 1
    assert all(isinstance(c, Chunk) for c in chunks)


def test_chunk_file_markdown_large():
    content = "# Section\n\nContent paragraph.\n\n" * 30
    chunks = chunk_file(content, Path("readme.md"))
    assert len(chunks) >= 1


def test_chunk_file_json():
    data = {f"field_{i}": f"value_{i}" for i in range(20)}
    content = json.dumps(data, indent=2)
    chunks = chunk_file(content, Path("data.json"))
    assert len(chunks) >= 1


def test_chunk_file_unknown_extension():
    content = "Some text content.\n" * 10
    chunks = chunk_file(content, Path("file.xyz"))
    assert len(chunks) >= 1


def test_chunk_file_no_chunk_exceeds_max_tokens():
    content = "word " * (MAX_TOKENS_PER_CHUNK * 4)
    chunks = chunk_file(content, Path("big.txt"))
    for c in chunks:
        tokens = count_tokens(c.content)
        assert tokens <= MAX_TOKENS_PER_CHUNK, (
            f"Chunk with {tokens} tokens exceeds MAX_TOKENS_PER_CHUNK={MAX_TOKENS_PER_CHUNK}"
        )


def test_chunk_file_language_set():
    content = "def foo(): pass\n" * 5
    chunks = chunk_file(content, Path("script.py"))
    assert all(c.language != "" for c in chunks)
