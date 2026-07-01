"""Live e2e proof of the structure-first extraction doctrine (P6/HR15-19/HR23).

E1  golden_edges.json (hand-authored reference set) is well-formed
E2  live extraction precision/recall on the synthetic shop-federation >= frozen baseline
    (load-bearing: the acceptance gate every Part-3 structural-migration step must pass)
E3  ladder-engagement / token-min proof — this fixture is fully structurally resolvable
    (protoc-bound gRPC codegen contracts), so the strongest available proof is *zero*
    DeepSeek spend, not merely a bounded budget
E4  doctrine/ratchet live — check_world_model.py CONFORMS + the regex/heuristic guard passes
    against the working tree, in the same run as the extraction proof above

Reads the process_graph.db already built once by the session-scoped `sample_workspace`
fixture (see tests.live._sample_workspace.build_sample_workspace, which calls
reconstruct_processes(fed_root) during setup) — no redundant rebuild, per the token-frugality
companion law (HR23): don't spend GPU/CPU/tokens re-deriving what's already on disk.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from opencode_search.core.config import root_process_db

pytestmark = pytest.mark.live

_GOLDEN_PATH = Path(__file__).parent / "golden_edges.json"

# Baseline captured 2026-07-01 on commit 3170f94 (pre-Part-3b) on the shop-federation fixture.
# Frozen acceptance floor: no Part-3 migration step may ship below these values.
_BASELINE_RECALL = 1.0
_BASELINE_PRECISION = 1.0


def _base_kind(kind: str) -> str:
    """Strip tier suffixes (_llm/_reranked) — golden set records *which* edge, not *which tier*."""
    return kind.split("_", 1)[0]


@pytest.fixture(scope="module")
def fed_root(sample_workspace) -> str:
    from tests.live._sample_workspace import SampleWorkspace
    assert isinstance(sample_workspace, SampleWorkspace)
    return sample_workspace.fed_root


@pytest.fixture(scope="module")
def process_con(fed_root):
    db = root_process_db(fed_root)
    assert db.exists(), "process_graph.db must exist — sample_workspace runs reconstruct_processes"
    con = sqlite3.connect(str(db))
    yield con
    con.close()


@pytest.fixture(scope="module")
def extracted_edges(process_con) -> set[tuple[str, str, str]]:
    rows = process_con.execute(
        "SELECT caller_service, callee_service, kind FROM cross_service_edges"
    ).fetchall()
    return {(c, e, _base_kind(k)) for c, e, k in rows}


@pytest.fixture(scope="module")
def golden_edges() -> set[tuple[str, str, str]]:
    data = json.loads(_GOLDEN_PATH.read_text())
    return {(e["caller_service"], e["callee_service"], e["kind"]) for e in data["edges"]}


def test_e1_golden_set_well_formed(golden_edges):
    assert golden_edges, "golden_edges.json must define at least one edge"
    for caller, callee, kind in golden_edges:
        assert caller != callee, f"golden edge is a self-loop: {caller}"
        assert kind in ("grpc", "http", "pubsub"), f"unexpected kind {kind!r}"


def test_e2_recall_and_precision_at_least_baseline(extracted_edges, golden_edges):
    if not extracted_edges:
        pytest.fail("E2: extraction produced zero cross-service edges")
    found = golden_edges & extracted_edges
    recall = len(found) / len(golden_edges)
    precision = len(found) / len(extracted_edges)
    assert recall >= _BASELINE_RECALL, (
        f"E2 recall regression: {recall:.2f} < {_BASELINE_RECALL:.2f}; "
        f"missing: {golden_edges - extracted_edges}"
    )
    assert precision >= _BASELINE_PRECISION, (
        f"E2 precision regression: {precision:.2f} < {_BASELINE_PRECISION:.2f}; "
        f"unexpected: {extracted_edges - golden_edges}"
    )


def test_e3_token_min_zero_llm_spend(process_con):
    """shop-federation is fully resolvable by protoc-bound structural constructors — the
    strongest token-min proof here is zero DeepSeek spend, not a budget.

    Checks the edge tier, not the bpre_link.calls global counter (HR23) — that counter is
    session-wide and other live tests (e.g. TE7) legitimately exercise the same DeepSeek call
    with synthetic data in the same pytest process, so it isn't a clean signal here.
    """
    kinds = [r[0] for r in process_con.execute("SELECT kind FROM cross_service_edges").fetchall()]
    llm_tier = [k for k in kinds if k.endswith("_llm")]
    assert not llm_tier, f"E3: DeepSeek residue used on a fully structural fixture: {llm_tier}"


def test_e4_doctrine_ratchet_conforms_live():
    repo_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [sys.executable, "scripts/check_world_model.py"],
        cwd=repo_root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0 and "CONFORMS" in result.stdout, (
        f"E4: check_world_model.py did not CONFORM: {result.stdout}\n{result.stderr}"
    )
    from tests.live import test_no_code_semantic_regex as ratchet
    ratchet.test_no_code_semantic_regex_in_category_a()
    ratchet.test_semantic_heuristic_debt_registry_is_accurate()
    ratchet.test_no_new_semantic_heuristics_beyond_debt_registry()
    ratchet.test_bpre_link_resolve_tokens_are_accounted()
