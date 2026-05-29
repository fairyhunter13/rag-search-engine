"""Tree-sitter AST extractor: emits NodeData + unresolved EdgeData.

Supports: Python, TypeScript, JavaScript, Go, Java, Kotlin, Rust.
Falls back to file-level node for unsupported languages.

All edges produced here have to_id=<raw callee string> (pre-resolution).
CallResolver in resolver.py maps them to real node IDs.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# Languages supported for deep extraction
_DEEP_LANGS: set[str] = {"python", "typescript", "javascript", "go", "java", "kotlin", "rust"}

# Map file extensions → language name used by tree_sitter_language_pack
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".rs": "rust",
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
        except Exception as exc:  # noqa: BLE001
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
        else:
            return _extract_generic(file_path, src, root, file_node, now)


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
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


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
    for alias, qualified in imports:
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
# Generic fallback (Java, Kotlin, Rust, etc.)
# ------------------------------------------------------------------

def _extract_generic(
    file_path: str,
    src: bytes,
    root: Any,
    file_node: Any,
    now: str,
) -> tuple[list[Any], list[_RawEdge]]:
    """Basic extraction: function and class nodes, no call edges."""
    from .storage import NodeData

    nodes: list[NodeData] = [file_node]
    raw_edges: list[_RawEdge] = []
    module_name = _basename(file_path)

    func_kinds = {
        "function_declaration", "function_definition", "method_declaration",
        "method_definition", "fn_item",
        "function_item",  # Rust
    }
    class_kinds = {"class_declaration", "class_definition", "struct_item"}

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
                    language=None, created_at=now, updated_at=now,
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
                    language=None, created_at=now, updated_at=now,
                ))
                for i in range(node.named_child_count()):
                    walk(node.named_child(i), name)
                return

        for i in range(node.named_child_count()):
            walk(node.named_child(i), class_ctx)

    walk(root)
    return nodes, raw_edges
