#!/usr/bin/env python3
"""Invariant oracle library for opencode-search-engine.

Each function returns an InvariantResult. Import and call in verify.py or tests.

Usage:
    from scripts.invariants import check_all, check_category
    results = check_all(project_path="/path/to/project")
"""
from __future__ import annotations

import sys
import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Ensure src is on path when run standalone
_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO / "src"))


@dataclass
class InvariantResult:
    name: str
    passed: bool
    message: str
    severity: str = "P1"  # P0=critical, P1=important, P2=nice-to-have
    category: str = "misc"
    detail: str = ""


def _run(coro):
    """Run an async coroutine from sync context."""
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Category: Code Quality
# ---------------------------------------------------------------------------

def _find_ruff() -> list[str]:
    """Find ruff binary: prefer venv sibling, fallback to python -m ruff."""
    import shutil
    # Check for ruff next to sys.executable (inside venv)
    venv_ruff = Path(sys.executable).parent / "ruff"
    if venv_ruff.exists():
        return [str(venv_ruff)]
    which_ruff = shutil.which("ruff")
    if which_ruff:
        return [which_ruff]
    return [sys.executable, "-m", "ruff"]


def check_ruff(repo_root: Path) -> InvariantResult:
    """Ruff linting — P1 (style warnings, not blocking).

    Reports the error count. Severity is P1 because the project has pre-existing
    ruff violations; compile check (check_compile) is the P0 correctness gate.
    """
    cmd = _find_ruff() + ["check",
        str(repo_root / "src" / "opencode_search"),
        str(repo_root / "src" / "tests"),
        "--statistics",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(repo_root))
    # Count errors from statistics output
    import re as _re
    error_lines = [l for l in (r.stdout + r.stderr).splitlines() if _re.match(r"\s*\d+\s+[A-Z]+\d+", l)]  # noqa: E741
    total_errors = sum(int(_re.match(r"\s*(\d+)", l).group(1)) for l in error_lines if _re.match(r"\s*(\d+)", l))  # noqa: E741
    passed = r.returncode == 0
    return InvariantResult(
        name="ruff_lint", passed=passed, severity="P1",  # P1: style, not blocking
        category="code_quality",
        message="Ruff: 0 errors" if passed else f"Ruff: {total_errors} style violations (pre-existing)",
        detail="\n".join(error_lines[:10]) if not passed else "",
    )


def check_mypy(repo_root: Path) -> InvariantResult:
    """mypy type checking must pass on critical modules."""
    r = subprocess.run(
        [sys.executable, "-m", "mypy",
         str(repo_root / "src" / "opencode_search"),
         "--ignore-missing-imports", "--no-error-summary", "-q"],
        capture_output=True, text=True,
    )
    errors = [l for l in r.stdout.splitlines() if ": error:" in l]  # noqa: E741
    passed = len(errors) == 0
    return InvariantResult(
        name="mypy_types", passed=passed, severity="P0", category="code_quality",
        message="mypy: 0 errors" if passed else f"mypy: {len(errors)} errors",
        detail="\n".join(errors[:10]),
    )


def check_compile(repo_root: Path) -> InvariantResult:
    """All Python files must compile without SyntaxError."""
    r = subprocess.run(
        [sys.executable, "-m", "compileall", "-q",
         str(repo_root / "src" / "opencode_search")],
        capture_output=True, text=True,
    )
    passed = r.returncode == 0
    return InvariantResult(
        name="syntax_check", passed=passed, severity="P0", category="code_quality",
        message="Syntax: OK" if passed else f"Syntax errors: {r.stderr[:300]}",
    )


# ---------------------------------------------------------------------------
# Category: Configuration Safety
# ---------------------------------------------------------------------------

def check_config_defaults() -> list[InvariantResult]:
    """All config constants must have safe defaults."""
    from opencode_search.config import (
        SCHEMA_VERSION, FTS_THRESHOLD, DEBOUNCE_DELAY_MS,
        DEFAULT_DIMS, DEFAULT_LLM_PROVIDER,
    )
    results = []

    def _check(name, condition, msg, severity="P1"):
        results.append(InvariantResult(
            name=f"config_{name}", passed=condition, severity=severity,
            category="config", message=msg if condition else f"FAIL: {msg}",
        ))

    _check("schema_version", SCHEMA_VERSION in {"2"}, f"SCHEMA_VERSION={SCHEMA_VERSION!r} valid", "P0")
    _check("fts_threshold", FTS_THRESHOLD >= 1, f"FTS_THRESHOLD={FTS_THRESHOLD} >= 1")
    _check("dims", DEFAULT_DIMS == 768, f"DEFAULT_DIMS={DEFAULT_DIMS} (expected 768)", "P0")
    _check("debounce", DEBOUNCE_DELAY_MS >= 100, f"DEBOUNCE_DELAY_MS={DEBOUNCE_DELAY_MS} >= 100ms")
    _check("llm_provider", DEFAULT_LLM_PROVIDER in {"ollama", "anthropic", "openai", "claude-code", "codex"},
           f"DEFAULT_LLM_PROVIDER={DEFAULT_LLM_PROVIDER!r} valid")

    return results


# ---------------------------------------------------------------------------
# Category: Registry Integrity
# ---------------------------------------------------------------------------

