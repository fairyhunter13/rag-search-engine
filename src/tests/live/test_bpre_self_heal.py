"""BPRE self-heal tests (BSH1-BSH5 + SG) — synthetic root, GPU-free, no mocks.

BSH1 — meta stamps survive the 5-table DELETE and are written after reconstruct.
BSH2 — _bpre_algo_version() is deterministic (4-char hex, stable across calls).
BSH3 — _narrative_incomplete returns False when OSE_WIKI_LLM=0 (allows reuse; no churn).
BSH4 — editing a member flips _bpre_source_sig (source-drift detection works).
BSH5 — _synthesize_artifacts(old_narr) carries over unchanged process narratives byte-for-byte.
SG   — reconcile_projects source contains the federation root-pass calls.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from opencode_search.core.config import root_process_db
from opencode_search.kb.bpre import reconstruct_processes

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def synth_fed():
    from tests.live._bpre_fixture import build_synth_federation, teardown_synth_federation
    fed = build_synth_federation()
    yield fed
    teardown_synth_federation(fed)


def _no_llm(root: str) -> int:
    prev = os.environ.get("OSE_WIKI_LLM")
    os.environ["OSE_WIKI_LLM"] = "0"
    try:
        return reconstruct_processes(root)
    finally:
        if prev is None:
            os.environ.pop("OSE_WIKI_LLM", None)
        else:
            os.environ["OSE_WIKI_LLM"] = prev


def test_bsh1_meta_stamps_survive_rebuild(synth_fed):
    """BSH1: bpre_algo + bpre_source_sig survive the 5-table DELETE."""
    _no_llm(synth_fed.root)
    db = root_process_db(synth_fed.root)
    with sqlite3.connect(str(db)) as con:
        algo = con.execute("SELECT value FROM meta WHERE key='bpre_algo'").fetchone()
        src = con.execute("SELECT value FROM meta WHERE key='bpre_source_sig'").fetchone()
    assert algo and algo[0], "bpre_algo stamp must be written"
    assert src and src[0], "bpre_source_sig stamp must be written"
    _no_llm(synth_fed.root)
    with sqlite3.connect(str(db)) as con:
        algo2 = con.execute("SELECT value FROM meta WHERE key='bpre_algo'").fetchone()
    assert algo2 and algo2[0] == algo[0], "algo stamp must be stable on stamp-match reuse"


def test_bsh2_algo_version_is_deterministic():
    """BSH2: _bpre_algo_version() returns a 4-char hex string, stable across calls."""
    from opencode_search.kb.bpre import _bpre_algo_version
    v = _bpre_algo_version()
    assert len(v) == 4 and all(c in "0123456789abcdef" for c in v)
    assert _bpre_algo_version() == v


def test_bsh3_narrative_incomplete_allows_reuse_when_llm_off(synth_fed):
    """BSH3: _narrative_incomplete returns False when OSE_WIKI_LLM=0 — no spurious rebuild."""
    from opencode_search.kb.bpre import _narrative_incomplete
    _no_llm(synth_fed.root)
    db = root_process_db(synth_fed.root)
    # Check _narrative_incomplete with the gate explicitly off.
    prev = os.environ.get("OSE_WIKI_LLM")
    os.environ["OSE_WIKI_LLM"] = "0"
    try:
        with sqlite3.connect(str(db)) as con:
            result = _narrative_incomplete(con)
    finally:
        if prev is None:
            os.environ.pop("OSE_WIKI_LLM", None)
        else:
            os.environ["OSE_WIKI_LLM"] = prev
    assert not result, (
        "_narrative_incomplete must be False when OSE_WIKI_LLM=0 "
        "(empty narratives are expected; no reuse-block needed)"
    )


def test_bsh4_source_drift_changes_sig(synth_fed):
    """BSH4: adding a file to a member changes _bpre_source_sig."""
    from opencode_search.daemon.federation import expand_federation
    from opencode_search.kb.bpre import _bpre_source_sig
    members = expand_federation(synth_fed.root)
    sig1 = _bpre_source_sig(members)
    new_file = synth_fed.cart + "/drift.go"
    open(new_file, "w").write("// drift\n")  # noqa: SIM115
    try:
        sig2 = _bpre_source_sig(members)
        assert sig1 != sig2, "source sig must change when a file is added to a member"
    finally:
        import contextlib
        with contextlib.suppress(OSError):
            os.remove(new_file)


def test_bsh5_delta_carry_over(synth_fed):
    """BSH5: _synthesize_artifacts(old_narr) carries sentinel narratives for unchanged processes."""
    from opencode_search.kb.bpre import _proc_sig, _synthesize_artifacts
    _no_llm(synth_fed.root)
    db = root_process_db(synth_fed.root)
    with sqlite3.connect(str(db)) as con:
        procs = con.execute("SELECT id, name, services_json FROM processes").fetchall()
    if not procs:
        pytest.skip("no processes in synthetic root — _trace_processes returned 0")
    # Build old_narr keyed by proc content sig.
    old_narr: dict[str, str] = {}
    with sqlite3.connect(str(db)) as con:
        for pid, name, svc_json in procs:
            steps = con.execute(
                "SELECT sid_or_endpoint, service, kind, guard FROM process_steps "
                "WHERE process_id=? ORDER BY order_index", (pid,)
            ).fetchall()
            old_narr[_proc_sig(name, svc_json, steps)] = "SENTINEL_BSH5"
        _synthesize_artifacts(con, old_narr)
        got = con.execute("SELECT narrative FROM process_artifacts").fetchall()
    assert got and all(r[0] == "SENTINEL_BSH5" for r in got), (
        f"carry-over failed; narratives: {[r[0] for r in got]}"
    )


def test_sg_reconcile_has_root_pass():
    """SG: reconcile_projects source contains both federation root-pass calls."""
    import inspect

    from opencode_search.daemon.sweeps import reconcile_projects
    src = inspect.getsource(reconcile_projects)
    assert "build_federation_hierarchy(" in src
    assert "reconstruct_processes(" in src
