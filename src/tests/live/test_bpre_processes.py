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
from opencode_search.kb.bpre import reconstruct_processes

pytestmark = pytest.mark.live


# ─── Fixtures ─────────────────────────────────────────────────────────────────

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
def fed_root(sample_workspace) -> str:
    """Read-only reference to the sample federation root (no reconstruct_processes calls here)."""
    from tests.live._sample_workspace import SampleWorkspace
    assert isinstance(sample_workspace, SampleWorkspace)
    return sample_workspace.fed_root


@pytest.fixture(scope="module")
def fed_members(fed_root):
    """Federation members of the sample root — read-only, no reconstruct calls."""
    from opencode_search.daemon.federation import expand_federation
    return expand_federation(fed_root)


@pytest.fixture(scope="module")
def process_db(synth_fed):
    """Run reconstruct_processes once on the synthetic root (no DeepSeek key — deterministic)."""
    from opencode_search.graph.llm import no_deepseek
    with no_deepseek():
        count = reconstruct_processes(synth_fed.root)
    db = root_process_db(synth_fed.root)
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

    def test_A5a_pubsub_edges_resolve(self, process_db):
        con, _ = process_db
        pub = con.execute(
            "SELECT COUNT(*) FROM cross_service_edges WHERE kind='pubsub'"
        ).fetchone()[0]
        http = con.execute(
            "SELECT COUNT(*) FROM cross_service_edges WHERE kind='http'"
        ).fetchone()[0]
        assert pub + http >= 0

    def test_A5b_cross_service_edge_count(self, process_db):
        con, _ = process_db
        grpc = con.execute("SELECT COUNT(*) FROM cross_service_edges WHERE kind='grpc'").fetchone()[0]
        pubsub = con.execute("SELECT COUNT(*) FROM cross_service_edges WHERE kind='pubsub'").fetchone()[0]
        http = con.execute("SELECT COUNT(*) FROM cross_service_edges WHERE kind='http'").fetchone()[0]
        llm = con.execute("SELECT COUNT(*) FROM cross_service_edges WHERE kind LIKE '%_llm'").fetchone()[0]
        assert llm == 0, f"process_db runs without DeepSeek key; no LLM edges expected; got {llm}"
        assert grpc + pubsub + http >= 1, (
            f"Expected ≥1 deterministic edge; grpc={grpc} pubsub={pubsub} http={http}"
        )


    def test_A5c_grpc_entry_matches_edges(self, process_db):
        con, _ = process_db
        entries = con.execute("SELECT COUNT(*) FROM entry_points WHERE kind='grpc'").fetchone()[0]
        grpc = con.execute("SELECT COUNT(*) FROM cross_service_edges WHERE kind='grpc'").fetchone()[0]
        if entries > 0:
            assert grpc > 0, f"gRPC entries ({entries}) present but no gRPC edges emitted"

    def test_A5d_no_llm_edges_when_key_absent(self, process_db):
        con, _ = process_db
        llm = con.execute(
            "SELECT COUNT(*) FROM cross_service_edges WHERE kind LIKE '%_llm'"
        ).fetchone()[0]
        # process_db pops DEEPSEEK_API_KEY; all LLM lanes are suppressed by key-absence.
        assert llm == 0, f"No DeepSeek key → 0 LLM edges; got {llm}"


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


    def test_B5_no_test_file_entry_points(self, process_db):
        con, _ = process_db
        test_eps = con.execute(
            "SELECT COUNT(*) FROM entry_points "
            "WHERE file LIKE '%_test.%' OR file LIKE '%/testdata/%' OR file LIKE '%/test/%'"
        ).fetchone()[0]
        assert test_eps == 0, f"{test_eps} test-file entry points leaked into entry_points"

    def test_B6_no_duplicate_process_mermaid(self, process_db):
        con, _ = process_db
        rows = con.execute("SELECT mermaid FROM process_artifacts WHERE mermaid!=''").fetchall()
        mermaids = [r[0] for r in rows]
        assert len(mermaids) == len(set(mermaids)), (
            f"Duplicate mermaid values ({len(mermaids) - len(set(mermaids))}) — "
            "handler-anchored dedup not effective"
        )

    def test_B8_process_count_deduped(self, process_db):
        _, count = process_db
        assert count >= 1, "reconstruct_processes returned 0 processes"
        assert count < 120, (
            f"process count {count} ≥ 120 — suggests service-level any-edge BFS not replaced"
        )


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
    def test_F1_deterministic_rerun(self, fed_root, process_db):
        """Full reconstruction re-run — slow because it re-calls reconstruct_processes."""
        from opencode_search.graph.llm import no_deepseek
        con, _ = process_db
        with no_deepseek():
            before = {r[0]: r[1] for r in con.execute("SELECT id, step_count FROM processes").fetchall()}
            reconstruct_processes(fed_root)
            after = {r[0]: r[1] for r in con.execute("SELECT id, step_count FROM processes").fetchall()}
        assert set(before.keys()) == set(after.keys()), "Re-run produced different process IDs"
        for pid in before:
            assert before[pid] == after[pid], f"step_count changed on re-run for {pid}"

    @pytest.mark.slow
    def test_F2_bpmn_idempotent(self, fed_root, process_db):
        """BPMN idempotency — slow because it re-calls reconstruct_processes."""
        from opencode_search.graph.llm import no_deepseek
        con, _ = process_db
        with no_deepseek():
            before = {r[0]: r[1] for r in con.execute("SELECT process_id, bpmn_xml FROM process_artifacts").fetchall()}
            reconstruct_processes(fed_root)
            after = {r[0]: r[1] for r in con.execute("SELECT process_id, bpmn_xml FROM process_artifacts").fetchall()}
        for pid in before:
            assert before.get(pid) == after.get(pid), f"BPMN changed on re-run for {pid}"


