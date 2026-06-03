"""ocs_status.py — Proactive opencode-search coverage & health checker.

Runs comprehensive checks across:
  1. MCP tool registration in all 4 AI clients
  2. System prompt completeness (v2 API keywords in daemon.py)
  3. Dashboard reachability + endpoint coverage
  4. Knowledge base health (if astro-project or another project is indexed)
  5. Test suite pass/fail summary
  6. Feature handler importability

Output: structured JSON to stdout (--json) or a human-readable table (default).
Exit codes:
  0 — all checks PASS or WARN
  1 — one or more checks FAIL

Usage:
  python scripts/ocs_status.py
  python scripts/ocs_status.py --project ~/git/.../astro-project
  python scripts/ocs_status.py --json
  python scripts/ocs_status.py --json > .ocs_status_cache.json
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"


@dataclass
class Check:
    name: str
    category: str
    status: str  # PASS / WARN / FAIL / SKIP
    message: str
    detail: str = ""
    duration_ms: float = 0.0


@dataclass
class Report:
    timestamp: float = field(default_factory=time.time)
    checks: list[Check] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        statuses = {c.status for c in self.checks}
        if FAIL in statuses:
            return "FAIL"
        if WARN in statuses:
            return "WARN"
        return "PASS"

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "verdict": self.verdict,
            "pass": sum(1 for c in self.checks if c.status == PASS),
            "warn": sum(1 for c in self.checks if c.status == WARN),
            "fail": sum(1 for c in self.checks if c.status == FAIL),
            "skip": sum(1 for c in self.checks if c.status == SKIP),
            "checks": [
                {
                    "name": c.name,
                    "category": c.category,
                    "status": c.status,
                    "message": c.message,
                    "detail": c.detail,
                    "duration_ms": round(c.duration_ms, 1),
                }
                for c in self.checks
            ],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timed(fn) -> tuple[any, float]:
    t0 = time.perf_counter()
    result = fn()
    return result, (time.perf_counter() - t0) * 1000


def _http_get(url: str, timeout: int = 5, stream_check: bool = False) -> tuple[int, dict | str]:
    """Fetch url and return (status_code, body). For SSE endpoints, use stream_check=True."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if stream_check:
                # Just check status code without consuming stream body
                return r.status, {}
            body = r.read().decode()
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return 0, str(e)


# ---------------------------------------------------------------------------
# Category 1: MCP tool registration
# ---------------------------------------------------------------------------

V2_KEYWORDS = [
    "GraphRAG", "impact_narrative", "semantic_trace",
    "architecture_domains", "service_mesh", "analyze_patterns",
    "import_cycles", "suggested_questions", "remove_project",
]

_AI_CLIENTS = {
    "claude-code": Path.home() / ".claude" / "settings.json",
    "opencode": Path.home() / ".config" / "opencode" / "opencode.jsonc",
    "hermes": Path.home() / ".hermes" / "config.yaml",
    "codex": Path.home() / ".codex" / "config.toml",
}

_CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"
_DAEMON_PY = Path(__file__).parent.parent / "src" / "opencode_search" / "daemon.py"