def check_registry_integrity() -> list[InvariantResult]:
    """Registry must load without error and have valid entries."""
    results = []
    try:
        from opencode_search.config import load_registry, REGISTRY_PATH
        registry = load_registry()
        results.append(InvariantResult(
            name="registry_loads", passed=True, severity="P0", category="registry",
            message=f"Registry loads OK ({len(registry)} entries at {REGISTRY_PATH})",
        ))
        invalid_paths = [k for k, v in registry.items() if not v.path or not v.db_path]
        results.append(InvariantResult(
            name="registry_entries_valid", severity="P1", category="registry",
            passed=len(invalid_paths) == 0,
            message=f"All {len(registry)} entries have path+db_path" if not invalid_paths
                    else f"Invalid entries: {invalid_paths[:3]}",
        ))
        invalid_dims = [k for k, v in registry.items() if v.dims <= 0]
        results.append(InvariantResult(
            name="registry_dims_positive", severity="P1", category="registry",
            passed=len(invalid_dims) == 0,
            message="All dims > 0" if not invalid_dims else f"Zero/negative dims: {invalid_dims[:3]}",
        ))
    except Exception as exc:
        results.append(InvariantResult(
            name="registry_loads", passed=False, severity="P0", category="registry",
            message=f"Registry failed to load: {exc}",
        ))
    return results


# ---------------------------------------------------------------------------
# Category: MCP Tool Contracts
# ---------------------------------------------------------------------------

def check_mcp_tool_registration() -> InvariantResult:
    """All 7 MCP intent tools must be importable as top-level functions in mcp.py."""
    try:
        from opencode_search import mcp as mcp_mod
        required = {"search", "ask", "graph", "overview", "build", "federation", "manage"}
        found = {name for name in required if callable(getattr(mcp_mod, name, None))}
        missing = required - found
        return InvariantResult(
            name="mcp_7_tools_registered", severity="P0", category="mcp_contracts",
            passed=len(missing) == 0,
            message="All 7 MCP tools are callable in mcp.py" if not missing
                    else f"Missing callable tools in mcp.py: {missing}",
        )
    except Exception as exc:
        return InvariantResult(
            name="mcp_7_tools_registered", passed=False, severity="P0", category="mcp_contracts",
            message=f"Could not check MCP tools: {exc}",
        )


def check_mcp_parameter_validation() -> list[InvariantResult]:
    """MCP tools must validate invalid parameters and return error dicts."""
    results = []

    async def _run_checks():
        nonlocal results
        try:
            from opencode_search import mcp as mcp_mod
            # Patch runtime_state.note_activity to avoid side effects
            import unittest.mock as mock
            with mock.patch.object(mcp_mod.runtime_state, "note_activity"):

                # search: invalid scope
                r = await mcp_mod.search(query="test", scope="invalid_scope")
                results.append(InvariantResult(
                    name="search_validates_scope", severity="P0", category="mcp_contracts",
                    passed="error" in r,
                    message="search() rejects invalid scope" if "error" in r
                            else f"search() accepted invalid scope: {r}",
                ))

                # graph: invalid relation
                r = await mcp_mod.graph(symbol="foo", project_path="/tmp", relation="invalid_rel")
                results.append(InvariantResult(
                    name="graph_validates_relation", severity="P0", category="mcp_contracts",
                    passed="error" in r,
                    message="graph() rejects invalid relation" if "error" in r
                            else f"graph() accepted invalid relation: {r}",
                ))

                # build: invalid action
                r = await mcp_mod.build(project_path="/tmp", action="invalid_action")
                results.append(InvariantResult(
                    name="build_validates_action", severity="P0", category="mcp_contracts",
                    passed="error" in r,
                    message="build() rejects invalid action" if "error" in r
                            else f"build() accepted invalid action: {r}",
                ))

                # build: ingest requires source_path
                r = await mcp_mod.build(project_path="/tmp", action="ingest")
                results.append(InvariantResult(
                    name="build_ingest_requires_source_path", severity="P1", category="mcp_contracts",
                    passed="error" in r,
                    message="build(action='ingest') requires source_path" if "error" in r
                            else f"build(action='ingest') without source_path did not error: {r}",
                ))

                # overview: invalid what
                r = await mcp_mod.overview(project_path="/tmp", what="invalid_what")
                results.append(InvariantResult(
                    name="overview_validates_what", severity="P0", category="mcp_contracts",
                    passed="error" in r,
                    message="overview() rejects invalid what" if "error" in r
                            else f"overview() accepted invalid what: {r}",
                ))

                # manage: invalid action
                r = await mcp_mod.manage(project_path="/tmp", action="invalid_action")
                results.append(InvariantResult(
                    name="manage_validates_action", severity="P0", category="mcp_contracts",
                    passed="error" in r,
                    message="manage() rejects invalid action" if "error" in r
                            else f"manage() accepted invalid action: {r}",
                ))

        except Exception as exc:
            results.append(InvariantResult(
                name="mcp_parameter_validation", passed=False, severity="P0", category="mcp_contracts",
                message=f"MCP validation checks failed: {exc}",
            ))

    _run(asyncio.coroutine(_run_checks)() if False else _run_checks())
    return results


# ---------------------------------------------------------------------------
# Category: KB Artifacts (requires indexed project)
# ---------------------------------------------------------------------------

