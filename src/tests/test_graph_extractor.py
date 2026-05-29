"""Tests for opencode_search.graph.extractor — tree-sitter AST extraction."""
from __future__ import annotations

import time

import pytest

from opencode_search.graph.extractor import GraphExtractor, language_for_file
from opencode_search.graph.storage import NodeData


@pytest.fixture
def extractor() -> GraphExtractor:
    return GraphExtractor()


def _names(nodes: list[NodeData]) -> set[str]:
    return {n.name for n in nodes}


def _kinds(nodes: list[NodeData]) -> dict[str, str]:
    return {n.name: n.kind for n in nodes}


def _qualifieds(nodes: list[NodeData]) -> set[str]:
    return {n.qualified_name for n in nodes}


# ---------------------------------------------------------------------------
# language_for_file
# ---------------------------------------------------------------------------


def test_language_for_file_python():
    assert language_for_file("/foo/bar.py") == "python"


def test_language_for_file_typescript():
    assert language_for_file("/foo/bar.ts") == "typescript"


def test_language_for_file_tsx():
    assert language_for_file("/foo/bar.tsx") == "typescript"


def test_language_for_file_javascript():
    assert language_for_file("/foo/bar.js") == "javascript"


def test_language_for_file_jsx():
    assert language_for_file("/foo/bar.jsx") == "javascript"


def test_language_for_file_go():
    assert language_for_file("/foo/bar.go") == "go"


def test_language_for_file_java():
    assert language_for_file("/foo/bar.java") == "java"


def test_language_for_file_rust():
    assert language_for_file("/foo/bar.rs") == "rust"


def test_language_for_file_unknown():
    assert language_for_file("/foo/bar.xyz") is None


# ---------------------------------------------------------------------------
# Python extraction
# ---------------------------------------------------------------------------


def test_extract_python_top_level_function(extractor):
    src = "def hello(x: int) -> str:\n    return str(x)\n"
    nodes, _ = extractor.extract_file("/tmp/mod.py", src, "python")
    names = _names(nodes)
    assert "hello" in names
    fn_node = next(n for n in nodes if n.name == "hello")
    assert fn_node.kind == "function"
    assert fn_node.language == "python"
    assert fn_node.start_line == 1


def test_extract_python_method_in_class(extractor):
    src = "class Foo:\n    def bar(self):\n        pass\n"
    nodes, _ = extractor.extract_file("/tmp/mod.py", src, "python")
    names = _names(nodes)
    assert "Foo" in names
    assert "bar" in names
    kinds = _kinds(nodes)
    assert kinds["Foo"] == "class"
    assert kinds["bar"] == "method"


def test_extract_python_decorated_function(extractor):
    src = "@staticmethod\ndef do_work():\n    pass\n"
    nodes, _ = extractor.extract_file("/tmp/mod.py", src, "python")
    assert "do_work" in _names(nodes)


def test_extract_python_async_function(extractor):
    src = "async def fetch():\n    pass\n"
    nodes, _ = extractor.extract_file("/tmp/mod.py", src, "python")
    assert "fetch" in _names(nodes)
    fn = next(n for n in nodes if n.name == "fetch")
    assert fn.kind == "function"


def test_extract_python_docstring_on_function(extractor):
    src = 'def greet(name):\n    """Greet someone."""\n    return name\n'
    nodes, _ = extractor.extract_file("/tmp/mod.py", src, "python")
    fn = next((n for n in nodes if n.name == "greet"), None)
    assert fn is not None
    assert fn.docstring is not None
    assert "Greet" in fn.docstring


def test_extract_python_docstring_on_class(extractor):
    src = 'class Foo:\n    """A foo class."""\n    pass\n'
    nodes, _ = extractor.extract_file("/tmp/mod.py", src, "python")
    cls = next((n for n in nodes if n.name == "Foo"), None)
    assert cls is not None
    assert cls.docstring is not None
    assert "foo class" in cls.docstring


