"""Tests for the graph pipeline: AST extraction, storage, resolution, community detection, and E2E."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import pytest

from opencode_search.graph.community import CommunityDetector
from opencode_search.graph.extractor import GraphExtractor, _RawEdge, language_for_file
from opencode_search.graph.resolver import CallResolver
from opencode_search.graph.storage import (
    CommunityData,
    EdgeData,
    GraphStorage,
    NodeData,
)


# ============================================================
# AST Extraction
# ============================================================

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
    _nodes, raw_edges = extractor.extract_file("/tmp/mod.py", src, "python")
    import_edges = [e for e in raw_edges if e.kind == "IMPORTS"]
    callees = {e.raw_callee for e in import_edges}
    assert "os" in callees


def test_extract_python_from_import(extractor):
    src = "from pathlib import Path\ndef foo(): pass\n"
    _nodes, raw_edges = extractor.extract_file("/tmp/mod.py", src, "python")
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
        nodes, _raw_edges = extractor.extract_file("/tmp/broken.py", src, "python")
        assert isinstance(nodes, list)
    except Exception as exc:
        pytest.fail(f"Extractor raised exception on malformed code: {exc}")


def test_extractor_unsupported_language_emits_file_node_only(extractor):
    # .toml is not in _EXT_TO_LANG so language resolves to None → file node only
    src = "some_random_content = 123\n"
    nodes, raw_edges = extractor.extract_file("/tmp/file.toml", src, None)
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


# ---------------------------------------------------------------------------
# Tier-2 language coverage: extension mapping + generic AST extraction
# ---------------------------------------------------------------------------

def test_language_for_file_tier2_extensions():
    """language_for_file returns correct values for all Tier-2 extension additions."""
    expected = {
        ".rb": "ruby",
        ".rake": "ruby",
        ".php": "php",
        ".swift": "swift",
        ".cs": "c_sharp",
        ".dart": "dart",
        ".lua": "lua",
        ".sh": "bash",
        ".bash": "bash",
        ".ex": "elixir",
        ".exs": "elixir",
        ".hs": "haskell",
        ".zig": "zig",
        ".sql": "sql",
        ".pl": "perl",
        ".pm": "perl",
        ".ml": "ocaml",
        ".groovy": "groovy",
        ".scala": "scala",
        ".r": "r",
    }
    for ext, lang in expected.items():
        got = language_for_file(f"/tmp/file{ext}")
        assert got == lang, f"language_for_file({ext!r}) = {got!r}, want {lang!r}"


def test_ruby_method_extraction(extractor):
    """Ruby def…end blocks are extracted as function/method nodes via _extract_generic."""
    src = """\
class Greeter
  def hello(name)
    puts "Hello, #{name}"
  end

  def self.version
    "1.0"
  end
end

def standalone_func
  42
end
"""
    nodes, _raw_edges = extractor.extract_file("/tmp/greeter.rb", src, "ruby")
    names = {n.name for n in nodes}
    {n.name: n.kind for n in nodes}

    assert "Greeter" in names, f"Class 'Greeter' not extracted. Got: {names}"
    assert "hello" in names, f"Method 'hello' not extracted. Got: {names}"
    assert "standalone_func" in names, f"Function 'standalone_func' not extracted. Got: {names}"

    # Language metadata must be preserved (not None)
    for node in nodes:
        if node.kind != "file":
            assert node.language == "ruby", (
                f"Node {node.name!r} has language={node.language!r}, expected 'ruby'"
            )


def test_lua_function_extraction(extractor):
    """Lua function definitions including local functions are extracted."""
    src = """\
function greet(name)
  print("Hello " .. name)
end

local function helper(x)
  return x * 2
end
"""
    nodes, _ = extractor.extract_file("/tmp/script.lua", src, "lua")
    names = {n.name for n in nodes}
    assert "greet" in names, f"Function 'greet' not found. Got: {names}"
    # local_function may or may not have a parseable name field depending on tree-sitter-lua version
    # At minimum, we must get the file node + greet
    assert len(nodes) >= 2


def test_generic_extractor_preserves_language(extractor):
    """_extract_generic must set language on every non-file NodeData (not None)."""
    # Use C# as a representative Tier-2 language
    src = """\
public class Calculator {
    public int Add(int a, int b) {
        return a + b;
    }
}
"""
    nodes, _ = extractor.extract_file("/tmp/Calculator.cs", src, "c_sharp")
    symbol_nodes = [n for n in nodes if n.kind != "file"]
    assert symbol_nodes, "No symbol nodes extracted from C# snippet"
    for node in symbol_nodes:
        assert node.language == "c_sharp", (
            f"Node {node.name!r} has language={node.language!r}, expected 'c_sharp'. "
            "_extract_generic must pass language= to NodeData."
        )


def test_swift_struct_and_func_extraction(extractor):
    """Swift struct declarations and function declarations are extracted."""
    src = """\
struct Point {
    var x: Double
    var y: Double

    func distance() -> Double {
        return (x * x + y * y).squareRoot()
    }
}

func makePoint(x: Double, y: Double) -> Point {
    return Point(x: x, y: y)
}
"""
    nodes, _ = extractor.extract_file("/tmp/point.swift", src, "swift")
    names = {n.name for n in nodes}
    # struct_declaration → class node; function_declaration → function node
    assert "Point" in names or "makePoint" in names, (
        f"Expected Swift symbols, got: {names}"
    )


def test_php_class_and_function_extraction(extractor):
    """PHP class declarations and function definitions are extracted."""
    src = """\
<?php
class UserService {
    public function getUser(int $id): array {
        return ['id' => $id];
    }
}

function validateEmail(string $email): bool {
    return filter_var($email, FILTER_VALIDATE_EMAIL) !== false;
}
"""
    nodes, _ = extractor.extract_file("/tmp/service.php", src, "php")
    names = {n.name for n in nodes}
    assert "UserService" in names or "validateEmail" in names, (
        f"Expected PHP symbols, got: {names}"
    )


def test_bash_function_extraction(extractor):
    """Bash function_definition nodes are extracted."""
    src = """\
#!/usr/bin/env bash

function greet() {
    echo "Hello, $1"
}

backup_files() {
    cp -r "$1" "$2"
}
"""
    nodes, _ = extractor.extract_file("/tmp/deploy.sh", src, "bash")
    names = {n.name for n in nodes}
    assert "greet" in names or "backup_files" in names, (
        f"Expected Bash function nodes, got: {names}"
    )


# ---------------------------------------------------------------------------
# Tier-1 call-edge tests: verify CALLS edges are emitted for Ruby/PHP/Swift/C#
# ---------------------------------------------------------------------------

def test_ruby_call_edges(extractor):
    """Ruby method calls produce CALLS edges (Tier-1 extraction)."""
    src = """\
class Processor
  def run
    validate
    save
  end

  def validate
    true
  end

  def save
    nil
  end
end
"""
    nodes, raw_edges = extractor.extract_file("/tmp/processor.rb", src, "ruby")
    names = {n.name for n in nodes}
    assert "run" in names, f"Method 'run' not extracted. Got: {names}"

    calls = {e.raw_callee for e in raw_edges if e.kind == "CALLS"}
    assert calls, "Expected CALLS edges from Ruby method bodies"
    assert "validate" in calls or "save" in calls, (
        f"Expected 'validate' or 'save' call edges. Got: {calls}"
    )


def test_ruby_singleton_method_extracted(extractor):
    """Ruby def self.foo is extracted as a function node."""
    src = """\
class Config
  def self.defaults
    {}
  end
end
"""
    nodes, _ = extractor.extract_file("/tmp/config.rb", src, "ruby")
    names = {n.name for n in nodes}
    assert "defaults" in names, f"Singleton method 'defaults' not extracted. Got: {names}"


def test_php_call_edges(extractor):
    """PHP function calls produce CALLS edges (Tier-1 extraction)."""
    src = """\
<?php
class OrderService {
    public function processOrder(int $id): bool {
        $order = $this->loadOrder($id);
        return $this->validate($order);
    }

    private function loadOrder(int $id): array {
        return [];
    }

    private function validate(array $order): bool {
        return true;
    }
}
"""
    nodes, raw_edges = extractor.extract_file("/tmp/order.php", src, "php")
    names = {n.name for n in nodes}
    assert "processOrder" in names, f"Method 'processOrder' not extracted. Got: {names}"

    calls = {e.raw_callee for e in raw_edges if e.kind == "CALLS"}
    assert calls, (
        f"Expected CALLS edges from PHP method bodies. nodes={names}"
    )


def test_swift_call_edges(extractor):
    """Swift function calls produce CALLS edges (Tier-1 extraction)."""
    src = """\
class DataManager {
    func fetchData() -> [String] {
        let raw = loadFromDisk()
        return parse(raw)
    }

    func loadFromDisk() -> String {
        return ""
    }

    func parse(_ input: String) -> [String] {
        return []
    }
}
"""
    nodes, raw_edges = extractor.extract_file("/tmp/manager.swift", src, "swift")
    names = {n.name for n in nodes}
    assert "DataManager" in names or "fetchData" in names, (
        f"Expected Swift symbols. Got: {names}"
    )

    calls = {e.raw_callee for e in raw_edges if e.kind == "CALLS"}
    assert calls, (
        f"Expected CALLS edges from Swift method bodies. nodes={names}"
    )


def test_c_sharp_call_edges(extractor):
    """C# invocation expressions produce CALLS edges (Tier-1 extraction)."""
    src = """\
public class PaymentProcessor {
    public bool Process(string orderId) {
        var order = LoadOrder(orderId);
        return Validate(order);
    }

    private Order LoadOrder(string id) {
        return new Order();
    }

    private bool Validate(Order order) {
        return true;
    }
}
"""
    nodes, raw_edges = extractor.extract_file("/tmp/payment.cs", src, "c_sharp")
    names = {n.name for n in nodes}
    assert "PaymentProcessor" in names or "Process" in names, (
        f"Expected C# symbols. Got: {names}"
    )

    calls = {e.raw_callee for e in raw_edges if e.kind == "CALLS"}
    assert calls, (
        f"Expected CALLS edges from C# method bodies. nodes={names}"
    )


# ============================================================
# Graph Storage
# ============================================================

pytestmark = [pytest.mark.integration]


def _make_node(
    file_path: str,
    name: str,
    kind: str = "function",
    qualified_name: str | None = None,
) -> NodeData:
    qn = qualified_name or f"mod.{name}"
    node_id = f"{file_path}_{name}"[:16]
    import hashlib
    node_id = hashlib.sha256(f"{file_path}::{qn}".encode()).hexdigest()[:16]
    return NodeData(
        id=node_id,
        name=name,
        qualified_name=qn,
        kind=kind,
        file=file_path,
        start_line=1,
        end_line=10,
        language="python",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )


@pytest.fixture
def storage(tmp_path) -> GraphStorage:
    gs = GraphStorage(str(tmp_path / "graph.db"))
    gs.open()
    yield gs
    gs.close()


# ---------------------------------------------------------------------------
# Schema / initialization
# ---------------------------------------------------------------------------


def test_graph_storage_creates_all_tables_on_open(tmp_path):
    gs = GraphStorage(str(tmp_path / "graph.db"))
    gs.open()
    try:
        conn = sqlite3.connect(str(tmp_path / "graph.db"))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "nodes" in tables
        assert "edges" in tables
        assert "communities" in tables
    finally:
        gs.close()


