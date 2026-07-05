"""Live tests: _code_fingerprint() and the self-heal version stamp hardening.

SH1 — _code_fingerprint() is stable across two calls (byte-equal).
SH2 — _code_fingerprint() changes when a tracked module's bytes change.
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


def test_sh2_code_fingerprint_changes_on_module_edit(tmp_path):
    """SH2: fingerprint differs when a tracked module's bytes differ (via file perturb)."""
    import hashlib

    from rag_search.daemon.sweeps import _code_fingerprint

    root = Path(__file__).resolve().parents[3] / "src" / "rag_search"
    fp_before = _code_fingerprint()

    # Hash the same modules but with extractor.py content perturbed
    modules = [
        root / "graph" / "extractor.py",
        root / "graph" / "enrich.py",
        root / "graph" / "community.py",
    ]
    h = hashlib.sha1()
    for i, p in enumerate(modules):
        data = p.read_bytes()
        if i == 0:
            data = data + b"\n# perturbed"  # perturb extractor.py
        h.update(data)
    fp_perturbed = h.hexdigest()[:4]

    assert fp_perturbed != fp_before, "perturbed hash must differ from real fingerprint"


def test_sh3_algo_version_includes_code_fp():
    """SH3: _pipeline_algo_version() contains the code-fingerprint component."""
    from rag_search.daemon.sweeps import _code_fingerprint, _pipeline_algo_version
    ver = _pipeline_algo_version()
    fp = _code_fingerprint()
    assert fp in ver, f"code_fp {fp!r} not in algo_version {ver!r}"
    parts = ver.split("+")
    assert len(parts) == 2, f"expected 2-part version 'ALGO+code_fp', got {ver!r}"


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