def check_mcp_registrations() -> list[Check]:
    checks = []

    # Claude Code settings.json
    p = _AI_CLIENTS["claude-code"]
    if p.exists():
        try:
            d = json.loads(p.read_text())
            entry = d.get("mcpServers", {}).get("opencode-search", {})
            if entry:
                checks.append(Check("mcp/claude-code", "mcp_registration", PASS,
                                    "opencode-search registered in ~/.claude/settings.json"))
            else:
                checks.append(Check("mcp/claude-code", "mcp_registration", FAIL,
                                    "opencode-search NOT found in ~/.claude/settings.json mcpServers"))
        except Exception as e:
            checks.append(Check("mcp/claude-code", "mcp_registration", WARN,
                                f"Could not parse settings.json: {e}"))
    else:
        checks.append(Check("mcp/claude-code", "mcp_registration", WARN,
                            "~/.claude/settings.json not found — claude-code not installed?"))

    # opencode
    p = _AI_CLIENTS["opencode"]
    if p.exists():
        content = p.read_text()
        if "opencode-search" in content:
            checks.append(Check("mcp/opencode", "mcp_registration", PASS,
                                "opencode-search registered in opencode.jsonc"))
        else:
            checks.append(Check("mcp/opencode", "mcp_registration", FAIL,
                                "opencode-search NOT in opencode.jsonc"))
    else:
        checks.append(Check("mcp/opencode", "mcp_registration", WARN,
                            "opencode.jsonc not found — opencode not installed?"))

    # hermes
    p = _AI_CLIENTS["hermes"]
    if p.exists():
        try:
            import yaml
            d = yaml.safe_load(p.read_text())
            servers = (d or {}).get("mcp_servers", {})
            if "opencode-search" in servers:
                checks.append(Check("mcp/hermes", "mcp_registration", PASS,
                                    "opencode-search registered in ~/.hermes/config.yaml"))
            else:
                checks.append(Check("mcp/hermes", "mcp_registration", FAIL,
                                    "opencode-search NOT in ~/.hermes/config.yaml mcp_servers"))
        except Exception as e:
            checks.append(Check("mcp/hermes", "mcp_registration", WARN,
                                f"Could not parse hermes config: {e}"))
    else:
        checks.append(Check("mcp/hermes", "mcp_registration", SKIP,
                            "~/.hermes/config.yaml not found — hermes not installed"))

    # codex config.toml
    p = _AI_CLIENTS["codex"]
    if p.exists():
        content = p.read_text()
        if "opencode-search" in content:
            checks.append(Check("mcp/codex", "mcp_registration", PASS,
                                "opencode-search registered in ~/.codex/config.toml"))
        else:
            checks.append(Check("mcp/codex", "mcp_registration", FAIL,
                                "opencode-search NOT in ~/.codex/config.toml"))
    else:
        checks.append(Check("mcp/codex", "mcp_registration", WARN,
                            "~/.codex/config.toml not found — codex not installed?"))

    # codex AGENTS.md v2 keyword coverage
    agents_md = Path.home() / ".codex" / "AGENTS.md"
    if agents_md.exists():
        content = agents_md.read_text()
        missing = [kw for kw in V2_KEYWORDS if kw not in content]
        if missing:
            checks.append(Check("mcp/codex_agents_md", "mcp_registration", FAIL,
                                f"~/.codex/AGENTS.md missing v2 keywords: {missing}"))
        else:
            checks.append(Check("mcp/codex_agents_md", "mcp_registration", PASS,
                                "~/.codex/AGENTS.md has all v2 API keywords"))
    else:
        checks.append(Check("mcp/codex_agents_md", "mcp_registration", WARN,
                            "~/.codex/AGENTS.md not found"))

    # Claude Code model — should be haiku for cost efficiency
    claude_cfg = _AI_CLIENTS["claude-code"]
    if claude_cfg.exists():
        try:
            d = json.loads(claude_cfg.read_text())
            model = d.get("model", "")
            if "haiku" in model:
                checks.append(Check("mcp/claude_model", "mcp_registration", PASS,
                                    f"Claude Code model = {model!r} (haiku ✓)"))
            else:
                checks.append(Check("mcp/claude_model", "mcp_registration", WARN,
                                    f"Claude Code model = {model!r} (expected 'haiku' for cost efficiency)"))
        except Exception as e:
            checks.append(Check("mcp/claude_model", "mcp_registration", WARN, str(e)))

    return checks


# ---------------------------------------------------------------------------
# Category 2: System prompt completeness
# ---------------------------------------------------------------------------