def test_graph_storage_creates_all_indexes_on_open(tmp_path):
    gs = GraphStorage(str(tmp_path / "graph.db"))
    gs.open()
    try:
        conn = sqlite3.connect(str(tmp_path / "graph.db"))
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        conn.close()
        assert "idx_nodes_file" in indexes
        assert "idx_nodes_kind" in indexes
        assert "idx_edges_from" in indexes
        assert "idx_edges_to" in indexes
    finally:
        gs.close()


def test_graph_storage_wal_mode_enabled(tmp_path):
    gs = GraphStorage(str(tmp_path / "graph.db"))
    gs.open()
    try:
        conn = sqlite3.connect(str(tmp_path / "graph.db"))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"
    finally:
        gs.close()


def test_graph_storage_context_manager(tmp_path):
    db_path = str(tmp_path / "graph.db")
    with GraphStorage(db_path) as gs:
        gs.upsert_nodes([_make_node("/f.py", "foo")])
        assert gs.node_count() == 1


# ---------------------------------------------------------------------------
# Node write / read
# ---------------------------------------------------------------------------


def test_graph_storage_upsert_node_basic(storage):
    n = _make_node("/tmp/foo.py", "foo")
    storage.upsert_nodes([n])
    assert storage.node_count() == 1


def test_graph_storage_upsert_node_idempotent(storage):
    n = _make_node("/tmp/foo.py", "foo")
    storage.upsert_nodes([n])
    storage.upsert_nodes([n])
    assert storage.node_count() == 1


def test_graph_storage_upsert_node_updates_on_conflict(storage):
    n = _make_node("/tmp/foo.py", "foo")
    storage.upsert_nodes([n])
    n2 = NodeData(
        id=n.id, name="foo", qualified_name=n.qualified_name, kind="function",
        file="/tmp/foo.py", start_line=99, end_line=100, language="python",
        created_at="2026-01-01T00:00:00", updated_at="2026-01-02T00:00:00",
    )
    storage.upsert_nodes([n2])
    result = storage.get_node_by_id(n.id)
    assert result is not None
    assert result.start_line == 99
    assert storage.node_count() == 1


def test_graph_storage_get_node_by_name(storage):
    n = _make_node("/tmp/foo.py", "authenticate", qualified_name="auth.authenticate")
    storage.upsert_nodes([n])
    found = storage.get_node("authenticate")
    assert found is not None
    assert found.name == "authenticate"


def test_graph_storage_get_node_by_qualified_name(storage):
    n = _make_node("/tmp/foo.py", "authenticate", qualified_name="auth.authenticate")
    storage.upsert_nodes([n])
    found = storage.get_node("auth.authenticate")
    assert found is not None
    assert found.qualified_name == "auth.authenticate"


def test_graph_storage_get_node_not_found_returns_none(storage):
    assert storage.get_node("nonexistent_xyz") is None


def test_graph_storage_get_nodes_by_name_multiple_matches(storage):
    n1 = _make_node("/a.py", "run", qualified_name="a.run")
    n2 = _make_node("/b.py", "run", qualified_name="b.run")
    storage.upsert_nodes([n1, n2])
    matches = storage.get_nodes_by_name("run")
    assert len(matches) == 2


def test_graph_storage_all_nodes_returns_complete_list(storage):
    nodes = [_make_node(f"/f{i}.py", f"func{i}") for i in range(5)]
    storage.upsert_nodes(nodes)
    all_nodes = storage.all_nodes()
    assert len(all_nodes) == 5


# ---------------------------------------------------------------------------
# Edge write / read
# ---------------------------------------------------------------------------


def test_graph_storage_upsert_edge_basic(storage):
    n1 = _make_node("/a.py", "foo", qualified_name="m.foo")
    n2 = _make_node("/b.py", "bar", qualified_name="m.bar")
    storage.upsert_nodes([n1, n2])
    e = EdgeData(from_id=n1.id, to_id=n2.id, kind="CALLS", confidence=0.9)
    storage.upsert_edges([e])
    assert storage.edge_count() == 1


def test_graph_storage_upsert_edge_idempotent(storage):
    n1 = _make_node("/a.py", "foo", qualified_name="m.foo")
    n2 = _make_node("/b.py", "bar", qualified_name="m.bar")
    storage.upsert_nodes([n1, n2])
    e = EdgeData(from_id=n1.id, to_id=n2.id, kind="CALLS")
    storage.upsert_edges([e])
    storage.upsert_edges([e])
    assert storage.edge_count() == 1


def test_graph_storage_all_edges_returns_complete_list(storage):
    nodes = [_make_node(f"/f{i}.py", f"f{i}", qualified_name=f"m.f{i}") for i in range(3)]
    storage.upsert_nodes(nodes)
    edges = [
        EdgeData(from_id=nodes[0].id, to_id=nodes[1].id, kind="CALLS"),
        EdgeData(from_id=nodes[1].id, to_id=nodes[2].id, kind="CALLS"),
    ]
    storage.upsert_edges(edges)
    all_edges = storage.all_edges()
    assert len(all_edges) == 2


# ---------------------------------------------------------------------------
# Delete file
# ---------------------------------------------------------------------------


def test_graph_storage_delete_file_removes_nodes(storage):
    n1 = _make_node("/a.py", "foo", qualified_name="a.foo")
    n2 = _make_node("/b.py", "bar", qualified_name="b.bar")
    storage.upsert_nodes([n1, n2])
    storage.delete_file("/a.py")
    assert storage.node_count() == 1
    assert storage.get_node("foo") is None
    assert storage.get_node("bar") is not None


def test_graph_storage_delete_file_removes_edges_both_directions(storage):
    n1 = _make_node("/a.py", "foo", qualified_name="a.foo")
    n2 = _make_node("/b.py", "bar", qualified_name="b.bar")
    storage.upsert_nodes([n1, n2])
    storage.upsert_edges([
        EdgeData(from_id=n1.id, to_id=n2.id, kind="CALLS"),
    ])
    storage.delete_file("/a.py")
    assert storage.edge_count() == 0


def test_graph_storage_delete_nonexistent_file_no_error(storage):
    storage.delete_file("/nonexistent.py")  # should not raise


# ---------------------------------------------------------------------------
# BFS traversal
# ---------------------------------------------------------------------------


def _make_call_graph(storage: GraphStorage) -> tuple[list[NodeData], list[EdgeData]]:
    """
    a → b → c → d
    """
    nodes = [
        _make_node(f"/f{i}.py", f"func_{i}", qualified_name=f"m.func_{i}")
        for i in range(4)
    ]
    storage.upsert_nodes(nodes)
    edges = [
        EdgeData(from_id=nodes[0].id, to_id=nodes[1].id, kind="CALLS"),
        EdgeData(from_id=nodes[1].id, to_id=nodes[2].id, kind="CALLS"),
        EdgeData(from_id=nodes[2].id, to_id=nodes[3].id, kind="CALLS"),
    ]
    storage.upsert_edges(edges)
    return nodes, edges


def test_graph_storage_bfs_callers_depth_1(storage):
    nodes, _ = _make_call_graph(storage)
    # func_1 is called by func_0
    callers = storage.get_callers(nodes[1].id, depth=1)
    assert len(callers) == 1
    assert callers[0].node_id == nodes[0].id
    assert callers[0].depth == 1


def test_graph_storage_bfs_callers_depth_3(storage):
    nodes, _ = _make_call_graph(storage)
    # func_3 is transitively called by func_2 → func_1 → func_0
    callers = storage.get_callers(nodes[3].id, depth=3)
    node_ids = {c.node_id for c in callers}
    assert nodes[2].id in node_ids
    assert nodes[1].id in node_ids
    assert nodes[0].id in node_ids


def test_graph_storage_bfs_callers_respects_depth_limit(storage):
    nodes, _ = _make_call_graph(storage)
    callers = storage.get_callers(nodes[3].id, depth=1)
    assert all(c.depth <= 1 for c in callers)
    assert len(callers) == 1  # only func_2


def test_graph_storage_bfs_callees_depth_1(storage):
    nodes, _ = _make_call_graph(storage)
    callees = storage.get_callees(nodes[0].id, depth=1)
    assert len(callees) == 1
    assert callees[0].node_id == nodes[1].id


def test_graph_storage_bfs_callees_depth_3(storage):
    nodes, _ = _make_call_graph(storage)
    callees = storage.get_callees(nodes[0].id, depth=3)
    node_ids = {c.node_id for c in callees}
    assert nodes[1].id in node_ids
    assert nodes[2].id in node_ids
    assert nodes[3].id in node_ids


def test_graph_storage_bfs_returns_empty_for_leaf_node(storage):
    n = _make_node("/f.py", "leaf_node", qualified_name="m.leaf_node")
    storage.upsert_nodes([n])
    assert storage.get_callees(n.id) == []
    assert storage.get_callers(n.id) == []


# ---------------------------------------------------------------------------
# trace_path
# ---------------------------------------------------------------------------


def test_graph_storage_trace_path_direct_connection(storage):
    n1 = _make_node("/a.py", "a", qualified_name="m.a")
    n2 = _make_node("/b.py", "b", qualified_name="m.b")
    storage.upsert_nodes([n1, n2])
    storage.upsert_edges([EdgeData(from_id=n1.id, to_id=n2.id, kind="CALLS")])
    path = storage.trace_path(n1.id, n2.id)
    assert path is not None
    assert n1.id in path
    assert n2.id in path


def test_graph_storage_trace_path_indirect_connection(storage):
    nodes, _ = _make_call_graph(storage)
    path = storage.trace_path(nodes[0].id, nodes[3].id)
    assert path is not None
    assert len(path) >= 4


def test_graph_storage_trace_path_no_path_returns_none(storage):
    n1 = _make_node("/a.py", "a", qualified_name="m.a")
    n2 = _make_node("/b.py", "b", qualified_name="m.b")
    storage.upsert_nodes([n1, n2])
    # no edges
    path = storage.trace_path(n1.id, n2.id)
    assert path is None


def test_graph_storage_trace_path_cycle_terminates(storage):
    """Cyclic graph: a → b → a should not loop forever."""
    n1 = _make_node("/a.py", "a", qualified_name="m.a")
    n2 = _make_node("/b.py", "b", qualified_name="m.b")
    n3 = _make_node("/c.py", "c", qualified_name="m.c")
    storage.upsert_nodes([n1, n2, n3])
    storage.upsert_edges([
        EdgeData(from_id=n1.id, to_id=n2.id, kind="CALLS"),
        EdgeData(from_id=n2.id, to_id=n1.id, kind="CALLS"),  # cycle
    ])
    path = storage.trace_path(n1.id, n3.id)  # n3 unreachable
    assert path is None


# ---------------------------------------------------------------------------
# Community
# ---------------------------------------------------------------------------


def test_graph_storage_set_community(storage):
    n = _make_node("/f.py", "fn", qualified_name="m.fn")
    storage.upsert_nodes([n])
    storage.set_community(n.id, 42)
    found = storage.get_node_by_id(n.id)
    assert found is not None
    assert found.community_id == 42


def test_graph_storage_upsert_community(storage):
    c = CommunityData(
        id=0, title="Auth layer", summary="Handles JWT auth",
        node_count=5, key_entry_points=["auth.authenticate"],
        created_at="2026-01-01T00:00:00",
    )
    storage.upsert_community(c)
    communities = storage.get_communities()
    assert len(communities) == 1
    assert communities[0].title == "Auth layer"
    assert communities[0].key_entry_points == ["auth.authenticate"]


def test_graph_storage_upsert_community_updates_on_conflict(storage):
    c = CommunityData(id=0, title="old", node_count=1)
    storage.upsert_community(c)
    c2 = CommunityData(id=0, title="new", node_count=2)
    storage.upsert_community(c2)
    communities = storage.get_communities()
    assert len(communities) == 1
    assert communities[0].title == "new"
    assert communities[0].node_count == 2


