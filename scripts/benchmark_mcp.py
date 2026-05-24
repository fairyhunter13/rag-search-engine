#!/usr/bin/env python3
"""Benchmark opencode-search MCP tool adoption across Claude Code and Codex.

Runs 10 fixed questions against the indexed project and measures:
  - Whether the AI called list_indexed_projects before search_code
  - Whether search_code was called at all
  - Whether any bash search commands (grep/rg/find/glob/fd) were used instead
  - Token count per question

Usage:
  python scripts/benchmark_mcp.py --client claude [--model claude-haiku-4-5-20251001]
  python scripts/benchmark_mcp.py --client codex  [--model gpt-5.4-mini]
  python scripts/benchmark_mcp.py --client both

Output: Markdown table to stdout + results saved to /tmp/benchmark_mcp_<client>_<timestamp>.json
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

QUESTIONS = [
    "How does the watcher manager detect file changes?",
    "Where is the registry of indexed projects stored and what format is it?",
    "What embedding model is used for the budget tier?",
    "How does search_code rank and rerank results?",
    "Where is the MCP server's FastMCP instance created?",
    "What happens when a client disconnects — how are watchers cleaned up?",
    "How is the project DB path computed from the project root?",
    "What does _index_project do when force=False?",
    "How are chunked code segments stored in the database?",
    "What is the default top_k for search_code and where is it configured?",
]

WORKDIR = str(Path(__file__).resolve().parent.parent)

_BASH_SEARCH_RE = re.compile(
    r'\b(grep|rg|ag|find\b.*-name|-exec|glob|fd)\b'
)


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# Claude Code runner
# ---------------------------------------------------------------------------

def run_claude(question: str, model: str) -> dict[str, Any]:
    cmd = [
        "claude", "-p", question,
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
    ]
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(Path.home() / ".claude")}
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            cwd=WORKDIR, env=env,
        )
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "question": question}

    output = proc.stdout + proc.stderr
    return _parse_stream_json(output, question)


def _parse_stream_json(raw: str, question: str) -> dict[str, Any]:
    tool_calls: list[str] = []
    bash_commands: list[str] = []
    input_tokens = 0
    output_tokens = 0

    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        typ = obj.get("type", "")

        # Tool invocations
        if typ == "tool_use":
            name = obj.get("name", "")
            tool_calls.append(name)
            if name in ("Bash", "bash"):
                inp = obj.get("input", {})
                cmd_str = inp.get("command", "") if isinstance(inp, dict) else ""
                bash_commands.append(cmd_str)

        # Token usage
        if typ == "usage" or "usage" in obj:
            usage = obj.get("usage", obj)
            input_tokens += usage.get("input_tokens", 0)
            output_tokens += usage.get("output_tokens", 0)

    called_list = "list_indexed_projects" in tool_calls
    called_search = "search_code" in tool_calls
    bash_search_used = any(_BASH_SEARCH_RE.search(c) for c in bash_commands)

    # Check ordering: list_indexed_projects before search_code
    correct_order = False
    if called_list and called_search:
        try:
            correct_order = tool_calls.index("list_indexed_projects") < tool_calls.index("search_code")
        except ValueError:
            pass

    return {
        "question": question,
        "tool_calls": tool_calls,
        "called_list_indexed_projects": called_list,
        "called_search_code": called_search,
        "correct_order": correct_order,
        "bash_search_used": bash_search_used,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


# ---------------------------------------------------------------------------
# Codex runner  (codex exec --json JSONL format)
# ---------------------------------------------------------------------------

def run_codex(question: str, model: str) -> dict[str, Any]:
    # codex exec --json emits JSONL events; --dangerously-bypass-approvals-and-sandbox
    # is required so the sandbox doesn't cancel MCP tool calls.
    cmd = ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox"]
    if model:
        cmd += ["-m", model]
    cmd.append(question)

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180,
            cwd=WORKDIR,
        )
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "question": question}

    raw = proc.stdout + proc.stderr
    return _parse_codex_jsonl(raw, question)


def _parse_codex_jsonl(raw: str, question: str) -> dict[str, Any]:
    """Parse JSONL from `codex exec --json`.

    Relevant event shapes:
      {"type":"item.completed","item":{"type":"mcp_tool_call","server":"opencode-search","tool":"search_code",...}}
      {"type":"item.completed","item":{"type":"local_shell_call","action":{"command":"..."},...}}
      {"type":"turn.completed","usage":{"input_tokens":N,"output_tokens":N,...}}
    """
    tool_calls: list[str] = []       # opencode-search tool names in call order
    bash_commands: list[str] = []
    input_tokens = 0
    output_tokens = 0

    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        typ = obj.get("type", "")

        if typ == "item.completed":
            item = obj.get("item", {})
            item_type = item.get("type", "")

            if item_type == "mcp_tool_call" and item.get("server") == "opencode-search":
                tool_calls.append(item.get("tool", ""))

            if item_type == "local_shell_call":
                action = item.get("action", {})
                cmd_str = action.get("command", "") if isinstance(action, dict) else ""
                if cmd_str:
                    bash_commands.append(cmd_str)

        if typ == "turn.completed":
            usage = obj.get("usage", {})
            input_tokens += usage.get("input_tokens", 0)
            output_tokens += usage.get("output_tokens", 0)

    called_list = "list_indexed_projects" in tool_calls
    called_search = "search_code" in tool_calls
    bash_search_used = any(_BASH_SEARCH_RE.search(c) for c in bash_commands)

    correct_order = False
    if called_list and called_search:
        try:
            correct_order = tool_calls.index("list_indexed_projects") < tool_calls.index("search_code")
        except ValueError:
            pass

    return {
        "question": question,
        "tool_calls": tool_calls,
        "called_list_indexed_projects": called_list,
        "called_search_code": called_search,
        "correct_order": correct_order,
        "bash_search_used": bash_search_used,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _print_table(results: list[dict[str, Any]], client: str) -> None:
    print(f"\n## {client} benchmark results\n")
    header = "| # | Called list? | Called search? | Correct order? | Bash search? | Tokens |"
    sep    = "|---|:---:|:---:|:---:|:---:|---:|"
    print(header)
    print(sep)
    for i, r in enumerate(results, 1):
        if "error" in r:
            print(f"| {i} | ERROR | - | - | - | - |")
            continue
        tick = lambda v: "✓" if v else "✗"
        print(
            f"| {i} | {tick(r['called_list_indexed_projects'])} "
            f"| {tick(r['called_search_code'])} "
            f"| {tick(r['correct_order'])} "
            f"| {tick(r['bash_search_used'])} "
            f"| {r['total_tokens']} |"
        )

    valid = [r for r in results if "error" not in r]
    if not valid:
        return
    n = len(valid)
    rate = lambda key: sum(1 for r in valid if r[key]) / n * 100
    print(f"\n**Summary ({n} questions)**")
    print(f"- list_indexed_projects usage: {rate('called_list_indexed_projects'):.0f}%")
    print(f"- search_code usage: {rate('called_search_code'):.0f}%")
    print(f"- Correct ordering (list before search): {rate('correct_order'):.0f}%")
    print(f"- Bash search fallback rate: {rate('bash_search_used'):.0f}%")
    avg_tok = sum(r['total_tokens'] for r in valid) / n
    print(f"- Avg tokens/question: {avg_tok:.0f}")


def _save_json(results: list[dict[str, Any]], client: str, timestamp: str) -> str:
    path = f"/tmp/benchmark_mcp_{client}_{timestamp}.json"
    with open(path, "w") as f:
        json.dump({"client": client, "timestamp": timestamp, "results": results}, f, indent=2)
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _run_client(client: str, model: str) -> list[dict[str, Any]]:
    runner = run_claude if client == "claude" else run_codex
    results = []
    for i, q in enumerate(QUESTIONS, 1):
        print(f"  [{i}/{len(QUESTIONS)}] {q[:60]}...", flush=True)
        r = runner(q, model)
        results.append(r)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client", choices=["claude", "codex", "both"], default="both")
    parser.add_argument("--model", default="", help="Override model (client-specific default used if omitted)")
    parser.add_argument("--claude-model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--codex-model", default="gpt-5.4-mini")
    args = parser.parse_args()

    ts = _now()
    clients = ["claude", "codex"] if args.client == "both" else [args.client]

    for client in clients:
        model = args.model or (args.claude_model if client == "claude" else args.codex_model)
        print(f"\nRunning {client} benchmark with model={model} …")
        results = _run_client(client, model)
        _print_table(results, f"{client}/{model}")
        path = _save_json(results, client, ts)
        print(f"\nResults saved to {path}")


if __name__ == "__main__":
    main()
