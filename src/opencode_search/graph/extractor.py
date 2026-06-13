"""Tree-sitter AST extractor: emits NodeData + unresolved EdgeData.

Tier-1 (dedicated extractors with call-edge tracking):
    Python, TypeScript, JavaScript, Go, Java, Kotlin, Rust, Protobuf, C, C++

Tier-2 (generic AST traversal, functions + classes, no call edges):
    Scala, Ruby, PHP, Swift, C#, Dart, Lua, Elixir, Bash, R, Zig,
    Haskell, SQL, Groovy, Perl, OCaml — and any other tree-sitter-supported
    language added to _DEEP_LANGS.

Unsupported extensions → file-level node only (no symbols).

All edges produced here have to_id=<raw callee string> (pre-resolution).
CallResolver in resolver.py maps them to real node IDs.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from opencode_search.discover import LANG_TO_GRAMMAR as _LANG_TO_GRAMMAR
from opencode_search.discover import LANGUAGE_MAP as _DISC_LANG_MAP

log = logging.getLogger(__name__)

# Languages with deep extraction (specific or generic AST traversal via tree-sitter)
_DEEP_LANGS: set[str] = {
    # Tier-1: dedicated extractors
    "python", "typescript", "javascript", "go",
    "java", "kotlin", "rust", "proto", "c", "cpp",
    # Tier-2: comprehensive generic extractor (tree-sitter AST, all node kinds below)
    "scala", "ruby", "php", "swift", "c_sharp", "dart",
    "lua", "elixir", "bash", "r", "zig", "haskell",
    "sql", "groovy", "perl", "ocaml",
}

# tsx files use the TypeScript grammar; csharp uses the c_sharp tree-sitter spelling
_GRAMMAR_OVERRIDES: dict[str, str] = {
    "tsx": "typescript",
    "csharp": "c_sharp",
}

# Derived from discover.LANGUAGE_MAP + LANG_TO_GRAMMAR — single source of truth for ext → grammar
_EXT_TO_LANG: dict[str, str] = {
    ext: _GRAMMAR_OVERRIDES.get(grammar, grammar)
    for ext, lang in _DISC_LANG_MAP.items()
    if (grammar := _LANG_TO_GRAMMAR.get(lang)) is not None
}


def language_for_file(file_path: str) -> str | None:
    """Return the tree-sitter language name for a file, or None if unsupported."""
    from pathlib import Path
    suffix = Path(file_path).suffix.lower()
    return _EXT_TO_LANG.get(suffix)


def _node_id(file_path: str, qualified_name: str) -> str:
    """Stable 16-char hex ID for a node."""
    raw = f"{file_path}::{qualified_name}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class _RawEdge:
    """Pre-resolution edge — to_id holds the raw callee string."""
    from_id: str
    raw_callee: str
    kind: str  # CALLS|IMPORTS|INHERITS


class GraphExtractor:
    """Extract structural nodes and unresolved call edges from source files."""

    def extract_file(
        self,
        file_path: str,
        content: str,
        language: str | None = None,
    ) -> tuple[list[Any], list[_RawEdge]]:
        """Parse one file. Returns (NodeData list, _RawEdge list).

        NodeData is imported from storage; raw edges have .raw_callee instead of .to_id.
        Caller (CallResolver) converts raw edges to real EdgeData.
        """
        from .storage import NodeData

        if language is None:
            language = language_for_file(file_path)

        # Always emit a file-level node
        file_node_id = _node_id(file_path, file_path)
        now = _now()
        file_node = NodeData(
            id=file_node_id,
            name=_basename(file_path),
            qualified_name=file_path,
            kind="file",
            file=file_path,
            language=language,
            created_at=now,
            updated_at=now,
        )

        if language not in _DEEP_LANGS or not content.strip():
            return [file_node], []

        try:
            return self._extract_deep(file_path, content, language, file_node, now)
        except Exception as exc:
            log.debug("graph extractor error in %s: %s", file_path, exc)
            return [file_node], []

    def _extract_deep(
        self,
        file_path: str,
        content: str,
        language: str,
        file_node: Any,
        now: str,
    ) -> tuple[list[Any], list[_RawEdge]]:
        from tree_sitter_language_pack.api import get_parser

        parser = get_parser(language)
        tree = parser.parse(content)
        if tree is None:
            return [file_node], []
        root = tree.root_node()
        src = content.encode()

        if language == "python":
            return _extract_python(file_path, src, root, file_node, now)
        elif language in ("typescript", "javascript"):
            return _extract_ts_js(file_path, src, root, file_node, now)
        elif language == "go":
            return _extract_go(file_path, src, root, file_node, now)
        elif language == "java":
            return _extract_java(file_path, src, root, file_node, now)
        elif language == "kotlin":
            return _extract_kotlin(file_path, src, root, file_node, now)
        elif language == "rust":
            return _extract_rust(file_path, src, root, file_node, now)
        elif language == "proto":
            return _extract_proto(file_path, src, root, file_node, now)
        elif language in ("c", "cpp"):
            return _extract_c_cpp(file_path, src, root, file_node, now)
        elif language == "ruby":
            return _extract_ruby(file_path, src, root, file_node, now)
        elif language == "php":
            return _extract_php(file_path, src, root, file_node, now)
        elif language == "swift":
            return _extract_swift(file_path, src, root, file_node, now)
        elif language == "c_sharp":
            return _extract_c_sharp(file_path, src, root, file_node, now)
        elif language == "scala":
            return _extract_scala(file_path, src, root, file_node, now)
        elif language == "groovy":
            return _extract_groovy(file_path, src, root, file_node, now)
        else:
            return _extract_generic(file_path, src, root, file_node, now, language=language)


# ------------------------------------------------------------------
# Text helpers
# ------------------------------------------------------------------

def _text(src: bytes, node: Any) -> str:
    return src[node.start_byte():node.end_byte()].decode("utf-8", errors="replace")


def _lineno(node: Any) -> int:
    return node.start_position().row + 1  # 1-based


def _endlineno(node: Any) -> int:
    return node.end_position().row + 1


def _basename(file_path: str) -> str:
    from pathlib import Path
    return Path(file_path).stem


def _now() -> str:
    from datetime import datetime
    return datetime.now(UTC).isoformat()


def _extract_docstring(src: bytes, body_node: Any) -> str | None:
    """Return first string literal in a block as docstring."""
    if body_node is None:
        return None
    # Python: block → first expression_statement → string
    # We look at the first named child of any kind
    for i in range(min(2, body_node.named_child_count())):
        child = body_node.named_child(i)
        kind = child.kind()
        if kind == "string":
            raw = _text(src, child).strip()
            return _strip_quotes(raw)
        if kind == "expression_statement":
            inner = child.named_child(0) if child.named_child_count() > 0 else None
            if inner and inner.kind() == "string":
                raw = _text(src, inner).strip()
                return _strip_quotes(raw)
    return None


def _strip_quotes(s: str) -> str:
    for q in ('"""', "'''", '"', "'"):
        if s.startswith(q) and s.endswith(q) and len(s) >= 2 * len(q):
            return s[len(q): -len(q)].strip()
    return s


