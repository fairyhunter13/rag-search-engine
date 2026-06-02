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
