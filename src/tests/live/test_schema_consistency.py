"""Phase 2.0 — schema-consistency static guards (runs at collection time, no GPU needed).

SC3  community_count() is scoped to level>=1 (structural spine excluded)
SC4  No unscoped FROM-communities read: known leaks are patched; allowlist enforced
SC6  Producer↔consumer symmetry: no write-only symbols column beyond _KNOWN_DEAD allowlist
SC8  community detection is leidenalg-free and deterministic

Four guards left with tier 3, all four of them anchored on the semantic-type taxonomy that
`graph/enrich.py` owned and `kb/wiki.py` mirrored. That taxonomy does not exist any more, and
as of R2 neither does the column: `communities.semantic_type` was dropped along with
`narrated`, `kind` and `path` once the census showed no writer and no reader left. These
guards were not re-pointed at a new subject; they had none.

  SC1  every `semantic_type NOT IN (…)` literal in ask.py ∈ enrich._TYPE_ORDER. Its subject
       went first: R0b removed ask.py's semantic-type scope, so by the time enrich.py left
       the loop was already iterating zero clauses and could not fail.
  SC2  EXCLUDED_FROM_RETRIEVAL ⊆ _TYPE_ORDER — both constants lived in enrich.py.
  SC5  the taxonomy's single-source binding: enrich._TYPE_ORDER == wiki._TYPE_ORDER ==
       wiki._TYPE_LABEL.keys(). It existed because `wiki._render_index` iterated its own
       copy and silently dropped any type missing from it, so a type added to enrich alone
       would vanish from the wiki index. Two of the three sources are deleted files.
  SC7  the semantic_type three-state contract (NULL=abstained, ''=L2-default, <type>=head),
       read out of `_overview.py`'s `feature_map` SQL. `feature_map` is one of the five
       overview variants R0a deleted, so this guard has been red since that commit.
"""
from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# SC3 — community_count() is scoped to semantic communities (level>=1)
# ---------------------------------------------------------------------------

def test_sc3_community_count_excludes_structural_spine():
    """SC3: community_count() SQL must carry WHERE level>=1 (excludes level=0 spine rows)."""
    from rag_search.graph import store as store_mod
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
    # The two `suggested_questions` readers were the original leak sites and both are gone —
    # `routes_search` (whole module) and `_overview`'s variant. `_overview` stays on this list
    # because its `communities` branch is now the only community read left on a served surface,
    # and it is the one that must keep the scope.
    ("rag_search.server._overview", "communities", "level>=1"),
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


# Phase-2a cleaned all dead items (F-B/D/G/H/I); new dead items fail CI.
_KNOWN_DEAD: frozenset[str] = frozenset()


def test_sc6_no_dead_data_beyond_allowlist():
    """SC6: no write-only symbols column outside _KNOWN_DEAD — write-amplification tripwire.

    Part B applied the same tripwire to process.db, reading every `CREATE TABLE IF NOT
    EXISTS` out of kb/bpre.py and requiring a payload SELECT for each somewhere in the
    package. There is no process.db and no second store any more; graph.db's symbols table
    is the only producer↔consumer pair left, and that is Part A.
    """
    from rag_search.graph.store import GraphStore

    _r = Path(__file__).parents[2] / "rag_search"
    ss = (_r / "graph/store.py").read_text()

    # symbols columns — parse INSERT col list; read cols from list_symbols source
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


_R2_DROPPED_COLS = ("semantic_type", "narrated", "kind", "path")

_PRE_R2_SCHEMA = """
    CREATE TABLE communities (
        id INTEGER PRIMARY KEY,
        level INTEGER NOT NULL DEFAULT 1,
        title TEXT,
        summary TEXT,
        member_count INTEGER DEFAULT 0,
        semantic_type TEXT,
        narrated INTEGER DEFAULT 0,
        kind TEXT DEFAULT 'community',
        path TEXT
    );
    INSERT INTO communities (id,level,title,summary,member_count,semantic_type,narrated,kind)
    VALUES (7,1,'Auth','3 symbol(s) (function) from auth.py.',3,'security',1,'community');
"""


def test_sc9_r2_purge_migrates_a_pre_purge_db(safe_tmp_path):
    """SC9: opening a pre-R2 graph.db drops the four dead community columns and keeps the rest.

    Every graph in the fleet was created with these columns, so the purge is a migration, not a
    schema edit — and nothing else exercises that path. The keep half is not decoration: a
    migration that dropped `summary` or `member_count` would satisfy "the dead columns are gone"
    just as well, and both halves read the same reopened DB.
    """
    import sqlite3

    from rag_search.graph.store import GraphStore

    db = safe_tmp_path / "pre_r2.db"
    con = sqlite3.connect(str(db))
    con.executescript(_PRE_R2_SCHEMA)
    con.commit()
    con.close()

    gs = GraphStore(db)
    try:
        cols = {r[1] for r in gs._con.execute("PRAGMA table_info(communities)")}
        row = gs._con.execute(
            "SELECT title, summary, member_count FROM communities WHERE id=7"
        ).fetchone()
    finally:
        gs.close()

    assert not cols.intersection(_R2_DROPPED_COLS), (
        f"SC9: pre-R2 columns survived: {sorted(cols.intersection(_R2_DROPPED_COLS))}"
    )
    assert {"id", "level", "title", "summary", "member_count"} <= cols, (
        f"SC9: the migration took a live column with it — surviving columns are {sorted(cols)}"
    )
    assert row is not None and row[0] == "Auth" and row[2] == 3, (
        f"SC9: DROP COLUMN lost the row's live data — read back {row!r}"
    )


def test_sc8_no_leidenalg_in_community():
    """SC8a: community.py must not import leidenalg (k-core replaced Leiden)."""
    import rag_search.graph.community as mod
    src = inspect.getsource(mod)
    assert "leidenalg" not in src, (
        "SC8: graph.community still imports leidenalg — Phase 2.0c k-core swap not complete"
    )


def test_sc8_detect_communities_deterministic(safe_tmp_path):
    """SC8b: detect_communities is byte-identical on two runs with the same graph."""
    from rag_search.graph.community import detect_communities
    from rag_search.graph.extractor import extract_symbols, symbol_id
    from rag_search.graph.store import GraphStore

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