# ------------------------------------------------------------------
# Python extraction
# ------------------------------------------------------------------

def _extract_python(
    file_path: str,
    src: bytes,
    root: Any,
    file_node: Any,
    now: str,
) -> tuple[list[Any], list[_RawEdge]]:
    from .storage import NodeData

    nodes: list[NodeData] = [file_node]
    raw_edges: list[_RawEdge] = []
    module_name = _basename(file_path)

    # Pass 1: collect imports for context (not stored as nodes, used in resolver)
    imports: list[tuple[str, str]] = []  # (alias, qualified)
    _collect_py_imports(src, root, imports)

    # Emit file-level IMPORTS edges
    for _alias, qualified in imports:
        raw_edges.append(_RawEdge(
            from_id=file_node.id,
            raw_callee=qualified,
            kind="IMPORTS",
        ))

    # Pass 2: walk functions and classes
    _walk_py_node(file_path, src, root, module_name, None, None, nodes, raw_edges, now)

    return nodes, raw_edges


def _collect_py_imports(src: bytes, node: Any, imports: list[tuple[str, str]]) -> None:
    kind = node.kind()
    if kind == "import_statement":
        # import foo.bar, import foo as f
        for i in range(node.named_child_count()):
            child = node.named_child(i)
            if child.kind() in ("dotted_name", "identifier"):
                qualified = _text(src, child)
                imports.append((qualified.split(".")[-1], qualified))
            elif child.kind() == "aliased_import":
                name_node = child.child_by_field_name("name")
                alias_node = child.child_by_field_name("alias")
                qualified = _text(src, name_node) if name_node else ""
                alias = _text(src, alias_node) if alias_node else qualified.split(".")[-1]
                imports.append((alias, qualified))
    elif kind == "import_from_statement":
        mod_node = node.child_by_field_name("module_name")
        module = _text(src, mod_node) if mod_node else ""
        for i in range(node.named_child_count()):
            child = node.named_child(i)
            if child.kind() == "dotted_name" and child is not mod_node:
                name = _text(src, child)
                imports.append((name, f"{module}.{name}" if module else name))
            elif child.kind() == "identifier":
                if child is not mod_node:
                    name = _text(src, child)
                    imports.append((name, f"{module}.{name}" if module else name))
            elif child.kind() == "aliased_import":
                name_node = child.child_by_field_name("name")
                alias_node = child.child_by_field_name("alias")
                name = _text(src, name_node) if name_node else ""
                alias = _text(src, alias_node) if alias_node else name
                qualified = f"{module}.{name}" if module else name
                imports.append((alias, qualified))
    else:
        for i in range(node.named_child_count()):
            _collect_py_imports(src, node.named_child(i), imports)


def _walk_py_node(
    file_path: str,
    src: bytes,
    node: Any,
    module_name: str,
    class_name: str | None,
    parent_id: str | None,
    nodes: list[Any],
    raw_edges: list[_RawEdge],
    now: str,
) -> None:
    from .storage import NodeData

    kind = node.kind()

    if kind in ("function_definition", "async_function_definition"):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _text(src, name_node)
        # Skip nested functions (too noisy) — only capture top-level or class-level
        # We track depth via class_name presence
        qualified = f"{module_name}.{class_name}.{name}" if class_name else f"{module_name}.{name}"
        node_id = _node_id(file_path, qualified)
        sym_kind = "method" if class_name else "function"

        body_node = node.child_by_field_name("body")
        docstring = _extract_docstring(src, body_node)

        # Build signature
        params_node = node.child_by_field_name("parameters")
        ret_node = node.child_by_field_name("return_type")
        sig_parts = [name]
        if params_node:
            sig_parts.append(_text(src, params_node))
        if ret_node:
            sig_parts.append(" -> " + _text(src, ret_node))
        signature = "".join(sig_parts[:2]) + (sig_parts[2] if len(sig_parts) > 2 else "")

        n = NodeData(
            id=node_id,
            name=name,
            qualified_name=qualified,
            kind=sym_kind,
            file=file_path,
            start_line=_lineno(node),
            end_line=_endlineno(node),
            language="python",
            signature=signature,
            docstring=docstring,
            created_at=now,
            updated_at=now,
        )
        nodes.append(n)

        # Collect calls inside body
        if body_node:
            _collect_py_calls(src, body_node, node_id, raw_edges)
        return  # Don't recurse into nested functions

    elif kind == "class_definition":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _text(src, name_node)
        qualified = f"{module_name}.{name}"
        node_id = _node_id(file_path, qualified)

        body_node = node.child_by_field_name("body")
        docstring = _extract_docstring(src, body_node)

        # Collect base classes for INHERITS
        bases_node = node.child_by_field_name("superclasses")
        if bases_node:
            for i in range(bases_node.named_child_count()):
                base_child = bases_node.named_child(i)
                base_name = _text(src, base_child)
                raw_edges.append(_RawEdge(
                    from_id=node_id, raw_callee=base_name, kind="INHERITS"
                ))

        n = NodeData(
            id=node_id,
            name=name,
            qualified_name=qualified,
            kind="class",
            file=file_path,
            start_line=_lineno(node),
            end_line=_endlineno(node),
            language="python",
            docstring=docstring,
            created_at=now,
            updated_at=now,
        )
        nodes.append(n)

        # Recurse into class body for methods
        if body_node:
            for i in range(body_node.named_child_count()):
                _walk_py_node(
                    file_path, src, body_node.named_child(i),
                    module_name, name, node_id, nodes, raw_edges, now,
                )
        return

    # Recurse into other nodes (module level only — no deep nesting)
    if class_name is None:
        for i in range(node.named_child_count()):
            _walk_py_node(
                file_path, src, node.named_child(i),
                module_name, class_name, parent_id, nodes, raw_edges, now,
            )


def _collect_py_calls(src: bytes, node: Any, from_id: str, raw_edges: list[_RawEdge]) -> None:
    kind = node.kind()
    if kind == "call":
        func_node = node.child_by_field_name("function")
        if func_node:
            callee = _text(src, func_node)
            # Skip obvious builtins
            if callee not in ("print", "len", "range", "isinstance", "type",
                              "str", "int", "float", "list", "dict", "set",
                              "super", "hasattr", "getattr", "setattr"):
                raw_edges.append(_RawEdge(from_id=from_id, raw_callee=callee, kind="CALLS"))
    for i in range(node.named_child_count()):
        _collect_py_calls(src, node.named_child(i), from_id, raw_edges)


# ------------------------------------------------------------------
# TypeScript / JavaScript extraction
# ------------------------------------------------------------------

