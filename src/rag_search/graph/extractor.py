"""Tree-sitter AST extraction: symbols + call edges for any language via process() API."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

# S3: the extraction contract's revision. Any change to what extraction *emits* — this module,
# or `sweeps._extract_graph`'s call resolution, which no fingerprint covers — must bump this in
# the same commit, and the bump is what invalidates every stored graph in the fleet.
#
# History, so the next bump knows what it is joining:
#   e1  2026-07-28  S8 family-gated call resolution; S1 grammar-decided names; S4 token-matched
#                   call nodes (+`macro` field, `*_signature` excluded); S7 shebang fallback.
EXTRACTOR_REV = "e1"

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

# F2: embedded-<script> host grammars (vue/svelte/astro/html) all expose the same
# `script_element` -> `raw_text` shape; the <script lang="..."> attribute (if any)
# picks the inner grammar. Structural only — no filename/vocabulary heuristic.
_EMBEDDED_SCRIPT_LANG: dict[str, str] = {
    "ts": "typescript", "tsx": "typescript", "typescript": "typescript",
    "js": "javascript", "jsx": "javascript", "javascript": "javascript",
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
            if _is_name_text(name):
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

def _is_name_text(name: str) -> bool:
    """S1: accept whatever the grammar handed back as a name, minus the impossible cases.

    `str.isidentifier()` stood here and answered a *Python* question about every language:
    it rejects `$user` (PHP), `list-ref` (Scheme, Clojure), `empty?` and `save!` (Ruby, Elixir)
    and `@media` — all of which are names their own grammar named. The grammar has already
    decided this node is a name; re-deciding it in Python only ever discards edges.

    What is still rejected is what no name can be: empty, or spanning lines/whitespace, which
    is the signature of an unwrap that fell through to a whole expression.
    """
    return bool(name) and not any(c.isspace() for c in name)


def _unwrap_callee(nn, code_bytes: bytes) -> str:
    """Unwrap member/attribute node to rightmost identifier; '' if not a name."""
    if nn is None:
        return ""
    if nn.kind() in _MEMBER_KINDS:
        # "field"=Go/JS, "property"=TS/JS, "attribute"=Python, "name"=Java/Kotlin
        nn = (nn.child_by_field_name("field") or nn.child_by_field_name("property")
              or nn.child_by_field_name("attribute") or nn.child_by_field_name("name") or nn)
    # S1: a name is a leaf. If the node still has named children after unwrapping it is an
    # expression — `factory()()`, `arr[i]()` — and its text is not a callee name.
    if nn.named_child_count():
        return ""
    br = nn.byte_range()
    name = code_bytes[br.start:br.end].decode("utf-8", errors="replace")
    return name if _is_name_text(name) else ""


def _callee_node(node):  # type: ignore[return]
    """Return the callee sub-node from a call/invocation node.

    Field names first — `function` (C, JS, Rust), `name`, `method` (Java), `macro` (Rust
    `macro_invocation`, added by S4), `callee`. Not every grammar names the field: Kotlin's
    `call_expression` is `(simple_identifier call_suffix)` with no field on either child, so
    every Kotlin call resolved to nothing and the language contributed **zero** call edges.
    That was true before S4 and was found by TS4b, not by reading the grammar.

    The fallback is positional and structural, never per-language: the first named child of a
    call node is its callee in every grammar that leaves the field unnamed, and it is only
    consulted once all the field lookups have missed.
    """
    named = (node.child_by_field_name("function")
             or node.child_by_field_name("name")
             or node.child_by_field_name("method")
             or node.child_by_field_name("macro")
             or node.child_by_field_name("callee"))
    if named is not None:
        return named
    return node.named_child(0) if node.named_child_count() else None


def _is_call_node(kind: str) -> bool:
    """S4: is this grammar node a call site? Matched on node-type *tokens*, not substring.

    `"call" in kind` is a substring test, so it fires on any node type that merely contains
    the letters — and it silently misses nothing it should have caught, because every call
    node type in the pack spells the word as its own `_`-separated token (`call`,
    `call_expression`, `function_call`, `method_invocation`, `macro_invocation`). Splitting on
    `_` keeps all of those and stops matching by accident.

    `*_signature` is excluded outright: a signature declares a callable, it does not call one.
    """
    if kind.endswith("_signature"):
        return False
    parts = kind.split("_")
    return "call" in parts or "invocation" in parts


def _collect_call_names(node, code_bytes: bytes, out: list[str]) -> None:
    if _is_call_node(node.kind()):
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


def _child_of_kind(node, kind: str):  # type: ignore[return]
    """First named child of node whose kind() == kind, else None."""
    return next(
        (node.named_child(i) for i in range(node.named_child_count())
         if node.named_child(i).kind() == kind), None,
    )


def _attr_value_text(attr_node, code_bytes: bytes) -> str:
    """Unquoted text of an HTML/SFC `attribute` node's value (e.g. lang="ts" -> "ts")."""
    vn = _child_of_kind(attr_node, "attribute_value")
    if vn is not None:
        br = vn.byte_range()
        return code_bytes[br.start:br.end].decode("utf-8", errors="replace")
    qvn = _child_of_kind(attr_node, "quoted_attribute_value")
    if qvn is not None:
        inner = _child_of_kind(qvn, "attribute_value")
        target = inner if inner is not None else qvn
        br = target.byte_range()
        return code_bytes[br.start:br.end].decode("utf-8", errors="replace").strip("\"'")
    return ""


