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
#   e3  2026-07-30  Rung 4: `_highlight_walk` extracts from the grammar's own highlights.scm
#                    when process() and the generic walk both come back empty. Reaches 237 of
#                    306 languages against `tags`' 48, which is why the ladder skips a tags rung
#                    entirely — measured, `tags` is empty for every language that gets this far.
#   e4  2026-07-30  Rung 5 (`_data_walk`, `data_extraction=True` -> `ProcessResult.data`) gives
#                    yaml/json/toml/hcl their top-level structure instead of a blank row, and
#                    the X2 rung `language_mismatch` records a file whose bytes do not parse as
#                    the language its extension claims. Both fire only where every earlier rung
#                    came back empty, so no file that already extracted changes.
#   e5  2026-07-30  Rung 1: `_injection_blocks` reads embedded regions out of the host grammar's
#                    own `injections.scm`, supplementing `_iter_script_blocks` rather than
#                    replacing it — vue's injections query is *empty*, and vue is the fleet's
#                    highest-coverage host grammar, so the replacement the plan proposed would
#                    have been a straight regression. Unlike e4 this *does* change files that
#                    already extracted (an astro file gains its frontmatter's symbols), so it
#                    carries its own rev rather than riding e4's re-derive silently.
#   e6  2026-07-30  The `_generic_walk` supplement gets a span-containment arm, so a class that
#                    process() reported without its members no longer loses them the moment the
#                    file also holds a top-level function. Found by W1-A part 5's ground-truth
#                    fixtures on the first run — python and typescript both dropped a method that
#                    the identical file *without* an unrelated function extracted fine. Strictly
#                    additive (a union with the old arm), but it changes files that already
#                    extract, so it carries its own rev.
#   e7  2026-07-31  Two arms, one rev, because one stamp move re-derives 151 graphs and four of
#                    them would have cost four. S5 `_named_binding_walk`: a function *value* bound
#                    to a name (`const getUser = () => {}`, `valueChange: function () {}`) is a
#                    definition process() reports as anonymous, so it arrived `name=None` and was
#                    dropped. Gated on `anon`, so a file process() named completely pays nothing.
#                    And `text.title` -> `section` at rung 4, screened by `_is_title_text` rather
#                    than `_is_name_text`, which recovers markdown headings whole instead of only
#                    the one-word ones. Measured on one JS-dominated store: strictly additive —
#                    0 symbols lost, 0 changed, 1,942 gained, and 55 of its 80 dark JS files lit.
#   e8  2026-07-31  Call *resolution*, not extraction — and the only rev here that is subtractive.
#                    `_extract_graph` emitted every same-family definition sharing the callee's
#                    name, so 2,220,234 of 2,320,130 fleet edges bound one call site to N
#                    definitions (95.7%; 70.2% of them in groups above 32) and `graph()` presented
#                    all N with equal confidence. Now: prefer definitions in the caller's own file,
#                    else the family, and emit only if that tier holds exactly one candidate after
#                    dropping the caller (`_MAX_CALLEE_FANOUT` in `daemon/sweeps.py`). Measured
#                    over 155 stores, 193,309 call-site groups: precision 0.182 -> 1.000 at recall
#                    0.633; **fleet edges fall 2,320,130 -> ~122,324, a 19x drop, which is the
#                    intended result and not a regression.** Median modularity_q rises 0.578 ->
#                    0.769. `ALGO_VERSION` deliberately stays `fg3`: the partition changes because
#                    its input did, not its algorithm. This rev is mandatory rather than
#                    conventional — the resolution lives in `sweeps.py`, which S3 below explains is
#                    deliberately outside `_code_fingerprint`, so nothing else would have noticed.
#   e9  2026-08-03  No extractor code changed at all — the *grammars* did.
#                    `tree-sitter-language-pack` 1.12.1 -> 1.14.0, and this is exactly the case S3
#                    below warns about: a dependency version is invisible to `_code_fingerprint`,
#                    which hashes source modules, so without this rev the fleet would keep serving
#                    edges the installed pack no longer produces. Measured by regenerating
#                    `docs/reference/grammar-capability-matrix.md` on both versions and diffing all
#                    306 pre-existing rows: **0 regressions, 0 languages removed, 65 added, 36
#                    gained capability.** The gains land on the fleet's dark mass rather than the
#                    long tail — php gains highlights *and* injections (rung 6 -> rung 1), which is
#                    the `injections.scm` absence §1g measured on 746 `*.tpl.php` templates;
#                    typescript and tsx gain tags and locals (rung 6 -> rung 4); vue gains
#                    highlights and static injections. Totals: languages 306 -> 371, highlights
#                    237 -> 319, tags 48 -> 71, injections 110 -> 157 (static 75 -> 111), locals
#                    81 -> 108, no query files 66 -> 51.
EXTRACTOR_REV = "e11"

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

# Rung 4: the capture names in a grammar's own `highlights.scm` that name a *definition*, and
# the kind each maps to. Authored upstream by the grammar, exactly like `_STRUCTURE_KIND_MAP`
# above — this is not a vocabulary invented about source text, and nothing here reads an
# identifier. Plain `type` is deliberately absent: in SQL it captures every `object_reference`,
# so a table merely *selected from* would enter the symbol table as a definition. Mainstream
# grammars reach rung 3 anyway, so excluding it costs nothing and avoids inventing symbols.
#
# `text.title` is the prose entry, and it is a *heading*, not a code definition: markdown reached
# rung 4 and left empty-handed because nothing mapped the one capture its grammar emits. Measured
# on this repo — 31 markdown files, rung `generic`, 0 symbols, 0.0% coverage. Its kind is
# `section` rather than a borrowed `module`, because a heading names a region of a document and
# call resolution must never mistake it for something a call could reach.
_HIGHLIGHT_DEF_CAPTURES: dict[str, str] = {
    "function": "function", "function.method": "method", "method": "method",
    "constructor": "method", "class": "class", "type.definition": "class",
    "text.title": "section",
}

