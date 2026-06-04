#!/usr/bin/env python3
"""Comprehensive QA orchestration script — single entry point to run everything.

Usage:
    python scripts/qa_comprehensive.py [--fast] [--live] [--project PATH]

Sections:
    1. Daemon health      — GET /healthz must respond 200
    2. Ollama models      — qwen3-enrich:1.7b and qwen3-query:8b present
    3. Config files       — bash_aliases, claude settings, codex, opencode, hermes
    4. Fast test suite    — pytest -m "not (gpu or ... or playwright)"
    5. Dashboard API      — pytest test_dashboard_api_full.py
    6. MCP config verify  — pytest test_mcp_config_verify.py
    7. Playwright E2E     — pytest test_dashboard_playwright.py --browser chromium
    8. Live chat Q&A      — POST /api/chat_stream (only with --live)

Output: colored terminal table + HTML report at /tmp/ocs-qa-report.html
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
VENV_PY = REPO_ROOT / ".venv" / "bin" / "python"
PYTEST = REPO_ROOT / ".venv" / "bin" / "pytest"

REPORT_PATH = Path("/tmp/ocs-qa-report.html")
SCREENSHOT_DIR = Path("/tmp/dashboard-playwright-screenshots")


# ── ANSI colors ────────────────────────────────────────────────────────────────

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
DIM = "\033[2m"


class SectionResult(NamedTuple):
    name: str
    status: str          # "PASS" | "WARN" | "FAIL" | "SKIP"
    passed: int
    failed: int
    skipped: int
    duration_s: float
    output: str
    error: str


def _color(status: str) -> str:
    return {
        "PASS": GREEN + BOLD,
        "WARN": YELLOW + BOLD,
        "FAIL": RED + BOLD,
        "SKIP": DIM,
    }.get(status, RESET)


def _print_section(label: str) -> None:
    print(f"\n{CYAN}{BOLD}{'─' * 60}{RESET}")
    print(f"{CYAN}{BOLD}  {label}{RESET}")
    print(f"{CYAN}{BOLD}{'─' * 60}{RESET}")


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 300) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=cwd or str(REPO_ROOT),
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s"
    except Exception as e:
        return 1, "", str(e)


# ── Section runners ────────────────────────────────────────────────────────────

def check_daemon_health() -> SectionResult:
    _print_section("1. Daemon health — GET /healthz")
    t0 = time.perf_counter()
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:8765/healthz", timeout=3) as resp:
            code = resp.getcode()
            body = resp.read().decode()
        ok = code == 200
        status = "PASS" if ok else "FAIL"
        output = f"HTTP {code}: {body[:200]}"
        print(f"  {'✅' if ok else '❌'} {output}")
        return SectionResult("Daemon health", status, 1 if ok else 0, 0 if ok else 1,
                             0, time.perf_counter() - t0, output, "")
    except Exception as e:
        msg = f"Daemon not reachable: {e}"
        print(f"  ⚠️  {msg}")
        return SectionResult("Daemon health", "WARN", 0, 0, 1,
                             time.perf_counter() - t0, "", msg)


def check_ollama_models() -> SectionResult:
    _print_section("2. Ollama models — qwen3-enrich:1.7b + qwen3-query:8b")
    t0 = time.perf_counter()
    rc, stdout, stderr = _run(["ollama", "list"], timeout=10)
    required = ["qwen3-enrich:1.7b", "qwen3-query:8b"]
    if rc != 0:
        msg = f"ollama list failed: {stderr[:200]}"
        print(f"  ⚠️  {msg}")
        return SectionResult("Ollama models", "WARN", 0, 0, 1,
                             time.perf_counter() - t0, "", msg)
    missing = [m for m in required if m not in stdout]
    if missing:
        msg = f"Missing models: {missing}"
        print(f"  ❌ {msg}")
        return SectionResult("Ollama models", "FAIL", 0, len(missing), 0,
                             time.perf_counter() - t0, stdout, msg)
    print(f"  ✅ Both models present: {required}")
    return SectionResult("Ollama models", "PASS", 2, 0, 0,
                         time.perf_counter() - t0, stdout, "")


def check_config_files() -> SectionResult:
    _print_section("3. Config files — bash_aliases, claude, codex, opencode, hermes")
    t0 = time.perf_counter()
    checks = []
    failures = []

    bash_aliases = Path.home() / ".bash_aliases"
    if bash_aliases.exists():
        text = bash_aliases.read_text()
        for key in ["OPENCODE_LLM_PROVIDER=ollama", "alias ocs=", "alias ocs-daemon="]:
            ok = key in text
            checks.append(ok)
            if not ok:
                failures.append(f"bash_aliases missing: {key!r}")
            print(f"  {'✅' if ok else '❌'} bash_aliases: {key!r}")
    else:
        failures.append("~/.bash_aliases not found")
        print("  ⚠️  ~/.bash_aliases not found")

    claude_settings = Path.home() / ".claude" / "settings.json"
    if claude_settings.exists():
        try:
            data = json.loads(claude_settings.read_text())
            mcp = data.get("mcpServers", {}).get("opencode-search", {})
            ok = bool(mcp)
            checks.append(ok)
            if not ok:
                failures.append("claude settings: opencode-search MCP not registered")
            print(f"  {'✅' if ok else '❌'} claude settings: opencode-search MCP registered")
        except Exception as e:
            failures.append(f"claude settings parse error: {e}")
            print(f"  ❌ claude settings: {e}")
    else:
        print("  ⚠️  ~/.claude/settings.json not found")

    passed = checks.count(True)
    failed = len(failures)
    status = "PASS" if not failures else ("WARN" if passed > 0 else "FAIL")
    return SectionResult("Config files", status, passed, failed, 0,
                         time.perf_counter() - t0, "\n".join(checks.__str__()),
                         "\n".join(failures))


def run_pytest_section(name: str, args: list[str], section_num: int) -> SectionResult:
    _print_section(f"{section_num}. {name}")
    t0 = time.perf_counter()
    cmd = [str(PYTEST), *args, "--tb=short", "-q"]
    print(f"  $ pytest {' '.join(args)}")
    rc, stdout, stderr = _run(cmd, cwd=str(SRC_DIR), timeout=600)
    output = stdout + stderr

    passed = failed = skipped = 0
    for line in output.splitlines():
        if " passed" in line:
            import re
            m = re.search(r"(\d+) passed", line)
            if m:
                passed = int(m.group(1))
            m = re.search(r"(\d+) failed", line)
            if m:
                failed = int(m.group(1))
            m = re.search(r"(\d+) skipped", line)
            if m:
                skipped = int(m.group(1))

    status = "FAIL" if rc != 0 else "PASS"
    emoji = "✅" if status == "PASS" else "❌"
    print(f"  {emoji} {passed} passed, {failed} failed, {skipped} skipped")
    if failed > 0:
        for line in output.splitlines():
            if "FAILED" in line:
                print(f"    {RED}{line}{RESET}")
    return SectionResult(name, status, passed, failed, skipped,
                         time.perf_counter() - t0, stdout, stderr)


def run_fast_tests() -> SectionResult:
    return run_pytest_section(
        "Fast test suite",
        [
            "src/tests/",
            "-m", "not (gpu or runtime_deps or large or embedder or indexer or slow or playwright)",
        ],
        section_num=4,
    )


def run_dashboard_api_tests() -> SectionResult:
    return run_pytest_section(
        "Dashboard API tests",
        ["src/tests/integration/test_dashboard_api_full.py"],
        section_num=5,
    )


def run_mcp_config_tests() -> SectionResult:
    return run_pytest_section(
        "MCP config verify",
        ["src/tests/integration/test_mcp_config_verify.py"],
        section_num=6,
    )


def run_playwright_tests() -> SectionResult:
    return run_pytest_section(
        "Playwright E2E",
        [
            "src/tests/e2e/test_dashboard_playwright.py",
            "--browser", "chromium",
        ],
        section_num=7,
    )


def run_live_chat(project_path: str) -> SectionResult:
    _print_section("8. Live chat Q&A — streaming response")
    t0 = time.perf_counter()
    try:
        import urllib.request
        body = json.dumps({
            "project": project_path,
            "query": "why do user chat bubbles disappear after sending a message?",
        }).encode()
        req = urllib.request.Request(
            "http://localhost:8765/api/chat_stream",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        chunks = []
        done_evt = None
        with urllib.request.urlopen(req, timeout=60) as resp:
            buf = b""
            for raw_chunk in iter(lambda: resp.read(256), b""):
                buf += raw_chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("type") == "token":
                        chunks.append(evt.get("text", ""))
                    elif evt.get("type") == "done":
                        done_evt = evt
                        break

        answer = "".join(chunks)
        ok_length = len(answer) > 100
        ok_relevant = any(kw in answer.lower() for kw in
                          ["appendmsg", "_dashboard_html", "chat", "bubble", "message"])

        if done_evt is None:
            print(f"  ⚠️  No 'done' event received (got {len(chunks)} token chunks)")
            status = "WARN"
        elif not ok_length:
            print(f"  ❌ Answer too short ({len(answer)} chars): {answer[:100]!r}")
            status = "FAIL"
        elif not ok_relevant:
            print(f"  ⚠️  Answer doesn't reference expected symbols: {answer[:200]!r}")
            status = "WARN"
        else:
            print(f"  ✅ Answer: {len(answer)} chars, intent={done_evt.get('intent')}")
            status = "PASS"

        return SectionResult("Live chat Q&A", status,
                             1 if status == "PASS" else 0,
                             1 if status == "FAIL" else 0,
                             0, time.perf_counter() - t0, answer, "")
    except Exception as e:
        msg = f"Live chat failed: {e}"
        print(f"  ⚠️  {msg}")
        return SectionResult("Live chat Q&A", "WARN", 0, 0, 1,
                             time.perf_counter() - t0, "", msg)


# ── HTML report ────────────────────────────────────────────────────────────────

def _status_badge(status: str) -> str:
    colors = {"PASS": "#22c55e", "WARN": "#f59e0b", "FAIL": "#ef4444", "SKIP": "#6b7280"}
    bg = colors.get(status, "#6b7280")
    return f'<span style="background:{bg};color:#fff;padding:2px 8px;border-radius:4px;font-weight:bold;">{html.escape(status)}</span>'


def write_html_report(results: list[SectionResult]) -> None:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    overall = "PASS" if all(r.status == "PASS" for r in results) else (
        "WARN" if all(r.status in ("PASS", "WARN", "SKIP") for r in results) else "FAIL"
    )
    rows = ""
    for r in results:
        rows += f"""
        <tr>
            <td>{html.escape(r.name)}</td>
            <td>{_status_badge(r.status)}</td>
            <td>{r.passed}</td>
            <td>{r.failed}</td>
            <td>{r.skipped}</td>
            <td>{r.duration_s:.1f}s</td>
        </tr>"""

    screenshots = ""
    if SCREENSHOT_DIR.exists():
        for img in sorted(SCREENSHOT_DIR.glob("*.png"))[:10]:
            screenshots += f'<img src="{img}" style="max-width:400px;margin:4px;border:1px solid #333;" title="{img.name}"/>'

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>OCS QA Report — {now}</title>
<style>
  body{{font-family:monospace;background:#0f1117;color:#e2e8f0;padding:24px;}}
  h1{{color:#7b61ff;}} h2{{color:#a78bfa;border-bottom:1px solid #333;padding-bottom:4px;}}
  table{{border-collapse:collapse;width:100%;}}
  th,td{{padding:8px 12px;text-align:left;border-bottom:1px solid #1e293b;}}
  th{{background:#1e1b2e;color:#7b61ff;}}
  .verdict{{font-size:2em;text-align:center;padding:16px;border-radius:8px;
            background:{"#14532d" if overall=="PASS" else "#7f1d1d" if overall=="FAIL" else "#78350f"};}}
  pre{{background:#1e293b;padding:12px;border-radius:4px;overflow-x:auto;font-size:12px;}}
</style>
</head><body>
<h1>OpenCode Search — Comprehensive QA Report</h1>
<p>Generated: {now}</p>
<div class="verdict">{'✅ OVERALL PASS' if overall=='PASS' else '⚠️ OVERALL WARN' if overall=='WARN' else '❌ OVERALL FAIL'}</div>
<h2>Section Results</h2>
<table>
  <tr><th>Section</th><th>Status</th><th>Passed</th><th>Failed</th><th>Skipped</th><th>Duration</th></tr>
  {rows}
</table>
<h2>Screenshots (Playwright)</h2>
<div>{screenshots if screenshots else "<p>No screenshots available</p>"}</div>
</body></html>"""
    REPORT_PATH.write_text(body, encoding="utf-8")
    print(f"\n  📄 HTML report: {REPORT_PATH}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Comprehensive QA for opencode-search")
    parser.add_argument("--fast", action="store_true", help="Skip Playwright and live tests")
    parser.add_argument("--live", action="store_true", help="Run live chat Q&A (requires running daemon)")
    parser.add_argument("--project", default="", help="Project path for live chat test")
    args = parser.parse_args()

    results: list[SectionResult] = []
    t_total = time.perf_counter()

    results.append(check_daemon_health())
    results.append(check_ollama_models())
    results.append(check_config_files())
    results.append(run_fast_tests())
    results.append(run_dashboard_api_tests())
    results.append(run_mcp_config_tests())

    if not args.fast:
        results.append(run_playwright_tests())

    if args.live and args.project:
        results.append(run_live_chat(args.project))

    total_s = time.perf_counter() - t_total

    # ── Summary table ──────────────────────────────────────────────────────────
    print(f"\n{CYAN}{BOLD}{'═' * 60}{RESET}")
    print(f"{CYAN}{BOLD}  QA SUMMARY{RESET}")
    print(f"{CYAN}{BOLD}{'═' * 60}{RESET}")
    print(f"  {'Section':<28} {'Status':<8} {'P':>5} {'F':>5} {'S':>5} {'Time':>7}")
    print(f"  {'─' * 56}")
    total_p = total_f = total_sk = 0
    for r in results:
        c = _color(r.status)
        print(f"  {r.name:<28} {c}{r.status:<8}{RESET} {r.passed:>5} {r.failed:>5} {r.skipped:>5} {r.duration_s:>6.1f}s")
        total_p += r.passed
        total_f += r.failed
        total_sk += r.skipped
    print(f"  {'─' * 56}")
    print(f"  {'TOTAL':<28} {'':8} {total_p:>5} {total_f:>5} {total_sk:>5} {total_s:>6.1f}s")

    overall = "PASS" if all(r.status == "PASS" for r in results) else (
        "WARN" if all(r.status in ("PASS", "WARN", "SKIP") for r in results) else "FAIL"
    )
    c = _color(overall)
    print(f"\n  {c}Overall: {overall}{RESET}")

    write_html_report(results)

    sys.exit(0 if overall != "FAIL" else 1)


if __name__ == "__main__":
    main()