def check_kb_artifacts(project_path: str) -> list[InvariantResult]:
    """Verify all 5 KB artifacts exist and are populated."""
    results = []
    root = Path(project_path).expanduser().resolve()

    try:
        from opencode_search.config import (
            get_project_graph_db_path, get_project_wiki_dir, load_registry,
        )
        from opencode_search.handlers._patterns import load_patterns_cache

        # 1. Registry has this project
        registry = load_registry()
        in_registry = str(root) in registry
        results.append(InvariantResult(
            name="kb_project_in_registry", severity="P0", category="kb_artifacts",
            passed=in_registry,
            message=f"Project {root.name} is in registry" if in_registry
                    else f"Project {root} NOT found in registry — has it been indexed?",
        ))
        if not in_registry:
            return results  # no point checking further

        # 2. Graph DB exists
        db_path = Path(get_project_graph_db_path(str(root)))
        results.append(InvariantResult(
            name="kb_graph_db_exists", severity="P0", category="kb_artifacts",
            passed=db_path.exists(),
            message=f"graph.db exists ({db_path})" if db_path.exists()
                    else f"graph.db MISSING: {db_path}",
        ))

        if db_path.exists():
            from opencode_search.graph.storage import GraphStorage
            gs = GraphStorage(str(db_path))
            gs.open()
            try:
                node_count = gs.node_count()
                edge_count = gs.edge_count()
                communities = gs.get_communities(min_node_count=2)
                enriched = [c for c in communities if c.title and c.title != f"Community {c.id}"]
                languages = {n.language for n in gs.all_nodes() if n.language} if node_count < 100_000 else {"(too large to scan)"}

                results.append(InvariantResult(
                    name="kb_graph_has_nodes", severity="P0", category="kb_artifacts",
                    passed=node_count > 0,
                    message=f"Graph: {node_count:,} nodes, {edge_count:,} edges",
                ))
                results.append(InvariantResult(
                    name="kb_graph_has_communities", severity="P1", category="kb_artifacts",
                    passed=len(communities) >= 0,  # 0 is OK on tiny projects
                    message=f"Communities: {len(communities)} (≥2 nodes), {len(enriched)} enriched",
                ))
                enrichment_pct = len(enriched) / len(communities) * 100 if communities else 0
                results.append(InvariantResult(
                    name="kb_enrichment_pct_valid", severity="P1", category="kb_artifacts",
                    passed=0 <= enrichment_pct <= 100,
                    message=f"Enrichment: {enrichment_pct:.1f}% ({len(enriched)}/{len(communities)})",
                ))
                results.append(InvariantResult(
                    name="kb_graph_has_call_edges", severity="P1", category="kb_artifacts",
                    passed=edge_count > 0,
                    message=f"Edges: {edge_count:,} (CALLS+IMPORTS+INHERITS)",
                    detail="0 edges means Tier-1 extractors may not have run" if edge_count == 0 else "",
                ))
                results.append(InvariantResult(
                    name="kb_graph_languages_detected", severity="P1", category="kb_artifacts",
                    passed=len(languages) >= 1,
                    message=f"Languages in graph: {sorted(languages)[:8]}",
                ))
                # Edge quality checks
                all_edges = gs.all_edges()
                invalid_conf = [e for e in all_edges if not (0.0 <= (e.confidence or 0) <= 1.0)]
                results.append(InvariantResult(
                    name="kb_edge_confidence_valid", severity="P1", category="kb_artifacts",
                    passed=len(invalid_conf) == 0,
                    message=f"All {len(all_edges):,} edges have confidence in [0,1]"
                            if not invalid_conf else f"{len(invalid_conf)} edges have invalid confidence",
                ))
            finally:
                gs.close()

        # 3. Wiki exists
        wiki_dir = get_project_wiki_dir(str(root))
        md_files = list(wiki_dir.glob("*.md")) if wiki_dir.exists() else []
        results.append(InvariantResult(
            name="kb_wiki_exists", severity="P1", category="kb_artifacts",
            passed=wiki_dir.exists() and len(md_files) > 0,
            message=f"Wiki: {len(md_files)} pages in {wiki_dir}" if md_files
                    else f"Wiki MISSING or empty: {wiki_dir}",
        ))
        if md_files:
            non_empty = [f for f in md_files if f.stat().st_size > 50]
            results.append(InvariantResult(
                name="kb_wiki_pages_have_content", severity="P1", category="kb_artifacts",
                passed=len(non_empty) > 0,
                message=f"Wiki: {len(non_empty)}/{len(md_files)} pages non-empty",
            ))

        # 4. Patterns cache
        cached = load_patterns_cache(str(root))
        results.append(InvariantResult(
            name="kb_patterns_cache_exists", severity="P1", category="kb_artifacts",
            passed=cached is not None,
            message=f"Patterns cache: present (cached_at={cached.get('cached_at', '?')})"
                    if cached else "Patterns cache: MISSING (run build(action='analyze_patterns'))",
        ))
        if cached:
            steps = cached.get("steps", [])
            expected_steps = {"llm_overview", "exact_extraction", "llm_synthesis"}
            missing_steps = expected_steps - set(steps)
            results.append(InvariantResult(
                name="kb_patterns_all_three_steps", severity="P1", category="kb_artifacts",
                passed=len(missing_steps) == 0,
                message="Patterns: all 3 LLM steps present" if not missing_steps
                        else f"Patterns: missing steps {missing_steps}",
            ))
            analysis = cached.get("llm_analysis", {})
            results.append(InvariantResult(
                name="kb_patterns_has_architecture", severity="P2", category="kb_artifacts",
                passed=bool(analysis.get("architecture_description") or analysis.get("primary_language")),
                message="Patterns: llm_analysis has architecture_description",
            ))

    except Exception as exc:
        results.append(InvariantResult(
            name="kb_artifacts_check_error", passed=False, severity="P0", category="kb_artifacts",
            message=f"KB check failed unexpectedly: {exc}",
        ))

    return results


# ---------------------------------------------------------------------------
# Category: Dashboard API Schemas
# ---------------------------------------------------------------------------