# Rung 4's filter, and the whole reason it is safe: a highlights capture says "this token is a
# function", not "this token is *defined* here". Measured — python's `@function` fires on the
# callee of `helper(a)`, and bash's on the `echo` of a command — so an unfiltered rung 4 would
# invent a definition for every call site. A definition is accepted only when its parent node
# kind carries one of these `_`-separated tokens, which is what `_GENERIC_DEF_SUFFIXES` already
# encodes plus `statement` (scss spells them `mixin_statement` / `function_statement`). Call
# parents are rejected outright via `_is_call_node`.
#
# `heading` covers markdown's `atx_heading` and `setext_heading` both. The plan that asked for
# this also asked for `section`; it is deliberately absent — markdown's heading nodes are the
# direct parent of the captured `inline`, so `heading` alone is sufficient here, and `section`
# would additionally admit whatever `[section]`-shaped grammars name that way, on no evidence.
_DEF_PARENT_TOKENS: frozenset[str] = frozenset({
    "definition", "declaration", "item", "specification", "statement", "heading",
})

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
    # `signature` and `docstring` stood here until 2026-07-31, populated on every symbol and read
    # by nothing. There was no producer worth the name (`process()` returns None for both on
    # python/go/rust/typescript), no consumer (`upsert_symbol`, store.py:118, does not take them)
    # and no column (the migration at store.py:80 drops it). This is the dataclass-level form of
    # exactly what SC6 already bans for columns, which is why SC6 now covers the dataclass too:
    # a field that is written and never read is a claim the schema does not honour, and it reads
    # as available data to anyone extending the extractor.


def symbol_id(file: str, name: str, start_line: int) -> str:
    return hashlib.sha256(f"{file}:{name}:{start_line}".encode()).hexdigest()[:16]


def _named_children(node) -> list:
    """`node`'s named children, left to right. One place, so the stack walks agree."""
    return [node.named_child(i) for i in range(node.named_child_count)]


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
        k = cur.type
        child_parent = par
        if any(k.endswith(s) for s in _GENERIC_DEF_SUFFIXES):
            name_node = cur.child_by_field_name("name")
            if name_node:
                name = code_bytes[
                    name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
                if _is_name_text(name):
                    qname = f"{par}.{name}" if par else name
                    if "function" in k or "method" in k or "func" in k:
                        sym_kind = "function"
                    elif "class" in k or "struct" in k or "trait" in k or "interface" in k:
                        sym_kind = "class"
                    elif "field" in k or "property" in k or "parameter" in k:
                        # Not a "conservative" default — calling a struct field a function is a
                        # confident wrong answer, and it is what this branch used to emit. Go's
                        # `type Point struct { X int }` yielded `X` with kind `function`, which
                        # is exactly what W1-A part 5 exists to catch. These tokens are the
                        # grammar's own node-kind spelling, like `struct`/`trait` above, so no
                        # vocabulary about source text is being invented (P6/HR15).
                        sym_kind = "field"
                    else:
                        sym_kind = "function"  # conservative default
                    result.append(Symbol(
                        file=file, name=name, qualified_name=qname, kind=sym_kind,
                        start_line=cur.start_point[0] + 1,
                        end_line=cur.end_point[0] + 1,
                        language=lang,
                    ))
                    child_parent = name
        stack.extend((c, child_parent) for c in reversed(_named_children(cur)))
    return result


# H2 helpers: generic call-node detection (replaces the old per-language call-node table)

# S5 (named-binding arm): a function value bound to a name. `const getUser = () => {}` is a
# definition in every sense that matters to search — it has a name, a body and callers — but
# `process()` reports the *function* node, which is genuinely anonymous, so it arrived as
# `name=None` and was dropped. Measured: 3,823 javascript files fleet-wide with 0 symbols, of
# which ~29.5% are dark for exactly this reason.
#
# The name lives on the binding, not the function, so it is read from the binding node's own name
# field. These are node kinds and field names — the grammar's spelling of its own structure, the
# same basis `_DEF_PARENT_TOKENS` and `_generic_walk`'s kind tests stand on — not a vocabulary
# about source text (P6/HR15).
# Several name fields per kind because the grammars disagree about the spelling of the same
# construct: a class field is `public_field_definition`/`name` in typescript and
# `field_definition`/`property` in javascript. Measured, not assumed — with only `name` listed,
# `class K { m = () => {}; }` recovered under typescript and stayed dark under javascript.
_BINDING_NAME_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("variable_declarator", ("name",)),               # const/let/var f = ...
    ("assignment_expression", ("left",)),             # f = ...  (and `obj.f = ...`)
    ("pair", ("key",)),                               # { f: () => {} }
    ("public_field_definition", ("name", "property")),
    ("field_definition", ("property", "name")),
    ("property_signature", ("name",)),
)
_BINDING_VALUE_FIELDS: tuple[str, ...] = ("value", "right")
# Function-shaped values. `function_declaration` is absent on purpose: it carries its own name and
# `process()` already reports it, so including it here would only re-emit what is not missing.
_FUNCTION_VALUE_KINDS: frozenset[str] = frozenset({
    "arrow_function", "function_expression", "function", "generator_function",
    "generator_function_declaration", "method_definition",
})