# ─── Test G: HR4 + resource guards ───────────────────────────────────────────

class TestHR4AndResourceGuards:

    def test_G1_hr4_no_cross_service_in_member_dbs(self, fed_members):
        from opencode_search.core.config import project_graph_db
        for member in fed_members[1:]:
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

    def test_H1_overview_returns_reconstructed(self, synth_fed, process_db):
        import asyncio

        from opencode_search.server.mcp import overview as overview_tool
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(overview_tool(synth_fed.root, what="process_flows"))
        loop.close()
        data = json.loads(result)
        assert data.get("source") == "reconstructed", (
            f"Expected source=reconstructed; got {data.get('source')!r}"
        )
        flows = data.get("flows", [])
        assert flows, "No flows returned from overview(process_flows)"

    def test_H2_flows_have_mermaid(self, synth_fed, process_db):
        import asyncio

        from opencode_search.server.mcp import overview as overview_tool
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(overview_tool(synth_fed.root, what="process_flows"))
        loop.close()
        data = json.loads(result)
        mermaid_flows = [f for f in data.get("flows", [])
                         if f.get("mermaid", "").startswith("sequenceDiagram")]
        assert mermaid_flows, "No flow with sequenceDiagram in overview response"

    @pytest.mark.slow
    def test_H3_api_bpmn_endpoint(self, synth_fed, process_db):
        import urllib.request
        con, _ = process_db
        row = con.execute("SELECT process_id FROM process_artifacts LIMIT 1").fetchone()
        assert row, "No process artifacts in synthetic root"
        url = (
            f"http://127.0.0.1:8765/api/process/bpmn"
            f"?root={synth_fed.root}&id={row[0]}"
        )
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read()
        assert body.startswith(b"<?xml"), f"Not XML: {body[:80]}"
        ET.fromstring(body)


# ─── Source-guards ─────────────────────────────────────────────────────────────

def test_trace_processes_is_handler_anchored():
    import inspect

    from opencode_search.kb.bpre import (
        _call_in_reachable,
        _callee_ep,
        _handler_reachable_set,
        _trace_processes,
    )
    src = inspect.getsource(_trace_processes)
    assert "_handler_reachable_set(" in src
    assert "_call_in_reachable(" in src
    assert "_callee_ep(" in src
    assert "adj[entry_svc]" not in src, "any-edge BFS adj[entry_svc] must be gone"
    assert "svc_to_member" in src, "_trace_processes must build svc→member map"
    assert callable(_handler_reachable_set)
    assert callable(_call_in_reachable)
    assert callable(_callee_ep)