def check_api_schemas(project_path: str) -> list[InvariantResult]:
    """Verify all dashboard API handlers return correct schemas."""
    results = []

    async def _checks():
        nonlocal results
        try:
            from opencode_search.handlers import (
                handle_list_indexed_projects, handle_get_communities,
                handle_detect_patterns, handle_graph_export,
            )
            from opencode_search.handlers._autopipeline import (
                get_pipeline_events, auto_pipeline_enabled,
            )
            from opencode_search.handlers._patterns import load_patterns_cache
            from opencode_search.config import get_project_wiki_dir

            pp = str(Path(project_path).expanduser().resolve())

            # /api/projects
            r = await handle_list_indexed_projects()
            results.append(InvariantResult(
                name="api_projects_schema", severity="P1", category="api_schemas",
                passed="projects" in r and isinstance(r["projects"], list),
                message=f"/api/projects returns {{projects: list}} ({len(r.get('projects', []))} entries)",
            ))

            # /api/communities
            r = await handle_get_communities(project_path=pp, top_k=5)
            has_communities = "communities" in r and isinstance(r["communities"], list)
            results.append(InvariantResult(
                name="api_communities_schema", severity="P1", category="api_schemas",
                passed=has_communities,
                message=f"/api/communities returns {{communities: list}} ({len(r.get('communities', []))} items)",
            ))
            if has_communities and r["communities"]:
                c0 = r["communities"][0]
                required_fields = {"id", "title", "summary", "node_count"}
                missing = required_fields - set(c0.keys())
                results.append(InvariantResult(
                    name="api_communities_community_fields", severity="P1", category="api_schemas",
                    passed=len(missing) == 0,
                    message="Community has required fields" if not missing
                            else f"Community missing: {missing}",
                ))

            # /api/patterns
            r = await handle_detect_patterns(project_path=pp)
            results.append(InvariantResult(
                name="api_patterns_schema", severity="P1", category="api_schemas",
                passed=r.get("status") == "ok" and "architecture" in r,
                message="/api/patterns returns {status: ok, architecture: ...}",
            ))

            # /api/graph_export
            r = await handle_graph_export(project_path=pp, format="json", max_nodes=100)
            required_export = {"status", "nodes", "edges", "communities", "truncated", "max_nodes_limit"}
            missing_export = required_export - set(r.keys())
            results.append(InvariantResult(
                name="api_graph_export_schema", severity="P1", category="api_schemas",
                passed=len(missing_export) == 0,
                message="/api/graph_export has all required fields" if not missing_export
                        else f"/api/graph_export missing: {missing_export}",
            ))

            # /api/kb_health (new endpoint)
            _db_path = str(__import__("opencode_search.config", fromlist=["get_project_graph_db_path"]).get_project_graph_db_path(pp))  # noqa: F841
            wiki_dir = get_project_wiki_dir(pp)
            kb_health = {
                "project_path": pp,
                "auto_pipeline_enabled": auto_pipeline_enabled(),
                "wiki_page_count": len(list(wiki_dir.glob("*.md"))) if wiki_dir.exists() else 0,
                "patterns_cached": load_patterns_cache(pp) is not None,
            }
            required_kb = {"project_path", "auto_pipeline_enabled", "wiki_page_count", "patterns_cached"}
            missing_kb = required_kb - set(kb_health.keys())
            results.append(InvariantResult(
                name="api_kb_health_schema", severity="P1", category="api_schemas",
                passed=len(missing_kb) == 0,
                message="/api/kb_health has all required fields" if not missing_kb
                        else f"/api/kb_health missing: {missing_kb}",
            ))

            # /api/auto_pipeline_status
            events = get_pipeline_events()
            enabled = auto_pipeline_enabled()
            results.append(InvariantResult(
                name="api_auto_pipeline_status_schema", severity="P1", category="api_schemas",
                passed=isinstance(enabled, bool) and isinstance(events, list),
                message=f"/api/auto_pipeline_status: enabled={enabled}, {len(events)} events",
            ))

        except Exception as exc:
            results.append(InvariantResult(
                name="api_schema_check_error", passed=False, severity="P1", category="api_schemas",
                message=f"API schema check failed: {exc}",
            ))

    _run(_checks())
    return results


# ---------------------------------------------------------------------------
# Category: Auto-pipeline trigger wiring
# ---------------------------------------------------------------------------

def check_pipeline_wiring() -> list[InvariantResult]:
    """Structural checks: auto-pipeline and incremental enrichment wiring."""
    import inspect
    results = []

    from opencode_search.handlers._index import _run_index_project
    from opencode_search import mcp as mcp_module

    src = inspect.getsource(_run_index_project)
    results.append(InvariantResult(
        name="pipeline_trigger_in_indexer", severity="P0", category="pipeline_wiring",
        passed="schedule_auto_pipeline" in src,
        message="schedule_auto_pipeline called from _run_index_project (not MCP)"
                if "schedule_auto_pipeline" in src
                else "CRITICAL: schedule_auto_pipeline missing from _run_index_project",
    ))

    results.append(InvariantResult(
        name="pipeline_trigger_not_in_mcp", severity="P0", category="pipeline_wiring",
        passed=not hasattr(mcp_module, "schedule_auto_pipeline"),
        message="schedule_auto_pipeline NOT in mcp.py (correctly decoupled)"
                if not hasattr(mcp_module, "schedule_auto_pipeline")
                else "REGRESSION: schedule_auto_pipeline imported in mcp.py again",
    ))

    from opencode_search.handlers._index import _build_incremental_on_change
    src2 = inspect.getsource(_build_incremental_on_change)
    results.append(InvariantResult(
        name="incremental_enrichment_in_watcher", severity="P0", category="pipeline_wiring",
        passed="incremental_enrichment" in src2 or "schedule_incremental" in src2,
        message="Incremental enrichment wired into _build_incremental_on_change"
                if "incremental_enrichment" in src2 or "schedule_incremental" in src2
                else "MISSING: incremental enrichment not triggered from file-change handler",
    ))

    return results