def _extract_ts_js(
    file_path: str,
    src: bytes,
    root: Any,
    file_node: Any,
    now: str,
) -> tuple[list[Any], list[_RawEdge]]:
    from .storage import NodeData

    nodes: list[NodeData] = [file_node]
    raw_edges: list[_RawEdge] = []
    module_name = _basename(file_path)
    lang = "typescript" if file_path.endswith((".ts", ".tsx")) else "javascript"

    _walk_ts_node(file_path, src, root, module_name, None, nodes, raw_edges, now, lang)
    return nodes, raw_edges


def _walk_ts_node(
    file_path: str,
    src: bytes,
    node: Any,
    module_name: str,
    class_name: str | None,
    nodes: list[Any],
    raw_edges: list[_RawEdge],
    now: str,
    lang: str,
) -> None:
    from .storage import NodeData

    kind = node.kind()

    if kind in ("function_declaration", "generator_function_declaration"):
        name_node = node.child_by_field_name("name")
        if name_node:
            name = _text(src, name_node)
            qualified = f"{module_name}.{class_name}.{name}" if class_name else f"{module_name}.{name}"
            node_id = _node_id(file_path, qualified)
            body = node.child_by_field_name("body")
            nodes.append(NodeData(
                id=node_id, name=name, qualified_name=qualified,
                kind="method" if class_name else "function",
                file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                language=lang, created_at=now, updated_at=now,
            ))
            if body:
                _collect_ts_calls(src, body, node_id, raw_edges)
            return

    elif kind == "lexical_declaration":
        # const/let foo = () => {} or function expression
        _handle_ts_variable_declaration(
            file_path, src, node, module_name, class_name, nodes, raw_edges, now, lang,
        )
        return

    elif kind in ("method_definition", "function_signature"):
        name_node = node.child_by_field_name("name")
        if name_node:
            name = _text(src, name_node)
            qualified = f"{module_name}.{class_name}.{name}" if class_name else f"{module_name}.{name}"
            node_id = _node_id(file_path, qualified)
            body = node.child_by_field_name("body")
            nodes.append(NodeData(
                id=node_id, name=name, qualified_name=qualified,
                kind="method",
                file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                language=lang, created_at=now, updated_at=now,
            ))
            if body:
                _collect_ts_calls(src, body, node_id, raw_edges)
            return

    elif kind == "class_declaration":
        name_node = node.child_by_field_name("name")
        if name_node:
            name = _text(src, name_node)
            qualified = f"{module_name}.{name}"
            node_id = _node_id(file_path, qualified)
            nodes.append(NodeData(
                id=node_id, name=name, qualified_name=qualified,
                kind="class",
                file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                language=lang, created_at=now, updated_at=now,
            ))
            body = node.child_by_field_name("body")
            if body:
                for i in range(body.named_child_count()):
                    _walk_ts_node(
                        file_path, src, body.named_child(i),
                        module_name, name, nodes, raw_edges, now, lang,
                    )
            return

    elif kind == "import_statement":
        src_node = node.child_by_field_name("source")
        if src_node:
            mod = _text(src, src_node).strip("'\"")
            raw_edges.append(_RawEdge(
                from_id=_node_id(file_path, file_path), raw_callee=mod, kind="IMPORTS",
            ))

    # Recurse
    for i in range(node.named_child_count()):
        _walk_ts_node(
            file_path, src, node.named_child(i),
            module_name, class_name, nodes, raw_edges, now, lang,
        )


def _handle_ts_variable_declaration(
    file_path: str, src: bytes, node: Any, module_name: str, class_name: str | None,
    nodes: list[Any], raw_edges: list[_RawEdge], now: str, lang: str,
) -> None:
    from .storage import NodeData
    for i in range(node.named_child_count()):
        decl = node.named_child(i)
        if decl.kind() != "variable_declarator":
            continue
        name_node = decl.child_by_field_name("name")
        val_node = decl.child_by_field_name("value")
        if name_node and val_node and val_node.kind() in (
            "arrow_function", "function", "function_expression",
        ):
            name = _text(src, name_node).strip()
            qualified = f"{module_name}.{class_name}.{name}" if class_name else f"{module_name}.{name}"
            node_id = _node_id(file_path, qualified)
            body = val_node.child_by_field_name("body")
            nodes.append(NodeData(
                id=node_id, name=name, qualified_name=qualified,
                kind="function",
                file=file_path, start_line=_lineno(val_node), end_line=_endlineno(val_node),
                language=lang, created_at=now, updated_at=now,
            ))
            if body:
                _collect_ts_calls(src, body, node_id, raw_edges)


def _collect_ts_calls(src: bytes, node: Any, from_id: str, raw_edges: list[_RawEdge]) -> None:
    kind = node.kind()
    if kind == "call_expression":
        func_node = node.child_by_field_name("function")
        if func_node:
            callee = _text(src, func_node)
            raw_edges.append(_RawEdge(from_id=from_id, raw_callee=callee, kind="CALLS"))
    for i in range(node.named_child_count()):
        _collect_ts_calls(src, node.named_child(i), from_id, raw_edges)


# ------------------------------------------------------------------
# Go extraction
# ------------------------------------------------------------------

def _extract_go(
    file_path: str,
    src: bytes,
    root: Any,
    file_node: Any,
    now: str,
) -> tuple[list[Any], list[_RawEdge]]:
    from .storage import NodeData

    nodes: list[NodeData] = [file_node]
    raw_edges: list[_RawEdge] = []

    # Determine package name
    pkg_name = _basename(file_path)
    for i in range(root.named_child_count()):
        child = root.named_child(i)
        if child.kind() == "package_clause":
            name_node = child.named_child(0)
            if name_node:
                pkg_name = _text(src, name_node)
            break

    for i in range(root.named_child_count()):
        child = root.named_child(i)
        kind = child.kind()

        if kind == "function_declaration":
            name_node = child.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node)
                qualified = f"{pkg_name}.{name}"
                node_id = _node_id(file_path, qualified)
                body = child.child_by_field_name("body")
                nodes.append(NodeData(
                    id=node_id, name=name, qualified_name=qualified, kind="function",
                    file=file_path, start_line=_lineno(child), end_line=_endlineno(child),
                    language="go", created_at=now, updated_at=now,
                ))
                if body:
                    _collect_go_calls(src, body, node_id, raw_edges)

        elif kind == "method_declaration":
            name_node = child.child_by_field_name("name")
            recv_node = child.child_by_field_name("receiver")
            recv_type = ""
            if recv_node:
                # Extract receiver type name
                for j in range(recv_node.named_child_count()):
                    param = recv_node.named_child(j)
                    type_node = param.child_by_field_name("type")
                    if type_node:
                        recv_type = _text(src, type_node).lstrip("*")
                        break
            if name_node:
                name = _text(src, name_node)
                qualified = f"{pkg_name}.{recv_type}.{name}" if recv_type else f"{pkg_name}.{name}"
                node_id = _node_id(file_path, qualified)
                body = child.child_by_field_name("body")
                nodes.append(NodeData(
                    id=node_id, name=name, qualified_name=qualified, kind="method",
                    file=file_path, start_line=_lineno(child), end_line=_endlineno(child),
                    language="go", created_at=now, updated_at=now,
                ))
                if body:
                    _collect_go_calls(src, body, node_id, raw_edges)

        elif kind == "import_declaration":
            for j in range(child.named_child_count()):
                spec = child.named_child(j)
                if spec.kind() in ("import_spec", "interpreted_string_literal"):
                    path_node = spec.child_by_field_name("path") or spec
                    if path_node:
                        mod = _text(src, path_node).strip('"')
                        raw_edges.append(_RawEdge(
                            from_id=file_node.id, raw_callee=mod, kind="IMPORTS",
                        ))

    return nodes, raw_edges


