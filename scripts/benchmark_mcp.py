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

_EXPECTED_ANSWER_RULES: dict[str, list[list[str]]] = {
    "Where is the registry of indexed projects stored and what format is it?": [
        ["~/.local/share/opencode-search/projects.json"],
        ["json"],
    ],
    "What embedding model is used for the budget tier?": [
        ["jinaai/jina-embeddings-v2-small-en", "jina-embeddings-v2-small-en"],
    ],
    "How does search_code rank and rerank results?": [
        ["stage 1", "vector"],
        ["rerank"],
    ],
    "Where is the MCP server's FastMCP instance created?": [
        ["mcp.py"],
        ["fastmcp"],
    ],
    "What is the default top_k for search_code and where is it configured?": [
        ["10"],
        ["final_top_k"],
    ],
}

_MCP_FIRST_PROMPT_PREFIX = (
    "Use the opencode-search MCP tools to answer this question.\n"
    "Step 1: Call list_indexed_projects.\n"
    "Step 2: Call search_code with query EXACTLY equal to the question string (verbatim; do not paraphrase or add extra words).\n"
    "Step 3: Answer based strictly on the results, and cite the file paths + key symbols (function/constant names) you relied on.\n\n"
    "Question: "
)

WORKDIR = str(Path(__file__).resolve().parent.parent)

# Resolve the claude binary using the *shell's* PATH so nvm-managed node
# installations are found even when the benchmark is run from a Python
# subprocess that inherits a stripped PATH.
def _find_binary(name: str) -> str:
    """Return the absolute path to `name` via `which`, falling back to bare name."""
    import shutil
    # shutil.which searches os.environ['PATH'], which may be stripped.
    # Run `which` in a login shell to pick up nvm / rbenv / etc.
    try:
        result = subprocess.run(
            ["bash", "-lc", f"which {name}"],
            capture_output=True, text=True, timeout=5,
        )
        path = result.stdout.strip()
        if path:
            return path
    except Exception:
        pass
    return shutil.which(name) or name

_CLAUDE_BIN = _find_binary("claude")

_BASH_SEARCH_RE = re.compile(
    r'\b(grep|rg|ag|find\b.*-name|-exec|glob|fd)\b'
)


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


def _normalize_text(text: str) -> str:
    return " ".join(text.lower().split())


def _answer_is_valid(question: str, answer: str) -> bool | None:
    groups = _EXPECTED_ANSWER_RULES.get(question)
    if not groups:
        return None
    haystack = _normalize_text(answer)
    return all(any(token in haystack for token in group) for group in groups)


# ---------------------------------------------------------------------------
# Claude Code runner
# ---------------------------------------------------------------------------

# Claude Code normalises the MCP server name differently in different contexts:
# interactive sessions emit 'mcp__opencode-search__' (hyphen, matching the server name)
# while headless -p mode emits 'mcp__opencode_search__' (underscore, sanitised form).
# We must handle both.
_MCP_PREFIXES = ("mcp__opencode-search__", "mcp__opencode_search__")

def _strip_mcp_prefix(name: str) -> str:
    """Normalise MCP tool name → bare tool name (e.g. 'search_code')."""
    for prefix in _MCP_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


_CLAUDE_SYSTEM_PROMPT = (
    "MANDATORY RULE — no exceptions: Before using any native tool (Bash, Grep, Glob, Read) "
    "for code or file exploration, you MUST first call list_indexed_projects, then immediately "
    "call search_code. The search_code query must be EXACTLY the user question string (verbatim). "
    "Only use native tools if search_code returns no useful results. "
    "Do NOT answer from memory or training data. Do NOT skip the MCP tools step."
)


def run_claude(question: str, model: str) -> dict[str, Any]:
    # Block all native file-exploration tools: Bash, Grep, Glob, Read.
    # With no file tools, the model's only way to get code content is via MCP —
    # search_code returns relevant code chunks directly.
    # --allowedTools pre-approves MCP tools so no permission dialog blocks them.
    # Without Bash/Grep/Glob/Read in allowedTools they are effectively disabled
    # (headless -p has no user to approve them).
    disallowed_tools = "Bash Grep Glob Read"
    allowed = (
        "mcp__opencode-search__list_indexed_projects"
        " mcp__opencode-search__search_code"
        " mcp__opencode-search__project_status"
        " mcp__opencode-search__search_metrics"
    )
    # Prefix the question with an explicit MCP-first instruction so the model
    # treats tool ordering as part of the task, not just background guidance.
    # claude -p is single-shot headless; without this, the model ignores CLAUDE.md
    # workflow rules and reaches for native tools (Bash/Grep) directly.
    prompted_question = f"{_MCP_FIRST_PROMPT_PREFIX}{question}"
    cmd = [
        _CLAUDE_BIN, "-p", prompted_question,
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--disallowedTools", disallowed_tools,
        "--allowedTools", allowed,
        "--append-system-prompt", _CLAUDE_SYSTEM_PROMPT,
    ]
    # Do NOT override CLAUDE_CONFIG_DIR. The user-scope MCP server registration
    # lives in ~/.claude.json (home root), not ~/.claude/.claude.json. When
    # CLAUDE_CONFIG_DIR is set to ~/.claude, Claude reads ~/.claude/.claude.json
    # instead and loses the opencode-search MCP server registration entirely.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180,
            cwd=WORKDIR, env=env,
        )
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "question": question}

    output = proc.stdout + proc.stderr
    return _parse_stream_json(output, question)