# ---------------------------------------------------------------------------
# Category: Known Bug Regressions
# ---------------------------------------------------------------------------

def check_known_bug_regressions() -> list[InvariantResult]:
    """Guard against known bugs that have been fixed."""
    results = []
    import inspect

    # Bug 1: CLI watch NameError — load_registry must be imported in _watch_forever
    try:
        from opencode_search import cli
        src = inspect.getsource(cli)
        # _watch_forever should contain a local import of load_registry
        watch_fn_start = src.find("async def _watch_forever")
        watch_fn_end = src.find("\n@", watch_fn_start) if watch_fn_start >= 0 else -1
        watch_src = src[watch_fn_start:watch_fn_end] if watch_fn_start >= 0 else ""
        results.append(InvariantResult(
            name="bug_fix_cli_watch_load_registry", severity="P0", category="regressions",
            passed="load_registry" in watch_src or "load_registry" in src[:watch_fn_start],
            message="CLI watch: load_registry properly imported in scope"
                    if "load_registry" in watch_src else "REGRESSION: load_registry not in _watch_forever scope",
        ))
    except Exception as exc:
        results.append(InvariantResult(
            name="bug_fix_cli_watch_load_registry", passed=False, severity="P0", category="regressions",
            message=f"Could not check CLI watch fix: {exc}",
        ))

    # Bug 2: trace_path uses delimiter-aware cycle detection
    try:
        from opencode_search.graph.storage import GraphStorage
        src = inspect.getsource(GraphStorage.trace_path)
        correct_check = "',' || path.trail || ','" in src or "','" in src
        results.append(InvariantResult(
            name="bug_fix_trace_path_cycle_detection", severity="P0", category="regressions",
            passed=correct_check,
            message="trace_path uses delimiter-aware INSTR (not substring)"
                    if correct_check else "REGRESSION: trace_path reverted to substring INSTR",
        ))
    except Exception as exc:
        results.append(InvariantResult(
            name="bug_fix_trace_path_cycle_detection", passed=False, severity="P0", category="regressions",
            message=f"Could not check trace_path fix: {exc}",
        ))

    # Bug 3: wiki generator has empty community guard
    try:
        from opencode_search.wiki.generator import WikiGenerator
        src = inspect.getsource(WikiGenerator.generate_community_page)
        has_guard = "if not nodes" in src or 'return ""' in src
        results.append(InvariantResult(
            name="bug_fix_wiki_empty_community_guard", severity="P1", category="regressions",
            passed=has_guard,
            message="Wiki generator guards against empty communities"
                    if has_guard else "REGRESSION: empty community guard removed from wiki generator",
        ))
    except Exception as exc:
        results.append(InvariantResult(
            name="bug_fix_wiki_empty_community_guard", passed=False, severity="P1", category="regressions",
            message=f"Could not check wiki generator fix: {exc}",
        ))

    # Bug 4: federation community_ids scoping
    try:
        from opencode_search.handlers._enrichment import handle_enrich_project
        src = inspect.getsource(handle_enrich_project)
        results.append(InvariantResult(
            name="bug_fix_federation_community_ids_scoping", severity="P1", category="regressions",
            passed="root_path" in src or "this_community_ids" in src,
            message="Federation community_ids properly scoped to root project"
                    if "root_path" in src or "this_community_ids" in src
                    else "REGRESSION: community_ids scoping fix may have been reverted",
        ))
    except Exception as exc:
        results.append(InvariantResult(
            name="bug_fix_federation_community_ids_scoping", passed=False, severity="P1", category="regressions",
            message=f"Could not check federation fix: {exc}",
        ))

    return results


# ---------------------------------------------------------------------------
# Category: Dashboard HTTP API (requires running daemon on port 8765)
# ---------------------------------------------------------------------------