def _named_binding_walk(root, code_bytes: bytes, file_str: str, language: str) -> list[Symbol]:
    """Symbols for anonymous function values that are bound to a name.

    Spans the *function*, names it from the *binding*: the symbol's lines have to be the body a
    reader lands on, while the name is the one callers write. `symbol_id` keys on file+name+line,
    so re-running this over a file `process()` already covered is idempotent — a binding whose
    value was named anyway collides with the row that already exists rather than doubling it.
    """
    out: list[Symbol] = []
    fields = dict(_BINDING_NAME_FIELDS)
    stack = [root]
    while stack:
        cur = stack.pop()
        name_fields = fields.get(cur.type)
        if name_fields is not None:
            value = next(
                (v for v in (cur.child_by_field_name(f) for f in _BINDING_VALUE_FIELDS)
                 if v is not None), None)
            name_node = next(
                (n for n in (cur.child_by_field_name(f) for f in name_fields)
                 if n is not None), None)
            if (value is not None and name_node is not None
                    and value.type in _FUNCTION_VALUE_KINDS):
                # `_unwrap_callee` is the existing reader for "this node is, or ends in, a name":
                # it takes the rightmost identifier of a member expression, which is what makes
                # `obj.handler = () => {}` land on `handler` and not on the whole path.
                name = _unwrap_callee(name_node, code_bytes)
                if name:
                    out.append(Symbol(
                        file=file_str, name=name, qualified_name=name, kind="function",
                        start_line=value.start_point[0] + 1,
                        end_line=value.end_point[0] + 1,
                        language=language,
                    ))
        stack.extend(reversed(_named_children(cur)))
    return out


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


def _is_title_text(name: str) -> bool:
    """The screen for a heading's name, which `_is_name_text` cannot be.

    Headings are multi-word — "Public-release hardening and the runnable-by-anyone contract" is a
    name a document gave a section of itself — and `_is_name_text` rejects all whitespace on
    purpose, because in an *identifier* whitespace means an unwrap fell through to a whole
    expression. Relaxing that predicate to admit headings would spend a code-wide guarantee on a
    prose problem, so the two are separate tests and the caller picks by kind.

    What survives from the original: a name must be non-blank, and it must not span lines. A
    heading is one line by construction, so a multi-line hit is the same fell-through signal.
    """
    return bool(name.strip()) and "\n" not in name


def _unwrap_callee(nn, code_bytes: bytes) -> str:
    """Unwrap member/attribute node to rightmost identifier; '' if not a name."""
    if nn is None:
        return ""
    if nn.type in _MEMBER_KINDS:
        # "field"=Go/JS, "property"=TS/JS, "attribute"=Python, "name"=Java/Kotlin
        nn = (nn.child_by_field_name("field") or nn.child_by_field_name("property")
              or nn.child_by_field_name("attribute") or nn.child_by_field_name("name") or nn)
    # S1: a name is a leaf. If the node still has named children after unwrapping it is an
    # expression — `factory()()`, `arr[i]()` — and its text is not a callee name.
    if nn.named_child_count:
        return ""
    name = code_bytes[nn.start_byte:nn.end_byte].decode("utf-8", errors="replace")
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
    return node.named_child(0) if node.named_child_count else None


def _highlight_walk(code_bytes: bytes, file_str: str, language: str) -> list[Symbol]:
    """Rung 4: symbols from the grammar's own `highlights.scm`, for grammars with no structure.

    Reach is the point — `highlights` exists for 237 of the pack's 306 languages, against 48 for
    `tags`, and the languages that arrive here are exactly the ones `process()` and the generic
    walk both come back empty on. Measured on that fallback set: scss yields `@mixin fc` and
    `@function double`, bash yields its two function definitions; dockerfile, hcl and groovy
    yield nothing and stay honestly at rung 6 (groovy and gradle ship no query files at all, so
    their 2,222 fleet files can never leave it, whatever rungs get built).
    """
    out: list[Symbol] = []
    seen: set[tuple[str, int]] = set()
    for capture, nodes in _highlight_captures(code_bytes, language).items():
        kind = _HIGHLIGHT_DEF_CAPTURES.get(capture)
        if kind is None:
            continue
        for node in nodes:
            parent = node.parent
            if parent is None or not _is_definition_parent(parent.type):
                continue
            name = code_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
            line = node.start_point[0] + 1
            # A heading is screened as a title, everything else as an identifier — see
            # `_is_title_text`. Stripped because the capture spans the heading's inline text and
            # its surrounding spaces are the marker's, not the name's.
            name = name.strip() if kind == "section" else name
            ok = _is_title_text(name) if kind == "section" else _is_name_text(name)
            if not ok or (name, line) in seen:
                continue
            seen.add((name, line))
            out.append(Symbol(
                file=file_str, name=name, qualified_name=name, kind=kind,
                start_line=line, end_line=node.end_point[0] + 1, language=language,
            ))
    return out


def _is_definition_parent(kind: str) -> bool:
    """Does this parent node kind name a definition? Token-split, like `_is_call_node`."""
    return not _is_call_node(kind) and bool(set(kind.split("_")) & _DEF_PARENT_TOKENS)


def _highlight_captures(code_bytes: bytes, language: str):
    """`(capture_name -> [node])` from the grammar's own highlights.scm, or `{}`.

    Parses a *second* time, with the raw `tree_sitter` binding rather than the pack's. The
    original reason was that the two shipped different node APIs; as of pack 1.14.0 they do not
    — `get_parser` now returns a real `tree_sitter.Parser` — so what is left is the narrower
    rule that a `QueryCursor` only accepts nodes from the tree its own binding parsed. Keeping
    the second parse is one `Parser` construction on a fallback path whose alternative is zero
    symbols; unifying it is a separate change with its own re-derive, not a free simplification.
    """
    try:
        import tree_sitter as ts
        from tree_sitter_language_pack import get_highlights_query, get_language
        qtext = get_highlights_query(language)
        if not qtext:
            return {}
        lang_obj = get_language(language)
        root = ts.Parser(lang_obj).parse(code_bytes).root_node
        return ts.QueryCursor(ts.Query(lang_obj, qtext)).captures(root)
    except Exception:
        return {}


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
        (node.named_child(i) for i in range(node.named_child_count)
         if node.named_child(i).type == kind), None,
    )


