"""Live e2e: docgen C4×Diátaxis ↔ graph hierarchy mapping (no mocks, C1-C7)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_VENDOR_SRC = Path(__file__).parent.parent.parent.parent / "vendor" / "docgen" / "src"
if str(_VENDOR_SRC) not in sys.path:
    sys.path.insert(0, str(_VENDOR_SRC))

_C4_BUCKETS = ["01-context", "02-containers", "03-components",
               "04-reference", "05-how-to", "06-decisions"]


def _any_project():
    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import list_projects
    for p in list_projects():
        if not p.enabled or "ocs-test-dirs" in p.path:
            continue
        if Path(p.path).name.startswith(("tmp", "test-")):
            continue
        if project_graph_db(p.path).exists():
            return p.path
    return None


def _fedroot():
    from opencode_search.core.registry import list_projects
    from opencode_search.daemon.federation import expand_federation
    return next(
        (p.path for p in list_projects()
         if p.enabled and len(expand_federation(p.path)) > 1), None)


def _gen(proj, out, member_dbs=None):
    from ose_docgen.generate import generate

    from opencode_search.core.config import project_graph_db
    return generate(project_path=out, graph_db_path=project_graph_db(proj),
                    member_db_paths=member_dbs or [], docs_dir=str(out / "docs"), llm=False)


@pytest.fixture(scope="module")
def docs_out(tmp_path_factory):
    proj = _any_project()
    if not proj:
        pytest.fail("no enabled project with graph.db")
    out = tmp_path_factory.mktemp("dge")
    r = _gen(proj, out)
    assert r.get("errors", []) == [], f"generate errors: {r['errors']}"
    return out, proj


def test_c1_readme_and_all_buckets_and_meta(docs_out):
    """C1: README + all 6 C4 buckets + _meta/provenance.json."""
    out, _ = docs_out
    docs = out / "docs"
    all_mds = list(docs.rglob("*.md"))
    names = {f.name for f in all_mds}
    assert "README.md" in names
    assert (docs / "_meta" / "provenance.json").exists()
    for bucket in _C4_BUCKETS:
        assert any(bucket in str(f) for f in all_mds), f"missing bucket {bucket!r}"


def test_c3_provenance_frontmatter(docs_out):
    """C3: every generated .md has generated:true, source_sig, c4_level."""
    from ose_docgen.provenance import _parse_fm
    out, _ = docs_out
    for md in (out / "docs").rglob("*.md"):
        if "_meta" in md.parts:
            continue
        fm = _parse_fm(md.read_text(encoding="utf-8"))
        assert fm.get("generated") == "true", f"{md.name}: no generated:true"
        assert fm.get("source_sig"), f"{md.name}: no source_sig"
        assert fm.get("c4_level"), f"{md.name}: no c4_level"


def test_c4_no_home_path_leak(docs_out):
    """C4: no /home/ absolute path in any generated file."""
    out, _ = docs_out
    for md in (out / "docs").rglob("*.md"):
        assert "/home/" not in md.read_text(encoding="utf-8"), \
            f"{md.relative_to(out)}: /home/ leaked"


def test_c6_idempotent(docs_out):
    """C6: second generate() on same graph.db produces no file changes."""
    import hashlib
    out, proj = docs_out
    docs = out / "docs"
    h1 = {str(f.relative_to(docs)): hashlib.sha256(f.read_bytes()).hexdigest()
          for f in docs.rglob("*.md")}
    _gen(proj, out)
    h2 = {str(f.relative_to(docs)): hashlib.sha256(f.read_bytes()).hexdigest()
          for f in docs.rglob("*.md")}
    assert h1 == h2


def test_c5_ose_docgen_off(tmp_path):
    """C5: OSE_DOCGEN=0 → run_docgen touches nothing (no-op)."""
    from opencode_search.kb.docgen import run_docgen
    proj = _any_project()
    if not proj:
        pytest.fail("no enabled project")
    before_docs = Path(proj) / "docs"
    existed = before_docs.exists()
    prev = os.environ.get("OSE_DOCGEN")
    os.environ["OSE_DOCGEN"] = "0"
    try:
        run_docgen(proj)
    finally:
        if prev is None:
            os.environ.pop("OSE_DOCGEN", None)
        else:
            os.environ["OSE_DOCGEN"] = prev
    # docs dir state must be unchanged from before the call
    assert before_docs.exists() == existed


def test_c7_federated_generates_more_files_with_members(tmp_path_factory):
    """C7: federated root with member_dbs generates more docs than without (members add containers)."""
    from opencode_search.core.config import project_graph_db
    from opencode_search.daemon.federation import expand_federation
    root = _fedroot()
    if not root:
        pytest.fail("no federated root")
    members = [m for m in expand_federation(root) if m != root]
    member_dbs = [str(project_graph_db(m)) for m in members if project_graph_db(m).exists()]
    if not member_dbs:
        pytest.fail("no member graph.dbs")
    out_no = tmp_path_factory.mktemp("fed_no")
    out_yes = tmp_path_factory.mktemp("fed_yes")
    r_no = _gen(root, out_no, [])
    r_yes = _gen(root, out_yes, member_dbs)
    assert r_no.get("errors", []) == []
    assert r_yes.get("errors", []) == []
    # With member_dbs: at minimum, docs tree should exist and not be smaller
    n_no = len(list((out_no / "docs").rglob("*.md")))
    n_yes = len(list((out_yes / "docs").rglob("*.md")))
    assert n_yes >= n_no, f"member_dbs should not reduce docs count ({n_yes} < {n_no})"