def _collect_go_calls(src: bytes, node: Any, from_id: str, raw_edges: list[_RawEdge]) -> None:
    kind = node.kind()
    if kind == "call_expression":
        func_node = node.child_by_field_name("function")
        if func_node:
            callee = _text(src, func_node)
            raw_edges.append(_RawEdge(from_id=from_id, raw_callee=callee, kind="CALLS"))
    for i in range(node.named_child_count()):
        _collect_go_calls(src, node.named_child(i), from_id, raw_edges)


# ------------------------------------------------------------------
# Java extractor
# ------------------------------------------------------------------

def _extract_java(
    file_path: str,
    src: bytes,
    root: Any,
    file_node: Any,
    now: str,
) -> tuple[list[Any], list[_RawEdge]]:
    """Extract class/interface/method/import nodes from Java source."""
    from .storage import NodeData

    nodes: list[NodeData] = [file_node]
    raw_edges: list[_RawEdge] = []

    # Derive package/class context from file path
    pkg_name = _basename(file_path).replace(".java", "")

    def _walk_java(node: Any, class_ctx: str | None = None) -> None:
        kind = node.kind()

        if kind in ("import_declaration",):
            # import com.example.Class;
            path_node = node.named_child(0) if node.named_child_count() > 0 else None
            if path_node:
                raw_edges.append(_RawEdge(
                    from_id=file_node.id,
                    raw_callee=_text(src, path_node).rstrip(";").strip(),
                    kind="IMPORTS",
                ))

        elif kind in ("class_declaration", "interface_declaration", "enum_declaration",
                      "annotation_type_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node)
                qualified = f"{class_ctx}.{name}" if class_ctx else f"{pkg_name}.{name}"
                node_kind = "interface" if kind == "interface_declaration" else "class"
                nid = _node_id(file_path, qualified)
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified, kind=node_kind,
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="java", created_at=now, updated_at=now,
                ))
                # Walk body with this class as context
                body = node.child_by_field_name("body") or node
                for i in range(body.named_child_count()):
                    _walk_java(body.named_child(i), qualified)
                return

        elif kind in ("method_declaration", "constructor_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node)
                qualified = f"{class_ctx}.{name}" if class_ctx else f"{pkg_name}.{name}"
                nid = _node_id(file_path, qualified)
                node_kind = "constructor" if kind == "constructor_declaration" else "method"
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified, kind=node_kind,
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="java", created_at=now, updated_at=now,
                ))
                # Collect method invocations inside this method
                body = node.child_by_field_name("body")
                if body:
                    _collect_java_calls(src, body, nid, raw_edges)
                return

        for i in range(node.named_child_count()):
            _walk_java(node.named_child(i), class_ctx)

    _walk_java(root)
    return nodes, raw_edges


def _collect_java_calls(src: bytes, node: Any, from_id: str, raw_edges: list[_RawEdge]) -> None:
    kind = node.kind()
    if kind == "method_invocation":
        name_node = node.child_by_field_name("name")
        obj_node = node.child_by_field_name("object")
        if name_node:
            method_name = _text(src, name_node)
            obj = _text(src, obj_node) if obj_node else ""
            callee = f"{obj}.{method_name}" if obj else method_name
            raw_edges.append(_RawEdge(from_id=from_id, raw_callee=callee, kind="CALLS"))
    for i in range(node.named_child_count()):
        _collect_java_calls(src, node.named_child(i), from_id, raw_edges)


# ------------------------------------------------------------------
# Kotlin extractor
# ------------------------------------------------------------------

def _extract_kotlin(
    file_path: str,
    src: bytes,
    root: Any,
    file_node: Any,
    now: str,
) -> tuple[list[Any], list[_RawEdge]]:
    """Extract class/function/import nodes from Kotlin source."""
    from .storage import NodeData

    nodes: list[NodeData] = [file_node]
    raw_edges: list[_RawEdge] = []
    pkg_name = _basename(file_path).replace(".kt", "").replace(".kts", "")

    def _walk_kt(node: Any, class_ctx: str | None = None) -> None:
        kind = node.kind()

        if kind == "import_header":
            id_node = node.child_by_field_name("identifier") or (
                node.named_child(0) if node.named_child_count() > 0 else None
            )
            if id_node:
                raw_edges.append(_RawEdge(
                    from_id=file_node.id, raw_callee=_text(src, id_node), kind="IMPORTS",
                ))

        elif kind in ("class_declaration", "object_declaration", "interface_declaration"):
            name_node = node.child_by_field_name("name") or node.named_child(0)
            if name_node and name_node.kind() in ("simple_identifier", "type_identifier"):
                name = _text(src, name_node)
                qualified = f"{class_ctx}.{name}" if class_ctx else f"{pkg_name}.{name}"
                nid = _node_id(file_path, qualified)
                nkind = "interface" if kind == "interface_declaration" else "class"
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified, kind=nkind,
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="kotlin", created_at=now, updated_at=now,
                ))
                body = node.child_by_field_name("class_body") or node
                for i in range(body.named_child_count()):
                    _walk_kt(body.named_child(i), qualified)
                return

        elif kind in ("function_declaration", "secondary_constructor"):
            name_node = node.child_by_field_name("name") or node.named_child(0)
            if name_node and name_node.kind() == "simple_identifier":
                name = _text(src, name_node)
                qualified = f"{class_ctx}.{name}" if class_ctx else f"{pkg_name}.{name}"
                nid = _node_id(file_path, qualified)
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified,
                    kind="method" if class_ctx else "function",
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="kotlin", created_at=now, updated_at=now,
                ))
                body = node.child_by_field_name("function_body")
                if body:
                    _collect_kt_calls(src, body, nid, raw_edges)
                return

        for i in range(node.named_child_count()):
            _walk_kt(node.named_child(i), class_ctx)

    _walk_kt(root)
    return nodes, raw_edges


def _collect_kt_calls(src: bytes, node: Any, from_id: str, raw_edges: list[_RawEdge]) -> None:
    kind = node.kind()
    if kind == "call_expression":
        fn = node.child_by_field_name("calleeExpression") or node.named_child(0)
        if fn:
            raw_edges.append(_RawEdge(from_id=from_id, raw_callee=_text(src, fn), kind="CALLS"))
    for i in range(node.named_child_count()):
        _collect_kt_calls(src, node.named_child(i), from_id, raw_edges)


