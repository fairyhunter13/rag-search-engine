"""Tree-sitter AST extraction: functions, classes, methods → symbols."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

_TS_LANG: dict[str, str] = {
    "python": "python", "javascript": "javascript", "typescript": "typescript",
    "go": "go", "rust": "rust", "java": "java", "kotlin": "kotlin",
    "ruby": "ruby", "csharp": "c_sharp", "swift": "swift", "cpp": "cpp", "c": "c",
}

_DEF_KINDS: dict[str, dict[str, str]] = {
    "python": {"function_definition": "function", "class_definition": "class"},
    "javascript": {
        "function_declaration": "function", "class_declaration": "class",
        "method_definition": "method",
    },
    "typescript": {
        "function_declaration": "function", "class_declaration": "class",
        "method_definition": "method",
    },
    "go": {"function_declaration": "function", "method_declaration": "method"},
    "rust": {"function_item": "function", "impl_item": "class"},
    "java": {
        "method_declaration": "method", "class_declaration": "class",
        "constructor_declaration": "function",
    },
    "kotlin": {"function_declaration": "function", "class_declaration": "class"},
    "ruby": {"method": "method", "class": "class", "singleton_method": "method"},
}
_DEFAULT_KINDS: dict[str, str] = {
    "function_declaration": "function", "class_declaration": "class",
    "method_declaration": "method",
}

# (call-node-type, name-field) per language for call-edge extraction
_CALL_NODE: dict[str, tuple[str, str]] = {
    "python": ("call", "function"),
    "javascript": ("call_expression", "function"),
    "typescript": ("call_expression", "function"),
    "go": ("call_expression", "function"),
    "java": ("method_invocation", "name"),
    "kotlin": ("call_expression", "function"),
    "ruby": ("call", "method"),
}

# member/attribute node kinds — we extract just the right-hand identifier
_MEMBER_KINDS = {"member_expression", "attribute", "selector_expression", "field_access"}


@dataclass(slots=True)
class Symbol:
    file: str
    name: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int
    language: str
    signature: str = ""
    docstring: str = ""


def symbol_id(file: str, name: str, start_line: int) -> str:
    return hashlib.sha256(f"{file}:{name}:{start_line}".encode()).hexdigest()[:16]


def _walk(node, code_bytes: bytes, file: str, lang: str,
          kinds: dict[str, str], parent: str = "") -> list[Symbol]:
    result: list[Symbol] = []
    kind_str = node.kind()
    if kind_str in kinds:
        name_node = node.child_by_field_name("name")
        if name_node:
            br = name_node.byte_range()
            name = code_bytes[br.start:br.end].decode("utf-8", errors="replace")
            qname = f"{parent}.{name}" if parent else name
            result.append(Symbol(
                file=file, name=name, qualified_name=qname, kind=kinds[kind_str],
                start_line=node.start_position().row + 1,
                end_line=node.end_position().row + 1,
                language=lang,
            ))
            parent = name
    for i in range(node.named_child_count()):
        result.extend(_walk(node.named_child(i), code_bytes, file, lang, kinds, parent))
    return result


def _collect_call_names(node, code_bytes: bytes, call_type: str,
                        name_field: str, out: list[str]) -> None:
    if node.kind() == call_type:
        name_node = node.child_by_field_name(name_field)
        if name_node:
            if name_node.kind() in _MEMBER_KINDS:
                # a.b() or pkg.Func() — take the rightmost identifier
                name_node = (name_node.child_by_field_name("field")
                             or name_node.child_by_field_name("property")
                             or name_node.child_by_field_name("name")
                             or name_node)
            br = name_node.byte_range()
            name = code_bytes[br.start:br.end].decode("utf-8", errors="replace")
            if name and name.isidentifier():
                out.append(name)
    for i in range(node.named_child_count()):
        _collect_call_names(node.named_child(i), code_bytes, call_type, name_field, out)


def extract_calls(content: str, language: str) -> list[str]:
    """Return called function/method names found in content (tree-sitter parse)."""
    ts_lang = _TS_LANG.get(language)
    call_spec = _CALL_NODE.get(language)
    if ts_lang is None or call_spec is None:
        return []
    try:
        from tree_sitter_language_pack import api as ts_api
        tree = ts_api.get_parser(ts_lang).parse(content)
        root = tree.root_node()
    except Exception:
        return []
    call_type, name_field = call_spec
    code_bytes = content.encode("utf-8", errors="replace")
    out: list[str] = []
    _collect_call_names(root, code_bytes, call_type, name_field, out)
    return out


def extract_symbols(path: Path, content: str, language: str) -> list[Symbol]:
    """Return symbols from content via tree-sitter. Returns [] on unsupported lang."""
    ts_lang = _TS_LANG.get(language)
    if ts_lang is None:
        return []
    try:
        from tree_sitter_language_pack import api as ts_api
        tree = ts_api.get_parser(ts_lang).parse(content)
        root = tree.root_node()
    except Exception:
        return []
    kinds = _DEF_KINDS.get(language, _DEFAULT_KINDS)
    return _walk(root, content.encode("utf-8", errors="replace"), str(path), language, kinds)


# ─── Ordered call sites (BPRE D1) ────────────────────────────────────────────

@dataclass(slots=True)
class CallSite:
    """Call site with source-order index and enclosing branch depth (BPRE D1)."""
    caller_qualified_name: str
    callee_name: str
    order_index: int
    branch_id: int  # 0 = unconditional; >0 = inside if/for/switch
    guard: str      # enclosing keyword: "if", "for", "switch", …


_BRANCH_NODE_KINDS: frozenset[str] = frozenset({
    "if_statement", "for_statement", "while_statement", "switch_statement",
    "select_statement", "with_statement", "for_range_clause", "try_statement",
})


def _collect_sites(node, code_bytes: bytes, call_type: str, name_field: str,  # type: ignore[no-untyped-def]
                   out: list, counter: list, depth: int, kw: str) -> None:
    kind = node.kind()
    nd = depth + 1 if kind in _BRANCH_NODE_KINDS else depth
    nk = kind.split("_")[0] if kind in _BRANCH_NODE_KINDS else kw
    if kind == call_type:
        nn = node.child_by_field_name(name_field)
        if nn:
            if nn.kind() in _MEMBER_KINDS:
                nn = (nn.child_by_field_name("field") or nn.child_by_field_name("property")
                      or nn.child_by_field_name("name") or nn)
            br = nn.byte_range()
            name = code_bytes[br.start:br.end].decode("utf-8", errors="replace")
            if name and name.isidentifier():
                out.append(CallSite("", name, counter[0], nd, nk))
                counter[0] += 1
    for i in range(node.named_child_count()):
        _collect_sites(node.named_child(i), code_bytes, call_type, name_field, out, counter, nd, nk)


def _collect_calls_with_lines(
    node, code_bytes: bytes, call_type: str, name_field: str, out: list
) -> None:
    if node.kind() == call_type:
        nn = node.child_by_field_name(name_field)
        if nn:
            if nn.kind() in _MEMBER_KINDS:
                nn = (nn.child_by_field_name("field") or nn.child_by_field_name("property")
                      or nn.child_by_field_name("name") or nn)
            br = nn.byte_range()
            name = code_bytes[br.start:br.end].decode("utf-8", errors="replace")
            if name and name.isidentifier():
                out.append((name, node.start_position().row + 1))
    for i in range(node.named_child_count()):
        _collect_calls_with_lines(node.named_child(i), code_bytes, call_type, name_field, out)


def extract_calls_with_lines(content: str, language: str) -> list[tuple[str, int]]:
    """Return (callee_name, line_number) for each call in content (tree-sitter parse)."""
    ts_lang = _TS_LANG.get(language)
    call_spec = _CALL_NODE.get(language)
    if ts_lang is None or call_spec is None:
        return []
    try:
        from tree_sitter_language_pack import api as ts_api
        tree = ts_api.get_parser(ts_lang).parse(content)
        root = tree.root_node()
    except Exception:
        return []
    call_type, name_field = call_spec
    code_bytes = content.encode("utf-8", errors="replace")
    out: list[tuple[str, int]] = []
    _collect_calls_with_lines(root, code_bytes, call_type, name_field, out)
    return out


def extract_call_sites(content: str, language: str) -> list[CallSite]:
    """Return ordered call sites (DFS order). Branch depth tracks if/for/switch nesting."""
    ts_lang = _TS_LANG.get(language)
    call_spec = _CALL_NODE.get(language)
    if ts_lang is None or call_spec is None:
        return []
    try:
        from tree_sitter_language_pack import api as ts_api
        tree = ts_api.get_parser(ts_lang).parse(content)
        root = tree.root_node()
    except Exception:
        return []
    call_type, name_field = call_spec
    code_bytes = content.encode("utf-8", errors="replace")
    out: list[CallSite] = []
    _collect_sites(root, code_bytes, call_type, name_field, out, [0], 0, "")
    return out