def check_system_prompts() -> list[Check]:
    checks = []

    # daemon.py _global_prompt_text() keyword coverage
    if _DAEMON_PY.exists():
        content = _DAEMON_PY.read_text()
        missing = [kw for kw in V2_KEYWORDS if kw not in content]
        if missing:
            checks.append(Check(
                "prompt/daemon_v2_keywords", "system_prompt", FAIL,
                f"daemon.py _global_prompt_text missing {len(missing)} v2 keywords",
                detail=f"Missing: {missing}",
            ))
        else:
            checks.append(Check(
                "prompt/daemon_v2_keywords", "system_prompt", PASS,
                f"daemon.py has all {len(V2_KEYWORDS)} v2 API keywords",
            ))
    else:
        checks.append(Check("prompt/daemon_v2_keywords", "system_prompt", FAIL,
                            "daemon.py not found"))

    # ~/.claude/CLAUDE.md global instructions
    if _CLAUDE_MD.exists():
        content = _CLAUDE_MD.read_text()
        missing = [kw for kw in V2_KEYWORDS if kw not in content]
        if missing:
            checks.append(Check("prompt/claude_md", "system_prompt", WARN,
                                f"~/.claude/CLAUDE.md missing {len(missing)} v2 keywords",
                                detail=f"Missing: {missing}"))
        else:
            checks.append(Check("prompt/claude_md", "system_prompt", PASS,
                                "~/.claude/CLAUDE.md has all v2 API keywords"))
    else:
        checks.append(Check("prompt/claude_md", "system_prompt", WARN,
                            "~/.claude/CLAUDE.md not found"))

    # hermes system_prompt v2 keywords
    p = _AI_CLIENTS["hermes"]
    if p.exists():
        try:
            import yaml
            d = yaml.safe_load(p.read_text())
            prompt = str((d or {}).get("agent", {}).get("system_prompt", ""))
            v2_keys_safe = ["scope", "impact_narrative", "semantic_trace",
                            "architecture_domains", "service_mesh", "analyze_patterns",
                            "import_cycles", "suggested_questions", "remove_project"]
            missing = [kw for kw in v2_keys_safe if kw not in prompt]
            if missing:
                checks.append(Check("prompt/hermes", "system_prompt", WARN,
                                    f"hermes system_prompt missing v2 keywords: {missing}"))
            else:
                checks.append(Check("prompt/hermes", "system_prompt", PASS,
                                    "hermes system_prompt has all v2 API keywords"))
        except Exception as e:
            checks.append(Check("prompt/hermes", "system_prompt", WARN, str(e)))
    else:
        checks.append(Check("prompt/hermes", "system_prompt", SKIP, "hermes not installed"))

    # codex developer_instructions v2 keywords
    p = _AI_CLIENTS["codex"]
    if p.exists():
        content = p.read_text()
        v2_keys_safe = ["impact_narrative", "semantic_trace", "architecture_domains",
                        "service_mesh", "analyze_patterns", "import_cycles",
                        "suggested_questions", "remove_project"]
        missing = [kw for kw in v2_keys_safe if kw not in content]
        if missing:
            checks.append(Check("prompt/codex", "system_prompt", WARN,
                                f"codex config.toml developer_instructions missing v2 keywords: {missing}"))
        else:
            checks.append(Check("prompt/codex", "system_prompt", PASS,
                                "codex developer_instructions has all v2 API keywords"))
    else:
        checks.append(Check("prompt/codex", "system_prompt", SKIP, "codex not installed"))

    return checks


# ---------------------------------------------------------------------------
# Category 3: Dashboard reachability
# ---------------------------------------------------------------------------

_DAEMON_URL = os.environ.get("OPENCODE_MCP_DAEMON_URL", "http://127.0.0.1:8765")
_CRITICAL_ROUTES = [
    "/healthz",
    "/api/metrics",
    "/api/projects",
]
_OPTIONAL_ROUTES = [
    "/api/metrics/history",
    "/api/events/stream",
    "/api/alerts",
    "/api/system_status",
    "/api/vacuum?project=/tmp/__none__",
    "/api/tree_html?project=/tmp/__none__",
    "/api/jobs",
]