# ------------------------------------------------------------------
# Rust extractor
# ------------------------------------------------------------------

def _extract_rust(
    file_path: str,
    src: bytes,
    root: Any,
    file_node: Any,
    now: str,
) -> tuple[list[Any], list[_RawEdge]]:
    """Extract fn/struct/impl/trait/use nodes from Rust source."""
    from .storage import NodeData

    nodes: list[NodeData] = [file_node]
    raw_edges: list[_RawEdge] = []
    mod_name = _basename(file_path).replace(".rs", "")

    def _walk_rs(node: Any, impl_ctx: str | None = None) -> None:
        kind = node.kind()

        if kind == "use_declaration":
            tree_node = node.child_by_field_name("argument") or (
                node.named_child(0) if node.named_child_count() > 0 else None
            )
            if tree_node:
                raw_edges.append(_RawEdge(
                    from_id=file_node.id, raw_callee=_text(src, tree_node), kind="IMPORTS",
                ))

        elif kind in ("struct_item", "enum_item", "trait_item", "type_item"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node)
                qualified = f"{mod_name}::{name}"
                nkind = "class" if kind in ("struct_item", "enum_item") else "interface"
                nid = _node_id(file_path, qualified)
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified, kind=nkind,
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="rust", created_at=now, updated_at=now,
                ))

        elif kind == "impl_item":
            type_node = node.child_by_field_name("type")
            impl_type = _text(src, type_node) if type_node else impl_ctx
            body = node.child_by_field_name("body")
            if body:
                for i in range(body.named_child_count()):
                    _walk_rs(body.named_child(i), impl_type)
            return

        elif kind == "function_item":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node)
                qualified = (f"{mod_name}::{impl_ctx}::{name}" if impl_ctx
                             else f"{mod_name}::{name}")
                nid = _node_id(file_path, qualified)
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified,
                    kind="method" if impl_ctx else "function",
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="rust", created_at=now, updated_at=now,
                ))
                body = node.child_by_field_name("body")
                if body:
                    _collect_rust_calls(src, body, nid, raw_edges)
                return

        for i in range(node.named_child_count()):
            _walk_rs(node.named_child(i), impl_ctx)

    _walk_rs(root)
    return nodes, raw_edges


def _collect_rust_calls(src: bytes, node: Any, from_id: str, raw_edges: list[_RawEdge]) -> None:
    kind = node.kind()
    if kind in ("call_expression", "macro_invocation"):
        fn = node.child_by_field_name("function") or node.named_child(0)
        if fn:
            raw_edges.append(_RawEdge(from_id=from_id, raw_callee=_text(src, fn), kind="CALLS"))
    for i in range(node.named_child_count()):
        _collect_rust_calls(src, node.named_child(i), from_id, raw_edges)


# ------------------------------------------------------------------
# Protobuf extractor
# ------------------------------------------------------------------

def _extract_proto(
    file_path: str,
    src: bytes,
    root: Any,
    file_node: Any,
    now: str,
) -> tuple[list[Any], list[_RawEdge]]:
    """Extract message/service/rpc nodes from .proto files."""
    from .storage import NodeData

    nodes: list[NodeData] = [file_node]
    raw_edges: list[_RawEdge] = []
    pkg_name = _basename(file_path).replace(".proto", "")

    def _walk_proto(node: Any, svc_ctx: str | None = None) -> None:
        kind = node.kind()

        if kind == "import":
            str_node = node.named_child(0) if node.named_child_count() > 0 else None
            if str_node:
                raw_edges.append(_RawEdge(
                    from_id=file_node.id,
                    raw_callee=_text(src, str_node).strip('"'),
                    kind="IMPORTS",
                ))

        elif kind == "message":
            name_node = node.named_child(0)
            if name_node:
                name = _text(src, name_node)
                qualified = f"{pkg_name}.{name}"
                nid = _node_id(file_path, qualified)
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified, kind="class",
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="proto", created_at=now, updated_at=now,
                ))

        elif kind == "service":
            name_node = node.named_child(0)
            if name_node:
                name = _text(src, name_node)
                qualified = f"{pkg_name}.{name}"
                nid = _node_id(file_path, qualified)
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified, kind="interface",
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="proto", created_at=now, updated_at=now,
                ))
                # Walk RPCs inside service
                for i in range(node.named_child_count()):
                    _walk_proto(node.named_child(i), qualified)
                return

        elif kind == "rpc":
            name_node = node.named_child(0)
            if name_node and svc_ctx:
                name = _text(src, name_node)
                qualified = f"{svc_ctx}.{name}"
                nid = _node_id(file_path, qualified)
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified, kind="function",
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="proto", created_at=now, updated_at=now,
                ))
                # Request/response types as IMPORTS-like edges
                for i in range(node.named_child_count()):
                    child = node.named_child(i)
                    if child.kind() in ("message_type", "rpc_message_type"):
                        raw_edges.append(_RawEdge(
                            from_id=nid, raw_callee=_text(src, child), kind="IMPORTS",
                        ))

        for i in range(node.named_child_count()):
            _walk_proto(node.named_child(i), svc_ctx)

    _walk_proto(root)
    return nodes, raw_edges


# ------------------------------------------------------------------
# C / C++ extractor
# ------------------------------------------------------------------

def _extract_c_cpp(
    file_path: str,
    src: bytes,
    root: Any,
    file_node: Any,
    now: str,
) -> tuple[list[Any], list[_RawEdge]]:
    """Extract function/struct/class nodes from C/C++ source."""
    from .storage import NodeData

    nodes: list[NodeData] = [file_node]
    raw_edges: list[_RawEdge] = []
    lang = "cpp" if file_path.endswith((".cpp", ".cc", ".cxx", ".hpp")) else "c"
    mod_name = _basename(file_path).rsplit(".", 1)[0]

    def _walk_c(node: Any, class_ctx: str | None = None) -> None:
        kind = node.kind()

        if kind == "preproc_include":
            path_node = node.named_child(0) if node.named_child_count() > 0 else None
            if path_node:
                raw_edges.append(_RawEdge(
                    from_id=file_node.id,
                    raw_callee=_text(src, path_node).strip('"<>'),
                    kind="IMPORTS",
                ))

        elif kind in ("function_definition", "function_declaration"):
            declarator = node.child_by_field_name("declarator")
            if declarator:
                name_node = declarator.child_by_field_name("declarator") or declarator
                name = _text(src, name_node).split("(")[0].strip().split("::")[-1]
                if name and name.isidentifier():
                    qualified = (f"{class_ctx}::{name}" if class_ctx
                                 else f"{mod_name}::{name}")
                    nid = _node_id(file_path, qualified)
                    nodes.append(NodeData(
                        id=nid, name=name, qualified_name=qualified,
                        kind="method" if class_ctx else "function",
                        file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                        language=lang, created_at=now, updated_at=now,
                    ))
                    body = node.child_by_field_name("body")
                    if body:
                        _collect_c_calls(src, body, nid, raw_edges)
                    return

        elif kind in ("class_specifier", "struct_specifier"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node)
                qualified = f"{class_ctx}::{name}" if class_ctx else f"{mod_name}::{name}"
                nid = _node_id(file_path, qualified)
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified, kind="class",
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language=lang, created_at=now, updated_at=now,
                ))
                body = node.child_by_field_name("body")
                if body:
                    for i in range(body.named_child_count()):
                        _walk_c(body.named_child(i), qualified)
                return

        for i in range(node.named_child_count()):
            _walk_c(node.named_child(i), class_ctx)

    _walk_c(root)
    return nodes, raw_edges