def test_extract_python_absolute_import(extractor):
    src = "import os\ndef foo(): pass\n"
    nodes, raw_edges = extractor.extract_file("/tmp/mod.py", src, "python")
    import_edges = [e for e in raw_edges if e.kind == "IMPORTS"]
    callees = {e.raw_callee for e in import_edges}
    assert "os" in callees


def test_extract_python_from_import(extractor):
    src = "from pathlib import Path\ndef foo(): pass\n"
    nodes, raw_edges = extractor.extract_file("/tmp/mod.py", src, "python")
    import_callees = {e.raw_callee for e in raw_edges if e.kind == "IMPORTS"}
    # Should record 'pathlib.Path' or 'Path'
    assert any("Path" in c for c in import_callees)


def test_extract_python_from_import_multiple(extractor):
    src = "from os.path import join, exists, dirname\ndef f(): pass\n"
    _, raw_edges = extractor.extract_file("/tmp/mod.py", src, "python")
    import_callees = {e.raw_callee for e in raw_edges if e.kind == "IMPORTS"}
    # At least 2 of the 3 imports should be recorded
    matches = sum(1 for c in import_callees if any(n in c for n in ["join", "exists", "dirname"]))
    assert matches >= 2


def test_extract_python_call_expression(extractor):
    src = "def foo():\n    bar()\n"
    _, raw_edges = extractor.extract_file("/tmp/mod.py", src, "python")
    call_callees = {e.raw_callee for e in raw_edges if e.kind == "CALLS"}
    assert "bar" in call_callees


def test_extract_python_method_call(extractor):
    src = "def foo():\n    storage.write_chunks([])\n"
    _, raw_edges = extractor.extract_file("/tmp/mod.py", src, "python")
    call_callees = {e.raw_callee for e in raw_edges if e.kind == "CALLS"}
    assert "storage.write_chunks" in call_callees


def test_extract_python_chained_call(extractor):
    src = "def foo():\n    a.b.c()\n"
    _, raw_edges = extractor.extract_file("/tmp/mod.py", src, "python")
    call_callees = {e.raw_callee for e in raw_edges if e.kind == "CALLS"}
    assert "a.b.c" in call_callees


def test_extract_python_qualified_name(extractor):
    src = "class Auth:\n    def verify(self):\n        pass\n"
    nodes, _ = extractor.extract_file("/tmp/mod.py", src, "python")
    qualifieds = _qualifieds(nodes)
    assert any("mod.Auth.verify" in q for q in qualifieds)


def test_extract_python_base_class_inherits_edge(extractor):
    src = "class Child(Parent):\n    pass\n"
    _, raw_edges = extractor.extract_file("/tmp/mod.py", src, "python")
    inherits = [e for e in raw_edges if e.kind == "INHERITS"]
    assert any(e.raw_callee == "Parent" for e in inherits)


# ---------------------------------------------------------------------------
# TypeScript / JavaScript extraction
# ---------------------------------------------------------------------------


def test_extract_ts_function_declaration(extractor):
    src = "function greet(name: string): void {\n  console.log(name);\n}\n"
    nodes, _ = extractor.extract_file("/tmp/mod.ts", src, "typescript")
    assert "greet" in _names(nodes)
    fn = next(n for n in nodes if n.name == "greet")
    assert fn.kind == "function"


def test_extract_ts_arrow_function_const(extractor):
    src = "const fetchUser = async (id: number) => {\n  return id;\n};\n"
    nodes, _ = extractor.extract_file("/tmp/mod.ts", src, "typescript")
    assert "fetchUser" in _names(nodes)


def test_extract_ts_class_method(extractor):
    src = "class Service {\n  process(data: any) {\n    return data;\n  }\n}\n"
    nodes, _ = extractor.extract_file("/tmp/mod.ts", src, "typescript")
    assert "Service" in _names(nodes)
    assert "process" in _names(nodes)


def test_extract_ts_import_named(extractor):
    src = "import { foo, bar } from './mod';\n"
    _, raw_edges = extractor.extract_file("/tmp/app.ts", src, "typescript")
    import_callees = {e.raw_callee for e in raw_edges if e.kind == "IMPORTS"}
    assert any("mod" in c for c in import_callees)


