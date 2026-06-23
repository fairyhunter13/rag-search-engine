"""Phase 2 — Information structural spine tests (ST1–ST8 + SG2).

Research: PageIndex (vectorless tree), StructuralCoder (hybrid dir+AST),
MetaRAG booster MRs (arXiv 2509.09360), DIKW doctrine (HR22).

ST1  build_structure_tree consumes zero LLM tokens
ST2  code-less directory (docs-only) appears as a dir node
ST3  dir/file nodes excluded from ask.py selectors (kind guard)
ST4  member_count = direct-child count for dirs; symbol count for files
ST5  determinism MR: two builds produce byte-identical structural node sets
ST6  incrementality MR: +1 file → exactly one new file node
ST7  no-orphan invariant: every non-root structural node has a valid parent_id
ST8  semantic_type abstention: kind IN ('dir','file') ⇒ semantic_type IS NULL
SG2  structure.py imports no deepseek/llm module
"""
from __future__ import annotations

import inspect

import pytest

from opencode_search.graph.store import GraphStore
from opencode_search.kb.structure import build_structure_tree

pytestmark = pytest.mark.live


def _make_tree(root):
    """Minimal synthetic project: src/ has Python, docs/ is code-less."""
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text(
        "def alpha(): pass\ndef beta(x): return x\nclass Gamma: pass\n"
    )
    (root / "src" / "util.py").write_text("def helper(): pass\n")
    (root / "docs").mkdir()
    (root / "docs" / "README.md").write_text("# Project\n")


def _open_gs(tmp) -> GraphStore:
    return GraphStore(tmp / "g.db")


def _insert_symbols(gs: GraphStore, fpath, root) -> int:
    from opencode_search.graph.extractor import extract_symbols, symbol_id
    from opencode_search.index.discover import detect_language
    lang = detect_language(fpath)
    syms = list(extract_symbols(fpath, fpath.read_text(), lang))
    for s in syms:
        gs.upsert_symbol(
            symbol_id(str(fpath), s.name, s.start_line),
            s.name, s.qualified_name, s.kind,
            str(fpath), s.start_line, s.end_line, s.language,
        )
    gs.commit()
    return len(syms)


def test_st1_zero_llm_tokens(safe_tmp_path):
    """ST1: build_structure_tree consumes zero LLM completion tokens."""
    from opencode_search.graph.llm import llm_token_stats
    root = safe_tmp_path / "proj"
    root.mkdir()
    _make_tree(root)
    gs = _open_gs(safe_tmp_path)
    try:
        calls_before = llm_token_stats().get("enrich.calls", 0)
        build_structure_tree(gs, str(root))
        calls_after = llm_token_stats().get("enrich.calls", 0)
        assert calls_after == calls_before, (
            f"ST1: {calls_after - calls_before} LLM calls made — must be 0"
        )
        n = gs._con.execute("SELECT COUNT(*) FROM communities WHERE level=0").fetchone()[0]
        assert n > 0, "ST1: no structural nodes inserted"
    finally:
        gs.close()


def test_st2_codeless_dir_appears(safe_tmp_path):
    """ST2: a docs-only directory appears as a dir node."""
    root = safe_tmp_path / "proj"
    root.mkdir()
    _make_tree(root)
    gs = _open_gs(safe_tmp_path)
    try:
        build_structure_tree(gs, str(root))
        paths = {r[0] for r in gs._con.execute(
            "SELECT path FROM communities WHERE level=0 AND kind='dir'"
        ).fetchall()}
        assert "docs" in paths, f"ST2: docs-only dir missing. Got dirs: {paths}"
        assert "." in paths, "ST2: root dir node missing"
    finally:
        gs.close()


def test_st3_kind_guard_excludes_from_ask(safe_tmp_path):
    """ST3: dir/file nodes are absent from _top_communities_semantic and _community_context."""
    from opencode_search.query.ask import _community_context, _top_communities_semantic
    root = safe_tmp_path / "proj"
    root.mkdir()
    _make_tree(root)
    gs = _open_gs(safe_tmp_path)
    try:
        build_structure_tree(gs, str(root))
        gs.upsert_community(999, level=1, title="PaymentService",
                            summary="payment gateway routing service", member_count=10,
                            semantic_type="feature")
        gs.commit()
        sem = _top_communities_semantic("files", [gs])
        ctx = _community_context([gs])
        assert "subdirectory" not in sem, f"ST3: dir node in semantic: {sem[:200]}"
        assert "subdirectory" not in ctx, f"ST3: dir node in ctx: {ctx[:200]}"
        assert "symbol(s) [" not in sem, f"ST3: file node in semantic: {sem[:200]}"
        assert "symbol(s) [" not in ctx, f"ST3: file node in ctx: {ctx[:200]}"
    finally:
        gs.close()


