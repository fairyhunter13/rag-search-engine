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
#   e2  2026-07-29  S0 same-file call edges restored in `sweeps._extract_graph` — the resolution
#                   loop discarded every call whose target was defined in the same file, so ccw's
#                   stored graph held 0 same-file edges out of 1,934. Bundled deliberately with
#                   community.py's ALGO_VERSION fg1->fg2 (structural labels): both invalidate
#                   every stored graph, and they compose into ONE `_pipeline_algo_version`
#                   string, so shipping them together re-derives the 160-graph fleet once
#                   instead of twice.
EXTRACTOR_REV = "e2"

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


def _named_children(node) -> list:
    """`node`'s named children, left to right. One place, so the stack walks agree."""
    return [node.named_child(i) for i in range(node.named_child_count())]


def _generic_walk(node, code_bytes: bytes, file: str, lang: str,
                  parent: str = "") -> list[Symbol]:
    """Thin generic AST walk for grammars where process() returns empty structure.

    Matches universal node-kind suffixes and extracts the 'name' child field —
    no per-language vocabulary.

    D6: an explicit stack, not recursion. A tree-sitter AST's depth is bounded by the *source*,
    not by anything this module controls — minified JS, a generated parser table, or a long
    chained expression all nest linearly, and CPython's 1000-frame default is reached long
    before any of those is unusual. The recursive form raised `RecursionError` out of the
    bounded-parse worker, which `run_bounded` reports as `PARSE_TIMEOUT` — so a file too deep to
    walk was indistinguishable from a file too slow to parse, and both then read as "0 symbols".
    Stack order is children-reversed so the visit order is byte-for-byte the pre-order the
    recursion produced; nothing downstream is allowed to notice this change.
    """
    result: list[Symbol] = []
    stack: list[tuple] = [(node, parent)]
    while stack:
        cur, par = stack.pop()
        k = cur.kind()
        child_parent = par
        if any(k.endswith(s) for s in _GENERIC_DEF_SUFFIXES):
            name_node = cur.child_by_field_name("name")
            if name_node:
                br = name_node.byte_range()
                name = code_bytes[br.start:br.end].decode("utf-8", errors="replace")
                if _is_name_text(name):
                    qname = f"{par}.{name}" if par else name
                    if "function" in k or "method" in k or "func" in k:
                        sym_kind = "function"
                    elif "class" in k or "struct" in k or "trait" in k or "interface" in k:
                        sym_kind = "class"
                    else:
                        sym_kind = "function"  # conservative default
                    result.append(Symbol(
                        file=file, name=name, qualified_name=qname, kind=sym_kind,
                        start_line=cur.start_position().row + 1,
                        end_line=cur.end_position().row + 1,
                        language=lang,
                    ))
                    child_parent = name
        stack.extend((c, child_parent) for c in reversed(_named_children(cur)))
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
    """D6: stack, not recursion — see `_generic_walk`. Same pre-order."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if _is_call_node(cur.kind()):
            name = _unwrap_callee(_callee_node(cur), code_bytes)
            if name:
                out.append(name)
        stack.extend(reversed(_named_children(cur)))


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

    D6: stack, not recursion — see `_generic_walk`. A `script_element` is not descended into
    (it never nests another), which is why its children are simply not pushed.
    """
    out: list[tuple[str, bytes, int]] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.kind() != "script_element":
            stack.extend(reversed(_named_children(cur)))
            continue
        start_tag = _child_of_kind(cur, "start_tag")
        raw = _child_of_kind(cur, "raw_text")
        if raw is None:
            continue
        lang_attr = ""
        for attr in _named_children(start_tag) if start_tag else []:
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


# Every value `graph.store.file_extraction.rung` may hold: the ladder rungs this module can
# report, then the three outcomes only the caller can observe (`daemon/sweeps.py`), because by
# then the extraction function never ran. Declared in one place so the store, the sweep, the
# metrics block and the guard test cannot drift apart — the earlier version had each of them
# spelling the strings itself, which is how "timeout" came to mean three different events.
EXTRACTION_RUNGS: tuple[str, ...] = (
    "structure", "generic", "embedded", "unparsed", "no_grammar", "no_language",
    "timeout", "crashed", "error",
)


@dataclass(frozen=True)
class ExtractionStats:
    """What one file's extraction actually did — the per-file record `file_extraction` stores.

    Exists because "0 symbols" has at least five causes that were all rendered identically:
    a grammar the pack does not serve (`rung="no_grammar"`), bytes that do not parse as the
    detected language (`"unparsed"`), a grammar whose process() yields no structure
    (`"generic"`), symbols dropped for having no name (`anon_count`), and a file that
    genuinely defines nothing. Recording the rung is what separates them.
    """

    language: str
    rung: str
    symbol_count: int
    anon_count: int
    has_error: bool