def _attr_value_text(attr_node, code_bytes: bytes) -> str:
    """Unquoted text of an HTML/SFC `attribute` node's value (e.g. lang="ts" -> "ts")."""
    vn = _child_of_kind(attr_node, "attribute_value")
    if vn is not None:
        return code_bytes[vn.start_byte:vn.end_byte].decode("utf-8", errors="replace")
    qvn = _child_of_kind(attr_node, "quoted_attribute_value")
    if qvn is not None:
        inner = _child_of_kind(qvn, "attribute_value")
        target = inner if inner is not None else qvn
        return code_bytes[
            target.start_byte:target.end_byte].decode("utf-8", errors="replace").strip("\"'")
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
        if cur.type != "script_element":
            stack.extend(reversed(_named_children(cur)))
            continue
        start_tag = _child_of_kind(cur, "start_tag")
        raw = _child_of_kind(cur, "raw_text")
        if raw is None:
            continue
        lang_attr = ""
        for attr in _named_children(start_tag) if start_tag else []:
            if attr.type != "attribute":
                continue
            name_node = _child_of_kind(attr, "attribute_name")
            if name_node is None:
                continue
            if code_bytes[
                    name_node.start_byte:name_node.end_byte].decode("utf-8", "replace") == "lang":
                lang_attr = _attr_value_text(attr, code_bytes)
                break
        inner_lang = _EMBEDDED_SCRIPT_LANG.get(lang_attr.lower(), "javascript")
        out.append((inner_lang, code_bytes[raw.start_byte:raw.end_byte], raw.start_point[0]))
    return out


def _injection_regions(query, matches, code_bytes: bytes, language: str
                       ) -> list[tuple[str, bytes, int]]:
    """`(inner_language, bytes, line_offset)` for each statically-declared injection match.

    `inner == language` is skipped: several grammars inject themselves (a template re-entering
    its own body), and following that is an unbounded re-parse of the same file.
    """
    from tree_sitter_language_pack import has_language
    out: list[tuple[str, bytes, int]] = []
    for pattern_index, caps in matches:
        try:
            inner = (query.pattern_settings(pattern_index) or {}).get("injection.language", "")
            if not inner or inner == language or not has_language(inner):
                continue
            for node in caps.get("injection.content", []):
                out.append((inner, code_bytes[node.start_byte:node.end_byte],
                            node.start_point[0]))
        except Exception:
            continue
    return out


def _injection_blocks(code_bytes: bytes, language: str) -> list[tuple[str, bytes, int]]:
    """Rung 1: embedded regions the host grammar itself declares, via its `injections.scm`.

    **Only statically-declared languages are taken**, and that line is structural, not convenient.
    A `(#set! injection.language "php")` is the grammar author stating that this region is always
    php — a property of the host grammar, same species as `_STRUCTURE_KIND_MAP`. A dynamic
    `@injection.language` *capture* instead takes the language from the document's own text (a
    markdown fence's info string), which is reading a token to choose a grammar: the thing P6
    forbids, and it would enrol every fenced example in a README as a project definition.

    Measured across the pack's 110 injection-capable languages: **64 declare statically**, 12
    capture dynamically, 31 mark content with no language at all, and 3 (svelte, cairo, squirrel)
    encode it as the capture *name* in a third convention. Reading the static ones needs
    `matches()` rather than `captures()` — the language lives on the pattern, via
    `Query.pattern_settings(i)`, and `captures()` discards the pattern index.

    Parses a second time with the raw binding, for the reason `_highlight_captures` documents.
    """
    try:
        import tree_sitter as ts
        from tree_sitter_language_pack import get_injections_query, get_language
        qtext = get_injections_query(language)
        if not qtext:
            return []
        lang_obj = get_language(language)
        root = ts.Parser(lang_obj).parse(code_bytes).root_node
        query = ts.Query(lang_obj, qtext)
        matches = ts.QueryCursor(query).matches(root)
    except Exception:
        return []
    return _injection_regions(query, matches, code_bytes, language)


def _embedded_blocks(root, code_bytes: bytes, language: str) -> list[tuple[str, bytes, int]]:
    """Every embedded block in this file: the `script_element` walk, then rung 1's injections.

    **Rung 1 supplements `_iter_script_blocks`; it does not replace it**, which is what the plan
    proposed. Measured: **vue's injections query is empty**, and vue is the highest-coverage host
    grammar in the fleet (92 %, 2,020 of 2,190 files) — so the swap would have been a straight
    regression on the case that matters most. php's and erb's are empty too.

    Dedup is on the content *region*, not the triple, and the hand-rolled walk wins ties because
    it is strictly better informed: it reads `<script lang="ts">` and says typescript, where
    html's `#set!` can only say javascript for every script element. Keyed on the triple, those
    two would disagree, both be kept, and the file would report double its symbols.
    """
    out = list(_iter_script_blocks(root, code_bytes))
    seen = {(off, len(blk)) for _, blk, off in out}
    for inner_lang, blk, off in _injection_blocks(code_bytes, language):
        if (off, len(blk)) not in seen:
            seen.add((off, len(blk)))
            out.append((inner_lang, blk, off))
    return out


# `extract_calls` and its helper `_collect_call_names` were deleted 2026-07-31. Zero callers —
# `sweeps._extract_graph` has always used `extract_calls_with_lines` — but they were removed for
# being *wrong*, not merely unused, and the difference matters to whoever reads the two functions
# side by side next. `extract_calls` recursed through `_embedded_blocks`, which includes rung-1
# injections, and `extract_calls_with_lines` walks only `_iter_script_blocks`. That asymmetry
# looks exactly like a one-line bug worth closing.
#
# It is not. Running both variants over all 17,945 host-language files in the fleet (2026-07-30):
# html gains +191 call names and markdown +6, and **every one is CSS** — `var` ×158, `rgba` ×24,
# `blur`, `linear-gradient` — because html's `injections.scm` statically injects css and
# tree-sitter's css grammar reports `var(--x)` as a call node. php, rust, vue, svelte and nix gain
# zero. Adopting the "fix" would enrol stylesheet functions as code call edges across the fleet.
# Written up in docs/decisions/2026-07-31-call-extraction-skips-injections-deliberately.md.