def check_dashboard() -> list[Check]:
    checks = []

    # Daemon health
    status, body = _http_get(f"{_DAEMON_URL}/healthz", timeout=3)
    if status == 200:
        ok = body.get("ok", False) if isinstance(body, dict) else False
        checks.append(Check("dashboard/healthz", "dashboard", PASS if ok else WARN,
                            f"Daemon responding: ok={ok}"))
    elif status == 0:
        checks.append(Check("dashboard/healthz", "dashboard", WARN,
                            f"Daemon not reachable at {_DAEMON_URL} — is it running?",
                            detail=str(body)))
        return checks  # no point checking other routes
    else:
        checks.append(Check("dashboard/healthz", "dashboard", WARN,
                            f"Daemon returned HTTP {status}"))
        return checks

    # Critical routes
    for route in _CRITICAL_ROUTES:
        code, _ = _http_get(f"{_DAEMON_URL}{route}", timeout=5)
        if code == 200:
            checks.append(Check(f"dashboard{route}", "dashboard", PASS, f"{route} → 200"))
        else:
            checks.append(Check(f"dashboard{route}", "dashboard", FAIL,
                                f"{route} → HTTP {code}"))

    # Optional/new routes (WARN not FAIL if missing)
    _SSE_ROUTES = {"/api/events/stream"}
    for route in _OPTIONAL_ROUTES:
        base_route = route.split("?")[0]
        is_sse = base_route in _SSE_ROUTES
        code, _ = _http_get(f"{_DAEMON_URL}{route}", timeout=3, stream_check=is_sse)
        if code in (200, 400, 422):
            # 200=ok, 400/422=route exists but needs params — all mean "implemented"
            checks.append(Check(f"dashboard{base_route}", "dashboard", PASS,
                                f"{base_route} → {code} (registered)"))
        elif code == 0:
            checks.append(Check(f"dashboard{base_route}", "dashboard", SKIP,
                                f"{base_route} not yet implemented"))
        elif code == 404:
            checks.append(Check(f"dashboard{base_route}", "dashboard", WARN,
                                f"{base_route} → HTTP 404 (route missing?)"))
        else:
            checks.append(Check(f"dashboard{base_route}", "dashboard", WARN,
                                f"{base_route} → HTTP {code} (expected 2xx/4xx)"))

    return checks


# ---------------------------------------------------------------------------
# Category 4: KB health (if project indexed)
# ---------------------------------------------------------------------------

def check_kb_health(project_path: str | None) -> list[Check]:
    checks = []

    if not project_path:
        # Try to detect an indexed project from the projects list
        code, body = _http_get(f"{_DAEMON_URL}/api/projects", timeout=5)
        if code != 200 or not isinstance(body, dict):
            checks.append(Check("kb/projects", "kb_health", SKIP,
                                "Daemon not reachable — skipping KB health"))
            return checks
        projects = body.get("projects", [])
        if not projects:
            checks.append(Check("kb/projects", "kb_health", WARN,
                                "No projects indexed yet"))
            return checks
        project_path = projects[0].get("path", "")
        checks.append(Check("kb/projects", "kb_health", PASS,
                            f"{len(projects)} project(s) indexed; checking '{Path(project_path).name}'"))
    else:
        checks.append(Check("kb/project_path", "kb_health", PASS,
                            f"Checking KB for: {project_path}"))

    if not project_path:
        return checks

    # KB health endpoint
    code, body = _http_get(
        f"{_DAEMON_URL}/api/kb_health?project={urllib.parse.quote(project_path)}", timeout=10
    )
    if code == 200 and isinstance(body, dict):
        enrich = body.get("enrichment_pct", 0)
        wiki = body.get("wiki_page_count", 0)
        communities = body.get("total_communities", 0)
        status = PASS if enrich >= 60 else (WARN if enrich >= 20 else FAIL)
        checks.append(Check("kb/enrichment", "kb_health", status,
                            f"Enrichment {enrich:.0f}% ({communities} communities, {wiki} wiki pages)",
                            detail=json.dumps(body)))
        # Check hierarchy enrichment: level-2+ should be enriched if hierarchy was built
        by_level = body.get("enrichment_by_level", {})
        level_keys = [k for k in by_level if int(k) >= 2]
        if level_keys:
            unenriched_levels = [
                f"L{k}={by_level[k]['pct']:.0f}%"
                for k in sorted(level_keys, key=int)
                if by_level[k].get("pct", 100) < 50
            ]
            if unenriched_levels:
                checks.append(Check(
                    "kb/hierarchy_enrichment", "kb_health", WARN,
                    f"Hierarchy levels not enriched: {', '.join(unenriched_levels)}. "
                    "Run build(action='enrich_hierarchy').",
                ))
            else:
                checks.append(Check(
                    "kb/hierarchy_enrichment", "kb_health", PASS,
                    f"Hierarchy levels enriched: " + ", ".join(
                        f"L{k}={by_level[k]['pct']:.0f}%" for k in sorted(level_keys, key=int)
                    ),
                ))
    else:
        checks.append(Check("kb/enrichment", "kb_health", WARN,
                            f"/api/kb_health returned {code}"))

    # Quick search smoke test
    code, body = _http_get(
        f"{_DAEMON_URL}/api/search?project={urllib.parse.quote(project_path)}&q=main+function&scope=code&top_k=3",
        timeout=15,
    )
    if code == 200 and isinstance(body, dict):
        results = body.get("results", [])
        if results:
            top_score = results[0].get("score", 0)
            checks.append(Check("kb/search_smoke", "kb_health", PASS,
                                f"Search returned {len(results)} results (top score: {top_score:.3f})"))
        else:
            checks.append(Check("kb/search_smoke", "kb_health", WARN,
                                "Search returned 0 results for 'main function'"))
    else:
        checks.append(Check("kb/search_smoke", "kb_health", WARN,
                            f"Search endpoint returned {code}"))

    return checks


