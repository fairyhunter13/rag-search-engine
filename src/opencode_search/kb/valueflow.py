"""Intra-procedural value-flow / constant-propagation over tree-sitter AST.

Resolves non-literal call arguments (const/var/field) through per-file
def-use maps.  Generic AST node-kind primitives only — no framework vocab,
no regex.  Language-agnostic: Go / Java / Python / TS / JS.

Feasibility: YASA (arXiv:2601.17390) — build-free AST data-flow at 31.8
KLOC/min.  We graft only the generic def/use primitives, NOT YASA's
per-framework handlers (which would violate the zero-vocab doctrine).
"""
from __future__ import annotations

# String literal kinds across tree-sitter grammars (structural, not vocab)
_STR_KINDS: frozenset[str] = frozenset({
    "interpreted_string_literal", "raw_string_literal",  # Go
    "string_literal",                                     # Java / Kotlin
    "string",                                             # Python
    "template_string", "string_fragment",                 # JS / TS
    "simple_string_literal",                              # Ruby
})
_ID_KINDS: frozenset[str] = frozenset({
    "identifier", "type_identifier", "field_identifier",
})


def _t(node, b: bytes) -> str:
    r = node.byte_range()
    return b[r.start:r.end].decode("utf-8", "replace")


def _first_str(node, b: bytes, depth: int = 3) -> str | None:
    """BFS for the first string literal within *node*, depth-limited."""
    if node.kind() in _STR_KINDS:
        return _t(node, b).strip("\"'`")
    if depth <= 0:
        return None
    for i in range(node.named_child_count()):
        v = _first_str(node.named_child(i), b, depth - 1)
        if v is not None:
            return v
    return None


def build_def_use(root, b: bytes) -> dict[str, str]:
    """Walk the file AST; return {identifier -> string_value} for all scopes.

    Intra-procedural only — does not follow calls.
    First lexical assignment wins (conservative).
    Covers: Go const/var/:=, Python =, JS/TS const/let/var, Java/Kotlin locals.
    """
    du: dict[str, str] = {}
    stk = [root]
    while stk:
        n = stk.pop()
        k = n.kind()

        # Go: const x = "v" / var x = "v"
        if k in ("const_spec", "var_spec"):
            nn, vn = n.child_by_field_name("name"), n.child_by_field_name("value")
            if nn and vn:
                name = _t(nn, b)
                v = _first_str(vn, b)
                if v is not None and name not in du:
                    du[name] = v

        # Go: x := "v"
        elif k == "short_var_decl":
            lft, rgt = n.child_by_field_name("left"), n.child_by_field_name("right")
            if lft and rgt and lft.named_child_count() == rgt.named_child_count() == 1:
                idn = lft.named_child(0)
                if idn.kind() in _ID_KINDS:
                    name = _t(idn, b)
                    v = _first_str(rgt.named_child(0), b)
                    if v is not None and name not in du:
                        du[name] = v

        # Python: x = "v"
        elif k == "assignment":
            ln, rn = n.child_by_field_name("left"), n.child_by_field_name("right")
            if ln and rn and ln.kind() in _ID_KINDS:
                name = _t(ln, b)
                v = _first_str(rn, b)
                if v is not None and name not in du:
                    du[name] = v

        # JS/TS: const/let/var x = "v"
        elif k == "variable_declarator":
            nn, vn = n.child_by_field_name("name"), n.child_by_field_name("value")
            if nn and vn and nn.kind() in _ID_KINDS:
                name = _t(nn, b)
                v = _first_str(vn, b)
                if v is not None and name not in du:
                    du[name] = v

        # Java/Kotlin: String x = "v"
        elif k in ("local_variable_declaration", "property_declaration"):
            for i in range(n.named_child_count()):
                d = n.named_child(i)
                if d.kind() == "variable_declarator":
                    nn = d.child_by_field_name("name")
                    vn = d.child_by_field_name("value") or d.child_by_field_name("initializer")
                    if nn and vn and nn.kind() in _ID_KINDS:
                        name = _t(nn, b)
                        v = _first_str(vn, b)
                        if v is not None and name not in du:
                            du[name] = v

        stk.extend(n.named_child(i) for i in range(n.named_child_count() - 1, -1, -1))
    return du


def resolve_arg(arg, b: bytes, du: dict[str, str]) -> str | None:
    """Resolve one AST argument node to its string value.

    Order: literal fast-path → def-use identifier → selector field.
    Returns None for true dynamics (reflection, runtime results) — these
    propagate down to the GPU-rank and verified-LLM tiers.
    """
    v = _first_str(arg, b, depth=2)
    if v is not None:
        return v
    if arg.kind() in _ID_KINDS:
        return du.get(_t(arg, b))
    if arg.kind() in (
        "selector_expression", "member_expression", "attribute",
        "qualified_identifier", "field_access",
    ):
        fld = (arg.child_by_field_name("field")
               or arg.child_by_field_name("attribute")
               or arg.child_by_field_name("property"))
        if fld and fld.kind() in _ID_KINDS:
            return du.get(_t(fld, b))
    return None


def resolve_first_arg(args, b: bytes, du: dict[str, str]) -> str | None:
    """Resolve the first named child of an argument-list node to a string.

    Tries the first named child only (conservative — the path/topic arg
    is always first in gRPC/HTTP/pub-sub call conventions).
    """
    if args.named_child_count() == 0:
        return None
    return resolve_arg(args.named_child(0), b, du)
