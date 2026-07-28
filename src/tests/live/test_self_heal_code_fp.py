"""Live tests: _code_fingerprint() and the self-heal version stamp hardening.

SH1 — _code_fingerprint() is stable across two calls (byte-equal).
SH2 — _code_fingerprint() changes when a tracked module's bytes change.
SH2b — every module _FINGERPRINT_MODULES names still exists (a deleted one hashes silently).
SH3 — _pipeline_algo_version() includes the code fingerprint component.
SH4 — baseline-seed writes the new stamp without touching symbols or communities.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.live


def test_sh1_code_fingerprint_stable():
    """SH1: two calls return the same value (deterministic, byte-equal)."""
    from rag_search.daemon.sweeps import _code_fingerprint
    assert _code_fingerprint() == _code_fingerprint()


def _module_root() -> Path:
    return Path(__file__).resolve().parents[2] / "rag_search"  # src/tests/live/ -> src/


def test_sh2_code_fingerprint_changes_on_module_edit(tmp_path):
    """SH2: the production hasher's output moves when a tracked module's bytes move.

    This used to re-implement the hash beside the real one over a hand-written module list, so it
    asserted its own arithmetic and not the shipped function — and when `graph/enrich.py` was
    deleted with tier 3 the copy raised FileNotFoundError while `_code_fingerprint()` itself,
    which suppresses OSError, went on working. It now copies the tracked modules to tmp_path,
    checks the copies hash to the live fingerprint, then perturbs one copy on disk. Nothing under
    src/ is written: a real edit there would re-derive all 160 fleet graphs.
    """
    from rag_search.daemon.sweeps import (
        _FINGERPRINT_MODULES,
        _code_fingerprint,
        _fingerprint_paths,
    )

    root = _module_root()
    copies = []
    for rel in _FINGERPRINT_MODULES:
        dst = tmp_path / rel.replace("/", "__")
        dst.write_bytes((root / rel).read_bytes())
        copies.append(dst)

    assert _fingerprint_paths(copies) == _code_fingerprint(), (
        "byte-identical copies must hash to the live fingerprint — otherwise the perturbation "
        "below proves nothing about the shipped function"
    )
    copies[0].write_bytes(copies[0].read_bytes() + b"\n# perturbed")
    assert _fingerprint_paths(copies) != _code_fingerprint(), (
        f"perturbing {_FINGERPRINT_MODULES[0]} left the fingerprint unchanged — a code-only "
        "change to the graph pipeline would not self-heal the fleet"
    )


def test_sh2b_every_fingerprinted_module_exists():
    """SH2b: no dead entry in _FINGERPRINT_MODULES.

    `_code_fingerprint` suppresses OSError, so a module deleted out from under the list is silent:
    it contributes zero bytes and the fingerprint keeps working, while the list now claims to
    track a file whose changes it cannot see. That is what `graph/enrich.py` was after R0.
    """
    from rag_search.daemon.sweeps import _FINGERPRINT_MODULES

    root = _module_root()
    missing = [rel for rel in _FINGERPRINT_MODULES if not (root / rel).exists()]
    assert not missing, (
        f"_FINGERPRINT_MODULES names files that do not exist: {missing} — drop them, or the "
        "fingerprint silently stops covering what it advertises"
    )


def test_sh3_algo_version_includes_every_component():
    """SH3: every component of the graph identity is present and in its own slot.

    Was a bare `len(parts) == 2`, which is the [[feedback_guard_tests_must_discriminate]] shape:
    it says nothing about *which* two, so swapping a component for a constant would pass. It went
    red when S3 added EXTRACTOR_REV — correctly, since the identity changed — and it is rewritten
    rather than re-counted, so adding a fourth component fails only if the new one is unstamped.
    """
    from rag_search.daemon.sweeps import _code_fingerprint, _pipeline_algo_version
    from rag_search.graph.community import ALGO_VERSION
    from rag_search.graph.extractor import EXTRACTOR_REV
    ver = _pipeline_algo_version()
    parts = ver.split("+")
    assert parts == [ALGO_VERSION, EXTRACTOR_REV, _code_fingerprint()], (
        f"algo_version {ver!r} does not compose ALGO_VERSION + EXTRACTOR_REV + code_fp"
    )


def test_sh4_baseline_seed_no_mutation(safe_tmp_path):
    """SH4: seeding the stamp doesn't mutate symbols or communities."""
    from rag_search.core.config import ProjectEntry, project_graph_db
    from rag_search.core.registry import remove_project, upsert_project
    from rag_search.daemon.sweeps import _code_source_fingerprint, _pipeline_algo_version
    from rag_search.graph.store import GraphStore

    proj = str(safe_tmp_path)
    upsert_project(ProjectEntry(path=proj, enabled=True))
    try:
        gdb = project_graph_db(proj)
        gdb.parent.mkdir(parents=True, exist_ok=True)
        gs = GraphStore(gdb)
        gs.upsert_symbol("s1", "fn", "fn", "function", "a.py", 1, 2, "python")
        gs.upsert_community(0, level=1, title="grp", summary="ok", member_count=1)
        gs.commit()
        gs.close()

        # seed the stamp
        gs2 = GraphStore(gdb)
        try:
            gs2.set_meta("algo_version", _pipeline_algo_version())
            gs2.set_meta("source_sig", _code_source_fingerprint(proj))
            gs2.commit()
            n_syms = gs2._con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            n_comm = gs2._con.execute("SELECT COUNT(*) FROM communities").fetchone()[0]
        finally:
            gs2.close()

        assert n_syms == 1, "seed must not mutate symbols"
        assert n_comm == 1, "seed must not mutate communities"
    finally:
        remove_project(proj)
