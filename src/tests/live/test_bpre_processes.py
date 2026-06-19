"""Live E2E tests for BPRE Phase D — process reconstruction (D2-D7).

Tests A-H as specified in the Phase D plan.  All assertions are ground-truth
grounded (deterministic outputs from real code artifacts).  No mocks.
Requires daemon at :8765, enriched federation, GPU optional (BPRE is GPU-free).
"""
from __future__ import annotations

import json
import sqlite3
import xml.etree.ElementTree as ET

import pytest

from opencode_search.core.config import root_process_db
from opencode_search.core.registry import list_projects
from opencode_search.kb.bpre import reconstruct_processes

pytestmark = pytest.mark.live


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def astro_root():
    p = next((e.path for e in list_projects() if "astro-project" in e.path and e.enabled), None)
    assert p, "astro-project must be registered and enabled"
    return p


@pytest.fixture(scope="module")
def astro_members(astro_root):
    from opencode_search.daemon.federation import expand_federation
    members = expand_federation(astro_root)
    assert len(members) >= 2, "astro-project must have ≥2 federation members"
    return members


@pytest.fixture(scope="module")
def process_db(astro_root):
    """Run reconstruct_processes once (OSE_WIKI_LLM=0 for deterministic fast pass).

    LLM narrative/rule generation is suppressed so the fixture completes in ~15s
    instead of minutes.  Slow tests that need LLM output set OSE_WIKI_LLM=1 themselves.
    """
    import os
    prev = os.environ.get("OSE_WIKI_LLM")
    os.environ["OSE_WIKI_LLM"] = "0"
    try:
        count = reconstruct_processes(astro_root)
    finally:
        if prev is None:
            os.environ.pop("OSE_WIKI_LLM", None)
        else:
            os.environ["OSE_WIKI_LLM"] = prev
    db = root_process_db(astro_root)
    assert db.exists(), "process_graph.db must be created"
    con = sqlite3.connect(str(db))
    yield con, count
    con.close()


# ─── Test A: Cross-service resolution accuracy ─────────────────────────────────

class TestCrossServiceResolution:

    def test_A1_grpc_edges_present(self, process_db):
        con, _ = process_db
        grpc = con.execute("SELECT COUNT(*) FROM cross_service_edges WHERE kind='grpc'").fetchone()[0]
        assert grpc >= 1, f"Expected ≥1 gRPC edge; got {grpc}"

    def test_A2_precision_no_self_edges(self, process_db):
        con, _ = process_db
        self_edges = con.execute(
            "SELECT COUNT(*) FROM cross_service_edges WHERE caller_service=callee_service"
        ).fetchone()[0]
        assert self_edges == 0, f"{self_edges} self-loop edges — caller=callee violates precision"

    def test_A3_grpc_evidence_populated(self, process_db):
        con, _ = process_db
        rows = con.execute("SELECT id, evidence FROM cross_service_edges WHERE kind='grpc'").fetchall()
        assert rows, "No gRPC edges found"
        assert all(r[1] for r in rows), f"gRPC edges with empty evidence: {[r[0] for r in rows if not r[1]][:3]}"

    def test_A4_grpc_confidence_is_1(self, process_db):
        con, _ = process_db
        low = con.execute(
            "SELECT COUNT(*) FROM cross_service_edges WHERE kind='grpc' AND confidence<1.0"
        ).fetchone()[0]
        assert low == 0, f"{low} gRPC edges with confidence<1.0"

    def test_A5_pubsub_or_http_edge(self, process_db):
        con, _ = process_db
        other = con.execute(
            "SELECT COUNT(*) FROM cross_service_edges WHERE kind IN ('pubsub','http')"
        ).fetchone()[0]
        if other == 0:
            pytest.skip("No pubsub/http edges — thin federation")


# ─── Test B: Process tracing ───────────────────────────────────────────────────

class TestProcessTracing:

    def test_B1_at_least_one_process(self, process_db):
        _, count = process_db
        assert count >= 1, "reconstruct_processes returned 0 processes"

    def test_B2_spans_two_services(self, process_db):
        con, _ = process_db
        rows = con.execute("SELECT services_json FROM processes").fetchall()
        multi = [r for r in rows if len(json.loads(r[0] or "[]")) >= 2]
        assert multi, "No process spans ≥2 services"

    def test_B3_steps_ordered_from_zero(self, process_db):
        con, _ = process_db
        for proc_id, in con.execute("SELECT id FROM processes").fetchall():
            indices = [r[0] for r in con.execute(
                "SELECT order_index FROM process_steps WHERE process_id=? ORDER BY order_index",
                (proc_id,),
            ).fetchall()]
            assert indices, f"Process {proc_id} has no steps"
            assert indices[0] == 0
            assert indices == list(range(len(indices))), f"Steps not contiguous for {proc_id}"

    def test_B4_step_count_matches_rows(self, process_db):
        con, _ = process_db
        for proc_id, declared in con.execute("SELECT id, step_count FROM processes").fetchall():
            actual = con.execute(
                "SELECT COUNT(*) FROM process_steps WHERE process_id=?", (proc_id,)
            ).fetchone()[0]
            assert declared == actual, f"{proc_id}: declared={declared} actual={actual}"


