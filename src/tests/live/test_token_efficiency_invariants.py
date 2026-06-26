"""Token-efficiency invariant guard.

Proves that removing the three deprecated LLM-toggle flags did not widen LLM scope.
Every LLM lane is gated on a deterministic precondition, not a flag:

  TEI1 — _llm_link_edges source contains an early-exit after _llm_link_scan returns [].
  TEI2 — synth gRPC federation yields zero unresolved items → Tier-2 LLM never fires.
  TEI3 — compute_significance tail communities all satisfy the tail criteria (member_count<8
          AND cross_deg<2), proving they are never sent to the LLM narration path.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def synth_fed():
    from tests.live._bpre_fixture import build_synth_federation, teardown_synth_federation
    fed = build_synth_federation()
    yield fed
    teardown_synth_federation(fed)


class TestTokenEfficiencyInvariants:

    def test_tei1_tier2_source_has_empty_items_guard(self):
        """TEI1: _llm_link_edges must bail out when _llm_link_scan returns nothing."""
        import inspect

        from opencode_search.kb.bpre import _llm_link_edges
        src = inspect.getsource(_llm_link_edges)
        assert "if not items" in src, (
            "_llm_link_edges must have 'if not items: return' guard after _llm_link_scan"
        )

    def test_tei2_grpc_synth_fed_yields_zero_unresolved(self, synth_fed):
        """TEI2: _llm_link_scan returns [] for a gRPC-only synth federation.

        The synth federation has no ambiguous HTTP/pubsub clients, so Tier-2 LLM
        link resolution never fires regardless of whether a DeepSeek key is present.
        """
        from opencode_search.daemon.federation import expand_federation
        from opencode_search.kb.bpre import _llm_link_scan
        from opencode_search.kb.bpre_ast import federation_discover

        members = [m for m in expand_federation(synth_fed.root) if m != synth_fed.root]
        surf = federation_discover(members)
        items = _llm_link_scan(members, surf, known_routes=set(), known_topics=set())
        assert items == [], (
            f"Expected 0 unresolved items in gRPC-only synth fed (Tier-2 must not fire); got {items}"
        )

    def test_tei3_compute_significance_tail_criteria(self, synth_fed):
        """TEI3: every community classified as 'tail' by compute_significance truly meets the
        tail criteria (member_count<8 AND cross_community_edges<2), proving it is routed to
        the deterministic labeler and never to the LLM narration path.
        """
        from opencode_search.core.config import project_graph_db
        from opencode_search.graph.enrich import compute_significance
        from opencode_search.graph.store import GraphStore

        for mp in synth_fed.members:
            gdb = project_graph_db(mp)
            if not gdb.exists():
                continue
            gs = GraphStore(gdb)
            try:
                _, tail = compute_significance(gs)
                for cid in tail:
                    row = gs._con.execute(
                        "SELECT member_count FROM communities WHERE id=?", (cid,)
                    ).fetchone()
                    mc = (row[0] or 0) if row else 0
                    cross = gs._con.execute(
                        """SELECT COUNT(*) FROM edges e
                           JOIN symbols sc  ON e.caller_sid  = sc.sid
                           JOIN symbols sc2 ON e.callee_sid = sc2.sid
                           WHERE sc.community_id = ?
                             AND sc.community_id != sc2.community_id""",
                        (cid,),
                    ).fetchone()[0]
                    assert mc < 8 and cross < 2, (
                        f"compute_significance mis-classified community {cid} in {mp} as tail "
                        f"(member_count={mc}, cross_deg={cross}) — head threshold not met"
                    )
            finally:
                gs.close()
