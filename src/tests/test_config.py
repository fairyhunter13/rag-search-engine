"""Tests for opencode_search.config — registry I/O, model constants, and migration."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from opencode_search.config import (
    DEFAULT_DIMS,
    DEFAULT_EMBED_MODEL,
    DEFAULT_RERANK_MODEL,
    FINAL_TOP_K,
    GLOBAL_RERANK_MAX,
    SCHEMA_VERSION,
    STAGE1_RERANK_K,
    STAGE1_VECTOR_K,
    ProjectEntry,
    get_project_db_path,
    get_project_graph_db_path,
    get_project_index_dir,
    get_project_raw_dir,
    get_project_wiki_dir,
    load_registry,
    migrate_project_entry,
    save_registry,
)


# ---------------------------------------------------------------------------
# Phase 0: single model pair constants
# ---------------------------------------------------------------------------


def test_default_embed_model_is_jina_v2_base_code():
    assert DEFAULT_EMBED_MODEL == "jinaai/jina-embeddings-v2-base-code"


def test_default_rerank_model_is_jina_reranker_v1_turbo_en():
    assert DEFAULT_RERANK_MODEL == "jinaai/jina-reranker-v1-turbo-en"


def test_default_dims_is_768():
    assert DEFAULT_DIMS == 768


def test_no_get_tier_models_function_exists():
    import opencode_search.config as cfg
    assert not hasattr(cfg, "get_tier_models"), "get_tier_models should have been removed"


def test_no_get_tier_dims_function_exists():
    import opencode_search.config as cfg
    assert not hasattr(cfg, "get_tier_dims"), "get_tier_dims should have been removed"


def test_get_project_db_path_returns_tier_free_path(tmp_path):
    with patch("opencode_search.config.REGISTRY_PATH", tmp_path / "projects.json"):
        db_path = Path(get_project_db_path("/tmp/demo-project"))
        index_dir = get_project_index_dir("/tmp/demo-project")

    assert db_path.parent == index_dir
    assert db_path.name == "index"
    assert "budget" not in str(db_path)
    assert "balanced" not in str(db_path)
    assert "premium" not in str(db_path)
    assert ".opencode" not in db_path.parts


def test_get_project_graph_db_path(tmp_path):
    with patch("opencode_search.config.REGISTRY_PATH", tmp_path / "projects.json"):
        graph_path = Path(get_project_graph_db_path("/tmp/demo-project"))
        index_dir = get_project_index_dir("/tmp/demo-project")

    assert graph_path.parent == index_dir
    assert graph_path.name == "graph.db"


def test_get_project_wiki_dir(tmp_path):
    with patch("opencode_search.config.REGISTRY_PATH", tmp_path / "projects.json"):
        wiki_dir = get_project_wiki_dir("/tmp/demo-project")
        index_dir = get_project_index_dir("/tmp/demo-project")

    assert wiki_dir == index_dir / "wiki"


def test_get_project_raw_dir(tmp_path):
    with patch("opencode_search.config.REGISTRY_PATH", tmp_path / "projects.json"):
        raw_dir = get_project_raw_dir("/tmp/demo-project")
        index_dir = get_project_index_dir("/tmp/demo-project")

    assert raw_dir == index_dir / "raw"


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
# Phase 0: ProjectEntry — no tier field
# ---------------------------------------------------------------------------


def test_project_entry_has_no_tier_field():
    entry = ProjectEntry(path="/tmp/x", db_path="/tmp/x/index")
    assert not hasattr(entry, "tier"), "tier field should have been removed from ProjectEntry"


def test_project_entry_serializes_without_tier():
    entry = ProjectEntry(path="/tmp/x", db_path="/tmp/x/index", dims=768)
    d = entry.to_dict()
    assert "tier" not in d


def test_project_entry_from_dict_ignores_old_tier_key():
    d = {
        "path": "/tmp/test",
        "db_path": "/tmp/test/index",
        "tier": "budget",   # old registry key — must be silently ignored
        "dims": 768,
    }
    entry = ProjectEntry.from_dict(d)
    assert entry.path == "/tmp/test"
    assert not hasattr(entry, "tier")


def test_project_entry_dims_defaults_to_768():
    entry = ProjectEntry(path="/tmp/x", db_path="/tmp/x/index")
    assert entry.dims == DEFAULT_DIMS


def test_project_entry_round_trip():
    entry = ProjectEntry(
        path="/tmp/myproject",
        db_path=get_project_db_path("/tmp/myproject"),
        dims=768,
        indexed_at="2026-01-01T00:00:00Z",
        file_count=42,
        watch=True,
    )
    d = entry.to_dict()
    restored = ProjectEntry.from_dict(d)
    assert restored.path == entry.path
    assert restored.dims == entry.dims
    assert restored.watch == entry.watch
    assert restored.file_count == entry.file_count


def test_project_entry_from_dict_extra_keys_ignored():
    d = {
        "path": "/tmp/test",
        "db_path": "/tmp/test/index",
        "dims": 768,
        "unknown_future_key": "whatever",
    }
    entry = ProjectEntry.from_dict(d)
    assert entry.path == "/tmp/test"


def test_project_entry_defaults():
    entry = ProjectEntry(path="/tmp/x", db_path="/tmp/x/index", dims=768)
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
        db_path=get_project_db_path(project_path),
        dims=768,
        file_count=10,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        reg_path = Path(tmpdir) / "projects.json"
        with patch("opencode_search.config.REGISTRY_PATH", reg_path):
            save_registry({"/tmp/proj_a": entry})
            loaded = load_registry()

    assert "/tmp/proj_a" in loaded
    assert loaded["/tmp/proj_a"].file_count == 10


def test_save_registry_atomic(tmp_path):
    reg_path = tmp_path / "projects.json"
    project_path = "/tmp/proj_b"
    entry = ProjectEntry(
        path=project_path,
        db_path=get_project_db_path(project_path),
        dims=768,
    )
    with patch("opencode_search.config.REGISTRY_PATH", reg_path):
        save_registry({"/tmp/proj_b": entry})
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
        "/tmp/a": ProjectEntry(path="/tmp/a", db_path=get_project_db_path("/tmp/a"), dims=768),
        "/tmp/b": ProjectEntry(path="/tmp/b", db_path=get_project_db_path("/tmp/b"), dims=768),
    }
    with patch("opencode_search.config.REGISTRY_PATH", reg_path):
        save_registry(entries)
        loaded = load_registry()
    assert len(loaded) == 2


# ---------------------------------------------------------------------------
# Phase 0: Migration — old tier-suffixed db_paths are updated + indexed_at nulled
# ---------------------------------------------------------------------------


def test_old_registry_budget_loads_and_migrates_db_path(tmp_path):
    """Old registry with index_budget path migrates to index and nulls indexed_at."""
    project_root = tmp_path / "project"
    reg_path = tmp_path / "projects.json"
    reg_path.write_text(
        json.dumps({
            str(project_root): {
                "path": str(project_root),
                "db_path": str(tmp_path / "indexes" / "project-abc" / "index_budget"),
                "tier": "budget",
                "dims": 512,
                "indexed_at": "2025-01-01T00:00:00Z",
            }
        }),
        encoding="utf-8",
    )
    with patch("opencode_search.config.REGISTRY_PATH", reg_path):
        loaded = load_registry()

    entry = loaded[str(project_root)]
    assert Path(entry.db_path).name == "index", f"Expected 'index', got: {Path(entry.db_path).name}"
    assert entry.indexed_at is None  # nulled because dims changed


def test_old_registry_balanced_loads_and_migrates_db_path(tmp_path):
    """Old registry with index_balanced path migrates to index and nulls indexed_at."""
    project_root = tmp_path / "project"
    reg_path = tmp_path / "projects.json"
    reg_path.write_text(
        json.dumps({
            str(project_root): {
                "path": str(project_root),
                "db_path": str(tmp_path / "indexes" / "project-abc" / "index_balanced"),
                "tier": "balanced",
                "dims": 768,
                "indexed_at": "2025-01-01T00:00:00Z",
            }
        }),
        encoding="utf-8",
    )
    with patch("opencode_search.config.REGISTRY_PATH", reg_path):
        loaded = load_registry()

    entry = loaded[str(project_root)]
    assert Path(entry.db_path).name == "index", f"Expected 'index', got: {Path(entry.db_path).name}"
    assert entry.indexed_at is None


def test_old_registry_premium_loads_and_migrates_db_path(tmp_path):
    """Old registry with index_premium path migrates to index and nulls indexed_at."""
    project_root = tmp_path / "project"
    reg_path = tmp_path / "projects.json"
    reg_path.write_text(
        json.dumps({
            str(project_root): {
                "path": str(project_root),
                "db_path": str(tmp_path / "indexes" / "project-abc" / "index_premium"),
                "tier": "premium",
                "dims": 768,
                "indexed_at": "2025-01-01T00:00:00Z",
            }
        }),
        encoding="utf-8",
    )
    with patch("opencode_search.config.REGISTRY_PATH", reg_path):
        loaded = load_registry()

    entry = loaded[str(project_root)]
    assert Path(entry.db_path).name == "index", f"Expected 'index', got: {Path(entry.db_path).name}"
    assert entry.indexed_at is None


def test_old_registry_multiple_projects_all_migrate(tmp_path):
    """All three tier variants are migrated in a single load_registry call."""
    reg_path = tmp_path / "projects.json"
    projects = {}
    for tier_name in ("budget", "balanced", "premium"):
        proj = f"/tmp/proj_{tier_name}"
        projects[proj] = {
            "path": proj,
            "db_path": str(tmp_path / "indexes" / f"proj-xyz-abc" / f"index_{tier_name}"),
            "tier": tier_name,
            "dims": 512 if tier_name == "budget" else 768,
            "indexed_at": "2025-01-01T00:00:00Z",
        }
    reg_path.write_text(json.dumps(projects), encoding="utf-8")

    with patch("opencode_search.config.REGISTRY_PATH", reg_path):
        loaded = load_registry()

    for tier_name in ("budget", "balanced", "premium"):
        proj = f"/tmp/proj_{tier_name}"
        entry = loaded[proj]
        assert Path(entry.db_path).name == "index", f"{tier_name}: Expected 'index', got {Path(entry.db_path).name}"
        assert entry.indexed_at is None


def test_migrate_project_entry_legacy_per_project_path_moves_data(tmp_path):
    """Legacy .opencode/index_budget inside project root is moved (not tier-suffixed)."""
    project_root = tmp_path / "project"
    # Simulate old per-project path (pre-centralized-root, no tier suffix in actual name detection)
    # This is a path that doesn't end with index_budget|balanced|premium — it ends with something else
    # so it should be moved not just db_path-updated.
    legacy_index = project_root / ".opencode" / "mydata"
    legacy_index.mkdir(parents=True)
    (legacy_index / "marker.txt").write_text("ok", encoding="utf-8")

    with patch("opencode_search.config.REGISTRY_PATH", tmp_path / "projects.json"):
        entry = ProjectEntry(
            path=str(project_root),
            db_path=str(legacy_index),
            dims=768,
            indexed_at="2025-01-01T00:00:00Z",
        )
        changed = migrate_project_entry(entry)
        migrated_path = Path(entry.db_path)

    assert changed is True
    assert migrated_path.exists()
    assert (migrated_path / "marker.txt").read_text(encoding="utf-8") == "ok"
    # indexed_at preserved since dims didn't change
    assert entry.indexed_at == "2025-01-01T00:00:00Z"


def test_already_canonical_path_not_changed(tmp_path):
    """Entry already pointing to canonical path returns False from migrate."""
    with patch("opencode_search.config.REGISTRY_PATH", tmp_path / "projects.json"):
        canonical = get_project_db_path("/tmp/project")
        entry = ProjectEntry(path="/tmp/project", db_path=canonical, dims=768)
        changed = migrate_project_entry(entry)

    assert changed is False
    assert entry.db_path == canonical