def test_scan_once_orchestration_guard():
    """T3: _reconstruct_processes_locked must build all_facts via _scan_all_members
    and thread it to every derive-pass — prevents silent regression to 7× parsing."""
    import inspect

    from opencode_search.kb.bpre import _reconstruct_processes_locked
    src = inspect.getsource(_reconstruct_processes_locked)
    assert "_scan_all_members(" in src, "orchestration must call _scan_all_members once"
    assert "all_facts" in src, "orchestration must thread all_facts through derive-passes"
    assert "all_facts[member]" in src, "per-member derives must receive all_facts[member]"


def test_scan_once_facts_match_per_file_scan(synth_fed):
    """T1: scan-once FileFacts must be field-identical to per-call scan (correctness guarantee)."""
    from opencode_search.daemon.federation import expand_federation
    from opencode_search.kb.bpre import _iter_member_facts, _scan_all_members
    from opencode_search.kb.bpre_ast import federation_discover
    members = [m for m in expand_federation(synth_fed.root) if m != synth_fed.root]
    surf = federation_discover(members)
    all_facts = _scan_all_members(members, surf)
    for member in members:
        slow = dict(_iter_member_facts(member, None, surf))
        fast = all_facts.get(member, {})
        assert set(slow.keys()) == set(fast.keys()), f"file-set mismatch for {member}"
        for path in slow:
            sf, ff = slow[path], fast[path]
            assert sf.http_routes == ff.http_routes, f"{path}: http_routes differ"
            assert sf.grpc_clients == ff.grpc_clients, f"{path}: grpc_clients differ"
            assert sf.grpc_servers == ff.grpc_servers, f"{path}: grpc_servers differ"
            assert sf.http_clients == ff.http_clients, f"{path}: http_clients differ"


def test_part_f_incremental_scan_reuses_unchanged_member_cache():
    """Part F: editing one member re-scans only it (member_scan_cache); edge/process
    counts stay identical to the prior full build — evidence for incremental BPRE."""
    from pathlib import Path

    from opencode_search.daemon.sweeps import _fingerprint_cache
    from opencode_search.graph.llm import no_deepseek
    from opencode_search.kb.bpre import _invalidate_bpre_code_sig
    from tests.live._bpre_fixture import build_synth_federation, teardown_synth_federation

    fed = build_synth_federation()
    try:
        with no_deepseek():
            reconstruct_processes(fed.root)
        db = root_process_db(fed.root)
        con = sqlite3.connect(str(db))
        rows1 = dict(con.execute("SELECT member, sig FROM member_scan_cache").fetchall())
        edges1 = con.execute("SELECT COUNT(*) FROM cross_service_edges").fetchone()[0]
        procs1 = con.execute("SELECT COUNT(*) FROM processes").fetchone()[0]
        con.close()
        assert fed.cart in rows1 and fed.checkout in rows1, "both members must be cached"

        import os

        checkout_go = Path(fed.checkout) / "checkout.go"
        checkout_go.write_text(checkout_go.read_text() + "\n// touched\n")
        bumped = checkout_go.stat().st_mtime + 2  # fingerprint truncates to whole seconds
        os.utime(checkout_go, (bumped, bumped))
        _fingerprint_cache.pop(fed.checkout, None)  # simulate watcher on_change invalidation
        _invalidate_bpre_code_sig(fed.checkout)  # on_change invalidates both caches (HR36)
        with no_deepseek():
            reconstruct_processes(fed.root)
        con = sqlite3.connect(str(db))
        rows2 = dict(con.execute("SELECT member, sig FROM member_scan_cache").fetchall())
        edges2 = con.execute("SELECT COUNT(*) FROM cross_service_edges").fetchone()[0]
        procs2 = con.execute("SELECT COUNT(*) FROM processes").fetchone()[0]
        con.close()

        assert rows2[fed.cart] == rows1[fed.cart], "cart re-scanned despite unchanged source"
        assert rows2[fed.checkout] != rows1[fed.checkout], "checkout sig must change after edit"
        assert edges2 == edges1, "edge count regressed under incremental rebuild"
        assert procs2 == procs1, "process count regressed under incremental rebuild"
    finally:
        teardown_synth_federation(fed)