def _collect_c_calls(src: bytes, node: Any, from_id: str, raw_edges: list[_RawEdge]) -> None:
    kind = node.kind()
    if kind == "call_expression":
        fn = node.child_by_field_name("function")
        if fn:
            raw_edges.append(_RawEdge(from_id=from_id, raw_callee=_text(src, fn), kind="CALLS"))
    for i in range(node.named_child_count()):
        _collect_c_calls(src, node.named_child(i), from_id, raw_edges)


# ------------------------------------------------------------------
# Ruby (Tier-1: method/singleton_method + call edges)
# ------------------------------------------------------------------

def _extract_ruby(
    file_path: str,
    src: bytes,
    root: Any,
    file_node: Any,
    now: str,
) -> tuple[list[Any], list[_RawEdge]]:
    """Extract class/module/method nodes and call edges from Ruby source."""
    from .storage import NodeData

    nodes: list[NodeData] = [file_node]
    raw_edges: list[_RawEdge] = []
    mod_name = _basename(file_path)

    def _walk_rb(node: Any, class_ctx: str | None = None) -> None:
        kind = node.kind()

        if kind in ("class", "module"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node).strip()
                qualified = f"{mod_name}::{name}"
                nid = _node_id(file_path, qualified)
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified, kind="class",
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="ruby", created_at=now, updated_at=now,
                ))
                body = node.child_by_field_name("body") or node
                for i in range(body.named_child_count()):
                    _walk_rb(body.named_child(i), qualified)
                return

        elif kind == "method":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node).strip()
                qualified = f"{class_ctx}#{name}" if class_ctx else f"{mod_name}#{name}"
                nid = _node_id(file_path, qualified)
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified,
                    kind="method" if class_ctx else "function",
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="ruby", created_at=now, updated_at=now,
                ))
                body = node.child_by_field_name("body")
                if body:
                    _collect_ruby_calls(src, body, nid, raw_edges)
                return

        elif kind == "singleton_method":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node).strip()
                qualified = f"{class_ctx}.{name}" if class_ctx else f"{mod_name}.{name}"
                nid = _node_id(file_path, qualified)
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified, kind="function",
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="ruby", created_at=now, updated_at=now,
                ))
                body = node.child_by_field_name("body")
                if body:
                    _collect_ruby_calls(src, body, nid, raw_edges)
                return

        for i in range(node.named_child_count()):
            _walk_rb(node.named_child(i), class_ctx)

    _walk_rb(root)
    return nodes, raw_edges


def _collect_ruby_calls(src: bytes, node: Any, from_id: str, raw_edges: list[_RawEdge]) -> None:
    kind = node.kind()
    if kind == "call":
        # Receiver-based call: obj.method(...)
        method_node = node.child_by_field_name("method")
        if method_node:
            raw_edges.append(_RawEdge(from_id=from_id, raw_callee=_text(src, method_node), kind="CALLS"))
    elif kind == "identifier":
        # Bare method call: validate, save, etc. — top-level identifiers in method body
        name = _text(src, node).strip()
        if name:
            raw_edges.append(_RawEdge(from_id=from_id, raw_callee=name, kind="CALLS"))
        return  # identifiers have no named children
    for i in range(node.named_child_count()):
        _collect_ruby_calls(src, node.named_child(i), from_id, raw_edges)


# ------------------------------------------------------------------
# PHP (Tier-1: function/method + call edges)
# ------------------------------------------------------------------

def _extract_php(
    file_path: str,
    src: bytes,
    root: Any,
    file_node: Any,
    now: str,
) -> tuple[list[Any], list[_RawEdge]]:
    """Extract class/interface/function nodes and call edges from PHP source."""
    from .storage import NodeData

    nodes: list[NodeData] = [file_node]
    raw_edges: list[_RawEdge] = []
    mod_name = _basename(file_path)

    def _walk_php(node: Any, class_ctx: str | None = None) -> None:
        kind = node.kind()

        if kind in ("class_declaration", "interface_declaration", "trait_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node).strip()
                qualified = f"{mod_name}\\{name}"
                nid = _node_id(file_path, qualified)
                nkind = "interface" if kind == "interface_declaration" else "class"
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified, kind=nkind,
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="php", created_at=now, updated_at=now,
                ))
                body = node.child_by_field_name("body") or node
                for i in range(body.named_child_count()):
                    _walk_php(body.named_child(i), qualified)
                return

        elif kind == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node).strip()
                qualified = f"{class_ctx}::{name}" if class_ctx else f"{mod_name}::{name}"
                nid = _node_id(file_path, qualified)
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified,
                    kind="method" if class_ctx else "function",
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="php", created_at=now, updated_at=now,
                ))
                body = node.child_by_field_name("body")
                if body:
                    _collect_php_calls(src, body, nid, raw_edges)
                return

        elif kind == "method_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node).strip()
                qualified = f"{class_ctx}::{name}" if class_ctx else f"{mod_name}::{name}"
                nid = _node_id(file_path, qualified)
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified, kind="method",
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="php", created_at=now, updated_at=now,
                ))
                body = node.child_by_field_name("body")
                if body:
                    _collect_php_calls(src, body, nid, raw_edges)
                return

        for i in range(node.named_child_count()):
            _walk_php(node.named_child(i), class_ctx)

    _walk_php(root)
    return nodes, raw_edges


def _collect_php_calls(src: bytes, node: Any, from_id: str, raw_edges: list[_RawEdge]) -> None:
    kind = node.kind()
    if kind == "function_call_expression":
        fn = node.child_by_field_name("function")
        if fn:
            raw_edges.append(_RawEdge(from_id=from_id, raw_callee=_text(src, fn), kind="CALLS"))
    elif kind == "member_call_expression":
        name_node = node.child_by_field_name("name")
        if name_node:
            raw_edges.append(_RawEdge(from_id=from_id, raw_callee=_text(src, name_node), kind="CALLS"))
    for i in range(node.named_child_count()):
        _collect_php_calls(src, node.named_child(i), from_id, raw_edges)


# ------------------------------------------------------------------
# Swift (Tier-1: function/class/struct + call edges)
# ------------------------------------------------------------------

