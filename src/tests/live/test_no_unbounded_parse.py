"""HR39 guard: every tree-sitter parse is bounded (subprocess pool), no exception.

Two invariants enforced:
1. Only the whitelisted worker-executed modules (graph/extractor.py, kb/bpre_ast.py) may
   contain a direct tree-sitter `.parse(` call — every other module's parses must go through
   `index.bounded_parse.run_bounded`. Nodes aren't picklable, so parsing must physically live
   inside the extraction function's own source, not at the call-site — this is why the guard
   is scoped to "outside the worker modules", not a bare "no .parse( anywhere" ban.
2. Any module that calls one of the worker-executed extraction functions must import
   `run_bounded` — a bare, unbounded call to `extract_symbols`/`scan_file`/etc. from a new
   production call-site is a regression this test catches immediately (covers future hot paths).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_ROOT = Path(__file__).resolve().parents[2] / "rag_search"

_WORKER_MODULES = {"graph/extractor.py", "kb/bpre_ast.py"}
_PARSE_CALL_RE = re.compile(r"get_parser\([^)]*\)\.parse\(|\bparser\.parse\(")
_WORKER_FUNCS = (
    "extract_symbols(", "extract_calls_with_lines(", "extract_calls(",
    "extract_call_sites(", "scan_file(", "_scan_pb_go_file(",
)


def _all_py_files() -> list[Path]:
    return sorted(_ROOT.rglob("*.py"))


def test_no_direct_parse_outside_worker_modules() -> None:
    violations: list[str] = []
    for py in _all_py_files():
        rel = py.relative_to(_ROOT).as_posix()
        if rel in _WORKER_MODULES or rel == "index/bounded_parse.py":
            continue
        src = py.read_text(errors="replace")
        if _PARSE_CALL_RE.search(src):
            violations.append(rel)
    assert not violations, (
        "Unbounded tree-sitter .parse( call found outside the worker-executed modules "
        f"({sorted(_WORKER_MODULES)}) — route through index.bounded_parse.run_bounded:\n"
        + "\n".join(violations)
    )


def test_worker_functions_only_invoked_via_run_bounded() -> None:
    violations: list[str] = []
    for py in _all_py_files():
        rel = py.relative_to(_ROOT).as_posix()
        if rel in _WORKER_MODULES:
            continue  # definitions live here, not call-sites
        src = py.read_text(errors="replace")
        called = [fn for fn in _WORKER_FUNCS if fn in src]
        if called and "run_bounded" not in src:
            violations.append(f"{rel}: calls {called} without importing run_bounded")
    assert not violations, (
        "Worker-executed extraction function called without routing through "
        "index.bounded_parse.run_bounded:\n" + "\n".join(violations)
    )


def test_worker_modules_are_exhaustive() -> None:
    """Whitelist accuracy: each entry must actually still contain a direct parse call."""
    for rel in _WORKER_MODULES:
        src = (_ROOT / rel).read_text(errors="replace")
        assert _PARSE_CALL_RE.search(src), (
            f"{rel} is whitelisted as a worker module but no longer contains a direct "
            "tree-sitter parse — remove it from _WORKER_MODULES"
        )


def test_bounded_parse_workers_never_import_embedder() -> None:
    """GPU-only doctrine: bounded_parse workers are CPU-only, must never import CUDA/embedder."""
    src = (_ROOT / "index/bounded_parse.py").read_text(errors="replace")
    for banned in ("fastembed", "onnxruntime", "CUDAExecutionProvider", "embed.model"):
        assert banned not in src, f"bounded_parse.py must not reference {banned!r} (CPU-only workers)"
    assert 'mp.get_context("spawn")' in src, "workers must use spawn, never fork (CUDA-after-fork is unsafe)"