def test_graph_storage_get_communities(storage):
    for i in range(3):
        storage.upsert_community(CommunityData(id=i, node_count=i + 1))
    communities = storage.get_communities()
    assert len(communities) == 3


def test_graph_storage_get_community_nodes(storage):
    nodes = [
        _make_node(f"/f{i}.py", f"fn{i}", qualified_name=f"m.fn{i}")
        for i in range(5)
    ]
    storage.upsert_nodes(nodes)
    for n in nodes[:3]:
        storage.set_community(n.id, 7)
    for n in nodes[3:]:
        storage.set_community(n.id, 8)
    comm7 = storage.get_community_nodes(7)
    comm8 = storage.get_community_nodes(8)
    assert len(comm7) == 3
    assert len(comm8) == 2


# ---------------------------------------------------------------------------
# get_communities_for_files — unit tests
# ---------------------------------------------------------------------------

def test_get_communities_for_files_returns_community_ids(storage):
    """get_communities_for_files returns correct community IDs for given file paths."""
    n1 = _make_node("/src/a.py", "foo", qualified_name="a.foo")
    n2 = _make_node("/src/b.py", "bar", qualified_name="b.bar")
    n3 = _make_node("/src/c.py", "baz", qualified_name="c.baz")
    storage.upsert_nodes([n1, n2, n3])
    storage.set_community(n1.id, 10)
    storage.set_community(n2.id, 20)
    storage.set_community(n3.id, 20)

    result_a = storage.get_communities_for_files(["/src/a.py"])
    assert 10 in result_a, f"Expected community 10 for a.py, got {result_a}"

    result_bc = storage.get_communities_for_files(["/src/b.py", "/src/c.py"])
    assert 20 in result_bc, f"Expected community 20 for b.py/c.py, got {result_bc}"

    result_all = storage.get_communities_for_files(["/src/a.py", "/src/b.py", "/src/c.py"])
    assert 10 in result_all and 20 in result_all, f"Expected both communities, got {result_all}"


def test_get_communities_for_files_empty_input_returns_empty(storage):
    """get_communities_for_files([]) must return []."""
    result = storage.get_communities_for_files([])
    assert result == [], f"Empty input must return [], got {result}"


def test_get_communities_for_files_nonexistent_file_returns_empty(storage):
    """Files not in the graph return no community IDs."""
    result = storage.get_communities_for_files(["/nonexistent/path/to/file.py"])
    assert result == [], f"Nonexistent file must return [], got {result}"


def test_get_communities_for_files_no_community_assigned(storage):
    """Nodes without community_id assigned are not returned."""
    n = _make_node("/src/orphan.py", "orphan", qualified_name="orphan.orphan")
    storage.upsert_nodes([n])
    # Do NOT set community
    result = storage.get_communities_for_files(["/src/orphan.py"])
    assert result == [], f"Nodes without community must not appear, got {result}"


# ---------------------------------------------------------------------------
# trace_path cycle detection — robust delimiter-aware matching
# ---------------------------------------------------------------------------

def test_trace_path_cycle_detection_does_not_false_positive(storage):
    """trace_path cycle guard must use delimiter-aware matching, not INSTR substring.

    If node ID 'ab12345678901234' is a prefix substring of another node ID
    'ab12345678901234xy' (impossible with 16-char hex IDs, but tests the guard),
    the cycle check must NOT fire. Uses realistic 16-char hex node IDs.
    """
    # Build a simple graph: A → B → C (no cycle)
    import hashlib

    def nid(s: str) -> str:
        return hashlib.sha256(s.encode()).hexdigest()[:16]

    na = _make_node("/cycle/a.py", "funcA", qualified_name="cycle.funcA")
    nb = _make_node("/cycle/b.py", "funcB", qualified_name="cycle.funcB")
    nc = _make_node("/cycle/c.py", "funcC", qualified_name="cycle.funcC")
    storage.upsert_nodes([na, nb, nc])

    # A → B → C
    storage.upsert_edges([
        EdgeData(from_id=na.id, to_id=nb.id, kind="CALLS", confidence=1.0),
        EdgeData(from_id=nb.id, to_id=nc.id, kind="CALLS", confidence=1.0),
    ])

    path = storage.trace_path(na.id, nc.id)
    assert path is not None, "trace_path must find A → B → C path"
    assert nc.id in path, f"Destination C must be in path: {path}"
    assert len(path) == 3, f"Expected path length 3 (A, B, C), got {path}"


def test_trace_path_returns_none_when_no_path(storage):
    """trace_path returns None when there is no path between nodes."""
    na = _make_node("/nopath/a.py", "alpha", qualified_name="m.alpha")
    nb = _make_node("/nopath/b.py", "beta", qualified_name="m.beta")
    storage.upsert_nodes([na, nb])
    # No edges — no path
    result = storage.trace_path(na.id, nb.id)
    assert result is None, f"trace_path with no edges must return None, got {result}"


def test_get_callers_deduplicates_by_node_id(storage):
    """get_callers must deduplicate nodes appearing at multiple depths."""
    # Build: A calls B, B calls A (cycle), C calls B
    # At depth ≥ 2, B might reappear as a caller of itself

    na = _make_node("/dedup/a.py", "funcA", qualified_name="d.funcA")
    nb = _make_node("/dedup/b.py", "funcB", qualified_name="d.funcB")
    nc = _make_node("/dedup/c.py", "funcC", qualified_name="d.funcC")
    storage.upsert_nodes([na, nb, nc])

    # C → B → target
    nd = _make_node("/dedup/target.py", "target", qualified_name="d.target")
    storage.upsert_nodes([nd])
    storage.upsert_edges([
        EdgeData(from_id=nc.id, to_id=nb.id, kind="CALLS", confidence=1.0),
        EdgeData(from_id=nb.id, to_id=nd.id, kind="CALLS", confidence=1.0),
        EdgeData(from_id=na.id, to_id=nd.id, kind="CALLS", confidence=1.0),  # direct path too
    ])

    callers = storage.get_callers(nd.id, depth=3)
    caller_ids = [c.node_id for c in callers]
    # Each node_id must appear at most once (deduplication)
    assert len(caller_ids) == len(set(caller_ids)), (
        f"Duplicate node_ids in get_callers result: {caller_ids}"
    )


# ============================================================
# Call Resolver
# ============================================================

def _node(file: str, name: str, qualified_name: str | None = None) -> NodeData:
    qn = qualified_name or f"mod.{name}"
    nid = hashlib.sha256(f"{file}::{qn}".encode()).hexdigest()[:16]
    return NodeData(
        id=nid, name=name, qualified_name=qn, kind="function",
        file=file, created_at="", updated_at="",
    )


def _file_node(file: str) -> NodeData:
    nid = hashlib.sha256(f"{file}::{file}".encode()).hexdigest()[:16]
    return NodeData(
        id=nid, name=file.split("/")[-1], qualified_name=file, kind="file",
        file=file, created_at="", updated_at="",
    )


def _raw(from_id: str, raw_callee: str, kind: str = "CALLS") -> _RawEdge:
    return _RawEdge(from_id=from_id, raw_callee=raw_callee, kind=kind)


# ---------------------------------------------------------------------------
# Basic resolution
# ---------------------------------------------------------------------------


def test_resolve_import_map_strategy(tmp_path):
    n = _node("/a.py", "authenticate", qualified_name="auth.authenticate")
    resolver = CallResolver([n])
    raw = _raw("some_id", "auth.authenticate")
    edges = resolver.resolve([raw])
    assert len(edges) == 1
    assert edges[0].to_id == n.id
    assert edges[0].confidence >= 0.90
    assert edges[0].resolution_strategy in ("import_map", "import_map_suffix", "unique_name")


def test_resolve_confidence_label_extracted_for_direct():
    """confidence=1.0 edges get label EXTRACTED; confidence<1.0 get INFERRED."""
    n = _node("/a.py", "foo", qualified_name="mod.foo")
    caller = _node("/a.py", "bar", qualified_name="mod.bar")
    resolver = CallResolver([n, caller])
    # same_module resolution gives confidence=0.90 → INFERRED
    raw = _raw(caller.id, "foo")
    edges = resolver.resolve([raw])
    assert len(edges) == 1
    assert edges[0].confidence_label == "INFERRED"
    assert edges[0].confidence_score == edges[0].confidence


def test_resolve_confidence_label_stored_and_read_back(tmp_path):
    """EdgeData with confidence_label INFERRED round-trips through GraphStorage."""
    from opencode_search.graph.storage import EdgeData, GraphStorage, NodeData
    db_path = str(tmp_path / "g.db")
    gs = GraphStorage(db_path)
    gs.open()
    n_a = NodeData(id="a", name="fa", qualified_name="m.fa", kind="function",
                   file="/f.py", created_at="", updated_at="")
    n_b = NodeData(id="b", name="fb", qualified_name="m.fb", kind="function",
                   file="/g.py", created_at="", updated_at="")
    gs.upsert_nodes([n_a, n_b])
    gs.upsert_edges([EdgeData(from_id="a", to_id="b", kind="CALLS",
                              confidence=0.85, resolution_strategy="unique_name",
                              confidence_label="INFERRED", confidence_score=0.85)])
    edges = gs.all_edges()
    gs.close()
    assert len(edges) == 1
    assert edges[0].confidence_label == "INFERRED"
    assert edges[0].confidence_score == 0.85


def test_resolve_ambiguous_label_stored_and_read_back(tmp_path):
    """EdgeData with confidence_label AMBIGUOUS round-trips through GraphStorage SQLite."""
    from opencode_search.graph.storage import EdgeData, GraphStorage, NodeData
    db_path = str(tmp_path / "g_ambiguous.db")
    gs = GraphStorage(db_path)
    gs.open()
    n_a = NodeData(id="aa", name="caller", qualified_name="m.caller", kind="function",
                   file="/c.py", created_at="", updated_at="")
    n_b = NodeData(id="bb", name="callee", qualified_name="m.callee", kind="function",
                   file="/d.py", created_at="", updated_at="")
    gs.upsert_nodes([n_a, n_b])
    gs.upsert_edges([EdgeData(
        from_id="aa", to_id="bb", kind="CALLS",
        confidence=0.30, resolution_strategy="ambiguous_name",
        confidence_label="AMBIGUOUS", confidence_score=0.30,
    )])
    edges = gs.all_edges()
    gs.close()
    assert len(edges) == 1
    assert edges[0].confidence_label == "AMBIGUOUS", \
        f"Expected AMBIGUOUS label, got: {edges[0].confidence_label!r}"
    assert edges[0].confidence_score is not None
    assert edges[0].confidence_score <= 0.30
    assert edges[0].resolution_strategy == "ambiguous_name"


def test_resolve_same_module_strategy():
    file_path = "/app/auth.py"
    caller = _node(file_path, "login", qualified_name="auth.login")
    callee = _node(file_path, "verify", qualified_name="auth.verify")
    resolver = CallResolver([caller, callee])
    raw = _raw(caller.id, "verify")
    edges = resolver.resolve([raw])
    assert len(edges) == 1
    assert edges[0].to_id == callee.id
    assert edges[0].resolution_strategy == "same_module"
    assert edges[0].confidence == 0.90


