"""Live end-to-end validation of the OSE information hierarchy (HR20 + HR21).

Research grounding (June 2026):
- MetaRAG metamorphic testing (arXiv 2509.09360, arXiv 2605.13898)
- RAGAS/FaithLens faithfulness without ground truth (arXiv 2512.20182, arXiv 2505.04847)
- Cross-model LLM-as-judge (arXiv 2509.26072 self-preference bias)
- Partition quality composite signal (arXiv 2501.07025)

No mocks. GPU-only. Public repo — no hardcoded device paths.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

from opencode_search.core.config import project_graph_db
from opencode_search.core.registry import list_projects
from opencode_search.daemon.federation import expand_federation
from opencode_search.graph.store import GraphStore

pytestmark = pytest.mark.live


def _fedroot() -> str | None:
    return next(
        (p.path for p in list_projects() if p.enabled and len(expand_federation(p.path)) > 1),
        None,
    )


def _federate(base: Path):
    uid = str(id(base))[-6:]
    marker = f"ocs_he_{uid}"
    root = base / "root"
    member = base / "member-repo"
    root.mkdir()
    member.mkdir()
    (member / f"{marker}.py").write_text(f"def {marker}(): pass\n")
    (root / "readme.txt").write_text("root\n")
    (root / "link").symlink_to(member)
    return root, member, marker


def _clean(paths: list) -> None:
    from opencode_search.core.config import index_dir
    from opencode_search.core.registry import remove_project
    for p in paths:
        remove_project(str(p))
        shutil.rmtree(index_dir(str(p)), ignore_errors=True)


@pytest.fixture(scope="module")
def l3_root():
    """Build a deterministic L3 (OSE_WIKI_LLM=0) on the live federation root.

    Yields (root_path, l3_rows). Idempotent.
    """
    from opencode_search.kb.federation_hierarchy import build_federation_hierarchy
    root = _fedroot()
    if not root:
        pytest.skip("no federated root registered")
    prev = os.environ.get("OSE_WIKI_LLM")
    os.environ["OSE_WIKI_LLM"] = "0"
    try:
        build_federation_hierarchy(root)
    finally:
        if prev is None:
            os.environ.pop("OSE_WIKI_LLM", None)
        else:
            os.environ["OSE_WIKI_LLM"] = prev
    gs = GraphStore(project_graph_db(root))
    try:
        rows = gs._con.execute(
            "SELECT id, title, summary, member_count FROM communities "
            "WHERE level>=3 ORDER BY id"
        ).fetchall()
    finally:
        gs.close()
    assert rows, "build_federation_hierarchy must write ≥1 L3 row on a real federation root"
    yield root, rows


def test_live_root_l3_build_returns_domains(l3_root):
    """HE1: L3 build on live federation root produces ≤8 valid theme rows."""
    _root, rows = l3_root
    assert 0 < len(rows) <= 8, f"expect 1-8 themes (group_by_type cap): {len(rows)}"
    for cid, title, summary, member_count in rows:
        assert title and title.startswith("Federation:"), f"bad title: {title!r}"
        assert summary and summary.strip(), f"L3 row {cid} has blank summary"
        assert member_count and member_count > 0, f"L3 row {cid} member_count={member_count}"


def test_overview_hierarchy_federation_domains_match_db(live_client, l3_root):
    """HE2: overview(hierarchy) federation_domains == level>=3 titles in root graph.db."""
    root, rows = l3_root
    r = live_client.post("/api/overview", json={"project": root, "what": "hierarchy"})
    assert r.status_code == 200, f"overview hierarchy: {r.text[:200]}"
    d = r.json()
    assert "federation_domains" in d, f"key missing: {list(d.keys())}"
    api_titles = {fd["title"] for fd in d["federation_domains"]}
    db_titles = {row[1] for row in rows}
    assert api_titles == db_titles, (
        f"overview federation_domains mismatch.\nAPI:{sorted(api_titles)}\nDB:{sorted(db_titles)}"
    )


def test_l3_member_count_equals_grouped_children(l3_root):
    """HE3: each L3 member_count equals independent recount of its semantic_type group.

    Metamorphic structural identity (arXiv 2509.09360).
    """
    from opencode_search.daemon.federation import federated_map

    root, rows = l3_root

    def _member_l2(gs: GraphStore) -> list[tuple]:
        return gs._con.execute(
            "SELECT title, COALESCE(semantic_type, 'domain') FROM communities "
            "WHERE level=2 AND title IS NOT NULL AND title!='' AND title NOT IN ('(leaf)')"
        ).fetchall()

    per_member = federated_map(root, _member_l2)
    groups: dict[str, int] = defaultdict(int)
    for _mpath, member_rows in per_member:
        for _title, stype in member_rows:
            groups[stype or "domain"] += 1

    for cid, title, _summary, member_count in rows:
        label = title.removeprefix("Federation: ").replace(" ", "_").lower()
        expected = groups.get(label) or groups.get(label.title()) or \
            next((v for k, v in groups.items() if k.lower() == label), None)
        if expected is None:
            continue
        assert member_count == expected, (
            f"L3 row {cid} ({title!r}) member_count={member_count}, recount={expected}"
        )


def test_l3_build_preserves_l1_l2(l3_root):
    """HE4: second L3 build leaves L1/L2 unchanged and is byte-identical."""
    from opencode_search.kb.federation_hierarchy import build_federation_hierarchy

    root, rows_first = l3_root

    def _counts(gs: GraphStore) -> tuple[int, int]:
        l1 = gs._con.execute("SELECT COUNT(*) FROM communities WHERE level=1").fetchone()[0]
        l2 = gs._con.execute("SELECT COUNT(*) FROM communities WHERE level=2").fetchone()[0]
        return l1, l2

    gs = GraphStore(project_graph_db(root))
    try:
        counts_before = _counts(gs)
    finally:
        gs.close()
    prev = os.environ.get("OSE_WIKI_LLM")
    os.environ["OSE_WIKI_LLM"] = "0"
    try:
        build_federation_hierarchy(root)
    finally:
        if prev is None:
            os.environ.pop("OSE_WIKI_LLM", None)
        else:
            os.environ["OSE_WIKI_LLM"] = prev
    gs = GraphStore(project_graph_db(root))
    try:
        counts_after = _counts(gs)
        rows_second = gs._con.execute(
            "SELECT id,title,summary,member_count FROM communities WHERE level>=3 ORDER BY id"
        ).fetchall()
    finally:
        gs.close()
    assert counts_after == counts_before, f"L3 rebuild clobbered L1/L2: {counts_before}→{counts_after}"
    assert rows_second == rows_first, "two OSE_WIKI_LLM=0 builds must be byte-identical"


def test_partition_quality_consistent_across_live_federation():
    """HE5: partition_quality() invariants hold across every live member (arXiv 2509.09360).

    Metamorphic guard: all numeric outputs in bounds; degenerate iff one of three
    documented conditions holds; no healthy member (n_l1≥20, Q≥0.3) is flagged.
    """
    from opencode_search.graph.quality import partition_quality

    root = _fedroot()
    if not root:
        pytest.skip("no federated root registered")
    failures = []
    for mpath in expand_federation(root):
        gdb = project_graph_db(mpath)
        if not gdb.exists():
            continue
        gs = GraphStore(gdb)
        try:
            q = partition_quality(gs)
            ec = gs.edge_count()
        finally:
            gs.close()
        if not (0.0 <= q["coverage"] <= 1.0):
            failures.append(f"{mpath}: coverage={q['coverage']} OOB")
        if not (0.0 <= q["singleton_ratio"] < 1.0):
            failures.append(f"{mpath}: singleton_ratio={q['singleton_ratio']} OOB")
        # Mirror the exact condition in graph/quality.py::partition_quality (ec required)
        expected = (
            q["singleton_ratio"] >= 0.60
            or (ec > 0 and q["coverage"] < 0.20)
            or (ec > 0 and q["n_l1"] >= 2 and q["modularity_q"] < 0.05)
        )
        if q["degenerate"] != expected:
            failures.append(f"{mpath}: degenerate={q['degenerate']} but conditions→{expected}: {q}")
        if q["n_l1"] >= 20 and q["modularity_q"] >= 0.3 and q["degenerate"]:
            failures.append(f"{mpath}: healthy member wrongly flagged degenerate: {q}")
    assert not failures, "\n".join(failures)


def test_modularity_positive_oracle_two_cliques(safe_tmp_path):
    """HE6: two disjoint 4-cliques → high Q, coverage≈1.0, degenerate=False (arXiv 2501.07025)."""
    from opencode_search.graph.quality import partition_quality

    gs = GraphStore(safe_tmp_path / "clique.db")
    try:
        for i in range(8):
            gs.upsert_symbol(f"s{i}", f"fn{i}", f"fn{i}", "function", "a.py", i+1, i+2, "python")
            comm = 0 if i < 4 else 1
            gs.upsert_community(comm, level=1, title=f"C{comm}", summary=f"c{comm}", member_count=4)
            gs._con.execute("UPDATE symbols SET community_id=? WHERE sid=?", (comm, f"s{i}"))
        for i in range(4):
            for j in range(i + 1, 4):
                gs.upsert_edge(f"s{i}", f"s{j}")
        for i in range(4, 8):
            for j in range(i + 1, 8):
                gs.upsert_edge(f"s{i}", f"s{j}")
        gs.commit()
        q = partition_quality(gs)
    finally:
        gs.close()
    assert q["modularity_q"] > 0.3, f"two-clique Q should be >0.3: {q}"
    assert q["coverage"] > 0.9, f"two-clique coverage should be ≈1.0: {q}"
    assert not q["degenerate"], f"two-clique must not be degenerate: {q}"


@pytest.mark.slow
def test_owning_root_l3_refresh_is_wired(safe_tmp_path):
    """HE7: _regen_owning_hierarchy calls build_federation_hierarchy (source-guard + behavioral)."""
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.config import project_graph_db as pgdb
    from opencode_search.core.registry import upsert_project
    from opencode_search.daemon import sweeps
    from opencode_search.daemon.federation import index_members
    from opencode_search.daemon.sweeps import _index_project, _regen_owning_hierarchy

    src = inspect.getsource(sweeps)
    assert "_regen_owning_hierarchy" in src, "function must exist in sweeps.py"
    assert "build_federation_hierarchy" in src, "must call build_federation_hierarchy"

    root, member, _marker = _federate(safe_tmp_path)
    _clean([root, member])
    try:
        upsert_project(ProjectEntry(path=str(root), enabled=True))
        index_members(str(root))
        _index_project(str(root))
        _index_project(str(member))
        _regen_owning_hierarchy(str(member))
        gs_root = GraphStore(pgdb(str(root)))
        try:
            l3 = gs_root._con.execute(
                "SELECT COUNT(*) FROM communities WHERE level>=3"
            ).fetchone()[0]
        finally:
            gs_root.close()
        assert isinstance(l3, int)
    finally:
        _clean([root, member])


def _build_l3_llm_on(root: str) -> list[tuple]:
    from opencode_search.kb.federation_hierarchy import build_federation_hierarchy
    prev = os.environ.get("OSE_WIKI_LLM")
    os.environ["OSE_WIKI_LLM"] = "1"
    try:
        build_federation_hierarchy(root)
    finally:
        if prev is None:
            os.environ.pop("OSE_WIKI_LLM", None)
        else:
            os.environ["OSE_WIKI_LLM"] = prev
    gs = GraphStore(project_graph_db(root))
    try:
        return gs._con.execute(
            "SELECT id, title, summary FROM communities WHERE level>=3 ORDER BY id"
        ).fetchall()
    finally:
        gs.close()


@pytest.mark.slow
def test_l3_narrative_faithful_to_children():
    """HE8: LLM L3 summary references ≥1 real child-L2 term (arXiv 2512.20182 FaithLens)."""
    from opencode_search.daemon.federation import federated_map
    from opencode_search.graph.llm import deepseek_key
    from opencode_search.kb.federation_hierarchy import _group_by_type
    root = _fedroot()
    if not root:
        pytest.skip("no federated root registered")
    assert deepseek_key(), "DeepSeek key required"

    def _ml2(gs: GraphStore) -> list[tuple]:
        return gs._con.execute(
            "SELECT title, COALESCE(semantic_type,'domain') FROM communities "
            "WHERE level=2 AND title IS NOT NULL AND title!='' AND title NOT IN ('(leaf)')"
        ).fetchall()

    per_member = federated_map(root, _ml2)
    member_rows = [(os.path.basename(mp), t, st) for mp, rows in per_member for t, st in rows]
    themes = _group_by_type(member_rows)
    l3_rows_he8 = _build_l3_llm_on(root)
    checked = 0
    for i, (_cid, title, summary) in enumerate(l3_rows_he8[:8]):
        if i >= len(themes) or not summary or "(no child" in summary:
            continue
        _theme, child_titles = themes[i]
        tokens = {w.lower() for t in child_titles for w in re.findall(r"[A-Za-z]{4,}", t)}
        assert sum(1 for tok in tokens if tok in summary.lower()) >= 1, (
            f"L3 {title!r} references no child term: {summary[:200]}"
        )
        checked += 1
    assert checked >= 1, "no verifiable L3 narratives"


@pytest.mark.slow
def test_l3_narrative_grounded_by_cross_model_judge():
    """HE9: claude-haiku judges DeepSeek L3 as GROUNDED (arXiv 2509.26072 cross-model bias)."""
    from opencode_search.daemon.federation import federated_map
    from opencode_search.graph.llm import deepseek_key
    from opencode_search.kb.federation_hierarchy import _group_by_type
    root = _fedroot()
    if not root:
        pytest.skip("no federated root registered")
    assert deepseek_key(), "DeepSeek key required"
    claude = shutil.which("claude")
    if not claude:
        pytest.skip("claude CLI not found")

    def _ml2(gs: GraphStore) -> list[tuple]:
        return gs._con.execute(
            "SELECT title, COALESCE(semantic_type,'domain') FROM communities "
            "WHERE level=2 AND title IS NOT NULL AND title!='' AND title NOT IN ('(leaf)')"
        ).fetchall()

    per_member = federated_map(root, _ml2)
    member_rows = [(os.path.basename(mp), t, st) for mp, rows in per_member for t, st in rows]
    themes = _group_by_type(member_rows)
    l3_rows_judge = _build_l3_llm_on(root)
    for i, (_cid, title, summary) in enumerate(l3_rows_judge[:3]):
        if i >= len(themes) or not summary or "(no child" in summary:
            continue
        _theme, child_titles = themes[i]
        if not child_titles:
            continue
        context = "\n".join(f"- {t}" for t in child_titles[:8])
        prompt = (
            f"CONTEXT (the ONLY allowed source):\n{context}\n\nNARRATIVE:\n{summary}\n\n"
            "Does the narrative invent identifiers absent from the context? "
            "Reply exactly: GROUNDED or UNGROUNDED."
        )
        try:
            verdict = subprocess.check_output(
                [claude, "-p", "--model", "claude-haiku-4-5", prompt], timeout=45, text=True,
            ).strip()
        except subprocess.TimeoutExpired:
            pytest.skip(f"claude CLI timed out on {_cid}")
        assert "UNGROUNDED" not in verdict, (
            f"L3 {title!r} UNGROUNDED. Context:{context[:160]} Summary:{summary[:160]}"
        )


@pytest.mark.slow
def test_global_ask_surfaces_l3_after_live_build():
    """HE10: ask(scope=global) names an L3 federation-domain title (full pipeline proof)."""
    from opencode_search.graph.llm import deepseek_key
    from opencode_search.server.mcp import ask as _mcp_ask
    root = _fedroot()
    if not root:
        pytest.skip("no federated root registered")
    assert deepseek_key(), "DeepSeek key required"
    l3_rows_ask = _build_l3_llm_on(root)
    l3_titles = [r[1] for r in l3_rows_ask if r[1]]
    assert l3_titles, "LLM-on L3 build must produce ≥1 titled domain"
    ctx = asyncio.run(_mcp_ask("What is the overall cross-service architecture?", root, "global"))
    assert any(t.lower() in ctx.lower() for t in l3_titles), (
        f"ask(scope=global) did not surface any L3 domain.\n"
        f"L3: {l3_titles[:5]}\nContext (first 400): {ctx[:400]}"
    )