# Every value `graph.store.file_extraction.rung` may hold: the ladder rungs this module can
# report, then the three outcomes only the caller can observe (`daemon/sweeps.py`), because by
# then the extraction function never ran. Declared in one place so the store, the sweep, the
# metrics block and the guard test cannot drift apart — the earlier version had each of them
# spelling the strings itself, which is how "timeout" came to mean three different events.
EXTRACTION_RUNGS: tuple[str, ...] = (
    "structure", "generic", "highlights", "data", "embedded", "language_mismatch",
    "unparsed", "no_grammar", "no_language", "timeout", "crashed", "error",
)

# Rung 5's bounds. Depth 2 because that is where the useful key lives in every shape this rung
# exists for — `services.web`, `scripts.build`, `jobs.build` — while depth 1 alone would yield
# only the section headers. The cap is a flood guard, not a visibility one: `package-lock.json`
# has thousands of depth-2 keys, and a file that emitted 500 symbols has conspicuously not
# disappeared, which is the invariant the rung column exists to protect.
_DATA_MAX_DEPTH = 2
_DATA_MAX_SYMBOLS = 500

# X2's threshold: the fraction of a file's bytes sitting under ERROR/MISSING nodes above which
# the bytes are taken to contradict the language the extension claims. Measured 2026-07-30, and
# the margin is why one number suffices: XML bytes parsed as rust/typescript/python/go score
# 0.916-1.000 and python bytes parsed as rust score 1.000, while *degraded but correct* source
# scores far below — truncation mid-token 0.000 (tree-sitter recovers), minified javascript
# 0.000, NUL bytes injected into python 0.120. Nothing measured lands between 0.12 and 0.92.
_MISMATCH_ERROR_RATIO = 0.5


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
            outer_root = outer_parser.parse(code_bytes).root_node
        except Exception:
            outer_root = None
    try:
        r = ts_process(content, ProcessConfig(structure=True, language=language))
    except Exception:
        r = None
    syms, rung, anon = _extract_symbols_from(r, outer_root, code_bytes, file_str, language)
    if outer_root is not None:
        # After every rung has spoken, and before embedded blocks are merged — those recurse
        # through this same function and have already qualified themselves.
        _qualify_by_receiver(syms, outer_root, code_bytes)
        host_produced = bool(syms)
        for inner_lang, inner_bytes, line_offset in _embedded_blocks(
                outer_root, code_bytes, language):
            inner_src = inner_bytes.decode("utf-8", errors="replace")
            inner_syms, inner_stats = extract_symbols_with_stats(path, inner_src, inner_lang)
            # An embedded block's anonymous drops belong to the *host* file — a .svelte whose
            # <script> is all arrow functions must not read as an empty .svelte.
            anon += inner_stats.anon_count
            # Keyed on "the host contributed nothing", not on a list of rung names. The old
            # `rung in ("unparsed", "generic")` predated rungs 4/5 and `language_mismatch`, and
            # would have left a file reporting a rung that found no symbols while holding
            # symbols the embedded pass found — the exact contradiction the column exists to
            # prevent.
            if inner_syms and not host_produced:
                rung = "embedded"
            for s in inner_syms:
                syms.append(Symbol(
                    file=s.file, name=s.name, qualified_name=s.qualified_name, kind=s.kind,
                    start_line=s.start_line + line_offset, end_line=s.end_line + line_offset,
                    language=s.language,
                ))
    return syms, ExtractionStats(
        language=language, rung=rung, symbol_count=len(syms), anon_count=anon,
        has_error=_node_has_error(outer_root),
    )


def extract_symbols_calls_with_stats(
    path: Path, content: str, language: str
) -> tuple[list[Symbol], ExtractionStats, list[tuple[str, int]], list[str]]:
    """`extract_symbols_with_stats` plus the file's call sites, in one bounded-pool trip.

    `_extract_graph` walked the tree twice and paid `run_bounded` twice per file because callee
    resolution needs the whole symbol table, which does not exist until the first walk finishes.
    That ordering constraint is real; a second *IPC round trip* was never part of it. The caller
    now buffers the call sites instead (~11 MB on the largest project in the fleet: 2,516 files,
    94,867 call sites) and resolves them after the symbol commit.

    **Worth ~11%, not the ~28% this was filed as, and the spread is the useful part.** Measured
    2026-07-31 against an otherwise-identical checkout over five trees, 2,319 files, 3 reps each:
    java 13.90 -> 9.55 ms/file (-31%), php 28.54 -> 26.36 (-8%), ts -8%, js -4%, and **this repo
    35.13 -> 35.18, i.e. nothing at all.** The filed figure came from "IPC is 3.9 of 13.7 ms/file",
    which assumes the second trip costs what the first does — it does not. The second pass only
    parsed calls and already skipped every file with no symbols, so the saving scales with how
    cheap symbol extraction is *relative* to the call parse. Where files are large and
    symbol-dense the second parse was never the cost. Don't quote a single number for this.

    Deliberately a composition, not a shared parse tree: the two halves parse independently
    today, and threading one tree through both would rewrite `_extract_symbols_from` and
    `_collect_calls_with_lines` to buy a fraction of what the IPC and the second `read_text`
    already give. Module-level and picklable, which `run_bounded` requires.

    The fourth element is the file's import specifiers, added in e11 on the same argument that
    justified folding in the third: the ordering constraint is that resolution needs a table the
    walk has not built yet, and that has never required a second IPC round trip.
    """
    syms, stats = extract_symbols_with_stats(path, content, language)
    return (syms, stats, extract_calls_with_lines(content, language),
            extract_import_specs(content, language))


def _node_has_error(root) -> bool:
    """`has_error` is a property on some bindings and a method on others, so neither shape is
    assumed. Pack 1.12.1 exposed it as a method; 1.14.0 returns `tree_sitter.Node`, where it is
    a property. This function predates that move and survived it unchanged, which is the reason
    it is written this way.

    `bool(getattr(root, "has_error", False))` is the trap: against the method shape it returns
    True for every parsed file, measuring "the attribute exists" rather than "the parse failed".
    Measured: 43/43 svelte and 50/50 python files reported errors, which is the tell. Call it
    when callable, and treat a binding that exposes neither shape as "unknown" rather than
    inventing a verdict.
    """
    attr = getattr(root, "has_error", None)
    if attr is None:
        return False
    try:
        return bool(attr() if callable(attr) else attr)
    except Exception:
        return False


