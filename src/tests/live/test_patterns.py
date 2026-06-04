"""Live pattern detection tests — LLM must classify frameworks/architecture/conventions.

Tests GET /api/overview?what=patterns — requires daemon + indexed project.
LLM (qwen3-enrich:1.7b) must produce real classifications, not 'unknown' placeholders.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def patterns(http, project):
    """Fetch and cache the patterns result for this module."""
    r = http.get("/api/overview", params={"project": project, "what": "patterns"})
    assert r.status_code == 200, f"patterns failed: {r.status_code} {r.text[:200]}"
    return r.json()


def test_patterns_endpoint_returns_ok(patterns):
    """Patterns endpoint must return status=ok."""
    assert patterns.get("status") == "ok", f"Expected status=ok; got: {patterns.get('status')}"


def test_patterns_frameworks_non_empty(patterns):
    """LLM must identify at least one framework from the dependency list."""
    frameworks = patterns.get("key_frameworks", [])
    assert isinstance(frameworks, list), f"key_frameworks must be a list; got: {type(frameworks)}"
    assert len(frameworks) > 0, (
        f"LLM found no frameworks — either dependencies are empty or LLM classification failed. "
        f"Packages detected: {len(patterns.get('dependencies', {}).get('packages', []))}"
    )


def test_patterns_architecture_not_unknown(patterns):
    """LLM must classify the architecture — 'unknown' means LLM returned nothing useful."""
    arch = patterns.get("architecture", "")
    assert arch and arch != "unknown", (
        f"Architecture is '{arch}' — LLM classification returned no useful label. "
        f"Module structure type: {patterns.get('module_structure', {}).get('type', 'missing')}"
    )


def test_patterns_conventions_has_language(patterns):
    """LLM must identify the primary language from code samples."""
    conventions = patterns.get("conventions", {})
    assert isinstance(conventions, dict), f"conventions must be dict; got: {type(conventions)}"
    lang = conventions.get("language", "")
    assert lang and lang != "unknown", (
        f"Conventions language is '{lang}' — LLM returned no language classification"
    )


def test_patterns_module_structure_classified(patterns):
    """LLM must classify the module structure pattern — not 'unknown'."""
    module = patterns.get("module_structure", {})
    pattern_type = module.get("type", "unknown")
    assert pattern_type and pattern_type != "unknown", (
        f"Module structure type is '{pattern_type}' — LLM returned no classification. "
        f"Detected dirs: {module.get('detected_dirs', [])}"
    )


def test_patterns_has_languages(patterns):
    """Language breakdown must be present and non-empty."""
    languages = patterns.get("languages", [])
    assert len(languages) > 0, "No language breakdown — file scan may have failed"


def test_patterns_has_dependencies(patterns):
    """Dependency manifest must be detected."""
    deps = patterns.get("dependencies", {})
    manager = deps.get("manager", "unknown")
    packages = deps.get("packages", [])
    assert manager != "unknown" or len(packages) > 0, (
        "No dependency manager or packages detected — manifest scan may have failed"
    )
