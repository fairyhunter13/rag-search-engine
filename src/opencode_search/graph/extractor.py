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