def _first_type_name(node, code_bytes: bytes) -> str:
    """The first valid-name leaf under `node`'s nearest `type` field, breadth-first."""
    queue = [node]
    while queue:
        cur = queue.pop(0)
        typed = cur.child_by_field_name("type")
        if typed is not None:
            inner = [typed]
            while inner:
                x = inner.pop(0)
                text = code_bytes[x.start_byte:x.end_byte].decode("utf-8", errors="replace")
                if x.named_child_count == 0 and _is_name_text(text):
                    return text
                inner.extend(_named_children(x))
        queue.extend(_named_children(cur))
    return ""


def _qualify_by_receiver(syms: list[Symbol], root, code_bytes: bytes) -> None:
    """Name a method by the type it is declared *on*, where the grammar says so in a field.

    §1d of the plan read `qualified_name == name` on the structure rung and concluded that no
    container is ever recorded there. That was true when measured and is no longer: e7's
    containment arm gives a member its enclosing class, and re-measuring the *proposed* span-nesting
    fix found it recovers nothing further — **0 symbols gained across 18,766 in seven languages**,
    because containment already reaches everything span nesting can reach.

    What containment structurally cannot reach is a method declared *outside* its type. Go's
    `func (s *Server) Start()` is a top-level declaration nested in nothing, so no enclosing span
    exists to name it, and it is stored as `Start`. Measured on a 400-file Go corpus: **43.5% of
    unqualified symbols (2,561 of 5,881) are that shape.** `query/graph_handler.py:15` resolves
    `WHERE name = ? OR qualified_name = ?`, so `graph(symbol="Server.Start")` is wired and cannot
    match today.

    The receiver is a grammar *field*, exactly like the `name` field `_generic_walk` already reads,
    so no vocabulary about source text is invented (P6/HR15) and any grammar spelling a receiver the
    same way gets this for free — nothing here is keyed on the language being Go.

    It reads the receiver's `type` **field** rather than its last identifier. The positional rule
    was tried first and is right on 641/641 real receivers, but `(s *Stack[T])` ends in the type
    *parameter*: it names `T` where the field-based rule names `Stack`.
    """
    if not any(s.qualified_name == s.name for s in syms):
        return
    by_line: dict[int, str] = {}
    stack = [root]
    while stack:
        cur = stack.pop()
        recv = cur.child_by_field_name("receiver")
        if recv is not None:
            owner = _first_type_name(recv, code_bytes)
            if owner:
                by_line[cur.start_point[0] + 1] = owner
        stack.extend(_named_children(cur))
    for sym in syms:
        # Only fills a gap. `_generic_walk` tracks its own parent as it descends and its answer is
        # the better one; overwriting it here would be a regression dressed as coverage.
        if sym.qualified_name != sym.name:
            continue
        owner = by_line.get(sym.start_line)
        if owner and owner != sym.name:
            sym.qualified_name = f"{owner}.{sym.name}"


def _error_byte_ratio(root, total_bytes: int) -> float:
    """Fraction of the file covered by ERROR/MISSING nodes — X2's evidence, and only that.

    Deliberately not called "is this the wrong language": what it measures is that the bytes do
    not parse as the grammar the extension claims, which wrong-language bytes and catastrophically
    broken source both produce. The rung is named for the common case because that is what the
    fleet actually holds — a Katalon project's `.rs`/`.ts` files are XML, 2,439 of them — but this
    docstring is where the honest reading lives, so a later reader does not take the rung as a
    claim that the *real* language was identified. It was not; nothing here guesses one.

    Nested errors are not double-counted: an ERROR node's subtree is not descended into.

    **Plan item #10 proposed changing exactly that and it is costed and declined (2026-08-03).**
    The proposal — credit an ERROR for the bytes its well-formed named descendants cover — is right
    about the symptom: one PHP-5 `$_priv{PRIV_DELETE}` near the top of a `*.tpl.php` template makes
    the *root* an ERROR spanning all 39,630 bytes, so 746 fleet files score 1.0 and are labelled
    `language_mismatch` though 19,562 `function_call_expression` nodes parsed cleanly inside. It is
    wrong that the credit separates that file from genuinely wrong-language bytes. Measured against
    RB1's XML fixture under five claimed languages and 300 real `*.tpl.php`:

    | credit rule | XML-as-go | XML-as-rust | tpl.php still flagged |
    |---|---:|---:|---:|
    | none (this code) | 0.907 | 1.000 | 110/300 |
    | any named child | 0.113 | 0.134 | 0/300 |
    | children with named children | 0.144 | 0.289 | 0/300 |
    | children with >= 2 named children | 0.227 | 0.567 | 0/300 |

    Every rule that rescues the templates also drops XML below the 0.85 threshold, because
    tree-sitter's error recovery manufactures structurally plausible children out of garbage — so
    "are this ERROR's descendants well-formed" does not discriminate. The only thing that would is
    naming the node kinds real PHP produces, which is the mapping table P6/HR15 exists to forbid.

    What is being bought is also small: `language_mismatch` vs `generic` is a **label**, on files
    that legitimately define no symbols either way (§1g of the plan measured that re-parsing them as
    an HTML host rescues 2% — 14 of 746). Trading a working wrong-language detector for a nicer name
    on 1.8% of the fleet fails P10. RB1 and EL10 are the tests that said so, on the first run.
    """
    if root is None or total_bytes <= 0:
        return 0.0
    bad, stack = 0, [root]
    while stack:
        node = stack.pop()
        try:
            if node.type in ("ERROR", "MISSING"):
                bad += node.end_byte - node.start_byte
                continue
            stack.extend(node.named_child(i) for i in range(node.named_child_count))
        except Exception:
            return 0.0
    return bad / total_bytes


