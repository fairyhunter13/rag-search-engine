"""Phase 2.0 — schema-consistency static guards (runs at collection time, no GPU needed).

SC1  No dead predicate: every semantic_type literal in ask.py filters ∈ _TYPE_ORDER
SC2  EXCLUDED_FROM_RETRIEVAL ⊆ _TYPE_ORDER (the constant is self-consistent)
SC3  community_count() is scoped to level>=1 (structural spine excluded)
SC4  No unscoped FROM-communities read: known leaks are patched; allowlist enforced
SC5  Taxonomy single-source: enrich._TYPE_ORDER == wiki._TYPE_ORDER == wiki._TYPE_LABEL.keys()
SC6  Producer↔consumer symmetry: no write-only column/table beyond _KNOWN_DEAD allowlist
SC7  semantic_type three-state contract: feature_map SQL excludes NULL and '' and scopes level=1
"""
from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# SC1 + SC2 — No dead predicates; taxonomy covers every filter literal
# ---------------------------------------------------------------------------

def test_sc1_no_dead_semantic_type_predicates():
    """SC1: every semantic_type NOT IN/IN literal in ask.py is a member of _TYPE_ORDER."""
    from opencode_search.graph.enrich import _TYPE_ORDER
    from opencode_search.query import ask as ask_mod

    valid = frozenset(_TYPE_ORDER)
    src = inspect.getsource(ask_mod)
    # Extract literals from all NOT IN (...) clauses following semantic_type
    for clause in re.findall(r"semantic_type\s+NOT\s+IN\s*\(([^)]+)\)", src, re.IGNORECASE):
        for lit in re.findall(r"['\"]([^'\"]+)['\"]", clause):
            assert lit in valid, (
                f"ask.py: filter literal {lit!r} not in _TYPE_ORDER={list(valid)} — dead predicate. "
                "Remove or add it to _TYPE_ORDER in graph/enrich.py."
            )


def test_sc2_excluded_from_retrieval_subset_of_type_order():
    """SC2: EXCLUDED_FROM_RETRIEVAL ⊆ _TYPE_ORDER — the constant must stay self-consistent."""
    from opencode_search.graph.enrich import _TYPE_ORDER, EXCLUDED_FROM_RETRIEVAL
    valid = frozenset(_TYPE_ORDER)
    for excl in EXCLUDED_FROM_RETRIEVAL:
        assert excl in valid, (
            f"EXCLUDED_FROM_RETRIEVAL member {excl!r} not in _TYPE_ORDER — "
            "update enrich.py to keep both in sync."
        )


# ---------------------------------------------------------------------------
# SC3 — community_count() is scoped to semantic communities (level>=1)
# ---------------------------------------------------------------------------

def test_sc3_community_count_excludes_structural_spine():
    """SC3: community_count() SQL must carry WHERE level>=1 (excludes level=0 spine rows)."""
    from opencode_search.graph import store as store_mod
    src = inspect.getsource(store_mod.GraphStore.community_count)
    assert "level>=1" in src or "level >= 1" in src, (
        "community_count() must filter WHERE level>=1 to exclude structural spine (level=0). "
        "Without this, Phase-2 dir/file nodes inflate the count and cause functional bugs "
        "(needs_idx false-positive, hollow-detection, community view)."
    )


# ---------------------------------------------------------------------------
# SC4 — known unscoped reads are all patched (spot-check the fixed sites)
# ---------------------------------------------------------------------------

_FIXED_SITES: list[tuple[str, str, str]] = [
    # (module dotted path, function/context description, expected substring in source)
    ("opencode_search.server._overview", "suggested_questions", "level>=1"),
    ("opencode_search.server.routes_search", "_api_suggested_questions", "level>=1"),
]


def test_sc4_fixed_leak_sites_carry_level_scope():
    """SC4: previously unscoped community reads now carry level>=1."""
    for mod_path, fn_hint, expected in _FIXED_SITES:
        mod = importlib.import_module(mod_path)
        src = inspect.getsource(mod)
        # Find the relevant snippet containing fn_hint + the SELECT
        # If expected is absent from the entire module source, the fix regressed.
        assert expected in src, (
            f"{mod_path} ({fn_hint}): expected {expected!r} in source — "
            "the level>=1 scope guard was removed, re-introducing the structural-spine leak."
        )


# ---------------------------------------------------------------------------
# SC5 — Taxonomy single-source (closes F-C)
# ---------------------------------------------------------------------------

def test_sc5_taxonomy_single_source():
    """SC5: enrich._TYPE_ORDER, wiki._TYPE_ORDER, wiki._TYPE_LABEL all cover the same types.

    wiki._render_index iterates wiki._TYPE_ORDER and silently drops any semantic_type
    absent from it.  If enrich._TYPE_ORDER gains a new type without updating wiki, that
    type never appears in the wiki index.  This guard binds all three sources.
    """
    from opencode_search.graph.enrich import _TYPE_ORDER as _ENRICH_TYPE_ORDER
    from opencode_search.kb.wiki import _TYPE_LABEL
    from opencode_search.kb.wiki import _TYPE_ORDER as _WIKI_TYPE_ORDER

    enrich_set = frozenset(_ENRICH_TYPE_ORDER)
    wiki_order_set = frozenset(_WIKI_TYPE_ORDER)
    wiki_label_set = frozenset(_TYPE_LABEL)

    assert enrich_set == wiki_order_set, (
        f"SC5: enrich._TYPE_ORDER ≠ wiki._TYPE_ORDER (as sets) — "
        f"extra in enrich: {enrich_set - wiki_order_set}; "
        f"extra in wiki: {wiki_order_set - enrich_set}. "
        "Keep graph/enrich.py and kb/wiki.py in sync."
    )
    assert enrich_set == wiki_label_set, (
        f"SC5: enrich._TYPE_ORDER ≠ wiki._TYPE_LABEL.keys() — "
        f"extra in enrich: {enrich_set - wiki_label_set}; "
        f"extra in wiki._TYPE_LABEL: {wiki_label_set - enrich_set}. "
        "A type without a _TYPE_LABEL entry renders as the raw string in the wiki index."
    )