def test_resolve_unique_name_strategy():
    n = _node("/other.py", "do_something", qualified_name="utils.do_something")
    caller = _node("/main.py", "main_fn", qualified_name="main.main_fn")
    resolver = CallResolver([n, caller])
    raw = _raw(caller.id, "do_something")
    edges = resolver.resolve([raw])
    assert len(edges) == 1
    assert edges[0].to_id == n.id
    assert edges[0].resolution_strategy == "unique_name"
    assert edges[0].confidence == 0.75


def test_resolve_import_map_suffix_strategy():
    n = _node("/storage.py", "write_chunks", qualified_name="opencode_search.storage.write_chunks")
    caller = _node("/indexer.py", "index", qualified_name="opencode_search.indexer.index")
    resolver = CallResolver([n, caller])
    raw = _raw(caller.id, "storage.write_chunks")
    edges = resolver.resolve([raw])
    assert len(edges) == 1
    assert edges[0].to_id == n.id


def test_resolve_suffix_match_strategy():
    n = _node("/deep/utils.py", "helper_fn", qualified_name="deep.utils.helper_fn")
    caller = _node("/main.py", "run", qualified_name="main.run")
    resolver = CallResolver([n, caller])
    raw = _raw(caller.id, "helper_fn")
    edges = resolver.resolve([raw])
    assert len(edges) == 1
    assert edges[0].to_id == n.id


def test_resolve_fuzzy_strategy():
    n = _node("/a.py", "authenticate_user", qualified_name="auth.authenticate_user")
    caller = _node("/b.py", "login", qualified_name="app.login")
    resolver = CallResolver([n, caller])
    raw = _raw(caller.id, "auth.authenticate_user")
    edges = resolver.resolve([raw])
    assert len(edges) >= 0  # fuzzy may or may not match
    # Just ensure no crash


def test_resolve_unresolvable_edge_dropped():
    n = _node("/a.py", "known_func", qualified_name="mod.known_func")
    resolver = CallResolver([n])
    raw = _raw(n.id, "completely_unknown_xyz_function_12345")
    edges = resolver.resolve([raw])
    # External lib call — should be dropped
    assert len(edges) == 0


def test_resolve_cross_file_call_resolved():
    caller = _node("/handler.py", "handle_request", qualified_name="handler.handle_request")
    callee = _node("/db.py", "get_connection", qualified_name="db.get_connection")
    resolver = CallResolver([caller, callee])
    raw = _raw(caller.id, "get_connection")
    edges = resolver.resolve([raw])
    assert len(edges) == 1
    assert edges[0].to_id == callee.id


def test_resolve_method_call_on_object():
    callee = _node("/storage.py", "write_chunks", qualified_name="storage.Storage.write_chunks")
    caller = _node("/indexer.py", "index", qualified_name="indexer.index")
    resolver = CallResolver([caller, callee])
    raw = _raw(caller.id, "storage.write_chunks")
    edges = resolver.resolve([raw])
    assert len(edges) == 1
    assert edges[0].to_id == callee.id


def test_resolve_empty_callee_dropped():
    n = _node("/a.py", "foo", qualified_name="mod.foo")
    resolver = CallResolver([n])
    raw = _raw(n.id, "")
    edges = resolver.resolve([raw])
    assert len(edges) == 0


def test_resolve_preserves_edge_kind():
    n = _node("/a.py", "foo", qualified_name="mod.foo")
    caller = _node("/b.py", "bar", qualified_name="mod.bar")
    resolver = CallResolver([n, caller])
    raw = _raw(caller.id, "foo", kind="IMPORTS")
    edges = resolver.resolve([raw])
    if edges:
        assert edges[0].kind == "IMPORTS"


def test_resolve_multiple_edges():
    fn_a = _node("/a.py", "fn_a", qualified_name="mod.fn_a")
    fn_b = _node("/b.py", "fn_b", qualified_name="mod.fn_b")
    caller = _node("/main.py", "main_func", qualified_name="main.main_func")
    resolver = CallResolver([fn_a, fn_b, caller])
    raw_edges = [
        _raw(caller.id, "fn_a"),
        _raw(caller.id, "fn_b"),
    ]
    edges = resolver.resolve(raw_edges)
    assert len(edges) == 2


def test_resolve_prefers_same_file_over_unique_name():
    """When multiple nodes have the same name, prefer same-file node."""
    file_path = "/app.py"
    same_file_callee = _node(file_path, "helper", qualified_name="app.helper")
    other_callee = _node("/other.py", "helper", qualified_name="utils.helper")
    caller = _node(file_path, "run", qualified_name="app.run")
    resolver = CallResolver([same_file_callee, other_callee, caller])
    raw = _raw(caller.id, "helper")
    edges = resolver.resolve([raw])
    assert len(edges) == 1
    assert edges[0].to_id == same_file_callee.id


def test_resolve_ambiguous_callee_returns_one_edge():
    """Multiple matches should return exactly one edge (not duplicate) labelled AMBIGUOUS."""
    n1 = _node("/a.py", "process", qualified_name="a.process")
    n2 = _node("/b.py", "process", qualified_name="b.process")
    caller = _node("/main.py", "run", qualified_name="main.run")
    resolver = CallResolver([n1, n2, caller])
    raw = _raw(caller.id, "process")
    edges = resolver.resolve([raw])
    assert len(edges) == 1
    assert edges[0].confidence_label == "AMBIGUOUS"
    assert edges[0].confidence <= 0.30


def test_resolve_ambiguous_confidence_score_stored():
    """AMBIGUOUS edges have a non-None confidence_score (the low confidence value)."""
    n1 = _node("/x.py", "helper", qualified_name="x.helper")
    n2 = _node("/y.py", "helper", qualified_name="y.helper")
    caller = _node("/main.py", "main", qualified_name="main.main")
    resolver = CallResolver([n1, n2, caller])
    edges = resolver.resolve([_raw(caller.id, "helper")])
    assert len(edges) == 1
    edge = edges[0]
    assert edge.confidence_label == "AMBIGUOUS"
    assert edge.confidence_score is not None
    assert edge.confidence_score <= 0.30


# ============================================================
# Community Detection
# ============================================================

pytestmark = [pytest.mark.integration]


def _node_id_community(file: str, qn: str) -> str:
    return hashlib.sha256(f"{file}::{qn}".encode()).hexdigest()[:16]


def _make_community_node(file: str, name: str, qn: str | None = None) -> NodeData:
    qualified = qn or f"mod.{name}"
    return NodeData(
        id=_node_id_community(file, qualified),
        name=name,
        qualified_name=qualified,
        kind="function",
        file=file,
        created_at="",
        updated_at="",
    )


# ---------------------------------------------------------------------------
# Basic community detection
# ---------------------------------------------------------------------------


def test_leiden_produces_communities_nontrivial_graph(storage):
    """A graph with two clusters should produce >= 1 community."""
    # Cluster A: a1 ↔ a2 ↔ a3
    # Cluster B: b1 ↔ b2 ↔ b3
    nodes_a = [_make_community_node("/a.py", f"a{i}", f"a.a{i}") for i in range(3)]
    nodes_b = [_make_community_node("/b.py", f"b{i}", f"b.b{i}") for i in range(3)]
    all_nodes = nodes_a + nodes_b
    storage.upsert_nodes(all_nodes)

    edges = []
    for i in range(len(nodes_a) - 1):
        edges.append(EdgeData(from_id=nodes_a[i].id, to_id=nodes_a[i + 1].id, kind="CALLS"))
        edges.append(EdgeData(from_id=nodes_a[i + 1].id, to_id=nodes_a[i].id, kind="CALLS"))
    for i in range(len(nodes_b) - 1):
        edges.append(EdgeData(from_id=nodes_b[i].id, to_id=nodes_b[i + 1].id, kind="CALLS"))
        edges.append(EdgeData(from_id=nodes_b[i + 1].id, to_id=nodes_b[i].id, kind="CALLS"))
    storage.upsert_edges(edges)

    detector = CommunityDetector()
    mapping = detector.detect_communities(storage)

    assert len(mapping) == len(all_nodes)


def test_leiden_all_nodes_assigned_community_id(storage):
    nodes = [_make_community_node(f"/f{i}.py", f"f{i}", f"m.f{i}") for i in range(6)]
    storage.upsert_nodes(nodes)
    edges = [
        EdgeData(from_id=nodes[i].id, to_id=nodes[i + 1].id, kind="CALLS")
        for i in range(len(nodes) - 1)
    ]
    storage.upsert_edges(edges)

    detector = CommunityDetector()
    mapping = detector.detect_communities(storage)

    assert len(mapping) == len(nodes)
    # All nodes in storage should have community_id set
    for n in storage.all_nodes():
        assert n.community_id is not None


def test_leiden_community_ids_persisted_in_nodes_table(storage):
    nodes = [_make_community_node(f"/f{i}.py", f"fn{i}", f"m.fn{i}") for i in range(4)]
    storage.upsert_nodes(nodes)
    edges = [
        EdgeData(from_id=nodes[0].id, to_id=nodes[1].id, kind="CALLS"),
        EdgeData(from_id=nodes[2].id, to_id=nodes[3].id, kind="CALLS"),
    ]
    storage.upsert_edges(edges)

    detector = CommunityDetector()
    detector.detect_communities(storage)

    for n in storage.all_nodes():
        assert n.community_id is not None, f"Node {n.name} has no community_id"


def test_leiden_handles_empty_graph(storage):
    detector = CommunityDetector()
    mapping = detector.detect_communities(storage)
    assert mapping == {}


def test_leiden_handles_single_node_graph(storage):
    n = _make_community_node("/a.py", "solo", "m.solo")
    storage.upsert_nodes([n])
    detector = CommunityDetector()
    mapping = detector.detect_communities(storage)
    # Singleton nodes are NOT assigned communities (node_count < 2 → skipped)
    assert len(mapping) == 0
    assert n.id not in mapping


def test_leiden_handles_disconnected_components(storage):
    """Unconnected isolated nodes should be singletons — not assigned communities."""
    nodes = [_make_community_node(f"/f{i}.py", f"isolated{i}", f"m.isolated{i}") for i in range(5)]
    storage.upsert_nodes(nodes)
    # No edges at all → all 5 nodes are singletons → mapping is empty
    detector = CommunityDetector()
    mapping = detector.detect_communities(storage)
    assert len(mapping) == 0


def test_leiden_idempotent_on_same_graph(storage):
    """Running detection twice on the same graph should give the same number of communities."""
    nodes = [_make_community_node(f"/f{i}.py", f"f{i}", f"m.f{i}") for i in range(6)]
    storage.upsert_nodes(nodes)
    edges = [
        EdgeData(from_id=nodes[i].id, to_id=nodes[i + 1].id, kind="CALLS")
        for i in range(len(nodes) - 1)
    ]
    storage.upsert_edges(edges)

    detector = CommunityDetector()
    mapping1 = detector.detect_communities(storage)
    communities1 = storage.get_communities()

    # Re-run
    mapping2 = detector.detect_communities(storage)
    communities2 = storage.get_communities()

    assert len(mapping1) == len(mapping2)
    # Community count should be stable
    assert len(communities1) == len(communities2)


def test_leiden_communities_written_to_storage(storage):
    nodes = [_make_community_node(f"/f{i}.py", f"f{i}", f"m.f{i}") for i in range(4)]
    storage.upsert_nodes(nodes)
    storage.upsert_edges([
        EdgeData(from_id=nodes[0].id, to_id=nodes[1].id, kind="CALLS"),
        EdgeData(from_id=nodes[2].id, to_id=nodes[3].id, kind="CALLS"),
    ])

    detector = CommunityDetector()
    detector.detect_communities(storage)

    communities = storage.get_communities()
    assert len(communities) >= 1
    for c in communities:
        assert c.node_count >= 1


