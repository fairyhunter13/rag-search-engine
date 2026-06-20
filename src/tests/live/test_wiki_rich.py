"""Live E2E tests for the rich wiki bundle (Phase B). No mocks; June 2026 research-backed.

OSE's edge over an LLM-authored wiki: diagrams and citations are DETERMINISTIC from real data, so
most of these are *guarantees*, not probabilistic checks. Methods:
- recursive ecosystem testing — discover links → follow → validate cross-document consistency
  (glama PRD#28, recursive documentation testing).
- ground-truth citation/grounding — every cited source resolves on disk (vs RAGAS faithfulness /
  FaithLens arXiv 2512.20182, which an LLM-cited wiki can only approximate).
- mermaid validation (mermaid-validator; MermaidSeqBench arXiv 2511.14967).

Fast tests build with OSE_WIKI_LLM=0 (deterministic, no DeepSeek, zero GPU). Slow tests build with
real DeepSeek to check narrative faithfulness + the API surface. Requires daemon at :8765 + an
enriched project.
"""
import os
import re
from pathlib import Path

import pytest

from opencode_search.core.config import project_graph_db, project_wiki_dir
from opencode_search.core.registry import list_projects
from opencode_search.daemon.federation import expand_federation, federated_map
from opencode_search.graph.store import GraphStore
from opencode_search.kb.wiki import build_federated_index, build_wiki

pytestmark = pytest.mark.live

_LINK_RE = re.compile(r"\]\(([^)]+)\)")
_CITE_RE = re.compile(r"\[([^\]]+):(\d+)\]\(([^)]+)\)")
_MERMAID_RE = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)


def _enriched_project(min_communities: int = 10) -> str | None:
    for p in list_projects():
        if not p.enabled or not project_graph_db(p.path).exists():
            continue
        gs = GraphStore(project_graph_db(p.path))
        try:
            n = gs._con.execute(
                "SELECT COUNT(*) FROM communities WHERE level=1 AND summary IS NOT NULL "
                "AND summary!=''").fetchone()[0]
        finally:
            gs.close()
        if n >= min_communities:
            return p.path
    return None


def _federated_root() -> str | None:
    for p in list_projects():
        if p.enabled and len(expand_federation(p.path)) > 1:
            return p.path
    return None


def _build(project: str, out: Path, llm: bool = False) -> int:
    """Build a wiki into `out`; llm=False forces deterministic templated prose (no DeepSeek)."""
    prev = os.environ.get("OSE_WIKI_LLM")
    os.environ["OSE_WIKI_LLM"] = "1" if llm else "0"
    try:
        gs = GraphStore(project_graph_db(project))
        try:
            return build_wiki(gs, out)
        finally:
            gs.close()
    finally:
        if prev is None:
            os.environ.pop("OSE_WIKI_LLM", None)
        else:
            os.environ["OSE_WIKI_LLM"] = prev


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
def wiki(tmp_path_factory):
    project = _enriched_project()
    assert project, "need an enabled project with >=10 enriched L1 communities"
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


def test_domain_pages_list_exact_db_children(wiki):
    """W7 (cross-doc consistency): each domain page links exactly its real DB child communities."""
    d, project = wiki["dir"], wiki["project"]
    gs = GraphStore(project_graph_db(project))
    try:
        checked = 0
        for f in d.glob("domain_*.md"):
            cid = int(f.stem.split("_")[1])
            db_kids = {r[0] for r in gs._con.execute(
                "SELECT id FROM communities WHERE parent_id=?", (cid,)).fetchall()}
            linked = {int(m) for m in re.findall(r"community_(\d+)\.md", _read(d, f.name))}
            assert linked == db_kids, f"domain_{cid} children {linked} != DB {db_kids}"
            checked += 1
    finally:
        gs.close()
    assert checked >= 1, "no domain pages to verify"


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


def test_deterministic_build_is_byte_identical(tmp_path_factory):
    """W10 (determinism): with OSE_WIKI_LLM=0, two builds are byte-identical (no LLM variance)."""
    project = _enriched_project()
    assert project
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

    import opencode_search.kb.wiki as w
    src = inspect.getsource(w)
    assert "embedder" not in src and "Embedder" not in src, "wiki must not touch the embedder"
    assert "import chat" not in src, "wiki narrative must use cloud DeepSeek, not resident chat()"


def test_build_without_llm_uses_templated_prose(tmp_path_factory):
    """W12 (graceful degradation): with OSE_WIKI_LLM=0 the wiki builds with templated prose.

    Exercises the real kill-switch — the same `_narrate() -> ''` fallback branch a missing
    DeepSeek key takes — so the wiki always builds without the cloud LLM, using only real
    integration (this repo forbids test doubles). The determinism test proves this path is stable.
    """
    project = _enriched_project()
    assert project
    out = tmp_path_factory.mktemp("nollm")
    n = _build(project, out, llm=False)
    assert n > 1, "wiki must build with the LLM kill-switch on"
    domains = list(out.glob("domain_*.md"))
    if domains:
        txt = domains[0].read_text()
        assert "**Architecture Domain**" in txt and txt.strip(), "domain page must have templated prose"


