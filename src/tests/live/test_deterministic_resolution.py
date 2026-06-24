"""Tier-1 + Tier-1.5 deterministic resolution — gating guards + confidence ladder.

Validates:
  - reconstruct_processes is deterministic (two runs, same edge count)
  - when LLM gates off: zero _llm / _llm_file / *_llm* edges
  - all non-LLM edges have confidence ∈ [0.8, 1.0] (strict ladder invariant)
  - no edge below the Tier-2 LLM floor (0.7)

Uses a synthetic 2-service Go gRPC federation so the live astro process_graph.db is
never mutated by the test suite.
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
    """Synthetic 2-service Go gRPC federation — isolated, never touches production."""
    from tests.live._bpre_fixture import (
        build_synth_federation,
        teardown_synth_federation,
    )
    fed = build_synth_federation()
    yield fed
    teardown_synth_federation(fed)


@pytest.fixture(scope="module")
def det_db(synth_fed):
    """Run reconstruct_processes once on the synthetic root with all LLM gates OFF."""
    prev = {k: os.environ.get(k) for k in ("OSE_WIKI_LLM", "OSE_BPRE_LLM_LINK", "OSE_BPRE_LLM_FILE")}
    os.environ.update({"OSE_WIKI_LLM": "0", "OSE_BPRE_LLM_LINK": "0", "OSE_BPRE_LLM_FILE": "0"})
    try:
        count = reconstruct_processes(synth_fed.root)
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    db = root_process_db(synth_fed.root)
    assert db.exists(), "process_graph.db must exist after reconstruct_processes"
    con = sqlite3.connect(str(db))
    yield con, count
    con.close()


@pytest.mark.slow
class TestDeterministicResolution:

    def test_process_db_created(self, synth_fed):
        assert root_process_db(synth_fed.root).exists(), "process_graph.db must be created"

    def test_no_llm_edges_when_gates_off(self, det_db):
        con, _ = det_db
        n = con.execute(
            "SELECT COUNT(*) FROM cross_service_edges WHERE kind LIKE '%_llm%'"
        ).fetchone()[0]
        assert n == 0, f"Expected 0 LLM edges with OSE_BPRE_LLM_LINK=0; got {n}"

    def test_no_llm_file_edges_when_gates_off(self, det_db):
        con, _ = det_db
        n = con.execute(
            "SELECT COUNT(*) FROM cross_service_edges WHERE kind='_llm_file'"
        ).fetchone()[0]
        assert n == 0, f"Expected 0 _llm_file edges with gates off; got {n}"

    def test_non_llm_confidences_in_valid_range(self, det_db):
        """Without LLM tiers all edges must have confidence ∈ [0.8, 1.0]."""
        con, _ = det_db
        bad = con.execute(
            "SELECT id, kind, confidence FROM cross_service_edges "
            "WHERE confidence < 0.8 OR confidence > 1.0"
        ).fetchall()
        assert not bad, (
            "Non-LLM edges must have confidence ∈ [0.8, 1.0]:\n"
            + "\n".join(f"  {r}" for r in bad[:5])
        )

    def test_no_edge_below_tier2_floor(self, det_db):
        con, _ = det_db
        n = con.execute(
            "SELECT COUNT(*) FROM cross_service_edges WHERE confidence < 0.7"
        ).fetchone()[0]
        assert n == 0, f"{n} edges below the 0.7 Tier-2 floor"

    def test_deterministic_two_runs_same_count(self, synth_fed):
        """Two deterministic runs must produce byte-identical edge counts."""
        env_patch = {"OSE_WIKI_LLM": "0", "OSE_BPRE_LLM_LINK": "0", "OSE_BPRE_LLM_FILE": "0"}
        prev = {k: os.environ.get(k) for k in env_patch}
        os.environ.update(env_patch)
        try:
            reconstruct_processes(synth_fed.root)
            db = root_process_db(synth_fed.root)
            c1 = sqlite3.connect(str(db)).execute(
                "SELECT COUNT(*) FROM cross_service_edges"
            ).fetchone()[0]
            reconstruct_processes(synth_fed.root)
            c2 = sqlite3.connect(str(db)).execute(
                "SELECT COUNT(*) FROM cross_service_edges"
            ).fetchone()[0]
        finally:
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        assert c1 == c2, f"Non-deterministic: run1={c1} vs run2={c2} edges"

    def test_edge_kinds_are_known_non_llm(self, det_db):
        con, _ = det_db
        kinds = {r[0] for r in con.execute(
            "SELECT DISTINCT kind FROM cross_service_edges"
        ).fetchall()}
        allowed = {"grpc", "pubsub", "http", "grpc_reranked", "http_reranked", "pubsub_reranked"}
        unknown = kinds - allowed
        assert not unknown, f"Unexpected edge kinds with LLM gates off: {unknown}"
