"""Tests for opencode_search.config — registry I/O, tier models, constants."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

from opencode_search.config import (
    FINAL_TOP_K,
    GLOBAL_RERANK_MAX,
    SCHEMA_VERSION,
    SKIP_STAGE1_RERANK_N,
    STAGE1_RERANK_K,
    STAGE1_VECTOR_K,
    ProjectEntry,
    get_tier_dims,
    get_tier_models,
    load_registry,
    save_registry,
)


# ---------------------------------------------------------------------------
# Tier models
# ---------------------------------------------------------------------------


def test_get_tier_models_premium():
    embed, rerank = get_tier_models("premium")
    assert "jina-embeddings-v2-base-code" in embed
    assert "jina-reranker-v2-base-multilingual" in rerank


def test_get_tier_models_balanced():
    embed, rerank = get_tier_models("balanced")
    assert "jina-embeddings-v2-base-en" in embed
    assert "jina-reranker-v1-turbo-en" in rerank


def test_get_tier_models_budget():
    embed, rerank = get_tier_models("budget")
    assert "jina-embeddings-v2-small-en" in embed
    assert "ms-marco-MiniLM" in rerank


def test_get_tier_dims_premium():
    assert get_tier_dims("premium") == 768


def test_get_tier_dims_balanced():
    assert get_tier_dims("balanced") in (512, 768)


def test_get_tier_dims_budget():
    assert get_tier_dims("budget") in (384, 512)


def test_get_tier_models_unknown_raises():
    with pytest.raises((ValueError, KeyError)):
        get_tier_models("nonexistent")


# ---------------------------------------------------------------------------
# Constants are positive integers
# ---------------------------------------------------------------------------


def test_constants_positive():
    assert FINAL_TOP_K > 0
    assert GLOBAL_RERANK_MAX > 0
    assert STAGE1_VECTOR_K > 0
    assert STAGE1_RERANK_K > 0
    assert SKIP_STAGE1_RERANK_N > 0


def test_schema_version_is_string_digit():
    assert SCHEMA_VERSION.isdigit(), f"SCHEMA_VERSION must be a digit string, got {SCHEMA_VERSION!r}"


# ---------------------------------------------------------------------------
# ProjectEntry dataclass
# ---------------------------------------------------------------------------


def test_project_entry_round_trip():
    entry = ProjectEntry(
        path="/tmp/myproject",
        db_path="/tmp/myproject/.opencode/index_balanced",
        tier="balanced",
        dims=512,
        indexed_at="2026-01-01T00:00:00Z",
        file_count=42,
        watch=True,
    )
    d = entry.to_dict()
    restored = ProjectEntry.from_dict(d)
    assert restored.path == entry.path
    assert restored.tier == entry.tier
    assert restored.dims == entry.dims
    assert restored.watch == entry.watch
    assert restored.file_count == entry.file_count


def test_project_entry_from_dict_extra_keys_ignored():
    d = {
        "path": "/tmp/test",
        "db_path": "/tmp/test/.opencode/index_budget",
        "tier": "budget",
        "dims": 384,
        "unknown_future_key": "whatever",
    }
    entry = ProjectEntry.from_dict(d)
    assert entry.path == "/tmp/test"
    assert entry.tier == "budget"


def test_project_entry_defaults():
    entry = ProjectEntry(
        path="/tmp/x",
        db_path="/tmp/x/.opencode/index_balanced",
        tier="balanced",
        dims=512,
    )
    assert entry.watch is False
    assert entry.file_count == 0
    assert entry.indexed_at is None


# ---------------------------------------------------------------------------
# Registry I/O
# ---------------------------------------------------------------------------


def test_load_registry_missing_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_path = Path(tmpdir) / "nonexistent.json"
        with patch("opencode_search.config.REGISTRY_PATH", fake_path):
            result = load_registry()
    assert result == {}


def test_save_and_load_registry():
    entry = ProjectEntry(
        path="/tmp/proj_a",
        db_path="/tmp/proj_a/.opencode/index_balanced",
        tier="balanced",
        dims=512,
        file_count=10,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        reg_path = Path(tmpdir) / "projects.json"
        with patch("opencode_search.config.REGISTRY_PATH", reg_path):
            save_registry({"/tmp/proj_a": entry})
            loaded = load_registry()

    assert "/tmp/proj_a" in loaded
    assert loaded["/tmp/proj_a"].tier == "balanced"
    assert loaded["/tmp/proj_a"].file_count == 10


def test_save_registry_atomic(tmp_path):
    reg_path = tmp_path / "projects.json"
    entry = ProjectEntry(
        path="/tmp/proj_b",
        db_path="/tmp/proj_b/.opencode/index_budget",
        tier="budget",
        dims=384,
    )
    with patch("opencode_search.config.REGISTRY_PATH", reg_path):
        save_registry({"/tmp/proj_b": entry})
    # File should exist and be valid JSON
    assert reg_path.exists()
    with reg_path.open() as f:
        data = json.load(f)
    assert "/tmp/proj_b" in data


def test_load_registry_corrupted_file(tmp_path):
    reg_path = tmp_path / "projects.json"
    reg_path.write_text("not valid json")
    with patch("opencode_search.config.REGISTRY_PATH", reg_path):
        result = load_registry()
    assert result == {}


def test_registry_multiple_entries(tmp_path):
    reg_path = tmp_path / "projects.json"
    entries = {
        "/tmp/a": ProjectEntry(path="/tmp/a", db_path="/tmp/a/.opencode/index_budget",
                               tier="budget", dims=384),
        "/tmp/b": ProjectEntry(path="/tmp/b", db_path="/tmp/b/.opencode/index_premium",
                               tier="premium", dims=768),
    }
    with patch("opencode_search.config.REGISTRY_PATH", reg_path):
        save_registry(entries)
        loaded = load_registry()
    assert len(loaded) == 2
    assert loaded["/tmp/b"].tier == "premium"