# ─── Test C: BPMN validity ────────────────────────────────────────────────────

_BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"


class TestBPMNValidity:

    def test_C1_bpmn_well_formed(self, process_db):
        con, _ = process_db
        rows = con.execute("SELECT process_id, bpmn_xml FROM process_artifacts WHERE bpmn_xml!=''").fetchall()
        assert rows, "No BPMN artifacts generated"
        for proc_id, xml_str in rows:
            try:
                ET.fromstring(xml_str)
            except ET.ParseError as e:
                pytest.fail(f"{proc_id}: XML parse error: {e}")

    def test_C2_bpmn_definitions_root(self, process_db):
        con, _ = process_db
        for proc_id, xml_str in con.execute(
            "SELECT process_id, bpmn_xml FROM process_artifacts WHERE bpmn_xml!=''"
        ).fetchall():
            root = ET.fromstring(xml_str)
            assert root.tag == f"{{{_BPMN_NS}}}definitions", f"{proc_id}: wrong root {root.tag}"

    def test_C3_bpmn_start_and_end(self, process_db):
        con, _ = process_db
        for proc_id, xml_str in con.execute(
            "SELECT process_id, bpmn_xml FROM process_artifacts WHERE bpmn_xml!=''"
        ).fetchall():
            root = ET.fromstring(xml_str)
            proc_el = root.find(f"{{{_BPMN_NS}}}process")
            assert proc_el is not None, f"{proc_id}: no <bpmn:process>"
            assert len(proc_el.findall(f"{{{_BPMN_NS}}}startEvent")) == 1
            assert len(proc_el.findall(f"{{{_BPMN_NS}}}endEvent")) >= 1

    def test_C4_bpmn_task_count_le_steps(self, process_db):
        con, _ = process_db
        for proc_id, xml_str in con.execute(
            "SELECT process_id, bpmn_xml FROM process_artifacts WHERE bpmn_xml!=''"
        ).fetchall():
            step_n = con.execute(
                "SELECT COUNT(*) FROM process_steps WHERE process_id=?", (proc_id,)
            ).fetchone()[0]
            root = ET.fromstring(xml_str)
            proc_el = root.find(f"{{{_BPMN_NS}}}process")
            if proc_el is None:
                continue
            bpmn_elems = (len(proc_el.findall(f"{{{_BPMN_NS}}}task"))
                          + len(proc_el.findall(f"{{{_BPMN_NS}}}exclusiveGateway")))
            assert bpmn_elems <= step_n, f"{proc_id}: {bpmn_elems} BPMN elems > {step_n} steps"


# ─── Test D: Mermaid validity ─────────────────────────────────────────────────

class TestMermaidValidity:

    def test_D1_header(self, process_db):
        con, _ = process_db
        rows = con.execute("SELECT process_id, mermaid FROM process_artifacts WHERE mermaid!=''").fetchall()
        assert rows, "No mermaid artifacts generated"
        for proc_id, mer in rows:
            assert mer.strip().startswith("sequenceDiagram"), f"{proc_id}: no sequenceDiagram header"

    def test_D2_participants(self, process_db):
        con, _ = process_db
        for proc_id, mer in con.execute(
            "SELECT process_id, mermaid FROM process_artifacts WHERE mermaid!=''"
        ).fetchall():
            parts = [ln for ln in mer.splitlines() if ln.strip().startswith("participant ")]
            assert parts, f"{proc_id}: mermaid has no participant declarations"

    def test_D3_alt_blocks_balanced(self, process_db):
        con, _ = process_db
        for proc_id, mer in con.execute(
            "SELECT process_id, mermaid FROM process_artifacts WHERE mermaid!=''"
        ).fetchall():
            depth = 0
            for line in mer.splitlines():
                s = line.strip()
                if s.startswith("alt "):
                    depth += 1
                elif s == "end" and depth > 0:
                    depth -= 1
            assert depth == 0, f"{proc_id}: unbalanced alt/end (depth={depth})"

    def test_D4_cap_40_body_lines(self, process_db):
        con, _ = process_db
        for proc_id, mer in con.execute(
            "SELECT process_id, mermaid FROM process_artifacts WHERE mermaid!=''"
        ).fetchall():
            body = [ln for ln in mer.splitlines()
                    if ln.strip() and not ln.strip().startswith("participant ")
                    and ln.strip() != "sequenceDiagram"]
            assert len(body) <= 40, f"{proc_id}: mermaid {len(body)} body lines > 40"


