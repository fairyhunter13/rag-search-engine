"""Live tests: machine-generated code files never wake the enrich/wiki/BPRE cascade.

Regression for the inosoft-project idle-CPU loop: its SvelteKit dashboard rewrites
`wiki/src/lib/*.generated.js` every ~45-78s. tree-sitter parses those as `javascript`
(= code), which used to flip both code-drift signals and force a full 190-repo BPRE
rebuild each cycle. `is_generated_path()` now excludes them from the drift signals.

GEN1 — is_generated_path() truth table (conservative markers only).
GEN2 — sweeps._code_source_fingerprint() is unchanged when only a *.generated.js mtime bumps;
       changes when a real source file bumps.
GEN4 — is_ignored_path() drops generated files for watcher + indexer + _index_files alike.

GEN3 asserted the identical property for `bpre._bpre_code_sig`, BPRE's own per-member reuse
stamp — a second drift signal that had to agree with the first. It left with tier 3, and the
property did not need re-pointing: `_code_source_fingerprint` is now the only stamp that
decides whether a member is re-derived, and GEN2 is that assertion.

Cache note: the sig memoizes on the root dir's coarse mtime, and bumping a *file* mtime
does not change the parent dir mtime — so the helper invalidates the cache exactly as
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
        # Build artifacts, added 2026-07-31: 70 files / 2,738 chunks. Each has its input
        # versioned beside it, so indexing it stores the same information twice.
        "composer.lock",
        "Cargo.lock",
        "go.sum",
        "frontend/package-lock.json",
        "public/js/app.min.js",
        "public/css/style.min.css",
        "public/js/bundle.js.map",
        "public/css/style.css.map",
    ]
    hand_written = [
        "src/main.go",
        "src/handler.py",
        "wiki/src/lib/store.js",
        "generated_report.py",  # 'generated' in the stem, but no codegen marker
        "src/sourcemap.py",     # 'map' in the stem; `.js.map`/`.css.map` are the whole marker
        "internal/lockfile.go",
        "frontend/package.json",  # only the -lock sibling is derived
        "pkg/sum.go",             # `go.sum` is matched whole, never as a suffix
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