def _extract_swift(
    file_path: str,
    src: bytes,
    root: Any,
    file_node: Any,
    now: str,
) -> tuple[list[Any], list[_RawEdge]]:
    """Extract class/struct/protocol/function nodes and call edges from Swift source."""
    from .storage import NodeData

    nodes: list[NodeData] = [file_node]
    raw_edges: list[_RawEdge] = []
    mod_name = _basename(file_path)

    def _walk_swift(node: Any, class_ctx: str | None = None) -> None:
        kind = node.kind()

        if kind in ("class_declaration", "struct_declaration", "protocol_declaration",
                    "enum_declaration", "extension_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node).strip()
                qualified = f"{class_ctx}.{name}" if class_ctx else f"{mod_name}.{name}"
                nid = _node_id(file_path, qualified)
                nkind = "interface" if kind == "protocol_declaration" else "class"
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified, kind=nkind,
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="swift", created_at=now, updated_at=now,
                ))
                body = node.child_by_field_name("body") or node
                for i in range(body.named_child_count()):
                    _walk_swift(body.named_child(i), qualified)
                return

        elif kind == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node).strip()
                qualified = f"{class_ctx}.{name}" if class_ctx else f"{mod_name}.{name}"
                nid = _node_id(file_path, qualified)
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified,
                    kind="method" if class_ctx else "function",
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="swift", created_at=now, updated_at=now,
                ))
                body = node.child_by_field_name("body")
                if body:
                    _collect_swift_calls(src, body, nid, raw_edges)
                return

        for i in range(node.named_child_count()):
            _walk_swift(node.named_child(i), class_ctx)

    _walk_swift(root)
    return nodes, raw_edges


def _collect_swift_calls(src: bytes, node: Any, from_id: str, raw_edges: list[_RawEdge]) -> None:
    kind = node.kind()
    if kind == "call_expression":
        # Swift call_expression: first named child is the callee (simple_identifier or
        # member access), second child is call_suffix with arguments.
        fn = node.child_by_field_name("function") or (
            node.named_child(0) if node.named_child_count() > 0 else None
        )
        if fn and fn.kind() not in ("call_suffix", "value_arguments"):
            raw_edges.append(_RawEdge(from_id=from_id, raw_callee=_text(src, fn), kind="CALLS"))
    for i in range(node.named_child_count()):
        _collect_swift_calls(src, node.named_child(i), from_id, raw_edges)


# ------------------------------------------------------------------
# C# (Tier-1: class/struct/method + call edges)
# ------------------------------------------------------------------

def _extract_c_sharp(
    file_path: str,
    src: bytes,
    root: Any,
    file_node: Any,
    now: str,
) -> tuple[list[Any], list[_RawEdge]]:
    """Extract class/struct/interface/method nodes and call edges from C# source."""
    from .storage import NodeData

    nodes: list[NodeData] = [file_node]
    raw_edges: list[_RawEdge] = []
    mod_name = _basename(file_path)

    def _walk_cs(node: Any, class_ctx: str | None = None) -> None:
        kind = node.kind()

        if kind in ("class_declaration", "struct_declaration", "interface_declaration",
                    "enum_declaration", "record_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node).strip()
                qualified = f"{class_ctx}.{name}" if class_ctx else f"{mod_name}.{name}"
                nid = _node_id(file_path, qualified)
                nkind = "interface" if kind == "interface_declaration" else "class"
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified, kind=nkind,
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="c_sharp", created_at=now, updated_at=now,
                ))
                body = node.child_by_field_name("body") or node
                for i in range(body.named_child_count()):
                    _walk_cs(body.named_child(i), qualified)
                return

        elif kind in ("method_declaration", "constructor_declaration", "local_function_statement"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node).strip()
                qualified = f"{class_ctx}.{name}" if class_ctx else f"{mod_name}.{name}"
                nid = _node_id(file_path, qualified)
                nkind = "constructor" if kind == "constructor_declaration" else "method"
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified, kind=nkind,
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="c_sharp", created_at=now, updated_at=now,
                ))
                body = node.child_by_field_name("body")
                if body:
                    _collect_c_sharp_calls(src, body, nid, raw_edges)
                return

        for i in range(node.named_child_count()):
            _walk_cs(node.named_child(i), class_ctx)

    _walk_cs(root)
    return nodes, raw_edges


def _collect_c_sharp_calls(src: bytes, node: Any, from_id: str, raw_edges: list[_RawEdge]) -> None:
    kind = node.kind()
    if kind == "invocation_expression":
        fn = node.child_by_field_name("function")
        if fn:
            raw_edges.append(_RawEdge(from_id=from_id, raw_callee=_text(src, fn), kind="CALLS"))
    for i in range(node.named_child_count()):
        _collect_c_sharp_calls(src, node.named_child(i), from_id, raw_edges)


# ------------------------------------------------------------------
# Generic Tier-2 extractor (Scala, Ruby, PHP, Swift, C#, Dart, Lua,
# Elixir, Bash, R, Zig, Haskell, SQL, Groovy, Perl, OCaml, …)
# ------------------------------------------------------------------

def _extract_scala(
    file_path: str,
    src: bytes,
    root: Any,
    file_node: Any,
    now: str,
) -> tuple[list[Any], list[_RawEdge]]:
    """Extract class/object/trait/def nodes and call edges from Scala source."""
    from .storage import NodeData

    nodes: list[NodeData] = [file_node]
    raw_edges: list[_RawEdge] = []
    mod_name = _basename(file_path)

    def _walk(node: Any, ctx: str | None = None) -> None:
        kind = node.kind()

        if kind in ("class_definition", "object_definition", "trait_definition"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node).strip()
                qualified = f"{ctx}.{name}" if ctx else f"{mod_name}.{name}"
                nid = _node_id(file_path, qualified)
                obj_kind = "class" if "class" in kind else ("object" if "object" in kind else "trait")
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified, kind=obj_kind,
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="scala", created_at=now, updated_at=now,
                ))
                body = node.child_by_field_name("body") or node
                for i in range(body.named_child_count()):
                    _walk(body.named_child(i), qualified)
                return

        elif kind == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node).strip()
                qualified = f"{ctx}.{name}" if ctx else f"{mod_name}.{name}"
                nid = _node_id(file_path, qualified)
                nodes.append(NodeData(
                    id=nid, name=name, qualified_name=qualified,
                    kind="method" if ctx else "function",
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language="scala", created_at=now, updated_at=now,
                ))
                body = node.child_by_field_name("body")
                if body:
                    _collect_generic_calls(src, body, nid, raw_edges)
                return

        for i in range(node.named_child_count()):
            _walk(node.named_child(i), ctx)

    _walk(root)
    return nodes, raw_edges


