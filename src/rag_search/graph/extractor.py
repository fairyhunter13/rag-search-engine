"""Tree-sitter AST extraction: symbols + call edges for any language via process() API."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

# H1: StructureKind (process() output) → our canonical kind string.
# str(StructureKind.X) gives capitalised names e.g. "Function"; .lower() normalises.
_STRUCTURE_KIND_MAP: dict[str, str] = {
    "function": "function", "method": "method",
    "class": "class", "struct": "class", "trait": "class",
    "interface": "class", "enum": "class", "impl": "class",
    "module": "module", "namespace": "module",
}

# H1: generic node-kind suffixes for the thin AST fallback (empty-structure grammars).
_GENERIC_DEF_SUFFIXES: tuple[str, ...] = (
    "_definition", "_declaration", "_item", "_specification",
)

# H2: member/attribute node kinds — unwrap to extract rightmost identifier
_MEMBER_KINDS: frozenset[str] = frozenset({
    "member_expression", "attribute", "selector_expression", "field_access",
})


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


def _generic_walk(node, code_bytes: bytes, file: str, lang: str,
                  parent: str = "") -> list[Symbol]:
    """Thin generic AST walk for grammars where process() returns empty structure.

    Matches universal node-kind suffixes and extracts the 'name' child field —
    no per-language vocabulary.
    """
    result: list[Symbol] = []
    k = node.kind()
    if any(k.endswith(s) for s in _GENERIC_DEF_SUFFIXES):
        name_node = node.child_by_field_name("name")
        if name_node:
            br = name_node.byte_range()
            name = code_bytes[br.start:br.end].decode("utf-8", errors="replace")
            if name and name.isidentifier():
                qname = f"{parent}.{name}" if parent else name
                if "function" in k or "method" in k or "func" in k:
                    sym_kind = "function"
                elif "class" in k or "struct" in k or "trait" in k or "interface" in k:
                    sym_kind = "class"
                else:
                    sym_kind = "function"  # conservative default
                result.append(Symbol(
                    file=file, name=name, qualified_name=qname, kind=sym_kind,
                    start_line=node.start_position().row + 1,
                    end_line=node.end_position().row + 1,
                    language=lang,
                ))
                parent = name
    for i in range(node.named_child_count()):
        result.extend(_generic_walk(node.named_child(i), code_bytes, file, lang, parent))
    return result


# H2 helpers: generic call-node detection (replaces the old per-language call-node table)

def _unwrap_callee(nn, code_bytes: bytes) -> str:
    """Unwrap member/attribute node to rightmost identifier; '' if not an identifier."""
    if nn is None:
        return ""
    if nn.kind() in _MEMBER_KINDS:
        # "field"=Go/JS, "property"=TS/JS, "attribute"=Python, "name"=Java/Kotlin
        nn = (nn.child_by_field_name("field") or nn.child_by_field_name("property")
              or nn.child_by_field_name("attribute") or nn.child_by_field_name("name") or nn)
    br = nn.byte_range()
    name = code_bytes[br.start:br.end].decode("utf-8", errors="replace")
    return name if name and name.isidentifier() else ""


def _callee_node(node):  # type: ignore[return]
    """Return the callee sub-node from a call/invocation node (common field names)."""
    return (node.child_by_field_name("function")
            or node.child_by_field_name("name")
            or node.child_by_field_name("method")
            or node.child_by_field_name("callee"))


def _collect_call_names(node, code_bytes: bytes, out: list[str]) -> None:
    k = node.kind()
    if "call" in k or "invocation" in k:
        name = _unwrap_callee(_callee_node(node), code_bytes)
        if name:
            out.append(name)
    for i in range(node.named_child_count()):
        _collect_call_names(node.named_child(i), code_bytes, out)


def _get_parser_for(language: str):  # type: ignore[return]
    """Return (parser, True) for a pack-supported language; (None, False) on miss."""
    if not language or language == "unknown":
        return None, False
    try:
        from tree_sitter_language_pack import api as ts_api
        from tree_sitter_language_pack import has_language
        if not has_language(language):
            return None, False
        return ts_api.get_parser(language), True
    except Exception:
        return None, False


def extract_calls(content: str, language: str) -> list[str]:
    """Return called function/method names (H2: generic call-node detection, any language)."""
    parser, ok = _get_parser_for(language)
    if not ok:
        return []
    try:
        root = parser.parse(content).root_node()
    except Exception:
        return []
    out: list[str] = []
    _collect_call_names(root, content.encode("utf-8", errors="replace"), out)
    return out


def extract_symbols(path: Path, content: str, language: str) -> list[Symbol]:
    """Return symbols for any language via pack-native process() + generic-suffix fallback.

    H1: process() covers 306 canonical grammars with typed StructureKind output;
    _generic_walk is a thin last-resort for empty-structure grammars (Elixir, Haskell…).
    No per-language node-kind tables.
    """
    if not language or language == "unknown":
        return []
    try:
        from tree_sitter_language_pack import ProcessConfig, has_language
        from tree_sitter_language_pack import process as ts_process
    except ImportError:
        return []
    if not has_language(language):
        return []
    file_str = str(path)
    try:
        r = ts_process(content, ProcessConfig(structure=True, language=language))
    except Exception:
        return []
    if r.structure:
        syms: list[Symbol] = []
        for s in r.structure:
            kind = _STRUCTURE_KIND_MAP.get(str(s.kind).lower())
            if kind is None:
                continue
            syms.append(Symbol(
                file=file_str, name=s.name, qualified_name=s.name, kind=kind,
                start_line=s.span.start_line + 1, end_line=s.span.end_line + 1,
                language=language, signature=s.signature or "", docstring=s.doc_comment or "",
            ))
        # process() may yield only class/module nodes (e.g. Java, Kotlin) with no methods.
        # Supplement via _generic_walk so method names enter the symbol table for call-edge resolution.
        if not any(s.kind in ("function", "method") for s in syms):
            try:
                parser, ok = _get_parser_for(language)
                if ok:
                    gw_root = parser.parse(content).root_node()
                    code_bytes = content.encode("utf-8", errors="replace")
                    known = {s.name for s in syms}
                    syms.extend(
                        s for s in _generic_walk(gw_root, code_bytes, file_str, language)
                        if s.name not in known
                    )
            except Exception:
                pass
        return syms
    # process() returned no structure — fall back to generic AST walk
    parser, ok = _get_parser_for(language)
    if not ok:
        return []
    try:
        root = parser.parse(content).root_node()
        return _generic_walk(
            root, content.encode("utf-8", errors="replace"), file_str, language,
        )
    except Exception:
        return []


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


def _collect_sites(
    node, code_bytes: bytes, out: list, counter: list, depth: int, kw: str,
) -> None:
    kind = node.kind()
    nd = depth + 1 if kind in _BRANCH_NODE_KINDS else depth
    nk = kind.split("_")[0] if kind in _BRANCH_NODE_KINDS else kw
    if "call" in kind or "invocation" in kind:
        name = _unwrap_callee(_callee_node(node), code_bytes)
        if name:
            out.append(CallSite("", name, counter[0], nd, nk))
            counter[0] += 1
    for i in range(node.named_child_count()):
        _collect_sites(node.named_child(i), code_bytes, out, counter, nd, nk)


def _collect_calls_with_lines(node, code_bytes: bytes, out: list) -> None:
    k = node.kind()
    if "call" in k or "invocation" in k:
        name = _unwrap_callee(_callee_node(node), code_bytes)
        if name:
            out.append((name, node.start_position().row + 1))
    for i in range(node.named_child_count()):
        _collect_calls_with_lines(node.named_child(i), code_bytes, out)


def extract_calls_with_lines(content: str, language: str) -> list[tuple[str, int]]:
    """Return (callee_name, line_number) for each call (H2: generic, any language)."""
    parser, ok = _get_parser_for(language)
    if not ok:
        return []
    try:
        root = parser.parse(content).root_node()
    except Exception:
        return []
    out: list[tuple[str, int]] = []
    _collect_calls_with_lines(root, content.encode("utf-8", errors="replace"), out)
    return out


def extract_call_sites(content: str, language: str) -> list[CallSite]:
    """Return ordered call sites (DFS order). Branch depth tracks if/for/switch nesting.

    H2: generic call-node detection (node kind ∋ 'call'/'invocation'), any grammar.
    """
    parser, ok = _get_parser_for(language)
    if not ok:
        return []
    try:
        root = parser.parse(content).root_node()
    except Exception:
        return []
    out: list[CallSite] = []
    _collect_sites(root, content.encode("utf-8", errors="replace"), out, [0], 0, "")
    return out