def extract_symbols(path: Path, content: str, language: str) -> list[Symbol]:
    """Return symbols for any language via pack-native process() + generic-suffix fallback.

    H1: process() covers 306 canonical grammars with typed StructureKind output;
    _generic_walk is a thin last-resort for empty-structure grammars (Elixir, Haskell…).
    No per-language node-kind tables.
    """
    return extract_symbols_with_stats(path, content, language)[0]


def extract_symbols_with_stats(
    path: Path, content: str, language: str
) -> tuple[list[Symbol], ExtractionStats]:
    """`extract_symbols` plus the per-file extraction record. See `ExtractionStats`."""
    if not language or language == "unknown":
        return [], ExtractionStats(language or "unknown", "no_language", 0, 0, False)
    try:
        from tree_sitter_language_pack import ProcessConfig, has_language
        from tree_sitter_language_pack import process as ts_process
    except ImportError:
        return [], ExtractionStats(language, "no_grammar", 0, 0, False)
    if not has_language(language):
        return [], ExtractionStats(language, "no_grammar", 0, 0, False)
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
    syms, rung, anon = _extract_symbols_from(r, outer_root, code_bytes, file_str, language)
    if outer_root is not None:
        for inner_lang, inner_bytes, line_offset in _iter_script_blocks(outer_root, code_bytes):
            inner_src = inner_bytes.decode("utf-8", errors="replace")
            inner_syms, inner_stats = extract_symbols_with_stats(path, inner_src, inner_lang)
            # An embedded block's anonymous drops belong to the *host* file — a .svelte whose
            # <script> is all arrow functions must not read as an empty .svelte.
            anon += inner_stats.anon_count
            if inner_syms and rung in ("unparsed", "generic"):
                rung = "embedded"
            for s in inner_syms:
                syms.append(Symbol(
                    file=s.file, name=s.name, qualified_name=s.qualified_name, kind=s.kind,
                    start_line=s.start_line + line_offset, end_line=s.end_line + line_offset,
                    language=s.language, signature=s.signature, docstring=s.docstring,
                ))
    return syms, ExtractionStats(
        language=language, rung=rung, symbol_count=len(syms), anon_count=anon,
        has_error=_node_has_error(outer_root),
    )


def _node_has_error(root) -> bool:
    """`has_error` is a *method* on this binding's Node, not a property.

    `bool(getattr(root, "has_error", False))` therefore returns True for every parsed file —
    it measures "the attribute exists", not "the parse failed". Measured: 43/43 svelte and
    50/50 python files reported errors, which is the tell. Call it when callable, and treat a
    binding that exposes neither shape as "unknown" rather than inventing a verdict.
    """
    attr = getattr(root, "has_error", None)
    if attr is None:
        return False
    try:
        return bool(attr() if callable(attr) else attr)
    except Exception:
        return False


def _extract_symbols_from(
    r, outer_root, code_bytes: bytes, file_str: str, language: str
) -> tuple[list[Symbol], str, int]:
    """Shared structure/generic-walk logic, split out to keep the sub-parse merge separate.

    Returns `(symbols, rung, anon_count)`. `rung` names which path produced the symbols, so a
    caller can tell "this grammar has no structure output" from "this file has no code" — the
    two were indistinguishable, which is why the extraction gap was never measurable.

    `anon_count` is the number of structure entries dropped for having no name. process()
    reports anonymous and arrow functions with `name=None`; this path built them straight into
    `Symbol(name=None)` and `sweeps.py`'s `if not sym.name: continue` then discarded them
    without a counter, so a file of arrow functions read exactly like a file with no code.
    `_generic_walk` screens names via `_is_name_text`; this path did not, and that asymmetry
    was the defect. The drop condition here is deliberately the *same* falsy-name test sweeps
    already applied, so no symbol's fate changes — only whether the loss is visible.
    """
    if r is not None and r.structure:
        syms: list[Symbol] = []
        anon = 0
        for s in r.structure:
            kind = _STRUCTURE_KIND_MAP.get(str(s.kind).lower())
            if kind is None:
                continue
            if not s.name:
                anon += 1
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
        return syms, "structure", anon
    # process() returned no structure — fall back to generic AST walk
    if outer_root is None:
        return [], "unparsed", 0
    return _generic_walk(outer_root, code_bytes, file_str, language), "generic", 0


# The ordered-call-site layer (CallSite / _BRANCH_NODE_KINDS / _collect_sites /
# extract_call_sites) left with tier 3 on 2026-07-28. It carried source order and branch
# depth so BPRE could reconstruct a process from a call sequence; nothing else ever read
# order_index, branch_id or guard, so with BPRE deleted it was a parse nobody consumed.
# Edges keep coming from extract_calls_with_lines below, which sweeps.py:226 still calls.


def _collect_calls_with_lines(node, code_bytes: bytes, out: list) -> None:
    """D6: stack, not recursion — see `_generic_walk`. Same pre-order."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if _is_call_node(cur.kind()):
            name = _unwrap_callee(_callee_node(cur), code_bytes)
            if name:
                out.append((name, cur.start_position().row + 1))
        stack.extend(reversed(_named_children(cur)))


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