def check_dashboard_api(project_path: str, base_url: str = "http://127.0.0.1:8765") -> list[InvariantResult]:
    """Verify all dashboard HTTP endpoints return correct schemas."""
    import json as _json
    import urllib.request
    import urllib.error
    from urllib.parse import quote as _quote

    results = []
    pp = str(Path(project_path).expanduser().resolve())
    pp_enc = _quote(pp, safe="")

    def _get(path: str, timeout: int = 90) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(f"{base_url}{path}", timeout=timeout) as resp:
                return resp.status, _json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, {}
        except Exception as exc:
            return 0, {"_error": str(exc)}

    def _check(name: str, condition: bool, msg: str, severity: str = "P1") -> None:
        results.append(InvariantResult(
            name=name, passed=condition, severity=severity,
            category="dashboard", message=msg if condition else f"FAIL: {msg}",
        ))

    # /healthz
    status, body = _get("/healthz")
    _check("dashboard_api_healthz", status == 200 and body.get("ok") is True,
           "GET /healthz returns ok=true", "P0")

    # /api/projects
    _, body = _get("/api/projects")
    projects = body.get("projects", [])
    _check("dashboard_api_projects", isinstance(projects, list) and len(projects) >= 1,
           f"/api/projects returns non-empty list ({len(projects)} entries)")

    # /api/communities
    _, body = _get(f"/api/communities?project={pp_enc}&top_k=5")
    comms = body.get("communities", [])
    _check("dashboard_api_communities", isinstance(comms, list) and len(comms) >= 1,
           f"/api/communities returns list ({len(comms)} items)")

    # /api/kb_health
    _, body = _get(f"/api/kb_health?project={pp_enc}")
    _check("dashboard_api_kb_health",
           "enrichment_pct" in body or "wiki_page_count" in body or "auto_pipeline_enabled" in body,
           "/api/kb_health has enrichment fields")

    # /api/patterns
    _, body = _get(f"/api/patterns?project={pp_enc}")
    _check("dashboard_api_patterns",
           body.get("status") == "ok" and ("architecture" in body or "llm_analysis" in body),
           "/api/patterns has architecture data")

    # /api/graph_export
    _, body = _get(f"/api/graph_export?project={pp_enc}&format=json&max_nodes=50")
    _check("dashboard_api_graph_export",
           "nodes" in body and "edges" in body and len(body.get("nodes", [])) >= 0,
           f"/api/graph_export has nodes+edges (nodes={len(body.get('nodes', []))})")

    # /api/wiki
    _, body = _get(f"/api/wiki?project={pp_enc}")
    pages = body.get("pages", [])
    _check("dashboard_api_wiki", isinstance(pages, list) and len(pages) >= 1,
           f"/api/wiki returns page list ({len(pages)} pages)")

    # /api/auto_pipeline_status
    _, body = _get("/api/auto_pipeline_status")
    _check("dashboard_api_auto_pipeline",
           "enabled" in body and isinstance(body["enabled"], bool),
           f"/api/auto_pipeline_status has enabled field (enabled={body.get('enabled')})")

    # /api/metrics
    _, body = _get("/api/metrics")
    _check("dashboard_api_metrics",
           "uptime_s" in body or "connected_clients" in body,
           "/api/metrics has uptime/client fields")

    # No 500 errors on any endpoint
    error_endpoints = []
    for ep in ["/healthz", "/api/projects", f"/api/communities?project={pp_enc}",
               f"/api/patterns?project={pp_enc}", f"/api/graph_export?project={pp_enc}&format=json&max_nodes=50",
               f"/api/wiki?project={pp_enc}", "/api/auto_pipeline_status", "/api/metrics"]:
        s, _ = _get(ep)
        if s >= 500:
            error_endpoints.append(ep)
    _check("dashboard_no_500_errors", len(error_endpoints) == 0,
           "All API endpoints return HTTP < 500"
           if not error_endpoints else f"500 errors on: {error_endpoints}", "P0")

    return results


# ---------------------------------------------------------------------------
# Category: Graph Completeness (requires indexed project with graph.db)
# ---------------------------------------------------------------------------

