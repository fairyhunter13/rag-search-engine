"""P3 graph layer: extractor, store, community detection, LLM client, enrichment."""
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_PY = """
def add(x, y):
    return x + y

def sub(x, y):
    return x - y

class Calculator:
    def mul(self, x, y):
        return x * y
"""

_TS = """
function greet(name: string): void {
    console.log(name);
}
class Server {
    start(): void {}
    stop(): void {}
}
"""


# ── extractor ────────────────────────────────────────────────────────────────

def test_extract_python_symbols():
    from opencode_search.graph.extractor import extract_symbols
    syms = extract_symbols(Path("calc.py"), _PY, "python")
    names = {s.name for s in syms}
    assert "add" in names and "Calculator" in names


def test_extract_typescript_symbols():
    from opencode_search.graph.extractor import extract_symbols
    syms = extract_symbols(Path("srv.ts"), _TS, "typescript")
    names = {s.name for s in syms}
    assert "greet" in names and "Server" in names


def test_extract_unsupported_returns_empty():
    from opencode_search.graph.extractor import extract_symbols
    assert extract_symbols(Path("doc.md"), "# Title\ntext", "markdown") == []


def test_symbol_start_end_lines():
    from opencode_search.graph.extractor import extract_symbols
    syms = extract_symbols(Path("f.py"), _PY, "python")
    add_sym = next(s for s in syms if s.name == "add")
    assert add_sym.start_line >= 1
    assert add_sym.end_line >= add_sym.start_line


# ── store ────────────────────────────────────────────────────────────────────

def test_graph_store_insert_and_query():
    from opencode_search.graph.extractor import extract_symbols, symbol_id
    from opencode_search.graph.store import GraphStore
    with tempfile.TemporaryDirectory() as tmp:
        store = GraphStore(Path(tmp) / "g.db")
        syms = extract_symbols(Path("calc.py"), _PY, "python")
        for s in syms:
            sid = symbol_id(s.file, s.name, s.start_line)
            store.upsert_symbol(sid, s.name, s.qualified_name, s.kind,
                                s.file, s.start_line, s.end_line, s.language)
        store.commit()
        assert store.symbol_count() == len(syms)
        rows = store.list_symbols()
        assert any(r["name"] == "add" for r in rows)
        store.close()


def test_graph_store_edge_insert():
    from opencode_search.graph.store import GraphStore
    with tempfile.TemporaryDirectory() as tmp:
        store = GraphStore(Path(tmp) / "g.db")
        store.upsert_symbol("aaa", "foo", "foo", "function", "f.py", 1, 3, "python")
        store.upsert_symbol("bbb", "bar", "bar", "function", "f.py", 5, 7, "python")
        store.upsert_edge("aaa", "bbb")
        store.commit()
        edges = store._con.execute("SELECT * FROM edges").fetchall()
        assert len(edges) == 1
        store.close()


# ── community detection ───────────────────────────────────────────────────────

def test_community_detection_assigns_ids():
    from opencode_search.graph.community import detect_communities
    from opencode_search.graph.extractor import extract_symbols, symbol_id
    from opencode_search.graph.store import GraphStore
    with tempfile.TemporaryDirectory() as tmp:
        store = GraphStore(Path(tmp) / "g.db")
        for fname, code, lang in [("a.py", _PY, "python"), ("b.ts", _TS, "typescript")]:
            for s in extract_symbols(Path(fname), code, lang):
                sid = symbol_id(fname, s.name, s.start_line)
                store.upsert_symbol(sid, s.name, s.qualified_name, s.kind,
                                    fname, s.start_line, s.end_line, s.language)
        store.commit()
        mapping = detect_communities(store)
        assert len(mapping) == store.symbol_count()
        assert store.community_count() >= 1
        store.close()


def test_detect_communities_idempotent(tmp_path):
    """T2/F1/HR3: detect_communities re-run must NOT wipe existing L1 community summaries."""
    from opencode_search.graph.community import detect_communities
    from opencode_search.graph.extractor import extract_symbols, symbol_id
    from opencode_search.graph.store import GraphStore

    fpath = tmp_path / "a.py"
    fpath.write_text(_PY)
    gs = GraphStore(tmp_path / "g.db")
    try:
        for s in extract_symbols(fpath, _PY, "python"):
            sid = symbol_id(str(fpath), s.name, s.start_line)
            gs.upsert_symbol(sid, s.name, s.qualified_name, s.kind,
                             str(fpath), s.start_line, s.end_line, s.language)
        gs.commit()
        detect_communities(gs)
        gs._con.execute("UPDATE communities SET summary='sentinel' WHERE level=1 AND summary IS NULL")
        gs.commit()
        detect_communities(gs)  # re-run — must not wipe 'sentinel'
        wiped = gs._con.execute(
            "SELECT COUNT(*) FROM communities WHERE level=1 AND (summary IS NULL OR summary='')"
        ).fetchone()[0]
        assert wiped == 0, (
            f"detect_communities wiped {wiped} L1 summaries on re-run "
            f"(HR3/F1 violation: summary=None fix not effective)"
        )
    finally:
        gs.close()



# ── enrichment ────────────────────────────────────────────────────────────────

@pytest.mark.slow
def test_enrich_symbols_assigns_intent():
    from opencode_search.graph.enrich import enrich_symbols
    from opencode_search.graph.extractor import extract_symbols, symbol_id
    from opencode_search.graph.store import GraphStore
    with tempfile.TemporaryDirectory() as tmp:
        store = GraphStore(Path(tmp) / "g.db")
        for s in extract_symbols(Path("calc.py"), _PY, "python"):
            sid = symbol_id(s.file, s.name, s.start_line)
            store.upsert_symbol(sid, s.name, s.qualified_name, s.kind,
                                s.file, s.start_line, s.end_line, s.language)
        store.commit()
        count = enrich_symbols(store)
        assert count > 0
        assert any(r.get("intent") for r in store.list_symbols())
        store.close()


# ── R3: cross-project edges-schema guard ─────────────────────────────────────

def test_all_project_graph_dbs_have_canonical_edges_schema():
    """Every registered project's graph.db must have caller_sid/callee_sid (not legacy from_id/to_id)."""
    import sqlite3

    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import list_projects
    for entry in list_projects():
        if not entry.enabled:
            continue
        gdb = project_graph_db(entry.path)
        if not gdb.exists():
            continue
        with sqlite3.connect(str(gdb)) as con:
            cols = {r[1] for r in con.execute("PRAGMA table_info(edges)")}
        assert "caller_sid" in cols and "callee_sid" in cols, (
            f"{entry.path}: edges schema missing caller_sid/callee_sid (found: {cols}). "
            "Run GraphStore._open() migration or re-index."
        )
        assert "from_id" not in cols, (
            f"{entry.path}: edges still has legacy 'from_id' column — migration did not run."
        )


# ── Gap 2: call-site-accurate edges source-guard ─────────────────────────────

def test_index_project_attributes_edges_to_enclosing_symbol():
    """Gap 2: _index_project resolves each call to the innermost enclosing symbol.
    Caller_sid must NOT be a file-level representative; it must be the enclosing fn.
    """
    import inspect

    from opencode_search.daemon.sweeps import _index_project
    src = inspect.getsource(_index_project)
    assert "caller_sids[0]" not in src, (
        "representative-caller shortcut detected — Gap 2 regression: "
        "_index_project must attribute each call to its innermost enclosing symbol"
    )
    assert "enclosing" in src or "innermost" in src or "start_line" in src, (
        "_index_project must search for enclosing symbol by line range"
    )