def _iter_script_blocks(node, code_bytes: bytes) -> list[tuple[str, bytes, int]]:
    """Find `script_element` nodes (Vue/Svelte/Astro/HTML host grammars) and return
    (inner_language, inner_source_bytes, line_offset) for each embedded <script> block.

    F2: these grammars parse <script> content as one opaque `raw_text` leaf — this walk
    locates that leaf plus its `lang` attribute so callers can sub-parse it with the
    js/ts grammar and remap line numbers by `line_offset`.
    """
    out: list[tuple[str, bytes, int]] = []
    if node.kind() == "script_element":
        start_tag = _child_of_kind(node, "start_tag")
        raw = _child_of_kind(node, "raw_text")
        if raw is not None:
            lang_attr = ""
            for i in range(start_tag.named_child_count() if start_tag else 0):
                attr = start_tag.named_child(i)
                if attr.kind() != "attribute":
                    continue
                name_node = _child_of_kind(attr, "attribute_name")
                if name_node is None:
                    continue
                nbr = name_node.byte_range()
                if code_bytes[nbr.start:nbr.end].decode("utf-8", "replace") == "lang":
                    lang_attr = _attr_value_text(attr, code_bytes)
                    break
            inner_lang = _EMBEDDED_SCRIPT_LANG.get(lang_attr.lower(), "javascript")
            br = raw.byte_range()
            out.append((inner_lang, code_bytes[br.start:br.end], raw.start_position().row))
        return out  # script_element never nests another script_element
    for i in range(node.named_child_count()):
        out.extend(_iter_script_blocks(node.named_child(i), code_bytes))
    return out


def extract_calls(content: str, language: str) -> list[str]:
    """Return called function/method names (H2: generic call-node detection, any language)."""
    parser, ok = _get_parser_for(language)
    if not ok:
        return []
    try:
        root = parser.parse(content).root_node()
    except Exception:
        return []
    code_bytes = content.encode("utf-8", errors="replace")
    out: list[str] = []
    _collect_call_names(root, code_bytes, out)
    for inner_lang, inner_bytes, _offset in _iter_script_blocks(root, code_bytes):
        out.extend(extract_calls(inner_bytes.decode("utf-8", errors="replace"), inner_lang))
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
    code_bytes = content.encode("utf-8", errors="replace")
    outer_parser, outer_ok = _get_parser_for(language)
    outer_root = None
    if outer_ok:
        try:
            outer_root = outer_parser.parse(content).root_node()
        except Exception:
            outer_root = None
    try:
        r = ts_process(content, ProcessConfig(structure=True, language=language))
    except Exception:
        r = None
    syms = _extract_symbols_from(r, outer_root, code_bytes, file_str, language)
    if outer_root is not None:
        for inner_lang, inner_bytes, line_offset in _iter_script_blocks(outer_root, code_bytes):
            inner_src = inner_bytes.decode("utf-8", errors="replace")
            for s in extract_symbols(path, inner_src, inner_lang):
                syms.append(Symbol(
                    file=s.file, name=s.name, qualified_name=s.qualified_name, kind=s.kind,
                    start_line=s.start_line + line_offset, end_line=s.end_line + line_offset,
                    language=s.language, signature=s.signature, docstring=s.docstring,
                ))
    return syms


def _extract_symbols_from(r, outer_root, code_bytes: bytes, file_str: str, language: str) -> list[Symbol]:
    """Shared structure/generic-walk logic for extract_symbols, split out to keep sub-parse merge separate."""
    if r is not None and r.structure:
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
        if not any(s.kind in ("function", "method") for s in syms) and outer_root is not None:
            known = {s.name for s in syms}
            syms.extend(
                s for s in _generic_walk(outer_root, code_bytes, file_str, language)
                if s.name not in known
            )
        return syms
    # process() returned no structure — fall back to generic AST walk
    if outer_root is None:
        return []
    return _generic_walk(outer_root, code_bytes, file_str, language)


# The ordered-call-site layer (CallSite / _BRANCH_NODE_KINDS / _collect_sites /
# extract_call_sites) left with tier 3 on 2026-07-28. It carried source order and branch
# depth so BPRE could reconstruct a process from a call sequence; nothing else ever read
# order_index, branch_id or guard, so with BPRE deleted it was a parse nobody consumed.
# Edges keep coming from extract_calls_with_lines below, which sweeps.py:226 still calls.


def _collect_calls_with_lines(node, code_bytes: bytes, out: list) -> None:
    if _is_call_node(node.kind()):
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
    code_bytes = content.encode("utf-8", errors="replace")
    out: list[tuple[str, int]] = []
    _collect_calls_with_lines(root, code_bytes, out)
    for inner_lang, inner_bytes, line_offset in _iter_script_blocks(root, code_bytes):
        inner_src = inner_bytes.decode("utf-8", errors="replace")
        out.extend(
            (name, line + line_offset)
            for name, line in extract_calls_with_lines(inner_src, inner_lang)
        )
    return out