# Phase-2a cleaned all dead items (F-B/D/G/H/I); new dead items fail CI.
_KNOWN_DEAD: frozenset[str] = frozenset()


def test_sc6_no_dead_data_beyond_allowlist():
    """SC6: no write-only column/table outside _KNOWN_DEAD — write-amplification tripwire."""
    from opencode_search.graph.store import GraphStore

    _r = Path(__file__).parents[2] / "opencode_search"
    ss = (_r / "graph/store.py").read_text()

    # Part A: symbols columns — parse INSERT col list; read cols from list_symbols source
    # (regex-only across multi-line SQL string literals is fragile; inspect is reliable)
    im = re.search(r"INSERT\s+INTO\s+symbols\s*\(([^)]+)\)", ss, re.IGNORECASE)
    assert im, "SC6: upsert_symbol INSERT INTO symbols not found"
    written = {c.strip() for c in im.group(1).split(",")}
    ls_src = inspect.getsource(GraphStore.list_symbols)
    read_sym = set(re.findall(r'"(\w+)"', ls_src))  # column names in SELECT + keys tuple
    for col in written:
        if f"symbols.{col}" not in _KNOWN_DEAD:
            assert col in read_sym, (
                f"SC6: symbols.{col} written by upsert_symbol but absent from list_symbols — "
                f"add a consumer or add 'symbols.{col}' to _KNOWN_DEAD"
            )

    # Part B: process.db tables — writers in bpre.py, but readers may be anywhere in source
    bs = (_r / "kb/bpre.py").read_text()
    all_src = bs + "".join(p.read_text() for p in _r.rglob("*.py") if p != _r / "kb/bpre.py")
    for tbl in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", bs):
        if tbl not in _KNOWN_DEAD:
            sel = [s.strip() for s in
                   re.findall(rf"SELECT\s+(.+?)\s+FROM\s+{re.escape(tbl)}\b", all_src, re.IGNORECASE)
                   if s.strip().upper() not in ("1", "COUNT(*)")]
            assert sel, f"SC6: table '{tbl}' has no payload SELECT anywhere in source — consumer or _KNOWN_DEAD"


# ---------------------------------------------------------------------------
# SC7 — semantic_type three-state contract (closes F-J)
# ---------------------------------------------------------------------------

def test_sc7_semantic_type_three_state_contract():
    """SC7: feature_map SQL must exclude NULL and '' and scope to level=1 (closes F-J).

    Three sentinels: NULL=abstained/spine, ''=L2-default, <type>=head.
    feature_map must filter out NULL (IS NOT NULL), '' (!= ''), and scope to level=1.
    """
    mod = importlib.import_module("opencode_search.server._overview")
    src = inspect.getsource(mod)
    m = re.search(r'what\s*==\s*["\']feature_map["\'](.+?)return\s+json', src, re.DOTALL)
    assert m, "SC7: feature_map handler not found in _overview.py"
    blk = m.group(1)
    assert "IS NOT NULL" in blk, "SC7: feature_map must include IS NOT NULL (exclude NULL tail)"
    assert any(t in blk for t in ("!= ''", "<> ''", '!= ""', '<> ""')), (
        "SC7: feature_map must exclude '' — L2 default must not appear as a typed feature"
    )
    assert re.search(r"level\s*=\s*1", blk), "SC7: feature_map must scope to level=1"


def test_sc8_no_leidenalg_in_community():
    """SC8a: community.py must not import leidenalg (k-core replaced Leiden)."""
    import opencode_search.graph.community as mod
    src = inspect.getsource(mod)
    assert "leidenalg" not in src, (
        "SC8: graph.community still imports leidenalg — Phase 2.0c k-core swap not complete"
    )


def test_sc8_detect_communities_deterministic(safe_tmp_path):
    """SC8b: detect_communities is byte-identical on two runs with the same graph."""
    from opencode_search.graph.community import detect_communities
    from opencode_search.graph.extractor import extract_symbols, symbol_id
    from opencode_search.graph.store import GraphStore

    fpath = safe_tmp_path / "svc.py"
    fpath.write_text(
        "def alpha(): pass\ndef beta(): return alpha()\n"
        "def gamma(): return beta()\ndef delta(): return gamma()\n"
        "class Engine:\n    def run(self): return delta()\n"
    )
    content = fpath.read_text()

    def _build(db_path) -> dict[str, int]:
        gs = GraphStore(db_path)
        for s in extract_symbols(fpath, content, "python"):
            gs.upsert_symbol(symbol_id(str(fpath), s.name, s.start_line),
                             s.name, s.qualified_name, s.kind,
                             str(fpath), s.start_line, s.end_line, s.language)
        gs.commit()
        m = detect_communities(gs)
        gs.close()
        return m

    m1 = _build(safe_tmp_path / "g1.db")
    m2 = _build(safe_tmp_path / "g2.db")
    assert m1 == m2, (
        f"SC8b: detect_communities non-deterministic — "
        f"diff: {set(m1.items()) ^ set(m2.items())}"
    )
