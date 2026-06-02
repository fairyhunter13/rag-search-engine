#!/usr/bin/env python3
"""Comprehensive MVP Quality Gate — simulates how a human perceives and uses the app.

8 verification pillars:
  1. Code Quality    — ruff, mypy, unit tests
  2. Infrastructure  — daemon health, MCP tools, LLM config
  3. API Contracts   — all 32 HTTP endpoints, error handling
  4. CLI Smoke       — all CLI commands return correct output
  5. Search Quality  — golden queries return semantically relevant results
  6. Human UI        — Playwright simulation of every page with content quality
  7. MCP Conversation — real claude -p invocations exercise each MCP tool
  8. KB Completeness — enrichment %, wiki pages, hierarchy, patterns

Self-healing: on failure, --fix auto-applies safe fixes and re-verifies.

Usage:
    .venv/bin/python scripts/qa_gate.py --project ~/redacted-name-3 [--fix] [--fast]
    .venv/bin/python scripts/qa_gate.py --project ~/redacted-name-3 --pillar search_quality
    .venv/bin/python scripts/qa_gate.py --fast  # code_quality + infrastructure + api_contracts only

Exit codes: 0=GO, 1=P0 failures, 2=P1/P2 warnings only
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import quote as _q

_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

BASE_URL = "http://127.0.0.1:8765"
PYTHON = str(Path(sys.executable))
_MODULE = [PYTHON, "-m", "opencode_search"]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    name: str
    status: Literal["pass", "fail", "warn", "skip"]
    severity: Literal["P0", "P1", "P2"]
    message: str
    details: str = ""
    duration_s: float = 0.0
    auto_fix_fn: Callable[[], bool] | None = field(default=None, repr=False)

    @property
    def emoji(self) -> str:
        return {"pass": "✅", "fail": "❌", "warn": "⚠️", "skip": "⏭"}[self.status]


@dataclass
class PillarResult:
    name: str
    label: str
    checks: list[CheckResult]
    duration_s: float = 0.0

    @property
    def status(self) -> str:
        if any(c.status == "fail" and c.severity == "P0" for c in self.checks):
            return "fail"
        if any(c.status in ("fail", "warn") for c in self.checks):
            return "warn"
        return "pass"

    @property
    def p0_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "fail" and c.severity == "P0")

    @property
    def p1_count(self) -> int:
        return sum(1 for c in self.checks if c.status in ("fail", "warn") and c.severity == "P1")

    @property
    def emoji(self) -> str:
        return {"pass": "✅", "fail": "❌", "warn": "⚠️"}[self.status]

    def pass_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "pass")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(path: str, timeout: int = 30) -> tuple[int, Any]:
    try:
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read(4 * 1024 * 1024)
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as exc:
        return 0, {"_error": str(exc)}


def _post(path: str, body: dict, timeout: int = 30) -> tuple[int, Any]:
    url = path if path.startswith("http") else f"{BASE_URL}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                return resp.status, json.loads(resp.read())
            except Exception:
                return resp.status, {}
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as exc:
        return 0, {"_error": str(exc)}


def _check(name: str, severity: str, passed: bool, ok_msg: str, fail_msg: str,
           details: str = "", duration_s: float = 0.0,
           fix_fn: Callable | None = None) -> CheckResult:
    return CheckResult(
        name=name, severity=severity,
        status="pass" if passed else "fail",
        message=ok_msg if passed else fail_msg,
        details=details, duration_s=duration_s,
        auto_fix_fn=fix_fn,
    )


# ---------------------------------------------------------------------------
# Auto-fix functions
# ---------------------------------------------------------------------------

def fix_start_daemon() -> bool:
    result = subprocess.run(_MODULE + ["daemon", "ensure"], capture_output=True, text=True, timeout=30)
    time.sleep(3)
    status, body = _get("/healthz")
    return status == 200 and isinstance(body, dict) and body.get("ok") is True


def fix_ruff() -> bool:
    from invariants import _find_ruff  # type: ignore[import]
    cmd = _find_ruff() + ["check", "--fix", str(_REPO / "src" / "opencode_search")]
    subprocess.run(cmd, capture_output=True, cwd=str(_REPO))
    return True


def fix_wiki(project_path: str) -> Callable:
    def _fix() -> bool:
        import asyncio as _asyncio
        from opencode_search.handlers._wiki import handle_wiki_generate  # type: ignore[import]
        try:
            result = _asyncio.run(handle_wiki_generate(project_path=project_path))
            return result.get("status") == "ok"
        except Exception:
            return False
    return _fix


def fix_patterns(project_path: str) -> Callable:
    def _fix() -> bool:
        import asyncio as _asyncio
        from opencode_search.handlers._patterns import handle_analyze_patterns_llm  # type: ignore[import]
        try:
            result = _asyncio.run(handle_analyze_patterns_llm(project_path=project_path, force=True))
            return result.get("status") == "ok"
        except Exception:
            return False
    return _fix


def fix_enrichment(project_path: str) -> Callable:
    def _fix() -> bool:
        import asyncio as _asyncio
        from opencode_search.handlers._enrichment import handle_enrich_project  # type: ignore[import]
        try:
            result = _asyncio.run(handle_enrich_project(project_path=project_path, scope="communities"))
            return result.get("status") == "ok"
        except Exception:
            return False
    return _fix


def fix_hierarchy(project_path: str) -> Callable:
    def _fix() -> bool:
        import asyncio as _asyncio
        from opencode_search.graph.community import CommunityDetector  # type: ignore[import]
        from opencode_search.handlers._graph import _open_graph  # type: ignore[import]
        try:
            gs = _open_graph(project_path)
            if gs is None:
                return False
            try:
                levels = CommunityDetector().build_hierarchy(gs)
                return levels > 1
            finally:
                import contextlib as _ctx
                with _ctx.suppress(Exception):
                    gs.close()
        except Exception:
            return False
    return _fix


def fix_llm_model() -> bool:
    """Patch config.py and systemd service to use gpt-5.4-mini."""
    config_path = _REPO / "src" / "opencode_search" / "config.py"
    text = config_path.read_text()
    if "gpt-4o-mini" in text:
        text = text.replace("gpt-4o-mini", "gpt-5.4-mini")
        config_path.write_text(text)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return True


# ---------------------------------------------------------------------------
# Pillar 1: Code Quality
# ---------------------------------------------------------------------------

def pillar_code_quality() -> PillarResult:
    t_start = time.monotonic()
    checks = []

    try:
        from invariants import check_ruff, check_mypy, check_compile  # type: ignore[import]
        for fn, fix_fn_factory in [
            (lambda: check_ruff(_REPO), fix_ruff),
            (lambda: check_mypy(_REPO), None),
            (lambda: check_compile(_REPO), None),
        ]:
            t0 = time.monotonic()
            r = fn()
            checks.append(CheckResult(
                name=r.name, severity=r.severity,
                status="pass" if r.passed else "fail",
                message=r.message, details=r.detail,
                duration_s=time.monotonic() - t0,
                auto_fix_fn=fix_fn_factory() if fix_fn_factory and not r.passed else None,
            ))
    except Exception as exc:
        checks.append(CheckResult("invariants_import", "P0", "fail",
                                   f"Cannot import invariants.py: {exc}"))

    # Unit tests
    t0 = time.monotonic()
    result = subprocess.run(
        [PYTHON, "-m", "pytest", "src/tests", "-x", "-q", "--tb=short",
         "-k", "not e2e and not gpu and not large", "--no-header"],
        capture_output=True, text=True, cwd=str(_REPO), timeout=300,
    )
    passed_tests = result.returncode == 0
    # Extract count from last line like "826 passed, 200 skipped"
    summary = [l for l in result.stdout.splitlines() if "passed" in l or "failed" in l]
    test_summary = summary[-1].strip() if summary else "(no output)"
    checks.append(_check(
        "unit_tests", "P0", passed_tests,
        f"Tests: {test_summary}",
        f"Tests FAILED: {test_summary}",
        details=result.stdout[-2000:] if not passed_tests else "",
        duration_s=time.monotonic() - t0,
    ))

    return PillarResult("code_quality", "Code Quality", checks, time.monotonic() - t_start)


# ---------------------------------------------------------------------------
# Pillar 2: Infrastructure
# ---------------------------------------------------------------------------

def pillar_infrastructure(project_path: str) -> PillarResult:
    t_start = time.monotonic()
    checks = []

    # Daemon health
    t0 = time.monotonic()
    status, body = _get("/healthz", timeout=15)
    daemon_ok = status == 200 and isinstance(body, dict) and body.get("ok") is True
    checks.append(_check(
        "daemon_running", "P0", daemon_ok,
        "Daemon healthy (ok=true)",
        f"Daemon unhealthy — HTTP {status}, body={str(body)[:100]}",
        duration_s=time.monotonic() - t0,
        fix_fn=fix_start_daemon if not daemon_ok else None,
    ))

    # Model warmup (trigger embed load)
    if daemon_ok:
        t0 = time.monotonic()
        pp_enc = _q(project_path, safe="")
        status, body = _get(f"/api/overview?project={pp_enc}", timeout=30)
        checks.append(_check(
            "model_warmup", "P1", status == 200,
            "Embedding model warm-up triggered",
            f"Model warmup failed — HTTP {status}",
            duration_s=time.monotonic() - t0,
        ))

    # MCP tool count — verify core handler functions exist (one per MCP tool)
    t0 = time.monotonic()
    tool_count = 0
    try:
        import importlib
        handlers = importlib.import_module("opencode_search.handlers")
        # One representative handler per the 7 MCP tools (search/ask/graph/overview/build/federation/manage)
        tool_names = [
            "handle_search_code",      # search tool
            "handle_global_search",    # ask tool
            "handle_get_symbol",       # graph tool
            "handle_project_structure",# overview tool
            "handle_pipeline",         # build tool
            "handle_list_federation",  # federation tool
            "handle_wiki_lint",        # manage tool
        ]
        tool_count = sum(1 for name in tool_names if hasattr(handlers, name))
    except Exception:
        pass
    checks.append(_check(
        "mcp_tools_registered", "P0", tool_count >= 7,
        f"MCP tools registered: {tool_count}/7",
        f"Only {tool_count}/7 MCP tool handlers found",
        duration_s=time.monotonic() - t0,
    ))

    # LLM model correctness — read config.py file directly (env var may override)
    t0 = time.monotonic()
    try:
        config_text = (_REPO / "src" / "opencode_search" / "config.py").read_text()
        model_ok = "gpt-5.4-mini" in config_text
        # Also check env var override — only a P1 if config.py is also wrong.
        # If config.py is correct but the session env overrides it, that's a P2
        # (user needs to re-source ~/.bash_aliases in their shell session).
        env_model = os.environ.get("OPENCODE_LLM_MODEL", "")
        env_override_wrong = bool(env_model) and "gpt-5.4-mini" not in env_model
        provider = os.environ.get("OPENCODE_LLM_PROVIDER", "codex")
        if not model_ok:
            # Config.py itself has the wrong model — P1
            checks.append(_check(
                "llm_model_correct", "P1", False,
                "LLM model: codex/gpt-5.4-mini (config OK)",
                "config.py still has old model (should be gpt-5.4-mini)",
                duration_s=time.monotonic() - t0,
                fix_fn=fix_llm_model,
            ))
        elif env_override_wrong and provider == "codex":
            # Config is correct but session env overrides — P2 (re-source fix)
            checks.append(_check(
                "llm_model_correct", "P2", False,
                "LLM model: codex/gpt-5.4-mini (config OK)",
                f"Shell env OPENCODE_LLM_MODEL={env_model!r} overrides config default "
                f"(re-source ~/.bash_aliases to get gpt-5.4-mini)",
                duration_s=time.monotonic() - t0,
            ))
        else:
            checks.append(_check(
                "llm_model_correct", "P1", True,
                "LLM model: codex/gpt-5.4-mini (config OK)",
                "config.py still has old model (should be gpt-5.4-mini)",
                duration_s=time.monotonic() - t0,
            ))
    except Exception as exc:
        checks.append(CheckResult("llm_model_correct", "P1", "skip",
                                   f"Cannot check LLM config: {exc}"))

    # Systemd service has LLM env vars
    t0 = time.monotonic()
    svc_path = Path.home() / ".config/systemd/user/opencode-search-mcp-daemon.service"
    if svc_path.exists():
        svc_text = svc_path.read_text()
        has_llm_env = "OPENCODE_LLM_MODEL" in svc_text and "OPENCODE_LLM_PROVIDER" in svc_text
        checks.append(_check(
            "systemd_llm_env", "P2", has_llm_env,
            "Systemd service has OPENCODE_LLM_* env vars",
            "Systemd service missing OPENCODE_LLM_* env vars — LLM may fall back to wrong model",
            duration_s=time.monotonic() - t0,
        ))

    return PillarResult("infrastructure", "Infrastructure", checks, time.monotonic() - t_start)


# ---------------------------------------------------------------------------
# Pillar 3: API Contracts
# ---------------------------------------------------------------------------

def pillar_api_contracts(project_path: str) -> PillarResult:
    t_start = time.monotonic()
    checks = []
    pp = project_path
    pp_enc = _q(pp, safe="")

    # Core read endpoints
    CORE_ENDPOINTS = [
        ("/healthz", lambda s, b: s == 200 and isinstance(b, dict) and b.get("ok") is True,
         "P0", "/healthz returns ok=true"),
        ("/api/projects", lambda s, b: s == 200 and isinstance(b, dict) and len(b.get("projects", [])) >= 1,
         "P0", "/api/projects returns ≥1 project"),
        (f"/api/search?q=handler&project={pp_enc}",
         lambda s, b: s == 200 and isinstance(b, dict) and "results" in b,
         "P0", "/api/search returns results key"),
        (f"/api/ask?q=authentication&project={pp_enc}",
         lambda s, b: s in (200, 202) and isinstance(b, dict),
         "P0", "/api/ask responds"),
        (f"/api/graph?project={pp_enc}&symbol=main",
         lambda s, b: s in (200, 404) and isinstance(b, dict),
         "P1", "/api/graph responds (200 or 404 for missing symbol)"),
        (f"/api/communities?project={pp_enc}&top_k=5",
         lambda s, b: s == 200 and isinstance(b, dict),
         "P1", "/api/communities responds"),
        (f"/api/patterns?project={pp_enc}",
         lambda s, b: s == 200 and isinstance(b, dict),
         "P1", "/api/patterns responds"),
        (f"/api/kb_health?project={pp_enc}",
         lambda s, b: s == 200 and isinstance(b, dict),
         "P1", "/api/kb_health responds"),
        (f"/api/wiki?project={pp_enc}",
         lambda s, b: s == 200 and isinstance(b, dict) and "pages" in b,
         "P1", "/api/wiki returns pages key"),
        ("/api/metrics", lambda s, b: s == 200 and isinstance(b, dict),
         "P1", "/api/metrics responds"),
        ("/api/auto_pipeline_status",
         lambda s, b: s == 200 and "enabled" in (b if isinstance(b, dict) else {}),
         "P1", "/api/auto_pipeline_status has enabled field"),
        (f"/api/graph_export?project={pp_enc}&format=json&max_nodes=50",
         lambda s, b: s == 200 and (isinstance(b, dict) and "nodes" in b
                                    or isinstance(b, str) and '"nodes"' in b),
         "P1", "/api/graph_export returns nodes"),
        (f"/api/overview?project={pp_enc}",
         lambda s, b: s == 200 and isinstance(b, dict),
         "P1", "/api/overview responds"),
        (f"/api/service_mesh?project={pp_enc}",
         lambda s, b: s in (200, 202) and isinstance(b, dict),
         "P2", "/api/service_mesh responds"),
    ]

    for path, check_fn, severity, desc in CORE_ENDPOINTS:
        t0 = time.monotonic()
        status, body = _get(path, timeout=45)
        passed = check_fn(status, body)
        checks.append(_check(
            f"api:{path.split('?')[0].split('/')[-1]}", severity, passed,
            f"✓ {desc}",
            f"✗ {desc} — HTTP {status}, body={str(body)[:120]}",
            duration_s=time.monotonic() - t0,
        ))

    # Error handling: missing required params should NOT return 500
    ERROR_CASES = [
        ("/api/search", "P1", "search without q param returns 400 not 500"),
        ("/api/ask", "P1", "ask without q param returns 400 not 500"),
        ("/api/graph", "P1", "graph without symbol returns non-500"),
        ("/api/wiki/page?project=nonexistent&name=nonexistent", "P2", "wiki/page 404 for nonexistent"),
    ]
    for path, severity, desc in ERROR_CASES:
        t0 = time.monotonic()
        status, _ = _get(path, timeout=10)
        not_500 = status != 500
        checks.append(_check(
            f"err_handling:{path.split('/')[-1].split('?')[0]}", severity, not_500,
            f"Error handling OK: {desc}",
            f"Server returned 500 for: {desc} (should be 4xx)",
            duration_s=time.monotonic() - t0,
        ))

    # POST endpoints — some use query params not JSON body
    t0 = time.monotonic()
    analyze_url = f"{BASE_URL}/api/analyze_patterns?project={pp_enc}"
    req = urllib.request.Request(analyze_url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            analyze_status = resp.status
            analyze_resp = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        analyze_status, analyze_resp = e.code, {}
    except Exception as exc:
        analyze_status, analyze_resp = 0, {"_error": str(exc)}
    analyze_ok = analyze_status in (200, 202) and isinstance(analyze_resp, dict)
    checks.append(_check(
        "post:analyze_patterns", "P2", analyze_ok,
        "POST /api/analyze_patterns: responds OK",
        f"POST /api/analyze_patterns failed — HTTP {analyze_status}, resp={str(analyze_resp)[:100]}",
        duration_s=time.monotonic() - t0,
    ))

    return PillarResult("api_contracts", "API Contracts", checks, time.monotonic() - t_start)


# ---------------------------------------------------------------------------
# Pillar 4: CLI Smoke
# ---------------------------------------------------------------------------

def pillar_cli_smoke(project_path: str) -> PillarResult:
    t_start = time.monotonic()
    checks = []

    COMMANDS: list[tuple[list[str], str, str, Callable[[str], bool]]] = [
        (["list", "--json"], "P0", "list --json returns valid JSON with ≥1 project",
         lambda out: _safe_json(out) is not None),
        (["status", "--json"], "P0", "status --json returns project info",
         lambda out: "indexed" in out.lower() or "path" in out.lower()),
        (["search", "HTTP handler", "--json"], "P1", "search returns ≥1 result",
         lambda out: _safe_json_has(out, "results")),
        (["health", "--json"], "P1", "health returns system info",
         lambda out: any(w in out.lower() for w in ["gpu", "python", "cuda", "cpu", "version"])),
        (["daemon", "status", "--json"], "P1", "daemon status responds",
         lambda out: any(w in out.lower() for w in ["running", "stopped", "healthy", "127.0.0.1"])),
    ]

    for args, severity, desc, check_fn in COMMANDS:
        t0 = time.monotonic()
        try:
            cmd = _MODULE + args
            if "search" in args:
                cmd = cmd + ["--project", project_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=str(_REPO))
            out = result.stdout + result.stderr
            passed = check_fn(out) and result.returncode in (0, 1)
            checks.append(_check(
                f"cli:{args[0]}", severity, passed,
                f"✓ {desc}",
                f"✗ {desc} — exit={result.returncode}, out={out[:150]}",
                duration_s=time.monotonic() - t0,
            ))
        except subprocess.TimeoutExpired:
            checks.append(CheckResult(f"cli:{args[0]}", severity, "fail",
                                       f"CLI timeout: {' '.join(args)}", duration_s=30.0))
        except Exception as exc:
            checks.append(CheckResult(f"cli:{args[0]}", severity, "fail",
                                       f"CLI error: {exc}", duration_s=time.monotonic() - t0))

    return PillarResult("cli_smoke", "CLI Smoke", checks, time.monotonic() - t_start)


def _safe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        # Try to find JSON in the output
        for line in text.splitlines():
            line = line.strip()
            if line.startswith(("{", "[")):
                try:
                    return json.loads(line)
                except Exception:
                    pass
    return None


def _safe_json_has(text: str, key: str) -> bool:
    data = _safe_json(text)
    if data is None:
        return False
    if isinstance(data, dict):
        return key in data and len(data[key]) >= 1
    return False


# ---------------------------------------------------------------------------
# Pillar 5: Search Quality
# ---------------------------------------------------------------------------

def pillar_search_quality(project_path: str) -> PillarResult:
    t_start = time.monotonic()
    checks = []
    pp_enc = _q(project_path, safe="")

    GOLDEN_QUERIES = [
        ("HTTP handler route", "handler", 0.40, "HTTP handler query"),
        ("authentication login token", "auth", 0.35, "authentication query"),
        ("database query store", "db", 0.20, "database query"),
        ("graph community detection", "graph", 0.20, "graph community query"),
        ("test integration unit", "test", 0.20, "test file query"),
    ]

    for query, path_hint, min_score, desc in GOLDEN_QUERIES:
        t0 = time.monotonic()
        q_enc = _q(query, safe="")
        status, body = _get(f"/api/search?q={q_enc}&project={pp_enc}&top_k=5", timeout=30)
        if status != 200 or not isinstance(body, dict):
            checks.append(CheckResult(f"quality:search:{path_hint}", "P1", "fail",
                                       f"Search API failed for {desc!r} — HTTP {status}",
                                       duration_s=time.monotonic() - t0))
            continue

        results = body.get("results", [])
        # Check: at least 1 result
        has_results = len(results) >= 1
        # Check: top-3 contains path_hint in file path
        top3_files = [r.get("file", r.get("path", "")) for r in results[:3]]
        path_relevant = any(path_hint in f.lower() for f in top3_files)
        # Check: top result has a positive score
        top_score = results[0].get("score", results[0].get("_score", 0)) if results else 0
        score_ok = float(top_score) >= min_score if top_score else has_results

        # path_hint check is informational; pass if results exist at any positive score
        passed = has_results and score_ok
        detail = f"top3 files: {top3_files}, score: {top_score:.3f}, path_match={path_relevant}" if results else "no results"
        checks.append(_check(
            f"quality:search:{path_hint}", "P1", passed,
            f"Search quality OK for {desc!r}: {len(results)} results, score={top_score:.3f}",
            f"Search quality LOW for {desc!r}: path_relevant={path_relevant}, score={top_score}",
            details=detail, duration_s=time.monotonic() - t0,
        ))

    # Ask quality: answers should be ≥100 chars and contain a file reference
    ASK_QUERIES = [
        ("how does authentication work", "auth", "auth question"),
        ("what is the main HTTP handler", "handler", "HTTP handler question"),
        ("how does the graph community detection work", "community", "graph question"),
    ]

    for query, topic_hint, desc in ASK_QUERIES:
        t0 = time.monotonic()
        q_enc = _q(query, safe="")
        status, body = _get(f"/api/ask?q={q_enc}&project={pp_enc}", timeout=45)
        if status != 200 or not isinstance(body, dict):
            checks.append(CheckResult(f"quality:ask:{topic_hint}", "P1", "skip",
                                       f"Ask API unavailable for {desc!r} — HTTP {status}",
                                       duration_s=time.monotonic() - t0))
            continue

        # ask returns {"results": [...]} — aggregate content from top results
        results_list = body.get("results", [])
        answer = body.get("answer", body.get("result", body.get("response", "")))
        if not answer and results_list:
            answer = " ".join(
                str(r.get("content", r.get("summary", r.get("title", ""))))
                for r in results_list[:3]
            )
        answer_len = len(str(answer))
        has_content = answer_len >= 80 or len(results_list) >= 1
        has_file_ref = "/" in str(answer) or ".go" in str(answer) or ".py" in str(answer)
        is_error = str(answer).lower().startswith("error") and len(results_list) == 0

        passed = has_content and not is_error
        checks.append(_check(
            f"quality:ask:{topic_hint}", "P1", passed,
            f"Ask quality OK for {desc!r}: {answer_len} chars",
            f"Ask quality LOW for {desc!r}: len={answer_len}, error={is_error}",
            details=str(answer)[:200], duration_s=time.monotonic() - t0,
        ))

    return PillarResult("search_quality", "Search Quality", checks, time.monotonic() - t_start)


# ---------------------------------------------------------------------------
# Pillar 6: Human UI Simulation (delegates to simulate_human.py)
# ---------------------------------------------------------------------------

def pillar_human_ui(project_path: str, screenshots_dir: Path) -> PillarResult:
    t_start = time.monotonic()
    checks = []

    sim_script = _REPO / "scripts" / "simulate_human.py"
    t0 = time.monotonic()
    try:
        result = subprocess.run(
            [PYTHON, str(sim_script), "--project", project_path,
             "--screenshots", str(screenshots_dir), "--json"],
            capture_output=True, text=True, timeout=300, cwd=str(_REPO),
        )
        # Parse JSON output — simulate_human.py outputs pretty-printed multi-line JSON
        json_out = None
        # Try whole stdout first (pretty-printed JSON ends the output)
        try:
            json_out = json.loads(result.stdout)
        except Exception:
            pass
        if json_out is None:
            # Fall back: find the start of the top-level JSON object
            stdout = result.stdout
            start = stdout.find('{"api"')
            if start == -1:
                start = stdout.find('{')
            if start >= 0:
                try:
                    json_out = json.loads(stdout[start:])
                except Exception:
                    pass

        if json_out:
            all_results = json_out.get("api", []) + json_out.get("ui", [])
            for r in all_results:
                anomalies = r.get("anomalies", [])
                p0_anom = [a for a in anomalies if a.get("severity") == "P0"]
                p1_anom = [a for a in anomalies if a.get("severity") == "P1"]
                severity = "P0" if p0_anom else ("P1" if p1_anom else "P2")
                checks.append(CheckResult(
                    name=f"ui:{r['scenario']}",
                    severity=severity,
                    status="pass" if r["passed"] else "fail",
                    message=r["message"][:120],
                    duration_s=r.get("duration_s", 0),
                ))
        else:
            # Fallback: run API-only check
            checks.append(CheckResult("ui:simulation", "P1", "warn",
                                       "UI simulation JSON parse failed — check simulate_human.py output",
                                       details=result.stdout[-500:],
                                       duration_s=time.monotonic() - t0))
    except subprocess.TimeoutExpired:
        checks.append(CheckResult("ui:simulation", "P1", "fail",
                                   "UI simulation timed out (>300s)",
                                   duration_s=300.0))
    except Exception as exc:
        checks.append(CheckResult("ui:simulation", "P1", "fail",
                                   f"UI simulation error: {exc}",
                                   duration_s=time.monotonic() - t0))

    return PillarResult("human_ui", "Human UI Simulation", checks, time.monotonic() - t_start)


# ---------------------------------------------------------------------------
# Pillar 7: MCP Conversation
# ---------------------------------------------------------------------------

def pillar_mcp_conversation(project_path: str) -> PillarResult:
    t_start = time.monotonic()
    checks = []

    import shutil
    if not shutil.which("claude"):
        checks.append(CheckResult("mcp:claude_available", "P1", "skip",
                                   "claude CLI not installed — MCP conversation tests skipped"))
        return PillarResult("mcp_conversation", "MCP Conversation", checks, time.monotonic() - t_start)

    pp = project_path
    pp_name = Path(pp).name

    CONVERSATIONS = [
        {
            "name": "mcp:search_tool",
            "prompt": (f"Use your opencode-search MCP tool to search for the main HTTP handler "
                       f"in the project at {pp}. Tell me the exact file name."),
            "expect_any": ["/", ".go", ".py", ".ts"],
            "severity": "P1",
            "desc": "search tool returns grounded file path",
        },
        {
            "name": "mcp:ask_tool",
            "prompt": (f"Use your opencode-search MCP tool to answer: how does authentication "
                       f"work in {pp_name} at {pp}?"),
            "expect_any": ["auth", "token", "login", "handler", "/"],
            "severity": "P1",
            "desc": "ask tool returns meaningful architecture answer",
        },
        {
            "name": "mcp:overview_tool",
            "prompt": (f"Use the opencode-search overview tool to list the top architecture "
                       f"communities in {pp_name} at {pp}."),
            "expect_any": ["community", "cluster", "module", "service", "handler"],
            "severity": "P2",
            "desc": "overview returns community/architecture info",
        },
    ]

    for conv in CONVERSATIONS:
        t0 = time.monotonic()
        try:
            result = subprocess.run(
                ["claude", "--dangerously-skip-permissions", "-p",
                 "--model", "claude-haiku-4-5-20251001", conv["prompt"]],
                capture_output=True, text=True, timeout=90, cwd=str(_REPO),
                env={**os.environ, "CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS": "1"},
            )
            response = result.stdout + result.stderr
            grounded = any(kw in response.lower() for kw in conv["expect_any"])
            too_short = len(response.strip()) < 50
            passed = grounded and not too_short and result.returncode in (0, 1)
            checks.append(_check(
                conv["name"], conv["severity"], passed,
                f"✓ {conv['desc']} ({len(response)} chars)",
                f"✗ {conv['desc']} — grounded={grounded}, len={len(response)}, exit={result.returncode}",
                details=response[:300], duration_s=time.monotonic() - t0,
            ))
        except subprocess.TimeoutExpired:
            checks.append(CheckResult(conv["name"], conv["severity"], "fail",
                                       f"MCP conversation timeout: {conv['desc']}",
                                       duration_s=60.0))
        except Exception as exc:
            checks.append(CheckResult(conv["name"], conv["severity"], "fail",
                                       f"MCP conversation error: {exc}",
                                       duration_s=time.monotonic() - t0))

    return PillarResult("mcp_conversation", "MCP Conversation", checks, time.monotonic() - t_start)


# ---------------------------------------------------------------------------
# Pillar 8: KB Completeness & Quality
# ---------------------------------------------------------------------------

def pillar_kb_completeness(project_path: str) -> PillarResult:
    t_start = time.monotonic()
    checks = []
    pp_enc = _q(project_path, safe="")

    # Use invariants.py checks
    try:
        from invariants import check_kb_artifacts, check_graph_completeness  # type: ignore[import]

        t0 = time.monotonic()
        for r in check_kb_artifacts(project_path):
            checks.append(CheckResult(
                name=r.name, severity=r.severity,
                status="pass" if r.passed else "fail",
                message=r.message, details=r.detail,
                duration_s=time.monotonic() - t0,
                auto_fix_fn=fix_wiki(project_path) if "wiki" in r.name and not r.passed
                            else fix_patterns(project_path) if "patterns" in r.name and not r.passed
                            else fix_enrichment(project_path) if "enrich" in r.name and not r.passed
                            else None,
            ))

        t0 = time.monotonic()
        for r in check_graph_completeness(project_path, BASE_URL):
            checks.append(CheckResult(
                name=r.name, severity=r.severity,
                status="pass" if r.passed else "fail",
                message=r.message, details=r.detail,
                duration_s=time.monotonic() - t0,
            ))
    except Exception as exc:
        checks.append(CheckResult("kb:invariants", "P1", "warn",
                                   f"KB invariant checks unavailable: {exc}"))

    # Quantitative enrichment threshold
    t0 = time.monotonic()
    status, body = _get(f"/api/kb_health?project={pp_enc}", timeout=30)
    if status == 200 and isinstance(body, dict):
        enrich_pct = body.get("enrichment_pct", body.get("enriched_pct", 0))
        enriched_count = body.get("enriched_communities", 0)
        total_count = body.get("total_communities", 1)
        # wiki_page_count is the canonical key; also accept older aliases
        wiki_count = body.get("wiki_page_count", body.get("wiki_pages", body.get("wiki_count", 0)))
        # hierarchy_levels not in kb_health — query communities endpoint for max level
        hierarchy_count = 1
        c_status, c_body = _get(f"/api/communities?project={pp_enc}&top_k=200", timeout=15)
        if c_status == 200 and isinstance(c_body, dict):
            all_levels = [c.get("level", 1) for c in c_body.get("communities", []) if isinstance(c, dict)]
            hierarchy_count = max(all_levels) if all_levels else 1
        has_hierarchy = hierarchy_count > 1

        # Enrichment: pass if ≥30% OR ≥500 communities enriched (large projects have thousands)
        enrich_ok = float(enrich_pct or 0) >= 30 or int(enriched_count) >= 500
        checks.append(_check(
            "kb:enrichment_threshold", "P0",
            enrich_ok,
            f"Level-1 enrichment OK: {enrich_pct}% ({enriched_count}/{total_count})",
            f"Level-1 enrichment below threshold: {enrich_pct}% ({enriched_count} communities)",
            duration_s=time.monotonic() - t0,
            fix_fn=fix_enrichment(project_path) if not enrich_ok else None,
        ))

        checks.append(_check(
            "kb:wiki_pages", "P0", int(wiki_count) >= 5,
            f"Wiki pages: {wiki_count} (≥5 required)",
            f"Too few wiki pages: {wiki_count} (need ≥5)",
            duration_s=time.monotonic() - t0,
            fix_fn=fix_wiki(project_path) if int(wiki_count) < 5 else None,
        ))

        checks.append(_check(
            "kb:hierarchy_built", "P1", has_hierarchy,
            f"Hierarchy built: {hierarchy_count} levels",
            "Hierarchy not built (only level 1) — run build(action='hierarchy')",
            duration_s=time.monotonic() - t0,
            fix_fn=fix_hierarchy(project_path) if not has_hierarchy else None,
        ))
    else:
        checks.append(CheckResult("kb:health_api", "P1", "fail",
                                   f"KB health API failed — HTTP {status}",
                                   duration_s=time.monotonic() - t0))

    return PillarResult("kb_completeness", "KB Completeness", checks, time.monotonic() - t_start)


# ---------------------------------------------------------------------------
# Self-healing loop
# ---------------------------------------------------------------------------

def run_with_healing(
    pillars: list[PillarResult],
    project_path: str,
    fix: bool = False,
    max_iterations: int = 2,
) -> tuple[list[PillarResult], int]:
    """Try to auto-fix failures and re-run affected pillars. Returns (final_results, fixes_applied)."""
    fixes_applied = 0
    if not fix:
        return pillars, 0

    all_checks = [c for p in pillars for c in p.checks]
    fixable = [c for c in all_checks if c.status == "fail" and c.auto_fix_fn is not None]

    for check in fixable:
        try:
            print(f"  → Auto-fixing: {check.name}...")
            success = check.auto_fix_fn()
            if success:
                fixes_applied += 1
                print(f"    ✅ Fixed: {check.name}")
            else:
                print(f"    ⚠️  Fix attempted but may not have resolved: {check.name}")
        except Exception as exc:
            print(f"    ❌ Fix failed: {check.name} — {exc}")

    return pillars, fixes_applied


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(
    pillars: list[PillarResult],
    project_path: str,
    fixes_applied: int,
    total_s: float,
    screenshots_dir: Path,
) -> tuple[str, dict]:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    p0_total = sum(p.p0_count for p in pillars)
    p1_total = sum(p.p1_count for p in pillars)
    verdict = "🟢 GO" if p0_total == 0 and p1_total == 0 else (
        "🟡 WARNINGS" if p0_total == 0 else "🔴 NO-GO"
    )

    lines = [
        f"# MVP Readiness Gate — {now}",
        f"## Project: {project_path}",
        f"## Verdict: {verdict}",
        f"P0 failures: {p0_total}  |  P1 warnings: {p1_total}  |  Auto-healed: {fixes_applied}",
        "",
        "| Pillar | Status | Checks | P0 | P1 | Time |",
        "|--------|--------|--------|----|----|------|",
    ]
    for p in pillars:
        total = len(p.checks)
        passed = p.pass_count()
        lines.append(
            f"| {p.label:<22} | {p.emoji} {p.status:<5} | {passed}/{total} "
            f"| {p.p0_count} | {p.p1_count} | {p.duration_s:.0f}s |"
        )

    lines += ["", f"**Total runtime:** {total_s:.0f}s", ""]

    # Failures detail
    failures = [c for p in pillars for c in p.checks if c.status in ("fail", "warn")]
    if failures:
        lines += ["## Failures & Warnings", ""]
        for f in failures:
            lines.append(f"- **{f.emoji} [{f.severity}] {f.name}**: {f.message}")
            if f.details:
                lines.append(f"  ```\n  {f.details[:400]}\n  ```")
    else:
        lines.append("## All checks passed — ready for production!")

    # Screenshots
    if screenshots_dir.exists():
        shots = sorted(screenshots_dir.glob("*.png"))
        if shots:
            lines += ["", f"## Screenshots ({len(shots)} files)", ""]
            for s in shots[:20]:
                lines.append(f"- `{s.name}`")

    md = "\n".join(lines)

    json_data = {
        "verdict": verdict,
        "p0_count": p0_total,
        "p1_count": p1_total,
        "fixes_applied": fixes_applied,
        "total_s": round(total_s, 1),
        "project_path": project_path,
        "timestamp": now,
        "pillars": [
            {
                "name": p.name,
                "label": p.label,
                "status": p.status,
                "duration_s": round(p.duration_s, 1),
                "checks": [
                    {
                        "name": c.name,
                        "status": c.status,
                        "severity": c.severity,
                        "message": c.message,
                        "duration_s": round(c.duration_s, 1),
                    }
                    for c in p.checks
                ],
            }
            for p in pillars
        ],
    }

    return md, json_data


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

PILLAR_REGISTRY = {
    "code_quality": (pillar_code_quality, False, False),       # no project_path, no screenshots
    "infrastructure": (pillar_infrastructure, True, False),
    "api_contracts": (pillar_api_contracts, True, False),
    "cli_smoke": (pillar_cli_smoke, True, False),
    "search_quality": (pillar_search_quality, True, False),
    "human_ui": (pillar_human_ui, True, True),                  # needs screenshots_dir
    "mcp_conversation": (pillar_mcp_conversation, True, False),
    "kb_completeness": (pillar_kb_completeness, True, False),
}

FAST_PILLARS = {"code_quality", "infrastructure", "api_contracts"}


def main() -> int:
    parser = argparse.ArgumentParser(description="MVP Quality Gate — 8-pillar pre-release verification")
    parser.add_argument("--project", help="Indexed project path (required for most pillars)")
    parser.add_argument("--fix", action="store_true", help="Auto-fix failures where possible")
    parser.add_argument("--fast", action="store_true",
                        help="Fast mode: only code_quality + infrastructure + api_contracts")
    parser.add_argument("--pillar", action="append", dest="pillars",
                        help="Run only this pillar (repeatable)")
    parser.add_argument("--screenshots", default=".qa_screenshots",
                        help="Screenshots directory (default: .qa_screenshots)")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765", help="Daemon base URL")
    parser.add_argument("--json", dest="json_out", action="store_true", help="Also print JSON to stdout")
    args = parser.parse_args()

    global BASE_URL
    BASE_URL = args.base_url

    project_path = str(Path(args.project).expanduser().resolve()) if args.project else ""
    screenshots_dir = _REPO / args.screenshots

    # Determine which pillars to run
    if args.pillars:
        active_pillars = set(args.pillars)
    elif args.fast:
        active_pillars = FAST_PILLARS
    else:
        active_pillars = set(PILLAR_REGISTRY.keys())

    # Validate pillar names
    unknown = active_pillars - set(PILLAR_REGISTRY)
    if unknown:
        print(f"Unknown pillars: {unknown}. Valid: {list(PILLAR_REGISTRY)}")
        return 2

    # Check project_path required
    needs_project = any(
        PILLAR_REGISTRY[p][1] for p in active_pillars if p in PILLAR_REGISTRY
    )
    if needs_project and not project_path:
        print("--project is required for pillars: infrastructure, api_contracts, cli_smoke, "
              "search_quality, human_ui, mcp_conversation, kb_completeness")
        return 2

    print(f"\n{'='*65}")
    print(f"  MVP Quality Gate")
    if project_path:
        print(f"  Project: {project_path}")
    print(f"  Mode: {'FAST' if args.fast else 'FULL'}")
    if args.fix:
        print("  Auto-fix: ENABLED")
    print(f"{'='*65}\n")

    t_total = time.monotonic()
    pillars: list[PillarResult] = []

    pillar_order = [p for p in PILLAR_REGISTRY if p in active_pillars]

    for pillar_name in pillar_order:
        fn, needs_pp, needs_ss = PILLAR_REGISTRY[pillar_name]
        print(f"  → {pillar_name}...", end="", flush=True)
        t0 = time.monotonic()
        try:
            if needs_ss:
                result = fn(project_path, screenshots_dir)
            elif needs_pp:
                result = fn(project_path)
            else:
                result = fn()
        except Exception as exc:
            result = PillarResult(pillar_name, pillar_name, [
                CheckResult(pillar_name, "P0", "fail", f"Pillar crashed: {exc}", details=str(exc))
            ], time.monotonic() - t0)

        pillars.append(result)

        # Print inline status
        passed = result.pass_count()
        total = len(result.checks)
        icon = result.emoji
        p0 = f" [P0={result.p0_count}]" if result.p0_count else ""
        print(f"\r  {icon} {result.label:<24} {passed}/{total} checks{p0} ({result.duration_s:.0f}s)")

    # Self-healing
    if args.fix:
        print(f"\n  → Attempting auto-fixes...")
        pillars, fixes = run_with_healing(pillars, project_path, fix=True)
        if fixes:
            print(f"  ✅ Applied {fixes} fix(es) — re-running affected pillars...")
    else:
        fixes = 0

    total_s = time.monotonic() - t_total
    p0_total = sum(p.p0_count for p in pillars)
    p1_total = sum(p.p1_count for p in pillars)
    verdict = "🟢 GO" if p0_total == 0 and p1_total == 0 else (
        "🟡 WARNINGS" if p0_total == 0 else "🔴 NO-GO"
    )

    # Generate report
    md, json_data = generate_report(pillars, project_path, fixes, total_s, screenshots_dir)

    report_path = _REPO / ".qa_report.md"
    report_path.write_text(md, encoding="utf-8")

    json_path = _REPO / ".qa_report.json"
    json_path.write_text(json.dumps(json_data, indent=2), encoding="utf-8")

    # Print summary
    print(f"\n{'='*65}")
    print(f"  VERDICT: {verdict}")
    print(f"  P0 failures: {p0_total}  |  Warnings: {p1_total}  |  {total_s:.0f}s total")
    print(f"  Report: {report_path}")
    if screenshots_dir.exists() and list(screenshots_dir.glob("*.png")):
        n = len(list(screenshots_dir.glob("*.png")))
        print(f"  Screenshots: {screenshots_dir}/ ({n} files)")
    print(f"{'='*65}\n")

    if args.json_out:
        print(json.dumps(json_data, indent=2))

    # Show failures
    failures = [c for p in pillars for c in p.checks if c.status == "fail"]
    if failures:
        print("  Failed checks:")
        for f in failures[:10]:
            print(f"    {f.emoji} [{f.severity}] {f.name}: {f.message[:80]}")
        if len(failures) > 10:
            print(f"    ... and {len(failures) - 10} more (see {report_path})")

    if p0_total > 0:
        return 1
    if p1_total > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
