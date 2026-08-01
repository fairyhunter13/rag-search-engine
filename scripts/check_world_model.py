#!/usr/bin/env python3
"""WS-D: GPU-free, daemon-free RSE world-model fulfillment checker.

Evaluates L1 invariant check predicates from docs/world-model/model.yaml
against the working tree (default) or a specific git diff.

Exit codes: 0=CONFORMS, 1=AT_RISK, 2=error.
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YAML = ROOT / "docs" / "world-model" / "model.yaml"


def _parse_invariants(yaml_raw: str) -> list[dict]:
    entries = []
    block_re = re.compile(r"- id: (P\d+)\s+principle: \"([^\"]+)\"\s+check: (.+?)(?=\n  - id:|\nL2_|\Z)", re.DOTALL)
    for m in block_re.finditer(yaml_raw):
        pid, principle, check_raw = m.group(1), m.group(2), m.group(3).strip()
        if check_raw == "null":
            entries.append({"id": pid, "principle": principle, "check": None})
            continue
        grep_m = re.search(r'grep: "([^"]+)"', check_raw)
        paths_m = re.search(r'paths: "([^"]+)"', check_raw)
        verdict_m = re.search(r'verdict_if_match: (\w+)', check_raw)
        exclude_m = re.search(r'exclude_paths: "([^"]+)"', check_raw)
        if grep_m and paths_m:
            entries.append({
                "id": pid, "principle": principle,
                "check": {
                    "grep": grep_m.group(1).replace("\\\\", "\\"),
                    "paths": paths_m.group(1),
                    "exclude_paths": exclude_m.group(1).split() if exclude_m else [],
                    "verdict_if_match": verdict_m.group(1) if verdict_m else "AT_RISK",
                },
            })
        else:
            entries.append({"id": pid, "principle": principle, "check": None})
    return entries


def _git_changed_files(base: str | None, head: str | None) -> list[str]:
    if base and head:
        out = subprocess.run(["git", "diff", "--name-only", base, head], capture_output=True, text=True, cwd=ROOT)
    else:
        out = subprocess.run(["git", "diff", "--name-only", "HEAD"], capture_output=True, text=True, cwd=ROOT)
        staged = subprocess.run(["git", "diff", "--name-only", "--cached"], capture_output=True, text=True, cwd=ROOT)
        return (out.stdout + staged.stdout).splitlines()
    return out.stdout.splitlines()


def _strip_prose(text: str) -> str:
    """Remove comments and docstrings: every check here greps for source tokens, and prose is not one.

    Whole-line `#` comments have been stripped since this file was written. Docstrings were not —
    and in this codebase that is where nearly all the prose lives, including the prose that
    *explains the invariants*. P3 (`cross.*repo.*edge`) had been reporting AT_RISK against
    `sweeps._graph_reconcile_action`'s docstring, a sentence stating what HR4 forbids, which made
    a green run impossible for anyone who did not already know the match was spurious. A
    conformance checker that is permanently red is one nobody reads, so the false positive costs
    more than the invariant it protects.

    Fixed here rather than by narrowing P3's pattern, because this is the class and not the
    instance: any check whose tokens someone might one day *write about* has the same failure
    waiting, and P6's own principle string already records the last time one had to be widened
    around an accidental match.

    `ast` rather than a regex: a line-oriented pattern cannot find the end of a triple-quoted
    string, and a stripper that guesses wrong hides real violations instead of false ones. The
    original text is parsed — `ast` ignores comments already — so removing the `#` lines
    afterwards cannot disturb the line numbers the walk returns. A file that does not parse is
    scanned with its docstrings intact: unparseable is a reason to look harder, not less.
    """
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)) or not body:
                continue
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                for i in range(first.lineno - 1, first.end_lineno or first.lineno):
                    lines[i] = ""
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def _check_one(inv: dict, changed_files: list[str]) -> tuple[str, str | None]:
    chk = inv["check"]
    if chk is None:
        return "MANUAL", None
    pattern = chk["grep"]
    path_specs = chk["paths"].split()
    exclude_paths = {Path(p) for p in chk.get("exclude_paths", [])}
    verdict_if_match = chk["verdict_if_match"]

    relevant = []
    for f in changed_files:
        fp = Path(f)
        if any(fp == ep or str(fp).endswith(str(ep)) for ep in exclude_paths):
            continue
        for spec in path_specs:
            sp = Path(spec)
            if (sp.is_dir() and str(fp).startswith(str(sp))) or fp == sp or fp.name == sp.name:
                relevant.append(ROOT / fp)
                break

    for fpath in relevant:
        if not fpath.exists():
            continue
        try:
            text = _strip_prose(fpath.read_text(errors="replace"))
        except OSError:
            continue
        if re.search(pattern, text):
            return verdict_if_match, f"{fpath.relative_to(ROOT)} matches /{pattern}/"
    return "CONFORMS", None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RSE world-model fulfillment checker")
    ap.add_argument("--base", default=None, help="git base ref (default: HEAD)")
    ap.add_argument("--head", default=None, help="git head ref (default: working tree)")
    ap.add_argument("--all", action="store_true", help="check all source files, not just diff")
    args = ap.parse_args(argv)

    if not YAML.exists():
        print(f"ERROR: {YAML} not found", file=sys.stderr)
        return 2

    yaml_raw = YAML.read_text()
    invariants = _parse_invariants(yaml_raw)
    if not invariants:
        print("ERROR: no invariants parsed from model.yaml", file=sys.stderr)
        return 2

    if args.all:
        changed_files = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py") if ".venv" not in str(p)]
    else:
        changed_files = _git_changed_files(args.base, args.head)
        if not changed_files:
            print("No changed files — running full source scan.")
            changed_files = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py") if ".venv" not in str(p)]

    at_risk = []
    for inv in invariants:
        verdict, detail = _check_one(inv, changed_files)
        status = f"[{verdict:8s}]" if verdict != "MANUAL" else "[MANUAL  ]"
        msg = f"{status} {inv['id']}: {inv['principle'][:72]}"
        if detail:
            msg += f"\n           MATCH: {detail}"
        print(msg)
        if verdict == "AT_RISK":
            at_risk.append(inv["id"])

    print()
    if at_risk:
        print(f"AT_RISK: {', '.join(at_risk)} — review before merging.")
        return 1
    print("CONFORMS — all checkable L1 invariants satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