def _extract_groovy(
    file_path: str,
    src: bytes,
    root: Any,
    file_node: Any,
    now: str,
) -> tuple[list[Any], list[_RawEdge]]:
    """Extract class/method nodes and call edges from Groovy source.

    tree-sitter-language-pack's Groovy grammar uses command/unit/block/func nodes
    rather than class_declaration/method_declaration. This extractor maps that structure.
    """
    from .storage import NodeData

    nodes: list[NodeData] = [file_node]
    raw_edges: list[_RawEdge] = []
    mod_name = _basename(file_path)

    def _first_ident(node: Any) -> str | None:
        """Return text of the first identifier child."""
        for i in range(node.child_count()):
            child = node.child(i)
            if child.kind() == "identifier":
                return _text(src, child).strip()
        return None

    def _first_unit_ident(command_node: Any) -> str | None:
        """Return first identifier inside first unit child of a command node."""
        for i in range(command_node.child_count()):
            child = command_node.child(i)
            if child.kind() == "unit":
                return _first_ident(child)
        return None

    def _class_name_from_block(block_node: Any) -> str | None:
        """Get class name = first identifier in the first unit of a class block."""
        for i in range(block_node.child_count()):
            child = block_node.child(i)
            if child.kind() == "unit":
                return _first_ident(child)
        return None

    def _method_name_from_block(block_node: Any) -> str | None:
        """Get method name = identifier inside first func node in block's first unit."""
        for i in range(block_node.child_count()):
            child = block_node.child(i)
            if child.kind() == "unit":
                for j in range(child.child_count()):
                    func_node = child.child(j)
                    if func_node.kind() == "func":
                        return _first_ident(func_node)
        return None

    def _collect_groovy_calls(block_node: Any, from_id: str) -> None:
        """Collect call edges: command > unit > func > identifier in method body."""
        for i in range(block_node.child_count()):
            child = block_node.child(i)
            if child.kind() == "command":
                for j in range(child.child_count()):
                    unit = child.child(j)
                    if unit.kind() == "unit":
                        for k in range(unit.child_count()):
                            func_node = unit.child(k)
                            if func_node.kind() == "func":
                                callee = _first_ident(func_node)
                                if callee:
                                    raw_edges.append(
                                        _RawEdge(from_id=from_id, raw_callee=callee, kind="CALLS")
                                    )
                                break
                        break
            elif child.kind() == "block":
                _collect_groovy_calls(child, from_id)

    def _walk(node: Any, class_ctx: str | None = None) -> None:
        if node.kind() != "command":
            for i in range(node.child_count()):
                _walk(node.child(i), class_ctx)
            return

        first_kw = _first_unit_ident(node)
        if first_kw == "class":
            for i in range(node.child_count()):
                child = node.child(i)
                if child.kind() == "block":
                    class_name = _class_name_from_block(child)
                    if class_name:
                        qualified = f"{mod_name}.{class_name}"
                        nid = _node_id(file_path, qualified)
                        nodes.append(NodeData(
                            id=nid, name=class_name, qualified_name=qualified, kind="class",
                            file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                            language="groovy", created_at=now, updated_at=now,
                        ))
                        for j in range(child.child_count()):
                            _walk(child.child(j), qualified)
                    break
        elif first_kw == "def":
            for i in range(node.child_count()):
                child = node.child(i)
                if child.kind() == "block":
                    method_name = _method_name_from_block(child)
                    if method_name:
                        qualified = f"{class_ctx}.{method_name}" if class_ctx else f"{mod_name}.{method_name}"
                        nid = _node_id(file_path, qualified)
                        nodes.append(NodeData(
                            id=nid, name=method_name, qualified_name=qualified,
                            kind="method" if class_ctx else "function",
                            file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                            language="groovy", created_at=now, updated_at=now,
                        ))
                        _collect_groovy_calls(child, nid)
                    break
        else:
            for i in range(node.child_count()):
                _walk(node.child(i), class_ctx)

    _walk(root)
    return nodes, raw_edges


def _collect_generic_calls(
    src: bytes, node: Any, from_id: str, raw_edges: list[_RawEdge]
) -> None:
    """Walk a tree-sitter body node collecting method_invocation / call_expression call edges."""
    kind = node.kind()
    if kind in ("method_invocation", "call_expression", "invocation_expression"):
        name_node = (
            node.child_by_field_name("name")
            or node.child_by_field_name("function")
            or node.child_by_field_name("method")
        )
        if name_node:
            raw_callee = _text(src, name_node).strip()
            if raw_callee:
                raw_edges.append(_RawEdge(from_id=from_id, raw_callee=raw_callee, kind="CALLS"))
    for i in range(node.named_child_count()):
        _collect_generic_calls(src, node.named_child(i), from_id, raw_edges)


def _extract_generic(
    file_path: str,
    src: bytes,
    root: Any,
    file_node: Any,
    now: str,
    language: str | None = None,
) -> tuple[list[Any], list[_RawEdge]]:
    """Broad tree-sitter extraction: function/method/class nodes, no call edges.

    Covers all tree-sitter node kinds used by Tier-2 languages. Call-edge
    resolution is skipped (raw_edges stays empty); the graph still gives
    community detection and symbol lookup for these languages.
    """
    from .storage import NodeData

    nodes: list[NodeData] = [file_node]
    raw_edges: list[_RawEdge] = []
    module_name = _basename(file_path)
    lang = language or file_node.language  # preserve for NodeData

    func_kinds = {
        # Standard across many languages
        "function_declaration", "function_definition",
        "method_declaration", "method_definition",
        "fn_item", "function_item",  # Rust alt names
        # Ruby: def foo / def self.foo
        "method", "singleton_method",
        # Lua: local function foo() …
        "local_function",
        # Zig: fn foo(…) …
        "fn_declaration",
        # Haskell / some others
        "function",
        # SQL stored procedures
        "create_function_statement", "create_procedure_statement",
    }
    class_kinds = {
        # Standard
        "class_declaration", "class_definition", "struct_item",
        # Ruby: class Foo … / module Bar …
        "class", "module",
        # Swift, C#, PHP, Dart
        "struct_declaration", "interface_declaration", "protocol_declaration",
        "enum_declaration", "extension_declaration",
        # Elixir: defmodule Foo do … end
        "defmodule",
        # SQL table
        "create_table_statement",
    }

    def walk(node: Any, class_ctx: str | None = None) -> None:
        kind = node.kind()
        if kind in func_kinds:
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node)
                qualified = f"{module_name}.{class_ctx}.{name}" if class_ctx else f"{module_name}.{name}"
                nodes.append(NodeData(
                    id=_node_id(file_path, qualified),
                    name=name, qualified_name=qualified,
                    kind="method" if class_ctx else "function",
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language=lang, created_at=now, updated_at=now,
                ))
        elif kind in class_kinds:
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(src, name_node)
                qualified = f"{module_name}.{name}"
                nodes.append(NodeData(
                    id=_node_id(file_path, qualified),
                    name=name, qualified_name=qualified,
                    kind="class",
                    file=file_path, start_line=_lineno(node), end_line=_endlineno(node),
                    language=lang, created_at=now, updated_at=now,
                ))
                for i in range(node.named_child_count()):
                    walk(node.named_child(i), name)
                return

        for i in range(node.named_child_count()):
            walk(node.named_child(i), class_ctx)

    walk(root)
    return nodes, raw_edges