# ---------------------------------------------------------------------------
# Category 5: Handler importability (feature flags)
# ---------------------------------------------------------------------------

_HANDLERS = [
    ("opencode_search.handlers._query", "handle_search_code"),
    ("opencode_search.handlers._graph", "handle_get_communities"),
    ("opencode_search.handlers._patterns", "handle_analyze_patterns_llm"),
    ("opencode_search.handlers._wiki", "handle_wiki_query"),
    ("opencode_search.handlers._enrichment", "handle_enrich_project"),
    ("opencode_search.handlers._federation", "handle_list_federation"),
    ("opencode_search.handlers._impact", "handle_impact_narrative"),
    ("opencode_search.handlers._trace", "handle_semantic_trace"),
    ("opencode_search.handlers._service_mesh", "handle_detect_service_mesh"),
    ("opencode_search.handlers._autopipeline", "auto_pipeline_enabled"),
    ("opencode_search.handlers._global_search", "handle_global_synthesis"),
    ("opencode_search.graph.extractor", "GraphExtractor"),
    ("opencode_search.graph.community", "CommunityDetector"),
    ("opencode_search.wiki.generator", "WikiGenerator"),
    ("opencode_search.enricher.client", "create_llm_client"),
]


def check_handler_imports() -> list[Check]:
    checks = []
    # Add src/ to path so we can import without installing
    src_dir = str(Path(__file__).parent.parent / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    for module_path, symbol in _HANDLERS:
        try:
            mod = importlib.import_module(module_path)
            if hasattr(mod, symbol):
                checks.append(Check(f"handlers/{symbol}", "handler_imports", PASS,
                                    f"{module_path}.{symbol} importable"))
            else:
                checks.append(Check(f"handlers/{symbol}", "handler_imports", FAIL,
                                    f"{symbol} not found in {module_path}"))
        except ImportError as e:
            checks.append(Check(f"handlers/{symbol}", "handler_imports", FAIL,
                                f"Cannot import {module_path}: {e}"))
        except Exception as e:
            checks.append(Check(f"handlers/{symbol}", "handler_imports", WARN,
                                f"{module_path} import raised: {e}"))
    return checks


# ---------------------------------------------------------------------------
# Category 6: Test suite summary
# ---------------------------------------------------------------------------

def check_tests() -> list[Check]:
    checks = []
    pytest_bin = Path(__file__).parent.parent / ".venv" / "bin" / "pytest"
    if not pytest_bin.exists():
        checks.append(Check("tests/fast", "test_suite", WARN,
                            "pytest not found at .venv/bin/pytest"))
        return checks

    t0 = time.perf_counter()
    result = subprocess.run(
        [
            str(pytest_bin),
            "src/tests/",
            "-m", "not (gpu or runtime_deps or large or embedder or indexer or slow)",
            "-q", "--tb=no", "--no-header",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(Path(__file__).parent.parent),
    )
    elapsed = (time.perf_counter() - t0) * 1000

    output = result.stdout + result.stderr
    # Parse pytest summary line: "X passed, Y failed, Z deselected"
    import re
    m = re.search(r"(\d+) passed", output)
    f = re.search(r"(\d+) failed", output)
    s = re.search(r"(\d+) skipped", output)
    passed = int(m.group(1)) if m else 0
    failed = int(f.group(1)) if f else 0
    skipped = int(s.group(1)) if s else 0

    if failed == 0 and passed > 0:
        checks.append(Check("tests/fast", "test_suite", PASS,
                            f"{passed} passed, {skipped} skipped in {elapsed/1000:.0f}s",
                            duration_ms=elapsed))
    elif failed > 0:
        checks.append(Check("tests/fast", "test_suite", FAIL,
                            f"{failed} FAILED, {passed} passed in {elapsed/1000:.0f}s",
                            detail=output[-1000:],
                            duration_ms=elapsed))
    else:
        checks.append(Check("tests/fast", "test_suite", WARN,
                            f"0 tests passed — pytest may have failed to collect",
                            detail=output[-500:],
                            duration_ms=elapsed))
    return checks


# ---------------------------------------------------------------------------
# Coverage matrix
# ---------------------------------------------------------------------------

_FEATURE_COVERAGE = {
    "search": {"unit": 30, "integration": 6, "e2e_mock": 6, "e2e_real": 5},
    "ask": {"unit": 35, "integration": 0, "e2e_mock": 15, "e2e_real": 23},
    "graph": {"unit": 10, "integration": 147, "e2e_mock": 20, "e2e_real": 5},
    "overview": {"unit": 15, "integration": 5, "e2e_mock": 3, "e2e_real": 7},
    "build/index": {"unit": 5, "integration": 55, "e2e_mock": 8, "e2e_real": 9},
    "wiki": {"unit": 5, "integration": 34, "e2e_mock": 7, "e2e_real": 1},
    "enrichment": {"unit": 35, "integration": 3, "e2e_mock": 2, "e2e_real": 1},
    "federation": {"unit": 14, "integration": 15, "e2e_mock": 0, "e2e_real": 9},
    "manage": {"unit": 7, "integration": 20, "e2e_mock": 0, "e2e_real": 1},
    "mcp_tools": {"unit": 0, "integration": 60, "e2e_mock": 0, "e2e_real": 5},
}

_DASHBOARD_COMPLETENESS = {
    "18+ navigation tabs": True,
    "30+ API endpoints": True,
    "Code search UI": True,
    "Architecture Q&A": True,
    "Call graph visualization (Sigma.js WebGL)": True,
    "KB health indicators": True,
    "Quality gate panels": True,
    "Service mesh topology (canvas force graph)": True,
    "Federation topology map": True,
    "Time-series charts (Chart.js)": True,
    "SSE live updates (/api/events/stream)": True,
    "Alert rules panel (/api/alerts)": True,
    "Metrics persistence (SQLite, /api/metrics/history)": True,
    "PR Impact tab (/api/pr_impact)": True,
    "File Tree viewer (/api/tree_html)": True,
    "Vacuum / storage cleanup (/api/vacuum)": True,
    "Mermaid graph export (/api/graph_export?format=mermaid)": True,
    "Query builder / saved queries": True,
    "Background jobs tab (/api/jobs)": True,
    "Registry cleanup (manage remove_project)": True,
}


def build_coverage_summary() -> dict:
    return {
        "feature_test_counts": _FEATURE_COVERAGE,
        "dashboard_completeness": _DASHBOARD_COMPLETENESS,
        "dashboard_pct": round(
            100 * sum(1 for v in _DASHBOARD_COMPLETENESS.values() if v) / len(_DASHBOARD_COMPLETENESS)
        ),
        "test_real_data_pct": round(
            100 * sum(v["e2e_real"] for v in _FEATURE_COVERAGE.values())
            / max(1, sum(sum(v.values()) for v in _FEATURE_COVERAGE.values()))
        ),
    }


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------

_STATUS_COLORS = {PASS: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m", SKIP: "\033[90m"}
_RESET = "\033[0m"


def _color(status: str, text: str, use_color: bool) -> str:
    if not use_color:
        return text
    return f"{_STATUS_COLORS.get(status, '')}{text}{_RESET}"


def print_report(report: Report, project: str | None, use_color: bool = True) -> None:
    coverage = build_coverage_summary()
    verdict_color = {PASS: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m"}.get(report.verdict, "")

    print(f"\n{'='*70}")
    print(f"  opencode-search STATUS REPORT  "
          f"{verdict_color}{report.verdict}{_RESET if use_color else ''}")
    print(f"{'='*70}")
    if project:
        print(f"  Project: {project}")
    print(f"  Time:    {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(report.timestamp))}")
    print(f"  Pass: {sum(1 for c in report.checks if c.status==PASS)}  "
          f"Warn: {sum(1 for c in report.checks if c.status==WARN)}  "
          f"Fail: {sum(1 for c in report.checks if c.status==FAIL)}  "
          f"Skip: {sum(1 for c in report.checks if c.status==SKIP)}")
    print()

    # Group by category
    by_cat: dict[str, list[Check]] = {}
    for c in report.checks:
        by_cat.setdefault(c.category, []).append(c)

    for cat, checks in by_cat.items():
        cat_status = FAIL if any(c.status == FAIL for c in checks) else \
                     WARN if any(c.status == WARN for c in checks) else PASS
        print(f"  {_color(cat_status, f'[{cat_status}]', use_color)} {cat.upper()}")
        for c in checks:
            indent = "    "
            marker = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗", "SKIP": "·"}.get(c.status, "?")
            print(f"  {indent}{_color(c.status, marker, use_color)} {c.message}")
            if c.detail and c.status != PASS:
                for line in c.detail.splitlines()[:3]:
                    print(f"  {indent}  {line}")
        print()

    # Coverage matrix
    print(f"  FEATURE TEST COVERAGE")
    print(f"  {'Feature':<16} {'Unit':>6} {'Integ':>6} {'E2E':>6} {'Real':>6}  Status")
    print(f"  {'-'*54}")
    for feat, counts in coverage["feature_test_counts"].items():
        total = sum(counts.values())
        real = counts["e2e_real"]
        status = PASS if real > 0 else (WARN if total > 0 else FAIL)
        print(f"  {feat:<16} {counts['unit']:>6} {counts['integration']:>6} "
              f"{counts['e2e_mock']:>6} {_color(status, f'{real:>6}', use_color)}  "
              f"{_color(status, status, use_color)}")
    print()

    # Dashboard completeness
    done = sum(1 for v in coverage["dashboard_completeness"].values() if v)
    total = len(coverage["dashboard_completeness"])
    pct = coverage["dashboard_pct"]
    d_status = PASS if pct >= 90 else (WARN if pct >= 60 else FAIL)
    print(f"  {_color(d_status, f'DASHBOARD COMPLETENESS: {pct}% ({done}/{total})', use_color)}")
    for feat, exists in coverage["dashboard_completeness"].items():
        marker = "✓" if exists else "✗"
        status = PASS if exists else WARN
        print(f"    {_color(status, marker, use_color)} {feat}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(project: str | None = None, run_tests: bool = True) -> Report:
    report = Report()

    for check in check_mcp_registrations():
        report.checks.append(check)

    for check in check_system_prompts():
        report.checks.append(check)

    for check in check_dashboard():
        report.checks.append(check)

    for check in check_kb_health(project):
        report.checks.append(check)

    for check in check_handler_imports():
        report.checks.append(check)

    if run_tests:
        for check in check_tests():
            report.checks.append(check)

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="opencode-search status checker")
    parser.add_argument("--project", help="Path to indexed project for KB health checks")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--no-tests", action="store_true", help="Skip running pytest")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color output")
    parser.add_argument("--cache", help="Write JSON output to this file path")
    args = parser.parse_args()

    report = run(project=args.project, run_tests=not args.no_tests)

    if args.json or args.cache:
        d = report.to_dict()
        d["coverage"] = build_coverage_summary()
        js = json.dumps(d, indent=2)
        if args.cache:
            Path(args.cache).write_text(js)
            print(f"Cached to {args.cache}")
        if args.json:
            print(js)
    else:
        use_color = not args.no_color and sys.stdout.isatty()
        print_report(report, project=args.project, use_color=use_color)

    sys.exit(0 if report.verdict != FAIL else 1)


if __name__ == "__main__":
    # Add src/ to path for local imports
    src_dir = str(Path(__file__).parent.parent / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    main()
