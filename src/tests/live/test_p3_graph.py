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


# ── LLM client ───────────────────────────────────────────────────────────────

@pytest.mark.slow
def test_ollama_chat_returns_text():
    from opencode_search.graph.llm import chat
    result = chat("Reply with just the word 'pong'.")
    assert isinstance(result, str) and len(result) > 0


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
