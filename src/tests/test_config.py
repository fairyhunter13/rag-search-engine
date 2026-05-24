"""Tests for opencode_search.config — registry I/O, tier models, constants."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from opencode_search.config import (
    FINAL_TOP_K,
    GLOBAL_RERANK_MAX,
    SCHEMA_VERSION,
    STAGE1_RERANK_K,
    STAGE1_VECTOR_K,
    ProjectEntry,
    get_legacy_project_db_path,
    get_project_db_path,
    get_project_index_dir,
    get_tier_dims,
    get_tier_models,
    load_registry,
    migrate_project_entry,
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


def test_schema_version_is_string_digit():
    assert SCHEMA_VERSION.isdigit(), f"SCHEMA_VERSION must be a digit string, got {SCHEMA_VERSION!r}"


# ---------------------------------------------------------------------------
# ProjectEntry dataclass
# ---------------------------------------------------------------------------


def test_project_entry_round_trip():
    project_path = "/tmp/myproject"
    entry = ProjectEntry(
        path=project_path,
        db_path=get_project_db_path(project_path, "balanced"),
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
    project_path = "/tmp/test"
    d = {
        "path": project_path,
        "db_path": get_project_db_path(project_path, "budget"),
        "tier": "budget",
        "dims": 384,
        "unknown_future_key": "whatever",
    }
    entry = ProjectEntry.from_dict(d)
    assert entry.path == "/tmp/test"
    assert entry.tier == "budget"


def test_project_entry_defaults():
    project_path = "/tmp/x"
    entry = ProjectEntry(
        path=project_path,
        db_path=get_project_db_path(project_path, "balanced"),
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
    project_path = "/tmp/proj_a"
    entry = ProjectEntry(
        path=project_path,
        db_path=get_project_db_path(project_path, "balanced"),
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
    project_path = "/tmp/proj_b"
    entry = ProjectEntry(
        path=project_path,
        db_path=get_project_db_path(project_path, "budget"),
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
        "/tmp/a": ProjectEntry(path="/tmp/a", db_path=get_project_db_path("/tmp/a", "budget"),
                               tier="budget", dims=384),
        "/tmp/b": ProjectEntry(path="/tmp/b", db_path=get_project_db_path("/tmp/b", "premium"),
                               tier="premium", dims=768),
    }
    with patch("opencode_search.config.REGISTRY_PATH", reg_path):
        save_registry(entries)
        loaded = load_registry()
    assert len(loaded) == 2
    assert loaded["/tmp/b"].tier == "premium"


def test_get_project_db_path_uses_centralized_root(tmp_path):
    with patch("opencode_search.config.REGISTRY_PATH", tmp_path / "projects.json"):
        db_path = Path(get_project_db_path("/tmp/demo-project", "balanced"))
        index_dir = get_project_index_dir("/tmp/demo-project")

    assert db_path.parent == index_dir
    assert db_path.name == "index_balanced"
    assert ".opencode" not in db_path.parts


def test_migrate_project_entry_moves_legacy_index(tmp_path):
    project_root = tmp_path / "project"
    legacy_index = project_root / ".opencode" / "index_budget"
    legacy_index.mkdir(parents=True)
    (legacy_index / "marker.txt").write_text("ok", encoding="utf-8")

    with patch("opencode_search.config.REGISTRY_PATH", tmp_path / "projects.json"):
        entry = ProjectEntry(
            path=str(project_root),
            db_path=get_legacy_project_db_path(project_root, "budget"),
            tier="budget",
            dims=384,
        )

        changed = migrate_project_entry(entry)
        migrated_path = Path(entry.db_path)

    assert changed is True
    assert migrated_path.exists()
    assert (migrated_path / "marker.txt").read_text(encoding="utf-8") == "ok"
    assert not legacy_index.exists()


def test_load_registry_migrates_legacy_db_path_and_persists(tmp_path):
    project_root = tmp_path / "project"
    reg_path = tmp_path / "projects.json"
    legacy_index = project_root / ".opencode" / "index_budget"
    legacy_index.mkdir(parents=True)
    (legacy_index / "marker.txt").write_text("ok", encoding="utf-8")
    reg_path.write_text(
        json.dumps(
            {
                str(project_root): {
                    "path": str(project_root),
                    "db_path": get_legacy_project_db_path(project_root, "budget"),
                    "tier": "budget",
                    "dims": 384,
                }
            }
        ),
        encoding="utf-8",
    )

    with patch("opencode_search.config.REGISTRY_PATH", reg_path):
        loaded = load_registry()

    migrated_path = Path(loaded[str(project_root)].db_path)
    persisted = json.loads(reg_path.read_text(encoding="utf-8"))

    assert migrated_path.exists()
    assert not legacy_index.exists()
    assert persisted[str(project_root)]["db_path"] == str(migrated_path)