def _data_root(code_bytes: bytes, language: str):
    """`ProcessResult.data` for `language`, or None. Separate call — `data_extraction` is off in
    the rung-3 `ProcessConfig`, and paying for it on every file to serve the fallback path would
    invert the ladder's whole economy."""
    try:
        from tree_sitter_language_pack import ProcessConfig
        from tree_sitter_language_pack import process as ts_process
        return ts_process(code_bytes.decode("utf-8", errors="replace"),
                          ProcessConfig(data_extraction=True, language=language)).data
    except Exception:
        return None


def _data_walk(code_bytes: bytes, file_str: str, language: str) -> list[Symbol]:
    """Rung 5: top-level structure of a data file, from `process(data_extraction=True).data`.

    The output lands on `ProcessResult.data` as a `DataNode` tree (`key` / `value` / `span` /
    `children`) — **not** on `.data_extraction`, which does not exist and cost one probe to find
    out. Measured: hcl yields `resource."aws_s3_bucket"."b"` and `variable."v"`, yaml a compose
    file's `services` and its services, json its `scripts` and each script, toml its tables.

    These are the files X4 called "legitimately symbol-free", and for markdown or a `.env.example`
    that stays true. For a compose file, a k8s manifest, a `package.json` or a CI workflow it never
    was: the file has structure, tree-sitter can see it, and nothing was asking. `kind="data"`
    keeps it distinguishable downstream, and call resolution cannot confuse it with code because
    `sweeps._extract_graph` keys on `(family, name)` and `language_family` makes yaml/json/toml/hcl
    each their own family — a Go `build()` cannot bind to a `package.json` `build` key.
    """
    root = _data_root(code_bytes, language)
    out: list[Symbol] = []
    # A data node's span ends at the byte *after* its last character, so a block that owns its
    # trailing newline reports the row of a line that does not exist — measured on yaml, where the
    # final mapping of a 4-line file came back `end_line=5`. `graph(relation="definition")` slices
    # source by these lines, so a row past the end is a wrong answer rather than a cosmetic one.
    # Clamping here rather than in the reader keeps the store's spans true of the file they name.
    last_line = max(1, code_bytes.count(b"\n") + (0 if code_bytes.endswith(b"\n") else 1))
    stack: list[tuple[object, int, str]] = [(root, 0, "")] if root is not None else []
    while stack and len(out) < _DATA_MAX_SYMBOLS:
        node, depth, prefix = stack.pop()
        key = getattr(node, "key", None)
        qual = f"{prefix}.{key}" if prefix and key else (key or prefix)
        if key and _is_name_text(str(key)):
            span = getattr(node, "span", None)
            line = min((span.start_line + 1) if span is not None else 1, last_line)
            end = min((span.end_line + 1) if span is not None else line, last_line)
            out.append(Symbol(
                file=file_str, name=str(key), qualified_name=str(qual), kind="data",
                start_line=line, end_line=max(line, end), language=language))
        if depth < _DATA_MAX_DEPTH:
            for child in reversed(getattr(node, "children", None) or []):
                stack.append((child, depth + 1, str(qual)))
    return out


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
                language=language,
            ))
        # S5: recover the ones that were anonymous only as *values*. Gated on `anon` so a file
        # process() named completely pays nothing, and keyed by (name, start_line) — the same
        # identity `symbol_id` uses — so a recovered symbol can never shadow a structure entry.
        if anon and outer_root is not None:
            have = {(sym.name, sym.start_line) for sym in syms}
            for sym in _named_binding_walk(outer_root, code_bytes, file_str, language):
                if (sym.name, sym.start_line) not in have:
                    have.add((sym.name, sym.start_line))
                    syms.append(sym)
        # `anon` is deliberately NOT decremented per recovery, though the first cut of this arm
        # did. It counts *structure entries process() could not name*, and this is a different
        # walk over the same tree: the two do not correspond one-to-one. Measured on the EL3
        # fixture — process() drops one entry, the arm recovers two symbols — so subtracting
        # reports a number that is neither the drop count nor the recovery count. Coverage is
        # `symbol_count`; `anon_count` stays a measurement of what process() alone could do,
        # which is what EL3/EL5 pin it as.
        # process() may yield only class/module nodes (e.g. Java, Kotlin) with no methods.
        # Supplement via _generic_walk so method names enter the symbol table for call-edge resolution.
        #
        # The old gate was `if not any(function/method)` alone, and it asked the wrong question:
        # "did this *file* yield a function anywhere", when what it means is "did this *class*
        # yield its members". Measured 2026-07-30 by the ground-truth fixtures — `class Shape`
        # with a method `area` extracts both, and then *adding an unrelated top-level* `def make`
        # makes `area` disappear. Same shape in typescript: `class Svc { run() }` plus
        # `export function go()` loses `run`. Adding code removed symbols, which no extractor may
        # do, and it was invisible because every metamorphic property here is an invariance and
        # MM2's fixtures were two bare functions with no container between them.
        #
        # The containment arm asks the intended question generically: a reported class/module
        # whose span encloses a generic-walk name that structure did not report gets that name.
        # No per-language vocabulary — the test is span nesting. Kept as a *union* with the old
        # arm rather than a replacement so nothing that extracts today can lose a symbol.
        if outer_root is not None:
            known = {s.name for s in syms}
            extra = [s for s in _generic_walk(outer_root, code_bytes, file_str, language)
                     if s.name not in known]
            if not any(s.kind in ("function", "method") for s in syms):
                syms.extend(extra)
            elif extra:
                spans = [(s.start_line, s.end_line) for s in syms
                         if s.kind in ("class", "module")]
                # Members only. A struct's *fields* are inside the container span too, and
                # admitting them here is how this arm would have paid for a recovered method with
                # a `Foo.x` that call resolution could bind a `x()` call to (measured on rust).
                syms.extend(e for e in extra if e.kind != "field" and any(
                    lo <= e.start_line and e.end_line <= hi for lo, hi in spans))
        return syms, "structure", anon
    # process() returned no structure — fall back to generic AST walk, then to rung 4.
    if outer_root is None:
        return [], "unparsed", 0
    generic = _generic_walk(outer_root, code_bytes, file_str, language)
    if generic:
        return generic, "generic", 0
    # Rung 4 before 5, because it is the weakest *code* evidence in the ladder: `highlights.scm`
    # says what a token *is*, never where it is defined, so it is trusted only where nothing
    # stronger spoke.
    highlights = _highlight_walk(code_bytes, file_str, language)
    if highlights:
        return highlights, "highlights", 0
    data = _data_walk(code_bytes, file_str, language)
    if data:
        return data, "data", 0
    # Nothing extracted. Only now is the mismatch check worth its tree walk, and only now can its
    # answer matter: a file that reached a rung parsed well enough to yield symbols, whatever its
    # extension claims. Running it first would cost every file in the fleet a second walk to
    # diagnose the few thousand that produce nothing.
    if _error_byte_ratio(outer_root, len(code_bytes)) >= _MISMATCH_ERROR_RATIO:
        return [], "language_mismatch", 0
    # `generic` — "parsed, nothing found", which is what it has always meant. Reached only when
    # the bytes *do* parse as the claimed language and simply define nothing (X4).
    return [], "generic", 0


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
        if _is_call_node(cur.type):
            name = _unwrap_callee(_callee_node(cur), code_bytes)
            if name:
                out.append((name, cur.start_point[0] + 1))
        stack.extend(reversed(_named_children(cur)))


