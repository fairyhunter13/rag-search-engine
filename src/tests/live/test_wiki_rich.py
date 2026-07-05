"""Live E2E tests for the rich wiki bundle (Phase B). No mocks; June 2026 research-backed.

OSE's edge over an LLM-authored wiki: diagrams and citations are DETERMINISTIC from real data, so
most of these are *guarantees*, not probabilistic checks. Methods:
- recursive ecosystem testing — discover links → follow → validate cross-document consistency
  (glama PRD#28, recursive documentation testing).
- ground-truth citation/grounding — every cited source resolves on disk (vs RAGAS faithfulness /
  FaithLens arXiv 2512.20182, which an LLM-cited wiki can only approximate).
- mermaid validation (mermaid-validator; MermaidSeqBench arXiv 2511.14967).

Fast tests build deterministically (no DeepSeek — wiki prose is template-based, GPU-free).
Slow tests check narrative faithfulness + the API surface. Requires daemon at :8765 + an
enriched project.
"""
import os
import re
from pathlib import Path

import pytest

from rag_search.core.config import project_graph_db, project_wiki_dir
from rag_search.daemon.federation import federated_map
from rag_search.graph.store import GraphStore
from rag_search.kb.wiki import build_federated_index, build_wiki
from tests.live._sample_workspace import SampleWorkspace, replay_member_golden

pytestmark = pytest.mark.live

_LINK_RE = re.compile(r"\]\(([^)]+)\)")
_CITE_RE = re.compile(r"\[([^\]]+):(\d+)\]\(([^)]+)\)")
_MERMAID_RE = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)


def _build(project: str, out: Path, llm: bool = False) -> int:
    """Build a wiki into `out`. llm=False (default) uses no DeepSeek (wiki prose is template-based)."""
    gs = GraphStore(project_graph_db(project))
    try:
        return build_wiki(gs, out)
    finally:
        gs.close()


def _project_root(project: str) -> str:
    gs = GraphStore(project_graph_db(project))
    try:
        files = [r[0] for r in gs._con.execute(
            "SELECT DISTINCT file FROM symbols WHERE file IS NOT NULL AND file!=''").fetchall()]
    finally:
        gs.close()
    if not files:
        return project
    return os.path.commonpath(files) if len(files) > 1 else os.path.dirname(files[0])