def test_standalone_project_has_no_federation_page():
    """W14: build_federated_index is a no-op for a project with no members."""
    standalone = next((p.path for p in list_projects()
                       if p.enabled and len(expand_federation(p.path)) == 1), None)
    assert standalone, "need a standalone project"
    assert build_federated_index(standalone) == 0


def test_federated_root_gets_federation_index():
    """W13: a federated root (>1 member) gets federation.md naming members; rollup == union."""
    root = _federated_root()
    assert root, "no federated root registered (expected astro-project)"
    assert build_federated_index(root) == 1
    fed = (project_wiki_dir(root) / "federation.md").read_text(encoding="utf-8")
    per_member = federated_map(root, lambda gs: dict(gs._con.execute(
        "SELECT COALESCE(semantic_type,'unclassified'), COUNT(*) FROM communities "
        "WHERE level=1 GROUP BY semantic_type").fetchall()))
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
def test_domain_narrative_is_faithful(tmp_path_factory):
    """W15 (RAGAS/FaithLens faithfulness): a real DeepSeek domain narrative names real children."""
    from opencode_search.graph.llm import deepseek_key
    if not deepseek_key():
        pytest.skip("no DeepSeek key — faithfulness needs the real model")
    project = _enriched_project()
    out = tmp_path_factory.mktemp("llm")
    _build(project, out, llm=True)
    gs = GraphStore(project_graph_db(project))
    try:
        checked = 0
        for f in sorted(out.glob("domain_*.md"))[:3]:
            cid = int(f.stem.split("_")[1])
            kids = [r[0] for r in gs._con.execute(
                "SELECT title FROM communities WHERE parent_id=? AND title IS NOT NULL", (cid,)).fetchall()]
            if len(kids) < 2:
                continue
            narrative = f.read_text().split("**Architecture Domain**", 1)[-1].split("## ", 1)[0]
            tokens = {w.lower() for k in kids for w in re.findall(r"[A-Za-z]{4,}", k)}
            hit = sum(1 for t in tokens if t in narrative.lower())
            assert hit >= 1, f"domain_{cid} narrative references no real child terms: {narrative[:160]!r}"
            checked += 1
        assert checked >= 1, "no multi-child domain to check faithfulness"
    finally:
        gs.close()


@pytest.mark.slow
def test_api_build_serve_and_export_wiki(live_client):
    """W16 (API e2e): build_hierarchy?action=wiki → page serves rich content → export bundles it."""
    project = next((p.path for p in list_projects()
                    if p.enabled and project_graph_db(p.path).exists()), None)
    assert project, "need an indexed project"
    r = live_client.post(f"/api/build_hierarchy?project={project}&action=wiki", data=b"")
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


@pytest.mark.slow
def test_wiki_narrative_grounded_by_cross_model_judge(tmp_path_factory):
    """W17 (LLM-as-judge, cross-model): claude CLI judges a DeepSeek narrative as grounded.

    Cross-model on purpose — DeepSeek must not grade its own output (self-preference bias). The
    judge is conservative: it only fails a narrative that invents sub-systems absent from context.
    """
    from opencode_search.graph.llm import deepseek_key
    if not deepseek_key():
        pytest.skip("no DeepSeek key — nothing to judge")
    project = _enriched_project()
    out = tmp_path_factory.mktemp("judge")
    _build(project, out, llm=True)
    gs = GraphStore(project_graph_db(project))
    try:
        target = None
        for f in sorted(out.glob("domain_*.md")):
            cid = int(f.stem.split("_")[1])
            kids = [r[0] for r in gs._con.execute(
                "SELECT title FROM communities WHERE parent_id=? AND title IS NOT NULL", (cid,)).fetchall()]
            if len(kids) >= 2:
                target = (f.read_text(), kids)
                break
    finally:
        gs.close()
    if not target:
        pytest.skip("no multi-child domain to judge")
    txt, kids = target
    narrative = txt.split("**Architecture Domain**", 1)[-1].split("## ", 1)[0].strip()
    prompt = (
        "You are a grounding judge. CONTEXT lists the real sub-systems of a software domain. Reply "
        "ONLY 'GROUNDED' if the NARRATIVE describes only sub-systems consistent with the context "
        "(paraphrase is fine), or 'UNGROUNDED' if it invents systems not implied by it.\n\n"
        f"CONTEXT sub-systems: {', '.join(kids)}\n\nNARRATIVE: {narrative}\n\nVerdict:"
    )
    import shutil
    import subprocess
    claude = shutil.which("claude")
    if not claude:
        pytest.skip("claude CLI not available for cross-model judge")
    verdict = subprocess.check_output(
        [claude, "-p", "--model", "claude-haiku-4-5", prompt], timeout=30,
    ).decode(errors="replace").strip().upper()
    assert "UNGROUNDED" not in verdict, f"judge flagged narrative ungrounded: {verdict!r}\n{narrative[:200]}"