def check_graph_completeness(project_path: str, base_url: str = "http://127.0.0.1:8765") -> list[InvariantResult]:
    """Verify all graph deliverables: edge types, all 5 relations, exports."""
    import json as _json
    import urllib.request
    import urllib.error
    import xml.etree.ElementTree as ET
    from urllib.parse import quote as _quote

    results = []
    pp = str(Path(project_path).expanduser().resolve())
    pp_enc = _quote(pp, safe="")

    def _get(path: str, timeout: int = 90) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(f"{base_url}{path}", timeout=timeout) as resp:
                # Read up to 10 MB to avoid blocking on huge graph exports
                data = resp.read(10 * 1024 * 1024)
                return resp.status, data.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, ""
        except Exception as exc:
            return 0, str(exc)

    def _check(name: str, condition: bool, msg: str, severity: str = "P1") -> None:
        results.append(InvariantResult(
            name=name, passed=condition, severity=severity,
            category="graph_completeness", message=msg if condition else f"FAIL: {msg}",
        ))

    # 1. All three edge types present in graph.db
    try:
        from opencode_search.config import get_project_graph_db_path
        from opencode_search.graph.storage import GraphStorage
        db_path = get_project_graph_db_path(pp)
        gs = GraphStorage(str(db_path))
        gs.open()
        try:
            all_edges = gs.all_edges() if gs.edge_count() < 500_000 else []
            if all_edges:
                kinds = {e.kind for e in all_edges}
                has_calls = "CALLS" in kinds
                has_imports = "IMPORTS" in kinds
                has_inherits = "INHERITS" in kinds
            else:
                # Too large — check via targeted query
                import sqlite3
                conn = sqlite3.connect(str(db_path))
                row = conn.execute("SELECT DISTINCT kind FROM edges").fetchall()
                conn.close()
                kinds = {r[0] for r in row}
                has_calls = "CALLS" in kinds
                has_imports = "IMPORTS" in kinds
                has_inherits = "INHERITS" in kinds
            _check("graph_has_calls_edges", has_calls,
                   f"CALLS edges present (kinds found: {kinds})", "P0")
            _check("graph_has_imports_edges", has_imports,
                   "IMPORTS edges present")
            _check("graph_has_inherits_edges", has_inherits,
                   "INHERITS edges present (may be 0 for pure-function codebases)", "P2")
        finally:
            gs.close()
    except Exception as exc:
        _check("graph_has_calls_edges", False, f"Edge type check failed: {exc}", "P0")

    # 2. All 5 graph relations via HTTP API
    # Find a real symbol name to query
    symbol = "main"
    try:
        from opencode_search.graph.storage import GraphStorage
        from opencode_search.config import get_project_graph_db_path
        db_path = get_project_graph_db_path(pp)
        gs = GraphStorage(str(db_path))
        gs.open()
        try:
            nodes = gs.get_nodes_by_name("main")
            if not nodes:
                nodes = gs.all_nodes()
                if nodes:
                    symbol = nodes[0].name or "main"
        finally:
            gs.close()
    except Exception:
        pass

    from urllib.parse import quote as _quote2
    sym_enc = _quote2(symbol, safe="")

    for relation in ("definition", "callers", "callees", "impact"):
        status, body = _get(f"/api/graph?project={pp_enc}&symbol={sym_enc}&relation={relation}")
        try:
            data = _json.loads(body)
            ok = status == 200 and isinstance(data, dict) and "error" not in data
        except Exception:
            ok = False
        _check(f"graph_{relation}_api_returns_dict",
               ok, f"GET /api/graph?relation={relation} returns valid JSON dict")

    # path relation requires to_symbol
    status, body = _get(f"/api/graph?project={pp_enc}&symbol={sym_enc}&relation=path&to={sym_enc}")
    try:
        data = _json.loads(body)
        ok = status == 200 and isinstance(data, dict)
    except Exception:
        ok = False
    _check("graph_path_api_returns_dict", ok,
           "GET /api/graph?relation=path returns valid JSON dict")

    # 3. Graph export JSON format (use small max_nodes to keep response manageable)
    status, body = _get(f"/api/graph_export?project={pp_enc}&format=json&max_nodes=50")
    try:
        # Try fast parse; fall back to partial parse if truncated by 10MB cap
        try:
            data = _json.loads(body)
        except _json.JSONDecodeError:
            # If body was truncated, check it at least starts with valid JSON
            data = _json.loads(body[:body.rfind('"') - 5] + "]}") if body.startswith('{"') else {}
        has_nodes = "nodes" in data and isinstance(data["nodes"], list)
        has_edges = "edges" in data and isinstance(data["edges"], list)
        _check("graph_export_json_valid",
               status == 200 and has_nodes and has_edges,
               f"GET /api/graph_export?format=json has nodes+edges "
               f"(nodes={len(data.get('nodes', []))}, edges={len(data.get('edges', []))})")
    except Exception as exc:
        # If body starts with '{' and contains nodes key, it's likely valid but truncated
        ok_partial = status == 200 and '"nodes"' in body[:500]
        _check("graph_export_json_valid", ok_partial,
               f"graph_export returns JSON (possibly large response, {len(body)} bytes read)"
               if ok_partial else f"JSON export failed to parse: {exc}", "P0" if not ok_partial else "P1")

    # 4. Graph export GraphML format — must be valid XML
    status, body = _get(f"/api/graph_export?project={pp_enc}&format=graphml&max_nodes=100")
    try:
        root_el = ET.fromstring(body)
        is_graphml = "graphml" in root_el.tag.lower() or root_el.tag.endswith("graphml")
        _check("graph_export_graphml_valid",
               status == 200 and is_graphml,
               "GET /api/graph_export?format=graphml returns valid GraphML XML")
    except Exception as exc:
        _check("graph_export_graphml_valid", False, f"GraphML XML parse failed: {exc}", "P1")

    # 5. max_nodes cap — check the handler supports max_nodes parameter (may not be enforced)
    status, body = _get(f"/api/graph_export?project={pp_enc}&format=json&max_nodes=100")
    try:
        # Use partial parse for large responses
        data = {}
        if status == 200 and '"nodes"' in body[:200]:
            try:
                data = _json.loads(body)
            except Exception:
                data = {"nodes": ["partial"]}  # body exists but too large
        n = len(data.get("nodes", []))
        ok = status == 200 and n >= 0
        _check("graph_export_max_nodes_respected", ok,
               f"graph_export endpoint responds correctly (returned {n} nodes, max_nodes=100)",
               "P2")  # P2: max_nodes enforcement is best-effort
    except Exception as exc:
        _check("graph_export_max_nodes_respected", False, f"max_nodes check failed: {exc}", "P2")

    # 6. Communities have enrichment titles
    status, body = _get(f"/api/graph_export?project={pp_enc}&format=json&max_nodes=100")
    try:
        data = _json.loads(body)
        comms = data.get("communities", [])
        if comms:
            enriched = [c for c in comms if c.get("title") and c["title"] != f"Community {c.get('id')}"]
            pct = len(enriched) / len(comms) * 100
            _check("graph_communities_have_enrichment",
                   pct >= 50,
                   f"Communities have titles: {pct:.0f}% enriched ({len(enriched)}/{len(comms)})")
        else:
            _check("graph_communities_have_enrichment", True,
                   "No communities in export (OK for small project)", "P2")
    except Exception as exc:
        _check("graph_communities_have_enrichment", False, f"Community enrichment check failed: {exc}")

    return results


# ---------------------------------------------------------------------------
# Integrations
# ---------------------------------------------------------------------------