def _read(d: Path, name: str) -> str:
    return (d / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def enriched_promo(sample_workspace: SampleWorkspace) -> str:
    """Re-replay promo's enrichment golden before any wiki build (idempotent self-heal).

    Guards against an in-process _index_project() call from a preceding slow test clearing
    the replayed golden summaries, which would cause build_wiki to return 0 pages.
    """
    replay_member_golden(sample_workspace.promo)
    return sample_workspace.promo


@pytest.fixture(scope="module")
def wiki(tmp_path_factory, enriched_promo: str):
    """Build a wiki from sample promo-svc (7 enriched L1 communities)."""
    project = enriched_promo
    out = tmp_path_factory.mktemp("wiki")
    n = _build(project, out, llm=False)
    assert n > 1, f"expected multiple pages, got {n}"
    return {"dir": out, "project": project, "root": _project_root(project), "pages": n}


def test_no_dangling_internal_links(wiki):
    """W1 (recursive integrity): every internal *.md link resolves to a generated page."""
    d = wiki["dir"]
    dangling = []
    for f in d.glob("*.md"):
        for tgt in _LINK_RE.findall(_read(d, f.name)):
            if tgt.endswith(".md") and not (d / tgt).exists():
                dangling.append((f.name, tgt))
    assert not dangling, f"dangling internal links: {dangling[:8]}"


def test_index_reaches_every_page(wiki):
    """W2 (no orphans): BFS from index.md over *.md links reaches every generated page."""
    d = wiki["dir"]
    all_pages = {f.name for f in d.glob("*.md")}
    seen, frontier = {"index.md"}, ["index.md"]
    while frontier:
        cur = frontier.pop()
        for tgt in _LINK_RE.findall(_read(d, cur)):
            if tgt.endswith(".md") and tgt in all_pages and tgt not in seen:
                seen.add(tgt)
                frontier.append(tgt)
    orphans = all_pages - seen
    assert not orphans, f"pages unreachable from index.md: {sorted(orphans)[:8]}"


def test_citations_resolve_on_disk(wiki):
    """W3 (ground truth): every cited source FILE exists; >=97% of line numbers are in-bounds.

    100% file existence is the hard guarantee an LLM-cited wiki cannot make. A small line-drift
    tolerance absorbs files edited between indexing and this run (live repo), not hallucination.
    """
    d, root = wiki["dir"], wiki["root"]
    total = missing = oob = 0
    for f in d.glob("community_*.md"):
        for _txt, line, tgt in _CITE_RE.findall(_read(d, f.name)):
            total += 1
            abs_p = os.path.join(root, tgt)
            if not os.path.exists(abs_p):
                missing += 1
                continue
            with open(abs_p, encoding="utf-8", errors="replace") as fh:
                nlines = sum(1 for _ in fh)
            if int(line) > nlines:
                oob += 1
    assert total > 0, "no citations rendered — community pages must cite member sources"
    assert missing == 0, f"{missing}/{total} cited files do not exist (hallucinated citations)"
    assert oob / total <= 0.03, f"{oob}/{total} citation lines out-of-bounds (>3%): index too stale"


def test_no_absolute_device_path_leaks(wiki):
    """W4 (public-repo hygiene): no absolute device path appears in any page (citations relative)."""
    d = wiki["dir"]
    leaks = [f.name for f in d.glob("*.md") if "/home/" in _read(d, f.name)]
    assert not leaks, f"absolute device paths leaked into wiki pages: {leaks[:5]}"


def test_every_mermaid_block_is_valid(wiki):
    """W5 (mermaid validity): every ```mermaid block is a well-formed `graph TD` with capped nodes."""
    d = wiki["dir"]
    blocks = 0
    for f in d.glob("*.md"):
        for body in _MERMAID_RE.findall(_read(d, f.name)):
            blocks += 1
            assert body.startswith("graph TD\n"), f"{f.name}: mermaid must start 'graph TD'"
            node_ids = set(re.findall(r"\b(n\d+)\[", body))
            assert node_ids, f"{f.name}: mermaid block has no alnum node ids"
            assert len(node_ids) <= 40, f"{f.name}: {len(node_ids)} nodes exceeds cap"
            assert "-->" in body, f"{f.name}: mermaid block has no edges"
    assert blocks >= 1, "expected at least one mermaid diagram across the wiki"


def test_callgraph_mermaid_backed_by_real_edges(wiki):
    """W6 (diagrams from data, not LLM): a community 'Call graph' implies >=1 real intra-edge."""
    d, project = wiki["dir"], wiki["project"]
    gs = GraphStore(project_graph_db(project))
    try:
        checked = 0
        for f in d.glob("community_*.md"):
            if "## Call graph" not in _read(d, f.name):
                continue
            cid = int(f.stem.split("_")[1])
            n = gs._con.execute(
                "SELECT COUNT(*) FROM edges e JOIN symbols s1 ON e.caller_sid=s1.sid "
                "JOIN symbols s2 ON e.callee_sid=s2.sid "
                "WHERE s1.community_id=? AND s2.community_id=?", (cid, cid)).fetchone()[0]
            assert n >= 1, f"community_{cid} has a Call graph but 0 intra edges (fabricated)"
            checked += 1
    finally:
        gs.close()
    assert checked >= 1, "no community call-graphs to verify (expected some communities with edges)"


def test_no_domain_pages_generated(wiki):
    """W7 (WS-B): build_wiki must NOT write domain_*.md files (L2 hierarchy removed)."""
    d = wiki["dir"]
    domain_pages = list(d.glob("domain_*.md"))
    assert not domain_pages, f"domain_*.md pages must not be generated after WS-B: {domain_pages[:3]}"


def test_community_page_structure(wiki):
    """W8 (structure): each community page has a heading, a type badge, and a back-link to index."""
    d = wiki["dir"]
    for f in d.glob("community_*.md"):
        txt = _read(d, f.name)
        assert txt.startswith("# "), f"{f.name}: missing H1 title"
        assert "**Type:**" in txt and "**Members:**" in txt, f"{f.name}: missing type/member badge"
        assert "[← Index](index.md)" in txt, f"{f.name}: missing back-link to index"


def test_index_groups_by_semantic_type(wiki):
    """W9: the index groups communities under their real semantic-type sections."""
    d, project = wiki["dir"], wiki["project"]
    idx = _read(d, "index.md")
    gs = GraphStore(project_graph_db(project))
    try:
        types = {r[0] for r in gs._con.execute(
            "SELECT DISTINCT semantic_type FROM communities WHERE level=1 AND semantic_type IS NOT NULL "
            "AND summary IS NOT NULL AND summary!=''").fetchall()}
    finally:
        gs.close()
    labels = {"business_process": "Business Process", "business_rule": "Business Rule",
              "feature": "Feature", "utility": "Utility", "infrastructure": "Infrastructure",
              "domain": "Domain", "test": "Test"}
    present = sum(1 for t in types if f"### {labels.get(t, t)}" in idx)
    assert present >= max(1, len(types) - 1), f"index missing type sections; have {types}"


def test_deterministic_build_is_byte_identical(tmp_path_factory, enriched_promo: str):
    """W10 (determinism): two wiki builds are byte-identical (template-based, no LLM variance)."""
    project = enriched_promo
    a = tmp_path_factory.mktemp("det_a")
    b = tmp_path_factory.mktemp("det_b")
    _build(project, a, llm=False)
    _build(project, b, llm=False)
    names = sorted(x.name for x in a.glob("*.md"))
    assert names, "no pages built"
    diff = [n for n in names if (a / n).read_bytes() != (b / n).read_bytes()]
    assert not diff, f"non-deterministic pages with LLM off: {diff[:5]}"


def test_wiki_builder_uses_no_embedder_or_local_llm():
    """W11 (resource guard): the wiki module never imports the embedder or a resident LLM.

    The cost-shaped design: wiki assembly is pure SQL + an optional cloud DeepSeek call — no
    embedder (~1GB), no llama-server spin. Enforced at the source level.
    """
    import inspect

    import rag_search.kb.wiki as w
    src = inspect.getsource(w)
    assert "embedder" not in src and "Embedder" not in src, "wiki must not touch the embedder"
    assert "import chat" not in src, "wiki narrative must use cloud DeepSeek, not resident chat()"


def test_build_without_llm_builds_successfully(tmp_path_factory, enriched_promo: str):
    """W12: wiki builds without LLM (template-based prose, always deterministic)."""
    out = tmp_path_factory.mktemp("nollm")
    n = _build(enriched_promo, out, llm=False)
    assert n > 1, "wiki must build from template prose without any cloud LLM"
    assert (out / "index.md").exists(), "index.md must always be generated"


def test_standalone_project_has_no_federation_page(sample_workspace: SampleWorkspace):
    """W14: build_federated_index is a no-op for a project with no members."""
    assert build_federated_index(sample_workspace.ledger) == 0


def test_federated_root_gets_federation_index(sample_workspace: SampleWorkspace):
    """W13: a federated root (>1 member) gets federation.md naming members; rollup == union."""
    root = sample_workspace.fed_root
    assert root, "sample federation root must be built by sample_workspace fixture"
    assert build_federated_index(root) == 1
    fed = (project_wiki_dir(root) / "federation.md").read_text(encoding="utf-8")
    per_member = federated_map(root, lambda gs: dict(gs._con.execute(
        "SELECT COALESCE(NULLIF(semantic_type,''),'unclassified'), COUNT(*) FROM communities "
        "WHERE level=1 GROUP BY COALESCE(NULLIF(semantic_type,''),'unclassified')").fetchall()))
    for path, _types in per_member:
        assert os.path.basename(path) in fed, f"member {os.path.basename(path)} missing"
    union: dict[str, int] = {}
    for _path, types in per_member:
        for st, n in types.items():
            union[st] = union.get(st, 0) + n
    for st, n in union.items():
        if n:
            assert str(n) in fed, f"rollup missing count for {st}={n}"




@pytest.mark.slow
def test_api_build_serve_and_export_wiki(live_client, project_with_communities):
    """W16 (API e2e): build_wiki?action=wiki → page serves rich content → export bundles it."""
    project = project_with_communities
    r = live_client.post(f"/api/build_wiki?project={project}&action=wiki", data=b"")
    assert r.status_code == 200, f"action=wiki failed: {r.status_code} {r.text[:80]}"
    assert r.json().get("pages_written", 0) > 0
    pages = live_client.get(f"/api/wiki?project={project}").json().get("pages", [])
    assert "index" in pages, f"index page missing from {pages[:8]}"
    page = next((p for p in pages if p.startswith("community_")), "index")
    pr = live_client.get(f"/api/wiki/page?project={project}&name={page}")
    assert pr.status_code == 200, f"page fetch (name=) must succeed: {pr.status_code}"
    assert "# " in pr.json().get("content", ""), "served page must contain a markdown heading"
    ex = live_client.get(f"/api/wiki/export?project={project}&format=markdown")
    assert ex.status_code == 200
    body = ex.json()
    assert body.get("pages", 0) > 0 and "## Contents" in body.get("content", ""), "export needs a ToC"
    exj = live_client.get(f"/api/wiki/export?project={project}&format=json")
    assert isinstance(exj.json().get("pages"), list), "json export must return a pages list"