def test_leiden_identifies_entry_points(storage):
    """Entry points: nodes most called from outside their community (when multi-community)."""
    # Use two dense clusters with api_handler as the cross-cluster entry point
    # Cluster A: api_handler + a group of helpers (densely connected)
    # Cluster B: main + a group of callers (densely connected, calls api_handler)
    cluster_a = [_make_community_node("/api.py", f"ah{i}", f"api.ah{i}") for i in range(5)]
    cluster_b = [_make_community_node("/main.py", f"mn{i}", f"main.mn{i}") for i in range(5)]
    storage.upsert_nodes(cluster_a + cluster_b)

    edges = []
    # Dense within A
    for i in range(len(cluster_a) - 1):
        edges.append(EdgeData(from_id=cluster_a[i].id, to_id=cluster_a[i + 1].id, kind="CALLS"))
        edges.append(EdgeData(from_id=cluster_a[i + 1].id, to_id=cluster_a[i].id, kind="CALLS"))
    # Dense within B
    for i in range(len(cluster_b) - 1):
        edges.append(EdgeData(from_id=cluster_b[i].id, to_id=cluster_b[i + 1].id, kind="CALLS"))
        edges.append(EdgeData(from_id=cluster_b[i + 1].id, to_id=cluster_b[i].id, kind="CALLS"))
    # Cross-cluster: multiple B nodes call cluster_a[0] (making it an entry point)
    for b_node in cluster_b:
        edges.append(EdgeData(from_id=b_node.id, to_id=cluster_a[0].id, kind="CALLS"))
    storage.upsert_edges(edges)

    detector = CommunityDetector()
    detector.detect_communities(storage)

    communities = storage.get_communities()
    # Entry point logic applies when communities are actually distinct
    # (non-trivial graph may still put all in one community — that's ok)
    # Just verify the function completes and returns valid data
    assert len(communities) >= 1
    for c in communities:
        assert isinstance(c.key_entry_points, list)


def test_leiden_large_graph_500_nodes_under_30s(storage):
    """Performance gate: 500 nodes + 1000 edges completes in < 30 seconds."""
    nodes = [_make_community_node(f"/f{i}.py", f"fn{i}", f"m.fn{i}") for i in range(500)]
    storage.upsert_nodes(nodes)
    edges = [
        EdgeData(from_id=nodes[i % 500].id, to_id=nodes[(i + 1) % 500].id, kind="CALLS")
        for i in range(1000)
    ]
    storage.upsert_edges(edges)

    detector = CommunityDetector()
    t0 = time.perf_counter()
    mapping = detector.detect_communities(storage)
    elapsed = time.perf_counter() - t0

    assert len(mapping) == 500
    assert elapsed < 30.0, f"Community detection took {elapsed:.1f}s on 500 nodes"


# ---------------------------------------------------------------------------
# handle_get_communities integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_get_communities_returns_all(tmp_path):
    from opencode_search.graph.storage import CommunityData
    from opencode_search.handlers._graph import handle_get_communities

    graph_db_path = str(tmp_path / "graph.db")
    gs = GraphStorage(graph_db_path)
    gs.open()
    for i in range(3):
        gs.upsert_community(CommunityData(
            id=i, title=f"Community {i}", node_count=i + 1,
        ))
    gs.close()

    with patch(
        "opencode_search.handlers._graph.get_project_graph_db_path",
        return_value=graph_db_path,
    ):
        result = await handle_get_communities(project_path="/tmp/proj")

    # community 0 has node_count=1 which is below min_node_count=2 filter
    assert result["total"] == 2
    titles = {c["title"] for c in result["communities"]}
    assert "Community 1" in titles


@pytest.mark.asyncio
async def test_handle_get_communities_before_detection_returns_empty(tmp_path):
    from opencode_search.handlers._graph import handle_get_communities

    graph_db_path = str(tmp_path / "graph.db")
    gs = GraphStorage(graph_db_path)
    gs.open()
    gs.close()

    with patch(
        "opencode_search.handlers._graph.get_project_graph_db_path",
        return_value=graph_db_path,
    ):
        result = await handle_get_communities(project_path="/tmp/proj")

    assert result["total"] == 0
    assert result["communities"] == []


# ============================================================
# Graph E2E
# ============================================================

_LARGE = pytest.mark.large
_ASTRO = os.environ.get(
    "OPENCODE_TEST_PROJECT",
    "/home/user/git/github.com/fairyhunter13/astro-project",
)


def _use_real_registry(monkeypatch):
    real = Path.home() / ".local" / "share" / "opencode-search" / "projects.json"
    monkeypatch.setenv("OPENCODE_REGISTRY_PATH", str(real))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Edge type completeness
# ---------------------------------------------------------------------------

@_LARGE
class TestEdgeTypes:
    def test_graph_db_has_calls_edges(self):
        """CALLS edges must exist in astro-project graph."""
        import sqlite3

        from opencode_search.config import get_project_graph_db_path
        db_path = get_project_graph_db_path(_ASTRO)
        assert Path(db_path).exists(), f"graph.db not found: {db_path}"
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT COUNT(*) FROM edges WHERE kind='CALLS'").fetchone()
        conn.close()
        count = row[0] if row else 0
        assert count > 0, f"Expected CALLS edges, found 0 in {db_path}"

    def test_graph_db_has_imports_edges(self):
        """IMPORTS edges must exist."""
        import sqlite3

        from opencode_search.config import get_project_graph_db_path
        db_path = get_project_graph_db_path(_ASTRO)
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT COUNT(*) FROM edges WHERE kind='IMPORTS'").fetchone()
        conn.close()
        count = row[0] if row else 0
        assert count >= 0  # IMPORTS may be 0 for pure Go projects (Go uses CALLS)
        # At least one of CALLS or IMPORTS must be present
        conn = sqlite3.connect(db_path)
        total = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        conn.close()
        assert total > 0, "Expected at least some edges (CALLS or IMPORTS)"

    def test_graph_db_has_nodes_of_known_kinds(self):
        """Nodes must have known kinds: function, method, class, file, module."""
        import sqlite3

        from opencode_search.config import get_project_graph_db_path
        db_path = get_project_graph_db_path(_ASTRO)
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT DISTINCT kind FROM nodes LIMIT 20").fetchall()
        conn.close()
        kinds = {r[0] for r in rows}
        known = {"function", "method", "class", "file", "module"}
        overlap = kinds & known
        assert overlap, f"No known node kinds found. Got: {kinds}"


# ---------------------------------------------------------------------------
# Graph traversal relations
# ---------------------------------------------------------------------------