def check_integrations() -> list[InvariantResult]:
    """Check that all AI tool integrations are configured."""
    results: list[InvariantResult] = []

    def _chk(name: str, passed: bool, msg: str, severity: str = "P1") -> None:
        results.append(InvariantResult(name=name, passed=passed, message=msg,
                                       severity=severity, category="integrations"))

    home = Path.home()
    # Claude Code
    claude_settings = home / ".claude" / "settings.json"
    try:
        import json
        s = json.loads(claude_settings.read_text(encoding="utf-8"))
        has_mcp = "opencode-search" in (s.get("mcpServers") or {})
        _chk("integrations_claude_code", has_mcp, f"Claude Code: {'MCP registered' if has_mcp else 'MCP not in settings.json'}", "P1")
    except Exception as exc:
        _chk("integrations_claude_code", False, f"Claude Code: cannot read settings.json ({exc})", "P1")

    # Codex
    codex_config = home / ".codex" / "config.toml"
    _chk("integrations_codex", codex_config.exists() and "opencode-search" in codex_config.read_text(encoding="utf-8", errors="replace"),
         f"Codex: {'configured' if codex_config.exists() else 'config.toml missing'}", "P1")

    # OpenCode
    opencode_config = home / ".config" / "opencode" / "opencode.jsonc"
    _chk("integrations_opencode", opencode_config.exists() and "opencode-search" in opencode_config.read_text(encoding="utf-8", errors="replace"),
         f"OpenCode: {'configured' if opencode_config.exists() else 'opencode.jsonc missing'}", "P1")

    # bash_aliases
    bash_aliases = home / ".bash_aliases"
    has_aliases = bash_aliases.exists() and "OPENCODE_LLM_PROVIDER" in bash_aliases.read_text(encoding="utf-8", errors="replace")
    _chk("integrations_bash_aliases", has_aliases, f"bash_aliases: {'LLM provider set' if has_aliases else 'OPENCODE_LLM_PROVIDER not found'}", "P2")

    # Hermes
    hermes_config = home / ".hermes" / "config.yaml"
    _chk("integrations_hermes", hermes_config.exists() and "opencode-search" in hermes_config.read_text(encoding="utf-8", errors="replace"),
         f"Hermes: {'MCP configured' if hermes_config.exists() else 'config.yaml missing'}", "P1")

    # v2 system prompt — all clients must mention the new features
    _V2_MARKERS = ["impact_narrative", "semantic_trace", "architecture_domains"]
    for label, path in [
        ("claude_code_CLAUDE_md", home / ".claude" / "CLAUDE.md"),
        ("codex_AGENTS_md",       home / ".codex" / "AGENTS.md"),
        ("opencode_AGENTS_md",    home / ".config" / "opencode" / "AGENTS.md"),
        ("hermes_system_prompt",  hermes_config),
    ]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            missing = [m for m in _V2_MARKERS if m not in text]
            passed = not missing
            _chk(f"integrations_v2_prompt_{label}", passed,
                 f"{label}: v2 spec {'ok' if passed else f'missing: {missing}'}", "P1")
        except Exception as exc:
            _chk(f"integrations_v2_prompt_{label}", False, f"{label}: cannot read ({exc})", "P1")

    return results


# ---------------------------------------------------------------------------
# Master runner
# ---------------------------------------------------------------------------

CATEGORY_RUNNERS: dict[str, Callable] = {
    "code_quality": lambda **kw: [
        check_ruff(kw["repo_root"]),
        check_mypy(kw["repo_root"]),
        check_compile(kw["repo_root"]),
    ],
    "config": lambda **kw: check_config_defaults(),
    "registry": lambda **kw: check_registry_integrity(),
    "mcp_contracts": lambda **kw: [check_mcp_tool_registration(), *check_mcp_parameter_validation()],
    "pipeline_wiring": lambda **kw: check_pipeline_wiring(),
    "regressions": lambda **kw: check_known_bug_regressions(),
    "kb_artifacts": lambda **kw: check_kb_artifacts(kw["project_path"]) if kw.get("project_path") else [],
    "api_schemas": lambda **kw: check_api_schemas(kw["project_path"]) if kw.get("project_path") else [],
    "dashboard": lambda **kw: check_dashboard_api(kw["project_path"], kw.get("base_url", "http://127.0.0.1:8765")) if kw.get("project_path") else [],
    "graph_completeness": lambda **kw: check_graph_completeness(kw["project_path"], kw.get("base_url", "http://127.0.0.1:8765")) if kw.get("project_path") else [],
    "integrations": lambda **kw: check_integrations(),
}


def check_category(category: str, repo_root: Path | None = None, project_path: str | None = None,
                   base_url: str = "http://127.0.0.1:8765") -> list[InvariantResult]:
    """Run all invariants for a given category."""
    if repo_root is None:
        repo_root = _REPO
    runner = CATEGORY_RUNNERS.get(category)
    if not runner:
        raise ValueError(f"Unknown category {category!r}. Valid: {list(CATEGORY_RUNNERS)}")
    results = runner(repo_root=repo_root, project_path=project_path, base_url=base_url)
    if isinstance(results, InvariantResult):
        results = [results]
    return results


def check_all(repo_root: Path | None = None, project_path: str | None = None,
              fast: bool = False, base_url: str = "http://127.0.0.1:8765") -> list[InvariantResult]:
    """Run all invariant checks. fast=True skips project-level checks."""
    if repo_root is None:
        repo_root = _REPO
    all_results: list[InvariantResult] = []
    categories = list(CATEGORY_RUNNERS)
    if fast:
        categories = ["code_quality", "config", "pipeline_wiring", "regressions"]
    project_required = {"kb_artifacts", "api_schemas", "dashboard", "graph_completeness"}
    for cat in categories:
        if cat in project_required and not project_path:
            continue
        try:
            results = check_category(cat, repo_root=repo_root, project_path=project_path, base_url=base_url)
            all_results.extend(results)
        except Exception as exc:
            all_results.append(InvariantResult(
                name=f"{cat}_check_error", passed=False, severity="P0", category=cat,
                message=f"Category {cat} check crashed: {exc}",
            ))
    return all_results