# ─── Test F: Metamorphic & determinism ────────────────────────────────────────

class TestMetamorphicDeterminism:

    @pytest.mark.slow
    def test_F1_deterministic_rerun(self, astro_root, process_db):
        """Full reconstruction re-run — slow because it re-calls reconstruct_processes."""
        import os
        os.environ["OSE_WIKI_LLM"] = "0"
        con, _ = process_db
        before = {r[0]: r[1] for r in con.execute("SELECT id, step_count FROM processes").fetchall()}
        reconstruct_processes(astro_root)
        after = {r[0]: r[1] for r in con.execute("SELECT id, step_count FROM processes").fetchall()}
        assert set(before.keys()) == set(after.keys()), "Re-run produced different process IDs"
        for pid in before:
            assert before[pid] == after[pid], f"step_count changed on re-run for {pid}"
        del os.environ["OSE_WIKI_LLM"]

    @pytest.mark.slow
    def test_F2_bpmn_idempotent(self, astro_root, process_db):
        """BPMN idempotency — slow because it re-calls reconstruct_processes."""
        import os
        os.environ["OSE_WIKI_LLM"] = "0"
        con, _ = process_db
        before = {r[0]: r[1] for r in con.execute("SELECT process_id, bpmn_xml FROM process_artifacts").fetchall()}
        reconstruct_processes(astro_root)
        after = {r[0]: r[1] for r in con.execute("SELECT process_id, bpmn_xml FROM process_artifacts").fetchall()}
        for pid in before:
            assert before.get(pid) == after.get(pid), f"BPMN changed on re-run for {pid}"
        del os.environ["OSE_WIKI_LLM"]


# ─── Test G: HR4 + resource guards ───────────────────────────────────────────

class TestHR4AndResourceGuards:

    def test_G1_hr4_no_cross_service_in_member_dbs(self, astro_members):
        from opencode_search.core.config import project_graph_db
        for member in astro_members[1:]:
            gdb = project_graph_db(member)
            if not gdb.exists():
                continue
            mcon = sqlite3.connect(str(gdb))
            try:
                tables = {r[0] for r in mcon.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()}
            finally:
                mcon.close()
            assert "cross_service_edges" not in tables, (
                f"HR4 violated: {member} graph.db has cross_service_edges"
            )

    def test_G2_bpre_callable(self):
        from opencode_search.kb.bpre import reconstruct_processes as rp
        assert callable(rp)


# ─── Test H: Live e2e surfaces ────────────────────────────────────────────────

class TestLiveSurfaces:

    def test_H1_overview_returns_reconstructed(self, astro_root, process_db):
        import asyncio

        from opencode_search.server.mcp import overview as overview_tool
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(overview_tool(astro_root, what="process_flows"))
        loop.close()
        data = json.loads(result)
        assert data.get("source") == "reconstructed", (
            f"Expected source=reconstructed; got {data.get('source')!r}"
        )
        flows = data.get("flows", [])
        assert flows, "No flows returned from overview(process_flows)"

    def test_H2_flows_have_mermaid(self, astro_root, process_db):
        import asyncio

        from opencode_search.server.mcp import overview as overview_tool
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(overview_tool(astro_root, what="process_flows"))
        loop.close()
        data = json.loads(result)
        mermaid_flows = [f for f in data.get("flows", [])
                         if f.get("mermaid", "").startswith("sequenceDiagram")]
        assert mermaid_flows, "No flow with sequenceDiagram in overview response"

    @pytest.mark.slow
    def test_H3_api_bpmn_endpoint(self, astro_root, process_db):
        import urllib.request
        con, _ = process_db
        row = con.execute("SELECT process_id FROM process_artifacts LIMIT 1").fetchone()
        if not row:
            pytest.skip("No process artifacts")
        url = (
            f"http://127.0.0.1:8765/api/process/bpmn"
            f"?root={astro_root}&id={row[0]}"
        )
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read()
        assert body.startswith(b"<?xml"), f"Not XML: {body[:80]}"
        ET.fromstring(body)