def _is_import_node(kind: str) -> bool:
    """S12: is this grammar node an import? Matched on node-type *tokens*, like `_is_call_node`.

    Every import-ish node type in the pack spells it as its own `_`-separated token:
    `import_statement` and `import_from_statement` (python, js/ts), `import_declaration` and
    `import_spec` (go, java), `namespace_use_declaration` (php), `use_declaration` (rust).
    Splitting on `_` keeps all of those without matching by accident, and — the point — without
    a per-language table naming which one each grammar chose.
    """
    return bool(_IMPORT_TOKENS & set(kind.split("_")))


# The grammar names the module path in a field, and where it does not, the path is the first
# path-shaped child. Both are structural reads, the same standing as `_generic_walk` reading
# `name` or e10's receiver arm reading `type`.
_IMPORT_TOKENS: frozenset[str] = frozenset({"import", "use", "require"})
_PATH_FIELDS: tuple[str, ...] = ("source", "module_name", "path", "name")
# A module path is one token however deep the grammar nests it. python's `dotted_name` and php's
# `namespace_name` have identifier children, and descending into them yields `rag_search`,
# `graph`, `extractor` as three specifiers instead of one path — measured: it read 5,988
# specifiers on this repo against the 2,155 imports actually there, and resolved none of them.
_PATH_NODES: frozenset[str] = frozenset({
    "string", "string_literal", "interpreted_string_literal", "raw_string_literal",
    "encapsed_string", "dotted_name", "namespace_name", "qualified_name", "identifier", "name",
})


def _collect_import_specs(node, code_bytes: bytes, out: list) -> None:
    """D6: stack, not recursion — see `_generic_walk`."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if not _is_import_node(cur.type):
            stack.extend(_named_children(cur))
            continue
        inner = [cur]
        while inner:
            x = inner.pop()
            got = None
            for fld in _PATH_FIELDS:
                n = x.child_by_field_name(fld)
                if n is not None and n.type in _PATH_NODES:
                    got = n
                    break
            if got is None:
                # php names nothing: `namespace_use_clause` holds a bare `qualified_name` child.
                # Taking the first path-shaped child is the same read as the field, for grammars
                # that declare none. Without it php reads zero imports — measured on a 1,116-`use`
                # store before this branch existed.
                for c in _named_children(x):
                    if c.type in _PATH_NODES:
                        got = c
                        break
            if got is not None:
                text = code_bytes[got.start_byte:got.end_byte].decode("utf-8", errors="replace")
                spec = text.strip("\"'`")
                if spec:
                    out.append(spec)
                continue
            inner.extend(_named_children(x))


def extract_import_specs(content: str, language: str) -> list[str]:
    """Return the module specifier of each import (S12: generic, any language).

    The *specifier*, not a resolved path: turning `./foo` or `App\\Models\\User` into a file is a
    filesystem question needing the repo root and the manifest, neither of which this function
    has and neither of which belongs in a per-file pure extractor. `_extract_graph` resolves.
    """
    parser, ok = _get_parser_for(language)
    if not ok:
        return []
    code_bytes = content.encode("utf-8", errors="replace")
    try:
        root = parser.parse(code_bytes).root_node
    except Exception:
        return []
    out: list[str] = []
    _collect_import_specs(root, code_bytes, out)
    for inner_lang, inner_bytes, _off in _iter_script_blocks(root, code_bytes):
        # A .vue component imports its children from inside <script>, and those imports are the
        # single largest gap this change closes: measured, *not one* of a vue store's import
        # pairs was already in the call graph, because a component is used in the template and
        # never called.
        out.extend(extract_import_specs(inner_bytes.decode("utf-8", errors="replace"), inner_lang))
    return out


def extract_calls_with_lines(content: str, language: str) -> list[tuple[str, int]]:
    """Return (callee_name, line_number) for each call (H2: generic, any language)."""
    parser, ok = _get_parser_for(language)
    if not ok:
        return []
    code_bytes = content.encode("utf-8", errors="replace")
    try:
        root = parser.parse(code_bytes).root_node
    except Exception:
        return []
    out: list[tuple[str, int]] = []
    _collect_calls_with_lines(root, code_bytes, out)
    for inner_lang, inner_bytes, line_offset in _iter_script_blocks(root, code_bytes):
        inner_src = inner_bytes.decode("utf-8", errors="replace")
        out.extend(
            (name, line + line_offset)
            for name, line in extract_calls_with_lines(inner_src, inner_lang)
        )
    return out


