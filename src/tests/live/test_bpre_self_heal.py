"""BPRE self-heal tests (BSH1-BSH5 + SG) — synthetic root, GPU-free, no mocks.

BSH1 — meta stamps survive the 5-table DELETE and are written after reconstruct.
BSH2 — _bpre_algo_version() is deterministic (4-char hex, stable across calls).
BSH3 — _narrative_incomplete returns False when DeepSeek key absent (allows reuse; no churn).
BSH4 — editing a member flips _bpre_source_sig (source-drift detection works).
BSH5 — _synthesize_artifacts(old_narr) carries over unchanged process narratives byte-for-byte.
SG   — reconcile_projects source contains the federation root-pass calls.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from opencode_search.core.config import root_process_db
from opencode_search.kb.bpre import _narrative_incomplete, reconstruct_processes

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def synth_fed():
    from tests.live._bpre_fixture import build_synth_federation, teardown_synth_federation
    fed = build_synth_federation()
    yield fed
    teardown_synth_federation(fed)


def _no_llm(root: str) -> int:
    from opencode_search.graph.llm import no_deepseek
    with no_deepseek():
        return reconstruct_processes(root)


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


def test_bsh3_narrative_incomplete_allows_reuse_when_key_absent(synth_fed):
    """BSH3: _narrative_incomplete returns False when DeepSeek key absent — no spurious rebuild."""
    from opencode_search.graph.llm import no_deepseek
    _no_llm(synth_fed.root)
    db = root_process_db(synth_fed.root)
    with no_deepseek(), sqlite3.connect(str(db)) as con:
        result = _narrative_incomplete(con)
    assert not result, (
        "_narrative_incomplete must be False when DeepSeek key absent "
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
        pytest.fail("synth_fed produced 0 processes — _trace_processes extraction failed (check fixture setup)")
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
    """SG: reconcile_projects source contains the BPRE root-pass call."""
    import inspect

    from opencode_search.daemon.sweeps import reconcile_projects
    src = inspect.getsource(reconcile_projects)
    assert "reconstruct_processes(" in src


def test_nt1_parse_narratives_preserves_hex_ids():
    """NT1: _parse_narratives keys results by hex strings — guards against int() regression."""
    from opencode_search.kb.bpre import _parse_narratives

    got = _parse_narratives('[{"id": "01f96b457ee69eca", "narrative": "Cart calls checkout."}]')
    assert got == {"01f96b457ee69eca": "Cart calls checkout."}, f"plain: {got}"

    fenced = '```json\n{"results": [{"id": "deadbeef0000cafe", "narrative": "Order."}]}\n```'
    assert _parse_narratives(fenced) == {"deadbeef0000cafe": "Order."}


def test_nt4_reuse_path_does_not_wipe_tables(synth_fed):
    """NT4: stamp-match reuse does not touch entry_points — sentinel survives.

    Proves cascade fix #2 (fresh src_sig): after a rebuild the very next call hits reuse
    (not another full rebuild that would DELETE entry_points and wipe the sentinel).
    """
    from opencode_search.core.config import root_process_db

    _no_llm(synth_fed.root)
    db = root_process_db(synth_fed.root)
    sentinel = "sentinel_nt4_0000"
    with sqlite3.connect(str(db)) as con:
        con.execute(
            "INSERT OR REPLACE INTO entry_points VALUES (?,?,?,?,?,?)",
            (sentinel, "test-svc", "test.go", 1, "http", "/sentinel"),
        )
        con.commit()
    _no_llm(synth_fed.root)  # no key → narrative_incomplete=False → stamps match → reuse
    with sqlite3.connect(str(db)) as con:
        row = con.execute("SELECT ep_id FROM entry_points WHERE ep_id=?", (sentinel,)).fetchone()
    assert row is not None, "SENTINEL_NT4 wiped — reuse triggered a full rebuild (cascade fix #2 regression)"


@pytest.mark.slow
def test_nt3_full_reconstruct_fills_all_narratives(synth_fed):
    """NT3: reconstruct_processes fills every narrative when DeepSeek key is present.

    Always runs: without a key, asserts structural count > 0.
    With a key: additionally asserts all narratives non-empty (proves int()→str() fix).
    """
    from opencode_search.core.config import root_process_db
    from opencode_search.graph.llm import deepseek_key
    from opencode_search.kb.bpre import reconstruct_processes

    count = reconstruct_processes(synth_fed.root)
    assert count > 0, "synth_fed produced 0 processes"
    if deepseek_key():
        db = root_process_db(synth_fed.root)
        with sqlite3.connect(str(db)) as con:
            rows = con.execute("SELECT process_id, narrative FROM process_artifacts").fetchall()
        empty = [pid for pid, narr in rows if not narr.strip()]
        assert not empty, f"{len(empty)}/{len(rows)} narratives still empty after real reconstruct: {empty}"


@pytest.mark.slow
def test_nt2_re_synthesis_not_full_rebuild(synth_fed):
    """NT2: stamps-match + incomplete narrative → re-synthesis, not full rebuild.

    Sentinel-survival: entry_points is wiped only by a full rebuild.  After inserting a
    sentinel row and zeroing one narrative, a second reconstruct must NOT wipe entry_points.
    With a key, the narrative must also be refilled (proves int()→str() fix end-to-end).
    """
    from opencode_search.core.config import root_process_db
    from opencode_search.graph.llm import deepseek_key
    from opencode_search.kb.bpre import reconstruct_processes

    reconstruct_processes(synth_fed.root)
    db = root_process_db(synth_fed.root)
    sentinel = "sentinel_nt2_0000"
    with sqlite3.connect(str(db)) as con:
        con.execute("INSERT OR REPLACE INTO entry_points VALUES (?,?,?,?,?,?)",
                    (sentinel, "test-svc", "test.go", 1, "http", "/sentinel"))
        pid = con.execute("SELECT process_id FROM process_artifacts LIMIT 1").fetchone()[0]
        con.execute("UPDATE process_artifacts SET narrative='' WHERE process_id=?", (pid,))
        con.commit()
    reconstruct_processes(synth_fed.root)
    with sqlite3.connect(str(db)) as con:
        survived = con.execute("SELECT ep_id FROM entry_points WHERE ep_id=?", (sentinel,)).fetchone()
        refilled = con.execute("SELECT narrative FROM process_artifacts WHERE process_id=?", (pid,)).fetchone()
    assert survived, "SENTINEL_NT2 wiped — full rebuild ran instead of expected path (cascade regression)"
    if deepseek_key():
        assert refilled and refilled[0].strip(), f"Narrative for {pid} still empty — int()→str() fix did not work"