def test_st4_member_count_direct_children(safe_tmp_path):
    """ST4: dir member_count = direct-child count; file member_count = symbol count."""
    root = safe_tmp_path / "proj"
    root.mkdir()
    _make_tree(root)
    gs = _open_gs(safe_tmp_path)
    try:
        for fpath in [(root / "src" / "main.py"), (root / "src" / "util.py")]:
            _insert_symbols(gs, fpath, root)
        build_structure_tree(gs, str(root))
        root_mc = gs._con.execute(
            "SELECT member_count FROM communities WHERE level=0 AND kind='dir' AND path='.'",
        ).fetchone()
        assert root_mc is not None, "ST4: root dir node missing"
        assert root_mc[0] == 2, f"ST4: root dir member_count={root_mc[0]}, expected 2"
        main_mc = gs._con.execute(
            "SELECT member_count FROM communities"
            " WHERE level=0 AND kind='file' AND path LIKE '%main.py'",
        ).fetchone()
        assert main_mc is not None, "ST4: main.py file node missing"
        assert main_mc[0] == 3, f"ST4: main.py member_count={main_mc[0]}, expected 3"
    finally:
        gs.close()


def test_st5_determinism_mr(safe_tmp_path):
    """ST5 (MR): two builds produce byte-identical structural node sets."""
    root = safe_tmp_path / "proj"
    root.mkdir()
    _make_tree(root)

    def _node_set(gs: GraphStore) -> frozenset:
        return frozenset(gs._con.execute(
            "SELECT kind, path, member_count FROM communities WHERE level=0 ORDER BY path"
        ).fetchall())

    gs1 = _open_gs(safe_tmp_path)
    try:
        build_structure_tree(gs1, str(root))
        first = _node_set(gs1)
    finally:
        gs1.close()
    gs2 = GraphStore(safe_tmp_path / "g2.db")
    try:
        build_structure_tree(gs2, str(root))
        second = _node_set(gs2)
    finally:
        gs2.close()
    assert first == second, (
        f"ST5: non-deterministic.\n  only in 1st: {first - second}\n  only in 2nd: {second - first}"
    )


def test_st6_incrementality_mr(safe_tmp_path):
    """ST6 (MR): adding one file produces exactly one new file node."""
    root = safe_tmp_path / "proj"
    root.mkdir()
    _make_tree(root)
    gs = _open_gs(safe_tmp_path)
    try:
        build_structure_tree(gs, str(root))
        before = gs._con.execute(
            "SELECT COUNT(*) FROM communities WHERE level=0 AND kind='file'"
        ).fetchone()[0]
        (root / "src" / "new_mod.py").write_text("def new_fn(): pass\n")
        build_structure_tree(gs, str(root))
        after = gs._con.execute(
            "SELECT COUNT(*) FROM communities WHERE level=0 AND kind='file'"
        ).fetchone()[0]
        assert after == before + 1, f"ST6: expected +1 file node, got {after - before}"
        found = gs._con.execute(
            "SELECT path FROM communities WHERE level=0 AND kind='file' AND path LIKE '%new_mod%'"
        ).fetchone()
        assert found, "ST6: new_mod.py file node not found after increment"
    finally:
        gs.close()


def test_st7_no_orphan_invariant(safe_tmp_path):
    """ST7: every non-root structural node has a valid parent_id."""
    root = safe_tmp_path / "proj"
    root.mkdir()
    _make_tree(root)
    gs = _open_gs(safe_tmp_path)
    try:
        build_structure_tree(gs, str(root))
        all_ids = {r[0] for r in gs._con.execute(
            "SELECT id FROM communities WHERE level=0"
        ).fetchall()}
        for _nid, path, parent_id in gs._con.execute(
            "SELECT id, path, parent_id FROM communities WHERE level=0 AND path != '.'"
        ).fetchall():
            assert parent_id is not None, f"ST7: path={path!r} has NULL parent_id"
            assert parent_id in all_ids, f"ST7: path={path!r} parent_id={parent_id} not found"
    finally:
        gs.close()


def test_st8_semantic_type_abstention(safe_tmp_path):
    """ST8: kind IN ('dir','file') ⇒ semantic_type IS NULL."""
    root = safe_tmp_path / "proj"
    root.mkdir()
    _make_tree(root)
    gs = _open_gs(safe_tmp_path)
    try:
        build_structure_tree(gs, str(root))
        typed = gs._con.execute(
            "SELECT path, kind, semantic_type FROM communities "
            "WHERE level=0 AND kind IN ('dir','file') AND semantic_type IS NOT NULL"
        ).fetchall()
        assert not typed, f"ST8: structural nodes with non-NULL semantic_type: {typed[:5]}"
    finally:
        gs.close()


def test_sg2_structure_imports_no_llm():
    """SG2: kb/structure.py must not import any LLM or DeepSeek module."""
    import importlib
    import re
    mod = importlib.import_module("opencode_search.kb.structure")
    src = inspect.getsource(mod)
    # Only flag actual import statements, not docstring/comment mentions.
    imports = re.findall(r"^(?:import|from)\s+\S+", src, re.MULTILINE)
    joined = " ".join(imports).lower()
    for banned in ("deepseek", "llm", "openai", "anthropic", "ollama"):
        assert banned not in joined, (
            f"SG2: structure.py imports {banned!r} — spine must be zero-token"
        )
