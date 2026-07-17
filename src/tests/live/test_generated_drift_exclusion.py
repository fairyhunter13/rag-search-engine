"""Live tests: machine-generated code files never wake the enrich/wiki/BPRE cascade.

Regression for the inosoft-project idle-CPU loop: its SvelteKit dashboard rewrites
`wiki/src/lib/*.generated.js` every ~45-78s. tree-sitter parses those as `javascript`
(= code), which used to flip both code-drift signals and force a full 190-repo BPRE
rebuild each cycle. `is_generated_path()` now excludes them from the drift signals.

GEN1 — is_generated_path() truth table (conservative markers only).
GEN2 — sweeps._code_source_fingerprint() is unchanged when only a *.generated.js mtime bumps;
       changes when a real source file bumps.
GEN3 — bpre._bpre_code_sig() has the same behavior (BPRE's own reuse stamp).

Cache note: both sigs memoize on the root dir's coarse mtime, and bumping a *file* mtime
does not change the parent dir mtime — so each helper invalidates the cache exactly as
daemon.sweeps.on_change does before recomputing.
"""
from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.live


def test_gen1_is_generated_path_truth_table():
    """GEN1: only unambiguous codegen markers match; hand-written source never does."""
    from rag_search.index.discover import is_generated_path

    generated = [
        "wiki/src/lib/classDiagram.generated.js",
        "wiki/src/lib/sequences.generated.json",
        "api/user.gen.go",
        "proto/user_pb2.py",
        "proto/user_pb2_grpc.py",
        "svc/user.pb.go",
        "model/user.freezed.dart",
    ]
    hand_written = [
        "src/main.go",
        "src/handler.py",
        "wiki/src/lib/store.js",
        "generated_report.py",  # 'generated' in the stem, but no codegen marker
    ]
    for p in generated:
        assert is_generated_path(p), f"{p} should be generated"
    for p in hand_written:
        assert not is_generated_path(p), f"{p} should NOT be generated"


def test_gen2_code_source_fingerprint_ignores_generated_churn(safe_tmp_path):
    """GEN2: regenerating a *.generated.js leaves _code_source_fingerprint unchanged."""
    from rag_search.daemon import sweeps

    proj = safe_tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "wiki" / "src" / "lib").mkdir(parents=True)
    real = proj / "src" / "main.py"
    real.write_text("def x():\n    return 1\n")
    gen = proj / "wiki" / "src" / "lib" / "diagram.generated.js"
    gen.write_text("export const d = 1;\n")

    def sig() -> str:
        sweeps._code_fingerprint_cache.pop(str(proj), None)  # mirror on_change invalidation
        return sweeps._code_source_fingerprint(str(proj))

    base = sig()
    future = time.time() + 120
    os.utime(gen, (future, future))
    assert sig() == base, "regenerating *.generated.js must NOT wake the code-drift gate"
    os.utime(real, (future, future))
    assert sig() != base, "editing real source MUST change the code-drift sig"


def test_gen4_is_ignored_path_drops_generated_files(safe_tmp_path):
    """GEN4: generated files are dropped by the shared resolver (watcher + indexer + _index_files),
    so regenerating them never triggers a re-embed; hand-written source is kept."""
    from rag_search.index.discover import is_ignored_path

    root = safe_tmp_path
    (root / "wiki" / "src" / "lib").mkdir(parents=True)
    (root / "src").mkdir()
    gen = root / "wiki" / "src" / "lib" / "diagram.generated.js"
    gen.write_text("export const d = 1;\n")
    real = root / "src" / "main.py"
    real.write_text("def x():\n    return 1\n")
    svelte = root / "wiki" / "src" / "lib" / "Diagram.svelte"
    svelte.write_text("<script>export let d;</script>\n")

    assert is_ignored_path(gen, root), "generated file must be dropped (no watch/index/embed)"
    assert not is_ignored_path(real, root), "real source must be kept"
    assert not is_ignored_path(svelte, root), "hand-written renderer must be kept"


def test_gen3_bpre_code_sig_ignores_generated_churn(safe_tmp_path):
    """GEN3: regenerating a *.generated.js does not flip BPRE's per-member reuse stamp."""
    from rag_search.kb import bpre

    member = safe_tmp_path / "member"
    (member / "src").mkdir(parents=True)
    (member / "wiki" / "src" / "lib").mkdir(parents=True)
    real = member / "src" / "handler.go"
    real.write_text("package main\nfunc H() {}\n")
    gen = member / "wiki" / "src" / "lib" / "diagram.generated.js"
    gen.write_text("export const d = 1;\n")

    def sig() -> str:
        bpre._invalidate_bpre_code_sig(str(member))  # mirror on_change invalidation
        return bpre._bpre_code_sig(str(member))

    base = sig()
    future = time.time() + 120
    os.utime(gen, (future, future))
    assert sig() == base, "regenerating *.generated.js must NOT flip the BPRE reuse stamp"
    os.utime(real, (future, future))
    assert sig() != base, "editing real source MUST flip the BPRE code sig"