def _parse_stream_json(raw: str, question: str) -> dict[str, Any]:
    tool_calls: list[str] = []   # bare tool names (mcp prefix stripped)
    bash_commands: list[str] = []
    answer_parts: list[str] = []
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

        # stream-json tool_use events live inside assistant message content blocks
        if typ == "assistant":
            for block in obj.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    raw_name = block.get("name", "")
                    name = _strip_mcp_prefix(raw_name)
                    tool_calls.append(name)
                    if name in ("Bash", "bash"):
                        inp = block.get("input", {})
                        bash_commands.append(inp.get("command", "") if isinstance(inp, dict) else "")
                elif block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        answer_parts.append(text)

        # Also handle top-level tool_use (older format)
        if typ == "tool_use":
            raw_name = obj.get("name", "")
            name = _strip_mcp_prefix(raw_name)
            tool_calls.append(name)
            if name in ("Bash", "bash"):
                inp = obj.get("input", {})
                bash_commands.append(inp.get("command", "") if isinstance(inp, dict) else "")

        # Token usage — in stream-json it appears inside message.usage
        if typ == "assistant":
            usage = obj.get("message", {}).get("usage", {})
            input_tokens += usage.get("input_tokens", 0)
            output_tokens += usage.get("output_tokens", 0)
        if typ == "result":
            usage = obj.get("usage", {})
            input_tokens += usage.get("input_tokens", 0)
            output_tokens += usage.get("output_tokens", 0)

    answer_text = "\n".join(answer_parts).strip()
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
        "answer": answer_text[:8000],
        "answer_excerpt": " ".join(answer_text.split())[:400],
        "answer_valid": _answer_is_valid(question, answer_text),
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
    cmd.append(f"{_MCP_FIRST_PROMPT_PREFIX}{question}")

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
    answer_parts: list[str] = []
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
            if item_type == "assistant_message":
                for block in item.get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            answer_parts.append(text)
            if item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    answer_parts.append(text)

        if typ == "turn.completed":
            usage = obj.get("usage", {})
            input_tokens += usage.get("input_tokens", 0)
            output_tokens += usage.get("output_tokens", 0)

    answer_text = "\n".join(answer_parts).strip()
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
        "answer": answer_text[:8000],
        "answer_excerpt": " ".join(answer_text.split())[:400],
        "answer_valid": _answer_is_valid(question, answer_text),
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _print_table(results: list[dict[str, Any]], client: str) -> None:
    print(f"\n## {client} benchmark results\n")
    header = "| # | Called list? | Called search? | Correct order? | Bash search? | Answer valid? | Tokens |"
    sep    = "|---|:---:|:---:|:---:|:---:|:---:|---:|"
    print(header)
    print(sep)
    for i, r in enumerate(results, 1):
        if "error" in r:
            print(f"| {i} | ERROR | - | - | - | - | - |")
            continue
        tick = lambda v: "✓" if v else "✗"
        answer_valid = r.get("answer_valid")
        answer_cell = "n/a" if answer_valid is None else tick(answer_valid)
        print(
            f"| {i} | {tick(r['called_list_indexed_projects'])} "
            f"| {tick(r['called_search_code'])} "
            f"| {tick(r['correct_order'])} "
            f"| {tick(r['bash_search_used'])} "
            f"| {answer_cell} "
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
    answer_scored = [r for r in valid if r.get("answer_valid") is not None]
    if answer_scored:
        answer_rate = sum(1 for r in answer_scored if r["answer_valid"]) / len(answer_scored) * 100
        print(f"- Answer validity rate (scored questions): {answer_rate:.0f}%")
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

def _select_questions(
    *,
    requested: list[int] | None,
    max_questions: int | None,
) -> list[str]:
    if requested:
        selected: list[str] = []
        for index in requested:
            if index < 1 or index > len(QUESTIONS):
                raise ValueError(f"Question index {index} is out of range 1..{len(QUESTIONS)}")
            selected.append(QUESTIONS[index - 1])
        return selected

    if max_questions is not None:
        return QUESTIONS[: max(0, min(len(QUESTIONS), max_questions))]

    return QUESTIONS


def _run_client(client: str, model: str, questions: list[str]) -> list[dict[str, Any]]:
    runner = run_claude if client == "claude" else run_codex
    results = []
    total = len(questions)
    for i, q in enumerate(questions, 1):
        print(f"  [{i}/{total}] {q[:60]}...", flush=True)
        r = runner(q, model)
        results.append(r)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client", choices=["claude", "codex", "both"], default="both")
    parser.add_argument("--model", default="", help="Override model (client-specific default used if omitted)")
    parser.add_argument("--claude-model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--codex-model", default="gpt-5.4-mini")
    parser.add_argument(
        "--question",
        dest="questions",
        type=int,
        action="append",
        help="Run only specific 1-based question numbers (repeatable).",
    )
    parser.add_argument(
        "--max-questions",
        type=int,
        default=None,
        help="Run only the first N questions.",
    )
    args = parser.parse_args()

    ts = _now()
    clients = ["claude", "codex"] if args.client == "both" else [args.client]
    questions = _select_questions(requested=args.questions, max_questions=args.max_questions)

    for client in clients:
        model = args.model or (args.claude_model if client == "claude" else args.codex_model)
        print(f"\nRunning {client} benchmark with model={model} …")
        results = _run_client(client, model, questions)
        _print_table(results, f"{client}/{model}")
        path = _save_json(results, client, ts)
        print(f"\nResults saved to {path}")


if __name__ == "__main__":
    main()