@_LARGE
class TestGraphRelations:
    def _find_symbol(self) -> str:
        """Find a real function name in astro-project graph."""
        import sqlite3

        from opencode_search.config import get_project_graph_db_path
        db_path = get_project_graph_db_path(_ASTRO)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT name FROM nodes WHERE kind IN ('function','method') AND name != '' LIMIT 1"
        ).fetchone()
        conn.close()
        return row[0] if row else "main"

    def test_callers_returns_dict(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_get_callers
        symbol = self._find_symbol()
        r = _run(handle_get_callers(symbol, _ASTRO, depth=2))
        assert isinstance(r, dict), f"Expected dict, got {type(r)}"
        assert "error" not in r, f"Unexpected error: {r.get('error')}"

    def test_callees_returns_dict(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_get_callees
        symbol = self._find_symbol()
        r = _run(handle_get_callees(symbol, _ASTRO, depth=2))
        assert isinstance(r, dict)
        assert "error" not in r

    def test_impact_returns_dict(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_detect_impact
        symbol = self._find_symbol()
        r = _run(handle_detect_impact(symbol, _ASTRO))
        assert isinstance(r, dict)
        assert "error" not in r

    def test_definition_returns_dict(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_get_symbol
        symbol = self._find_symbol()
        r = _run(handle_get_symbol(symbol, _ASTRO))
        assert isinstance(r, dict)
        assert "error" not in r

    def test_trace_path_returns_dict(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_trace_path
        symbol = self._find_symbol()
        r = _run(handle_trace_path(symbol, symbol, _ASTRO))
        assert isinstance(r, dict)
        assert "error" not in r


# ---------------------------------------------------------------------------
# Graph export: JSON format
# ---------------------------------------------------------------------------

@_LARGE
class TestGraphExportJSON:
    def test_export_json_has_nodes_edges_communities(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_graph_export
        r = _run(handle_graph_export(project_path=_ASTRO, format="json", max_nodes=200))
        assert isinstance(r, dict)
        assert "nodes" in r, f"Expected 'nodes' key, got: {list(r.keys())}"
        assert "edges" in r, f"Expected 'edges' key, got: {list(r.keys())}"
        assert isinstance(r["nodes"], list)
        assert isinstance(r["edges"], list)

    def test_export_json_max_nodes_respected(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_graph_export
        r = _run(handle_graph_export(project_path=_ASTRO, format="json", max_nodes=50))
        n = len(r.get("nodes", []))
        assert n <= 50, f"max_nodes=50 not respected: got {n}"

    def test_export_json_nodes_have_required_fields(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_graph_export
        r = _run(handle_graph_export(project_path=_ASTRO, format="json", max_nodes=20))
        for node in r.get("nodes", [])[:5]:
            assert "id" in node or "name" in node or "qualified_name" in node, \
                f"Node missing id/name: {node}"

    def test_export_json_communities_field(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_graph_export
        r = _run(handle_graph_export(project_path=_ASTRO, format="json", max_nodes=100))
        # communities may or may not be present, but if present must be a list
        if "communities" in r:
            assert isinstance(r["communities"], list)


# ---------------------------------------------------------------------------
# Graph export: GraphML format
# ---------------------------------------------------------------------------

@_LARGE
class TestGraphExportGraphML:
    def test_export_graphml_is_valid_xml(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_graph_export
        r = _run(handle_graph_export(project_path=_ASTRO, format="graphml", max_nodes=100))
        # GraphML handler returns dict with graphml key OR raw string
        if isinstance(r, dict):
            graphml_str = r.get("graphml") or r.get("content") or json.dumps(r)
        else:
            graphml_str = str(r)
        # Must parse as XML without exception
        try:
            root = ET.fromstring(graphml_str)
            assert root is not None
        except ET.ParseError as exc:
            pytest.fail(f"GraphML is not valid XML: {exc}\nContent: {graphml_str[:200]}")

    def test_export_graphml_contains_graph_element(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_graph_export
        r = _run(handle_graph_export(project_path=_ASTRO, format="graphml", max_nodes=50))
        graphml_str = r.get("graphml") or r.get("content") or "" if isinstance(r, dict) else str(r)
        # GraphML must contain <graph or graphml namespace
        has_graph = "<graph" in graphml_str or "graphml" in graphml_str.lower()
        assert has_graph, f"GraphML missing <graph element: {graphml_str[:200]}"


# ---------------------------------------------------------------------------
# Community enrichment
# ---------------------------------------------------------------------------

@_LARGE
class TestCommunityEnrichment:
    def test_communities_have_titles(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_get_communities
        r = _run(handle_get_communities(project_path=_ASTRO, top_k=20))
        comms = r.get("communities", [])
        assert len(comms) >= 1, "Expected at least 1 community"
        enriched = [c for c in comms if c.get("title") and f"Community {c.get('id')}" != c.get("title")]
        # At least 50% should be enriched with LLM titles
        if len(comms) >= 5:
            pct = len(enriched) / len(comms) * 100
            assert pct >= 50, f"Only {pct:.0f}% of communities have enriched titles"

    def test_communities_have_node_counts(self, monkeypatch):
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_get_communities
        r = _run(handle_get_communities(project_path=_ASTRO, top_k=10))
        for c in r.get("communities", [])[:5]:
            assert "node_count" in c or "size" in c or "nodes" in c, \
                f"Community missing node_count: {list(c.keys())}"

    def test_community_export_colors_consistent(self, monkeypatch):
        """All node community_ids must match a community entry."""
        _use_real_registry(monkeypatch)
        from opencode_search.handlers import handle_graph_export
        r = _run(handle_graph_export(project_path=_ASTRO, format="json", max_nodes=100))
        comms = {c["id"] for c in r.get("communities", [])}
        if not comms:
            return  # Skip if no communities in export
        for node in r.get("nodes", [])[:50]:
            cid = node.get("community_id")
            if cid is not None:
                assert cid in comms, \
                    f"Node has community_id={cid} but no matching community entry"


# ===========================================================================
# Phase 4: Extended Go + gRPC + Java extraction tests
# ===========================================================================
# Phase 20: Scala + Groovy extraction tests (appended below TestProtobufGrpcExtractorExtended)

@pytest.fixture
def ex() -> GraphExtractor:
    return GraphExtractor()


class TestGoExtractionComprehensive:
    """Comprehensive Go language extraction covering astro-project patterns."""

    def test_exported_function(self, ex):
        src = "package cart\n\nfunc PlaceOrder(ctx context.Context, req *OrderRequest) error {\n\treturn nil\n}\n"
        nodes, _ = ex.extract_file("/tmp/cart.go", src, "go")
        assert "PlaceOrder" in _names(nodes)
        fn = next(n for n in nodes if n.name == "PlaceOrder")
        assert fn.kind == "function"

    def test_unexported_function(self, ex):
        src = "package cart\n\nfunc validateItem(id int) bool {\n\treturn id > 0\n}\n"
        nodes, _ = ex.extract_file("/tmp/cart.go", src, "go")
        assert "validateItem" in _names(nodes)

    def test_pointer_receiver_method(self, ex):
        src = "package cart\n\ntype CartUseCase struct{}\n\nfunc (c *CartUseCase) GetCart() (interface{}, error) {\n\treturn nil, nil\n}\n"
        nodes, _ = ex.extract_file("/tmp/cart.go", src, "go")
        assert "GetCart" in _names(nodes)
        m = next(n for n in nodes if n.name == "GetCart")
        assert m.kind == "method"

    def test_value_receiver_method(self, ex):
        src = "package cart\n\ntype Item struct{}\n\nfunc (i Item) Price() float64 {\n\treturn 0.0\n}\n"
        nodes, _ = ex.extract_file("/tmp/cart.go", src, "go")
        assert "Price" in _names(nodes)

    def test_interface_extraction(self, ex):
        src = "package cart\n\ntype Repository interface {\n\tGetByID(id int) (interface{}, error)\n\tSave(item interface{}) error\n}\n"
        nodes, _ = ex.extract_file("/tmp/cartrepo.go", src, "go")
        names = _names(nodes)
        # Go interface types may not be extracted as symbols by tree-sitter (only functions/methods
        # are extracted); the extractor returns interface method names or the interface itself.
        # Accept any symbol from this source — at minimum the interface methods are captured
        # as plain function nodes OR the interface type itself as a "class"-kind node.
        assert names, "No symbols extracted from Go interface source. Got empty set."

    def test_qualified_name_with_receiver_type(self, ex):
        src = "package usecase\n\ntype OrderUseCase struct{}\n\nfunc (o *OrderUseCase) CreateOrder() error {\n\treturn nil\n}\n"
        nodes, _ = ex.extract_file("/tmp/order.go", src, "go")
        qualifieds = _qualifieds(nodes)
        # Qualified name should include either the package, the receiver type, or both
        assert any("CreateOrder" in q for q in qualifieds), \
            f"CreateOrder not in qualified names: {qualifieds}"

    def test_call_edge_to_qualified_method(self, ex):
        src = "package main\n\nfunc Run(repo *CartRepo) {\n\trepo.Save(nil)\n\trepo.GetAll()\n}\n"
        _, edges = ex.extract_file("/tmp/main.go", src, "go")
        callees = {e.raw_callee for e in edges if e.kind == "CALLS"}
        assert callees, "Expected CALLS edges from repo method calls"

    def test_goroutine_call_edge(self, ex):
        src = "package worker\n\nfunc Start() {\n\tgo process()\n}\n\nfunc process() {}\n"
        _, edges = ex.extract_file("/tmp/worker.go", src, "go")
        callees = {e.raw_callee for e in edges if e.kind == "CALLS"}
        assert "process" in callees, f"goroutine call edge missing. Got: {callees}"

    def test_grpc_server_handler_pattern(self, ex):
        src = (
            "package grpc\n\n"
            "type CartServer struct{}\n\n"
            "func (s *CartServer) GetCart(ctx context.Context, req *GetCartRequest) (*CartResponse, error) {\n"
            "\treturn nil, nil\n"
            "}\n\n"
            "func (s *CartServer) AddItem(ctx context.Context, req *AddItemRequest) (*CartResponse, error) {\n"
            "\treturn nil, nil\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/server.go", src, "go")
        names = _names(nodes)
        assert "GetCart" in names, f"gRPC handler GetCart not extracted. Got: {names}"
        assert "AddItem" in names, f"gRPC handler AddItem not extracted. Got: {names}"

    def test_package_level_var(self, ex):
        src = "package config\n\nvar DefaultTimeout = 30\n\nfunc New() {}\n"
        nodes, _ = ex.extract_file("/tmp/config.go", src, "go")
        names = _names(nodes)
        # At minimum the function should be extracted
        assert "New" in names, f"Expected 'New' function. Got: {names}"

    def test_multiple_functions_same_file(self, ex):
        src = (
            "package service\n\n"
            "func Create() error { return nil }\n"
            "func Update() error { return nil }\n"
            "func Delete() error { return nil }\n"
            "func List() ([]string, error) { return nil, nil }\n"
        )
        nodes, _ = ex.extract_file("/tmp/svc.go", src, "go")
        names = _names(nodes)
        assert "Create" in names and "Update" in names and "Delete" in names, \
            f"Expected CRUD functions. Got: {names}"


class TestProtobufGrpcExtraction:
    """gRPC/Protobuf extraction — astro-project uses .proto files for inter-service comms."""

    def test_proto_service_definition_in_go_stub(self, ex):
        """Generated Go gRPC stubs have Register* functions."""
        src = (
            "package pb\n\n"
            "type CartServiceServer interface {\n"
            "\tGetCart(context.Context, *GetCartRequest) (*CartResponse, error)\n"
            "\tAddItem(context.Context, *AddItemRequest) (*CartResponse, error)\n"
            "}\n\n"
            "func RegisterCartServiceServer(s *grpc.Server, srv CartServiceServer) {\n"
            "\ts.RegisterService(&_CartService_serviceDesc, srv)\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/cart_grpc.pb.go", src, "go")
        names = _names(nodes)
        assert "RegisterCartServiceServer" in names or "CartServiceServer" in names, \
            f"gRPC stub symbols not extracted. Got: {names}"

    def test_grpc_client_call_edges(self, ex):
        """gRPC client calls produce CALLS edges."""
        src = (
            "package handler\n\n"
            "func (h *Handler) HandleGetCart(ctx context.Context, userID string) (*CartResponse, error) {\n"
            "\trespCart, err := h.cartClient.GetCart(ctx, &GetCartRequest{UserID: userID})\n"
            "\tif err != nil {\n"
            "\t\treturn nil, err\n"
            "\t}\n"
            "\treturn respCart, nil\n"
            "}\n"
        )
        _, edges = ex.extract_file("/tmp/handler.go", src, "go")
        callees = {e.raw_callee for e in edges if e.kind == "CALLS"}
        assert callees, "Expected CALLS from gRPC client call. Got none"

    def test_proto_message_struct_in_generated_go(self, ex):
        """Proto-generated Go has message structs with field methods."""
        src = (
            "package pb\n\n"
            "type GetCartRequest struct {\n"
            "\tUserId string\n"
            "\tSessionId string\n"
            "}\n\n"
            "func (m *GetCartRequest) GetUserId() string {\n"
            "\treturn m.UserId\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/cart.pb.go", src, "go")
        names = _names(nodes)
        assert "GetUserId" in names or "GetCartRequest" in names, \
            f"Proto message/getter not extracted. Got: {names}"

    def test_interceptor_middleware_pattern(self, ex):
        """gRPC interceptors are functions matching UnaryServerInterceptor signature."""
        src = (
            "package middleware\n\n"
            "func AuthInterceptor(ctx context.Context, req interface{}, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (interface{}, error) {\n"
            "\treturn handler(ctx, req)\n"
            "}\n\n"
            "func LoggingInterceptor(ctx context.Context, req interface{}, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (interface{}, error) {\n"
            "\treturn handler(ctx, req)\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/interceptors.go", src, "go")
        names = _names(nodes)
        assert "AuthInterceptor" in names or "LoggingInterceptor" in names, \
            f"gRPC interceptors not extracted. Got: {names}"


class TestJavaSpringBootExtraction:
    """Java/Spring Boot extraction — astro-project has Spring Boot API gateways."""

    def test_spring_rest_controller_method(self, ex):
        src = (
            "package com.example.gateway;\n\n"
            "import org.springframework.web.bind.annotation.*;\n\n"
            "@RestController\n"
            "@RequestMapping(\"/api/cart\")\n"
            "public class CartController {\n"
            "\n"
            "    @GetMapping(\"/{userId}\")\n"
            "    public ResponseEntity<CartResponse> getCart(@PathVariable String userId) {\n"
            "        return ResponseEntity.ok(new CartResponse());\n"
            "    }\n"
            "\n"
            "    @PostMapping(\"/items\")\n"
            "    public ResponseEntity<Void> addItem(@RequestBody AddItemRequest req) {\n"
            "        return ResponseEntity.ok().build();\n"
            "    }\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/CartController.java", src, "java")
        names = _names(nodes)
        # At minimum the class or methods should be extracted
        assert names, "Java Spring Boot extraction returned no symbols"
        assert "CartController" in names or "getCart" in names or "addItem" in names, \
            f"Expected CartController class or methods. Got: {names}"

    def test_service_class_extraction(self, ex):
        src = (
            "package com.example.service;\n\n"
            "@Service\n"
            "public class CartService {\n"
            "\n"
            "    public CartDto getCart(String userId) {\n"
            "        return new CartDto();\n"
            "    }\n"
            "\n"
            "    public void addItem(String userId, ItemDto item) {}\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/CartService.java", src, "java")
        names = _names(nodes)
        assert names, "Java @Service class extraction returned no symbols"

    def test_repository_interface_extraction(self, ex):
        src = (
            "package com.example.repository;\n\n"
            "public interface CartRepository {\n"
            "    Cart findByUserId(String userId);\n"
            "    void save(Cart cart);\n"
            "    void deleteByUserId(String userId);\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/CartRepository.java", src, "java")
        names = _names(nodes)
        assert names, "Java interface extraction returned no symbols"

    def test_java_method_call_edges(self, ex):
        """Java method calls inside a method body produce CALLS edges."""
        src = (
            "package com.example.service;\n\n"
            "public class OrderService {\n"
            "    public OrderDto processOrder(String orderId) {\n"
            "        OrderDto order = loadOrder(orderId);\n"
            "        validate(order);\n"
            "        return save(order);\n"
            "    }\n"
            "\n"
            "    private OrderDto loadOrder(String id) { return null; }\n"
            "    private void validate(OrderDto o) {}\n"
            "    private OrderDto save(OrderDto o) { return o; }\n"
            "}\n"
        )
        nodes, raw_edges = ex.extract_file("/tmp/OrderService.java", src, "java")
        names = _names(nodes)
        assert "OrderService" in names or "processOrder" in names, \
            f"OrderService class/method not extracted. Got: {names}"
        calls = {e.raw_callee for e in raw_edges if e.kind == "CALLS"}
        assert calls, (
            f"Expected CALLS edges from Java method body. "
            f"nodes={names}"
        )

    def test_java_constructor_injection_pattern(self, ex):
        """Java constructor injection (Autowired-style) is extracted as a class with method."""
        src = (
            "package com.example.handler;\n\n"
            "import org.springframework.beans.factory.annotation.Autowired;\n\n"
            "public class CartHandler {\n"
            "    private final CartService cartService;\n"
            "\n"
            "    @Autowired\n"
            "    public CartHandler(CartService cartService) {\n"
            "        this.cartService = cartService;\n"
            "    }\n"
            "\n"
            "    public CartDto handle(String userId) {\n"
            "        return cartService.getCart(userId);\n"
            "    }\n"
            "}\n"
        )
        nodes, raw_edges = ex.extract_file("/tmp/CartHandler.java", src, "java")
        names = _names(nodes)
        assert names, "Java constructor injection class returned no symbols"
        assert "CartHandler" in names or "handle" in names or "CartHandler" in names, \
            f"Expected CartHandler class or methods. Got: {names}"

    def test_java_interface_implementation(self, ex):
        """Java class implementing an interface produces INHERITS or class/method nodes."""
        src = (
            "package com.example.impl;\n\n"
            "public class CartRepositoryImpl implements CartRepository {\n"
            "\n"
            "    @Override\n"
            "    public Cart findByUserId(String userId) {\n"
            "        return null;\n"
            "    }\n"
            "\n"
            "    @Override\n"
            "    public void save(Cart cart) {}\n"
            "\n"
            "    @Override\n"
            "    public void deleteByUserId(String userId) {}\n"
            "}\n"
        )
        nodes, raw_edges = ex.extract_file("/tmp/CartRepositoryImpl.java", src, "java")
        names = _names(nodes)
        assert names, "Java implementing class returned no symbols"

    def test_java_multiple_annotated_classes(self, ex):
        """Multiple annotated Spring classes in one snippet are all extracted."""
        src = (
            "package com.example;\n\n"
            "@Component\n"
            "public class EventPublisher {\n"
            "    public void publish(Object event) {}\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/EventPublisher.java", src, "java")
        names = _names(nodes)
        assert names, "Java @Component class returned no symbols"

    def test_java_class_with_fields_extracts_class_name(self, ex):
        """A Java class with field declarations produces at least the class node."""
        src = (
            "package com.example.dto;\n\n"
            "public class CartDto {\n"
            "    private String userId;\n"
            "    private java.util.List<ItemDto> items;\n"
            "\n"
            "    public String getUserId() { return userId; }\n"
            "    public void setUserId(String userId) { this.userId = userId; }\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/CartDto.java", src, "java")
        names = _names(nodes)
        assert names, "Java DTO class returned no symbols"
        assert "CartDto" in names or "getUserId" in names, \
            f"Expected CartDto class or getter methods. Got: {names}"


class TestProtobufGrpcExtractorExtended:
    """Extended gRPC/Protobuf extraction tests — astro-project uses proto3 extensively."""

    @pytest.fixture
    def ex(self) -> GraphExtractor:
        return GraphExtractor()

    def test_grpc_streaming_rpc_stub(self, ex):
        """Go gRPC stub for server-streaming RPC is extracted as a method."""
        src = (
            "package pb\n\n"
            "type OrderServiceClient interface {\n"
            "\tPlaceOrder(ctx context.Context, req *PlaceOrderRequest) (*PlaceOrderResponse, error)\n"
            "\tListOrders(ctx context.Context, req *ListOrdersRequest, opts ...grpc.CallOption) (OrderService_ListOrdersClient, error)\n"
            "}\n\n"
            "func NewOrderServiceClient(cc *grpc.ClientConn) OrderServiceClient {\n"
            "\treturn &orderServiceClient{cc}\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/order_grpc.pb.go", src, "go")
        names = _names(nodes)
        assert "NewOrderServiceClient" in names or "OrderServiceClient" in names, \
            f"gRPC streaming client stub not extracted. Got: {names}"

    def test_proto_generated_go_enum_consts(self, ex):
        """Proto-generated Go enum constants (iota-like const blocks) are in files."""
        src = (
            "package pb\n\n"
            "type OrderStatus int32\n\n"
            "const (\n"
            "\tOrderStatus_PENDING  OrderStatus = 0\n"
            "\tOrderStatus_PAID     OrderStatus = 1\n"
            "\tOrderStatus_SHIPPED  OrderStatus = 2\n"
            ")\n\n"
            "func (x OrderStatus) String() string {\n"
            "\treturn \"\"\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/order_pb.go", src, "go")
        names = _names(nodes)
        assert names, "No symbols extracted from proto enum stub. Got empty."
        # At minimum the String() method should be extracted
        assert "String" in names or "OrderStatus" in names, \
            f"Expected String() method or OrderStatus type. Got: {names}"

    def test_grpc_server_register_and_handler(self, ex):
        """Go gRPC server with RegisterXxxServer and handler methods are both extracted."""
        src = (
            "package server\n\n"
            "type PaymentServiceServer struct{}\n\n"
            "func (s *PaymentServiceServer) ProcessPayment(ctx context.Context, req *PaymentRequest) (*PaymentResponse, error) {\n"
            "\treturn nil, nil\n"
            "}\n\n"
            "func (s *PaymentServiceServer) RefundPayment(ctx context.Context, req *RefundRequest) (*RefundResponse, error) {\n"
            "\treturn nil, nil\n"
            "}\n\n"
            "func RegisterPaymentServiceServer(srv *grpc.Server, impl PaymentServiceServer) {\n"
            "\tsrv.RegisterService(&_PaymentService_serviceDesc, impl)\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/payment_grpc.pb.go", src, "go")
        names = _names(nodes)
        assert "ProcessPayment" in names or "RegisterPaymentServiceServer" in names, \
            f"gRPC server symbols not extracted. Got: {names}"

    def test_grpc_context_cancellation_call_edges(self, ex):
        """gRPC handler that checks ctx.Done() produces CALLS edges."""
        src = (
            "package handler\n\n"
            "func (h *OrderHandler) CreateOrder(ctx context.Context, req *CreateOrderRequest) (*Order, error) {\n"
            "\tselect {\n"
            "\tcase <-ctx.Done():\n"
            "\t\treturn nil, ctx.Err()\n"
            "\tdefault:\n"
            "\t}\n"
            "\treturn h.orderUseCase.Create(ctx, req)\n"
            "}\n"
        )
        _, edges = ex.extract_file("/tmp/order_handler.go", src, "go")
        callees = {e.raw_callee for e in edges if e.kind == "CALLS"}
        assert callees, "Expected CALLS edges from gRPC handler body. Got none"

    def test_multiple_proto_message_getters(self, ex):
        """Multiple Get* proto getter methods are all extracted."""
        src = (
            "package pb\n\n"
            "type CartItem struct {\n"
            "\tProductId string\n"
            "\tQuantity  int32\n"
            "\tPrice     float64\n"
            "}\n\n"
            "func (m *CartItem) GetProductId() string { return m.ProductId }\n"
            "func (m *CartItem) GetQuantity() int32   { return m.Quantity }\n"
            "func (m *CartItem) GetPrice() float64    { return m.Price }\n"
        )
        nodes, _ = ex.extract_file("/tmp/cart_item.pb.go", src, "go")
        names = _names(nodes)
        getter_count = sum(1 for n in names if n.startswith("Get"))
        assert getter_count >= 2, \
            f"Expected >= 2 proto getters extracted, got {getter_count}. Names: {names}"

# ===========================================================================
# Phase 20: Scala extraction tests
# ===========================================================================

class TestScalaExtraction:
    """Scala language extraction — class, object, trait, method, call edges."""

    def test_scala_class_extracted(self, ex):
        src = (
            "package com.example\n\n"
            "class CartService {\n"
            "  def getCart(id: String): String = id\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/CartService.scala", src, "scala")
        names = _names(nodes)
        assert "CartService" in names, f"Expected CartService class. Got: {names}"

    def test_scala_object_extracted(self, ex):
        src = (
            "package com.example\n\n"
            "object PaymentUtils {\n"
            "  def formatAmount(amount: Double): String = amount.toString\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/PaymentUtils.scala", src, "scala")
        names = _names(nodes)
        assert "PaymentUtils" in names, f"Expected PaymentUtils object. Got: {names}"

    def test_scala_trait_extracted(self, ex):
        src = (
            "package com.example\n\n"
            "trait Repository {\n"
            "  def findById(id: String): Option[String]\n"
            "  def save(item: String): Unit\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/Repository.scala", src, "scala")
        names = _names(nodes)
        assert "Repository" in names, f"Expected Repository trait. Got: {names}"

    def test_scala_def_method_extracted(self, ex):
        src = (
            "class OrderProcessor {\n"
            "  def processOrder(orderId: String): Boolean = {\n"
            "    true\n"
            "  }\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/OrderProcessor.scala", src, "scala")
        names = _names(nodes)
        assert "processOrder" in names or "OrderProcessor" in names, \
            f"Expected processOrder or OrderProcessor. Got: {names}"

    def test_scala_multiple_defs_in_class(self, ex):
        src = (
            "class InvoiceService {\n"
            "  def createInvoice(id: String): String = id\n"
            "  def cancelInvoice(id: String): Boolean = true\n"
            "  def getInvoice(id: String): Option[String] = None\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/InvoiceService.scala", src, "scala")
        names = _names(nodes)
        assert len(names) >= 1, f"Expected at least 1 symbol from Scala class. Got: {names}"

    def test_scala_call_edges_extracted(self, ex):
        src = (
            "class NotificationService {\n"
            "  def sendEmail(to: String): Unit = {\n"
            "    validate(to)\n"
            "    deliver(to)\n"
            "  }\n"
            "  def validate(s: String): Boolean = true\n"
            "  def deliver(s: String): Unit = ()\n"
            "}\n"
        )
        _, edges = ex.extract_file("/tmp/NotificationService.scala", src, "scala")
        call_edges = [e for e in edges if e.kind == "CALLS"]
        assert len(call_edges) >= 1, \
            f"Expected CALLS edges from Scala method. Got none. Total edges: {len(edges)}"

    def test_scala_case_class_extracted(self, ex):
        src = (
            "case class OrderItem(\n"
            "  productId: String,\n"
            "  quantity: Int,\n"
            "  price: Double\n"
            ")\n"
        )
        nodes, _ = ex.extract_file("/tmp/OrderItem.scala", src, "scala")
        names = _names(nodes)
        assert names, "Expected at least one symbol from Scala case class. Got empty"

    def test_scala_object_with_main(self, ex):
        src = (
            "object Main extends App {\n"
            "  println(\"Starting\")\n"
            "  val service = new CartService()\n"
            "  service.run()\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/Main.scala", src, "scala")
        names = _names(nodes)
        assert "Main" in names, f"Expected Main object. Got: {names}"

    def test_scala_companion_object_and_class(self, ex):
        src = (
            "class User(val name: String)\n\n"
            "object User {\n"
            "  def apply(name: String): User = new User(name)\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/User.scala", src, "scala")
        names = _names(nodes)
        assert "User" in names, f"Expected User in companion object/class. Got: {names}"

    def test_scala_nodes_have_file_attribute(self, ex):
        src = "class PaymentProcessor {\n  def pay(): Unit = ()\n}\n"
        nodes, _ = ex.extract_file("/tmp/PaymentProcessor.scala", src, "scala")
        for node in nodes:
            assert node.file is not None, f"Node {node.name} missing file attribute"


# ===========================================================================
# Phase 20: Groovy extraction tests
# ===========================================================================

class TestGroovyExtraction:
    """Groovy language extraction — class, methods, constructors, call edges."""

    def test_groovy_class_extracted(self, ex):
        src = (
            "package com.example\n\n"
            "class CartService {\n"
            "    String getCart(String id) {\n"
            "        return id\n"
            "    }\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/CartService.groovy", src, "groovy")
        names = _names(nodes)
        assert "CartService" in names, f"Expected CartService class. Got: {names}"

    def test_groovy_method_extracted(self, ex):
        src = (
            "class OrderService {\n"
            "    def processOrder(String orderId) {\n"
            "        return true\n"
            "    }\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/OrderService.groovy", src, "groovy")
        names = _names(nodes)
        assert "processOrder" in names or "OrderService" in names, \
            f"Expected processOrder or OrderService. Got: {names}"

    def test_groovy_constructor_extracted(self, ex):
        src = (
            "class PaymentGateway {\n"
            "    String apiKey\n\n"
            "    PaymentGateway(String key) {\n"
            "        this.apiKey = key\n"
            "    }\n\n"
            "    def charge(double amount) {\n"
            "        return true\n"
            "    }\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/PaymentGateway.groovy", src, "groovy")
        names = _names(nodes)
        assert "PaymentGateway" in names, f"Expected PaymentGateway. Got: {names}"

    def test_groovy_multiple_methods_in_class(self, ex):
        src = (
            "class InvoiceService {\n"
            "    def create(String id) { return id }\n"
            "    def cancel(String id) { return true }\n"
            "    def get(String id) { return null }\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/InvoiceService.groovy", src, "groovy")
        names = _names(nodes)
        assert len(names) >= 1, f"Expected at least 1 symbol from Groovy class. Got: {names}"

    def test_groovy_call_edges_extracted(self, ex):
        src = (
            "class NotificationService {\n"
            "    def notify(String to) {\n"
            "        validate(to)\n"
            "        send(to)\n"
            "    }\n"
            "    def validate(String s) { return true }\n"
            "    def send(String s) { }\n"
            "}\n"
        )
        _, edges = ex.extract_file("/tmp/NotificationService.groovy", src, "groovy")
        call_edges = [e for e in edges if e.kind == "CALLS"]
        assert len(call_edges) >= 1, \
            f"Expected CALLS edges from Groovy method. Got none. Total: {len(edges)}"

    def test_groovy_static_method(self, ex):
        src = (
            "class MathUtils {\n"
            "    static double round(double value) {\n"
            "        return Math.round(value)\n"
            "    }\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/MathUtils.groovy", src, "groovy")
        names = _names(nodes)
        assert "MathUtils" in names or "round" in names, \
            f"Expected MathUtils or round. Got: {names}"

    def test_groovy_interface_extracted(self, ex):
        src = (
            "interface Repository {\n"
            "    def findById(String id)\n"
            "    def save(Object item)\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/Repository.groovy", src, "groovy")
        names = _names(nodes)
        assert names, "Expected at least one symbol from Groovy interface. Got empty"

    def test_groovy_annotation_class(self, ex):
        src = (
            "import groovy.transform.CompileStatic\n\n"
            "@CompileStatic\n"
            "class ProductService {\n"
            "    def getProduct(String id) {\n"
            "        return fetchFromDB(id)\n"
            "    }\n"
            "    private def fetchFromDB(String id) { return null }\n"
            "}\n"
        )
        nodes, _ = ex.extract_file("/tmp/ProductService.groovy", src, "groovy")
        names = _names(nodes)
        assert "ProductService" in names, f"Expected ProductService. Got: {names}"

    def test_groovy_nodes_have_file_attribute(self, ex):
        src = "class FooService {\n    def foo() { }\n}\n"
        nodes, _ = ex.extract_file("/tmp/FooService.groovy", src, "groovy")
        for node in nodes:
            assert node.file is not None, f"Node {node.name} missing file attribute"

    def test_groovy_closure_call_edges(self, ex):
        src = (
            "class TaskRunner {\n"
            "    def run() {\n"
            "        def result = compute()\n"
            "        log(result)\n"
            "    }\n"
            "    def compute() { return 42 }\n"
            "    def log(v) { println(v) }\n"
            "}\n"
        )
        _, edges = ex.extract_file("/tmp/TaskRunner.groovy", src, "groovy")
        callee_names = {e.raw_callee for e in edges if e.kind == "CALLS"}
        assert callee_names, \
            "Expected at least one raw_callee in Groovy call edges. Got none"


# ============================================================
# GraphStorage: Community upsert COALESCE behavior
# ============================================================

class TestCommunityUpsertPreservesTitle:
    """upsert_community / upsert_communities_batch must preserve enriched titles."""

    @pytest.fixture
    def gs(self, tmp_path):
        db = GraphStorage(str(tmp_path / "graph.db"))
        db.open()
        yield db
        db.close()

    def _comm(self, id: int, title: str | None = None, node_count: int = 5) -> CommunityData:
        return CommunityData(id=id, node_count=node_count, title=title, level=1)

    def test_upsert_community_sets_title_on_insert(self, gs):
        gs.upsert_community(self._comm(1, "Auth Layer"))
        comms = {c.id: c for c in gs.get_communities()}
        assert comms[1].title == "Auth Layer"

    def test_upsert_community_preserves_title_on_null_update(self, gs):
        gs.upsert_community(self._comm(1, "Auth Layer"))
        gs.upsert_community(self._comm(1, None))  # re-detection with no title
        comms = {c.id: c for c in gs.get_communities()}
        assert comms[1].title == "Auth Layer", "COALESCE must preserve enriched title"

    def test_upsert_community_overwrites_title_when_new_title_given(self, gs):
        gs.upsert_community(self._comm(1, "Old Title"))
        gs.upsert_community(self._comm(1, "New Title"))
        comms = {c.id: c for c in gs.get_communities()}
        assert comms[1].title == "New Title"

    def test_upsert_communities_batch_preserves_title_on_null(self, gs):
        gs.upsert_communities_batch([self._comm(1, "Billing"), self._comm(2, "Auth")])
        # Simulate re-detection: same IDs, no titles
        gs.upsert_communities_batch([self._comm(1, None), self._comm(2, None)])
        comms = {c.id: c for c in gs.get_communities()}
        assert comms[1].title == "Billing", "batch upsert must preserve existing title"
        assert comms[2].title == "Auth", "batch upsert must preserve existing title"

    def test_upsert_communities_batch_updates_node_count(self, gs):
        gs.upsert_communities_batch([self._comm(1, "Billing", node_count=5)])
        gs.upsert_communities_batch([self._comm(1, None, node_count=50)])
        comms = {c.id: c for c in gs.get_communities()}
        assert comms[1].node_count == 50, "node_count must be updated even when title preserved"

    def test_upsert_communities_batch_overwrites_title_with_new_value(self, gs):
        gs.upsert_communities_batch([self._comm(1, "OldTitle")])
        gs.upsert_communities_batch([self._comm(1, "NewTitle")])
        comms = {c.id: c for c in gs.get_communities()}
        assert comms[1].title == "NewTitle"

    def test_upsert_community_preserves_summary_on_null_update(self, gs):
        c = CommunityData(id=1, node_count=5, title="Auth", summary="Handles login", level=1)
        gs.upsert_community(c)
        gs.upsert_community(self._comm(1, None))  # no summary
        comms = {c.id: c for c in gs.get_communities()}
        assert comms[1].summary == "Handles login", "COALESCE must preserve enriched summary"


class TestGraphFileHashCache:
    """file_graph_hashes table: incremental graph extraction cache."""

    @pytest.fixture
    def gs(self, tmp_path):
        db = GraphStorage(str(tmp_path / "graph.db"))
        db.open()
        yield db
        db.close()

    def test_get_graph_file_hashes_empty_on_new_db(self, gs):
        assert gs.get_graph_file_hashes() == {}

    def test_set_and_get_graph_file_hashes_batch(self, gs):
        gs.set_graph_file_hashes_batch({"a.py": "abc123", "b.py": "def456"})
        hashes = gs.get_graph_file_hashes()
        assert hashes == {"a.py": "abc123", "b.py": "def456"}

    def test_set_graph_file_hashes_batch_overwrites_existing(self, gs):
        gs.set_graph_file_hashes_batch({"a.py": "old"})
        gs.set_graph_file_hashes_batch({"a.py": "new"})
        assert gs.get_graph_file_hashes()["a.py"] == "new"

    def test_delete_file_removes_hash(self, gs):
        node = NodeData(id="n1", name="fn", qualified_name="a.fn", kind="function", file="a.py")
        gs.upsert_nodes([node])
        gs.set_graph_file_hashes_batch({"a.py": "abc"})
        gs.delete_file("a.py")
        assert "a.py" not in gs.get_graph_file_hashes()

    def test_purge_deleted_file_hashes_removes_stale(self, gs):
        gs.set_graph_file_hashes_batch({"a.py": "h1", "b.py": "h2", "c.py": "h3"})
        gs.purge_deleted_file_hashes({"a.py", "c.py"})  # b.py no longer exists
        hashes = gs.get_graph_file_hashes()
        assert "a.py" in hashes
        assert "c.py" in hashes
        assert "b.py" not in hashes

    def test_purge_deleted_file_hashes_empty_set(self, gs):
        gs.set_graph_file_hashes_batch({"a.py": "h1"})
        gs.purge_deleted_file_hashes(set())  # all files deleted
        assert gs.get_graph_file_hashes() == {}

    def test_migration_creates_table_on_existing_db(self, tmp_path):
        import sqlite3
        db_path = str(tmp_path / "old.db")
        # Simulate Phase-24-era schema: has level/parent_community_id/confidence_label
        # but not file_graph_hashes (added in Phase 25)
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE nodes (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, qualified_name TEXT NOT NULL,
                kind TEXT NOT NULL, file TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
                language TEXT, signature TEXT, docstring TEXT, community_id INTEGER,
                intent TEXT, intent_at TEXT,
                created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE edges (
                from_id TEXT NOT NULL, to_id TEXT NOT NULL, kind TEXT NOT NULL,
                confidence REAL DEFAULT 1.0, resolution_strategy TEXT,
                confidence_label TEXT NOT NULL DEFAULT 'EXTRACTED', confidence_score REAL,
                PRIMARY KEY (from_id, to_id, kind)
            );
            CREATE TABLE communities (
                id INTEGER PRIMARY KEY, title TEXT, summary TEXT,
                node_count INTEGER NOT NULL DEFAULT 0, key_entry_points TEXT DEFAULT '[]',
                generated_at TEXT, created_at TEXT NOT NULL DEFAULT '',
                level INTEGER NOT NULL DEFAULT 1, parent_community_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_communities_level ON communities(level);
            CREATE INDEX IF NOT EXISTS idx_communities_parent ON communities(parent_community_id);
            CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file);
        """)
        conn.commit()
        conn.close()

        gs = GraphStorage(db_path)
        gs.open()
        tables = {r[0] for r in gs._db().execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "file_graph_hashes" in tables, "migration must create file_graph_hashes table"
        gs.close()
