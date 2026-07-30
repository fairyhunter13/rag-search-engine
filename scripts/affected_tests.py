#!/usr/bin/env python3
"""Print the pytest node-ids a working-tree change can affect. Inner dev loop only.

    .venv/bin/python scripts/affected_tests.py            # vs HEAD (staged + unstaged)
    .venv/bin/python scripts/affected_tests.py --base main
    .venv/bin/pytest $(.venv/bin/python scripts/affected_tests.py)

Running 716 live tests to check a ten-line edit is what makes a fully-live suite feel slow, and the
fix is not to make the tests less real — it is to run fewer of them while iterating. Nothing here is
stubbed: the selected tests are the same live tests, they just are not all of them.

**This is never a CI gate, and must never become one.** An `impact` query that misses an edge would
silently shrink the gate — the same defect shape as the registry tripwire this work exists for, in a
new place. CI keeps running the whole suite; this only shortens the feedback loop between runs. When
in doubt it over-selects, and it says so on stderr rather than pretending the set is exact.

Dogfoods `graph(relation="impact")` on the repo that implements it — the cheapest available proof
that the relation returns something worth acting on. Reads the graph store directly (no daemon call,
no LLM), so it is safe to run beside a live daemon.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
TESTS = SRC / "tests"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True,
                          timeout=60).stdout


def _changed_files(base: str) -> list[Path]:
    names = set(_git("diff", "--name-only", base).split())
    names |= set(_git("diff", "--name-only", "--cached", base).split())
    names |= set(_git("ls-files", "--others", "--exclude-standard").split())
    return [REPO / n for n in sorted(names) if n.endswith(".py") and (REPO / n).exists()]


def _changed_line_numbers(path: Path, base: str) -> set[int]:
    """New-file line numbers touched by the diff. An untracked file counts as wholly changed."""
    out = _git("diff", "-U0", base, "--", str(path.relative_to(REPO)))
    if not out.strip():
        try:
            return set(range(1, len(path.read_text().splitlines()) + 1))
        except OSError:
            return set()
    lines: set[int] = set()
    for hunk in out.splitlines():
        if not hunk.startswith("@@"):
            continue
        try:  # @@ -old,n +new,m @@
            new = hunk.split("+", 1)[1].split("@@")[0].strip()
            start, _, count = new.partition(",")
            lines.update(range(int(start), int(start) + max(int(count or 1), 1)))
        except (ValueError, IndexError):
            continue
    return lines


def _symbols_at(path: Path, lines: set[int]) -> set[str]:
    """Names of the defs/classes whose body spans any changed line.

    Nested defs report their own name, not the enclosing one: `impact` is keyed on the symbol that
    actually changed, and widening to the parent would pull in callers of code nobody touched.
    """
    try:
        tree = ast.parse(path.read_text(), str(path))
    except (OSError, SyntaxError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        end = getattr(node, "end_lineno", node.lineno)
        if any(node.lineno <= n <= end for n in lines):
            found.add(node.name)
    return found


def _impacted_tests(symbol: str) -> set[str]:
    from rag_search.query.graph_handler import run_graph
    try:
        payload = json.loads(run_graph(symbol, str(REPO), "impact"))
    except Exception as exc:  # a graph miss must not take the loop down
        print(f"  ! impact({symbol}) failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return set()
    if "error" in payload:
        print(f"  ! impact({symbol}): {payload['error']}", file=sys.stderr)
        return set()
    node_ids = set()
    for m in payload.get("matches", []):
        f, name = m.get("file", ""), m.get("name", "")
        if not f or not name.startswith("test_"):
            continue
        p = Path(f)
        if TESTS in p.parents:
            node_ids.add(f"{p.relative_to(REPO)}::{name}")
    return node_ids


def main(argv: list[str]) -> int:
    base = "HEAD"
    if "--base" in argv:
        base = argv[argv.index("--base") + 1]
    sys.path.insert(0, str(SRC))

    changed = _changed_files(base)
    if not changed:
        print(f"no changed .py files vs {base}", file=sys.stderr)
        return 0

    node_ids: set[str] = set()
    whole_files: set[str] = set()
    symbols: set[str] = set()
    for path in changed:
        rel = path.relative_to(REPO)
        if TESTS in path.parents:
            # A changed test file runs whole. Selecting only its changed functions would skip the
            # module-scoped fixtures and collection-time guards its other tests share.
            whole_files.add(str(rel))
            continue
        symbols |= _symbols_at(path, _changed_line_numbers(path, base))
    for sym in sorted(symbols):
        node_ids |= _impacted_tests(sym)

    print(f"changed: {len(changed)} file(s), {len(symbols)} symbol(s) vs {base}", file=sys.stderr)
    if symbols and not node_ids and not whole_files:
        print("WARNING: no test reached these symbols. That may mean they are untested, or that "
              "the graph is stale — run the full suite, not this.", file=sys.stderr)
    for n in sorted(whole_files | node_ids):
        print(n)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