def test_extract_ts_call_expression(extractor):
    src = "function run() {\n  process();\n}\n"
    _, raw_edges = extractor.extract_file("/tmp/mod.ts", src, "typescript")
    calls = {e.raw_callee for e in raw_edges if e.kind == "CALLS"}
    assert "process" in calls


def test_extract_js_function_expression(extractor):
    src = "const handler = function(req, res) {\n  res.send('ok');\n};\n"
    nodes, _ = extractor.extract_file("/tmp/app.js", src, "javascript")
    assert "handler" in _names(nodes)


# ---------------------------------------------------------------------------
# Go extraction
# ---------------------------------------------------------------------------


def test_extract_go_top_level_function(extractor):
    src = "package main\n\nfunc Greet(name string) string {\n\treturn name\n}\n"
    nodes, _ = extractor.extract_file("/tmp/mod.go", src, "go")
    assert "Greet" in _names(nodes)
    fn = next(n for n in nodes if n.name == "Greet")
    assert fn.kind == "function"


def test_extract_go_method_with_receiver(extractor):
    src = "package main\n\ntype Storage struct{}\n\nfunc (s *Storage) Write() error {\n\treturn nil\n}\n"
    nodes, _ = extractor.extract_file("/tmp/mod.go", src, "go")
    assert "Write" in _names(nodes)
    fn = next(n for n in nodes if n.name == "Write")
    assert fn.kind == "method"


def test_extract_go_qualified_name_includes_package(extractor):
    src = "package store\n\nfunc OpenDB() error {\n\treturn nil\n}\n"
    nodes, _ = extractor.extract_file("/tmp/mod.go", src, "go")
    qualifieds = _qualifieds(nodes)
    assert any("store" in q and "OpenDB" in q for q in qualifieds)


def test_extract_go_call_expression(extractor):
    src = "package main\n\nfunc Run() {\n\thelper()\n}\n"
    _, raw_edges = extractor.extract_file("/tmp/mod.go", src, "go")
    calls = {e.raw_callee for e in raw_edges if e.kind == "CALLS"}
    assert "helper" in calls


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_extractor_handles_empty_file_gracefully(extractor):
    nodes, raw_edges = extractor.extract_file("/tmp/empty.py", "", "python")
    # Should return at least the file node, no crash
    assert isinstance(nodes, list)
    assert isinstance(raw_edges, list)


def test_extractor_handles_syntax_error_gracefully(extractor):
    # Malformed Python code — should not raise
    src = "def broken(\n    pass\n\nclass {invalid}:\n    ...\n"
    try:
        nodes, raw_edges = extractor.extract_file("/tmp/broken.py", src, "python")
        assert isinstance(nodes, list)
    except Exception as exc:
        pytest.fail(f"Extractor raised exception on malformed code: {exc}")


def test_extractor_unsupported_language_emits_file_node_only(extractor):
    src = "some_random_content = 123\n"
    nodes, raw_edges = extractor.extract_file("/tmp/file.lua", src, None)
    assert len(nodes) >= 1
    assert nodes[0].kind == "file"
    assert raw_edges == []


def test_extractor_large_file_200kb_completes_under_5s(extractor):
    src = "def func_{i}(x):\n    return x * {i}\n\n" * 5000
    src = "\n".join(
        f"def func_{i}(x):\n    return x * {i}\n" for i in range(5000)
    )
    t0 = time.perf_counter()
    nodes, _ = extractor.extract_file("/tmp/large.py", src, "python")
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0, f"Large file extraction took {elapsed:.1f}s"
    assert len(nodes) > 100


def test_extract_file_always_returns_file_node(extractor):
    """Every file extraction produces at least one node with kind='file'."""
    src = "x = 1\n"
    nodes, _ = extractor.extract_file("/tmp/simple.py", src, "python")
    file_nodes = [n for n in nodes if n.kind == "file"]
    assert len(file_nodes) == 1
    assert file_nodes[0].file == "/tmp/simple.py"
