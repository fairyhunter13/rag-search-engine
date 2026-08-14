"""PHP receiver types, so a call site names one callee instead of every same-named definition.

`sweeps._extract_graph` resolves a call by *name*, prefers the caller's own file, and drops the
edge when more than `_MAX_CALLEE_FANOUT` candidates survive — precision 1.000 at recall 0.633. The
drops are not wrong edges; they are edges name evidence alone cannot choose between. What this
module adds is the evidence: a receiver's declared type, the file's own `namespace`/`use` map,
`composer.json`'s PSR-4 map, and the `extends`/trait chain above the receiver's class. A call site
is only resolved when that narrows the pool to **exactly one** symbol, which is the standard the cap
already enforces — so recall rises and the cap stays 1.

HR15: nothing here infers semantics from a name, a keyword list, or a mapping table. Every type
read is one the source *declares* — a typed property, a typed parameter, `new X()`, `$this`,
`self`/`static`/`parent` — and every name-to-file step is either a PSR-4 prefix the repo checked
into `composer.json` or a class this index already parsed. A step that names no file resolves to
nothing rather than to a guess.

Measured before it shipped, by `scripts/probe_php_receivers.py` over 105 PHP roots / 22,842 files /
585,970 call sites: 63,917 dropped sites, **13,394 recovered (21.0%)**, +12.0% resolved call sites.
The two dialects read very differently and both are worth having: on Laravel the type hops carry it
(3,490 direct against 877 via the chain, median 37.4% recovery per root), while CodeIgniter 3
declares almost no types at all (510 direct out of 40,576 drops, 83.6% of them with no receiver type
at all) and is carried instead by the chain walk resolving `$this->m()` and `self::m()` onto an
ancestor — 6,010 edges no type-declaration tier would ever have found.

`_read_call` takes the callee name and line from `extractor`'s own `_callee_node`/`_unwrap_callee`
rather than reading the grammar a second time, because the hints are only ever consumed joined back
onto `extract_calls_with_lines` by `(name, line)`. Deriving that pair independently looked right and
joined on 44-87% of call sites, one root as low as 6.1%: PHP names a plain call's callee field
`function`, not `name`, so every bare `env(...)` in the fleet was missing, and the shipped collector
records the *call node's* line, which differs whenever a name wraps. 100% on all 105 roots after.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rag_search.graph.extractor import _callee_node, _is_call_node, _unwrap_callee

_CLASS_KINDS = ("class_declaration", "interface_declaration", "trait_declaration")
_FUNC_KINDS = ("method_declaration", "function_definition")
_NAMEY = ("name", "qualified_name")


def _txt(node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _type_name(node, src: bytes) -> str:
    """The one class a type node names, or "" for a primitive, a union, or no type at all.

    A union names more than one class, so it can never narrow a pool to one and is dropped.
    """
    if node is None:
        return ""
    if node.type == "optional_type":
        return _type_name(node.child(1) if node.child_count > 1 else None, src)
    return _txt(node, src).lstrip("\\") if node.type == "named_type" else ""


@dataclass(slots=True)
class FileFacts:
    """What one PHP file declares, and the call sites in it tagged with their receivers.

    A receiver is a hint *string*, not a node, because everything the later hops need from it is
    decided during the walk while the enclosing class and method are still in hand. Tags: `@rel|cls`
    for `$this`/`self`/`static`/`parent`, `Cname` for `Foo::m()`, `Pcls|prop` for
    `$this->prop->m()`, `Vmeth|var` for a local, `""` for a receiver nothing declares.
    """

    namespace: str = ""
    uses: dict[str, str] = field(default_factory=dict)
    classes: dict[str, tuple[list[str], dict[str, str]]] = field(default_factory=dict)
    locals: dict[tuple[str, str], str] = field(default_factory=dict)
    calls: list[tuple[str, int, str]] = field(default_factory=list)

    def hints(self) -> dict[tuple[str, int], str]:
        """`(callee name, line) -> receiver hint`, the key `extract_calls_with_lines` hands back.

        Two calls to the same name on one line are one key with two receivers, and there is no
        evidence here to say which is which — so the key is dropped rather than guessed. That is
        the cap's own rule applied one level earlier: a hint that might belong to the other call
        would narrow the pool to a confidently wrong edge, which is the one outcome worse than the
        drop this whole tier exists to undo.
        """
        out: dict[tuple[str, int], str] = {}
        for name, line, hint in self.calls:
            key = (name, line)
            if out.setdefault(key, hint) != hint:
                out[key] = ""
        return out


def parse_facts(src: bytes) -> FileFacts | None:
    """Parse one PHP source and return what it declares, or None if it will not parse."""
    from tree_sitter_language_pack import get_parser
    try:
        tree = get_parser("php").parse(src)
    except Exception:
        return None
    facts = FileFacts()
    try:
        _walk(tree.root_node, src, facts, cls="", meth="")
    except (RecursionError, ValueError):
        return None
    return facts


def _walk(node, src: bytes, f: FileFacts, *, cls: str, meth: str) -> None:
    for n in node.children:
        sub_cls, sub_meth = cls, meth
        if n.type == "namespace_definition":
            nn = n.child_by_field_name("name")
            f.namespace = _txt(nn, src) if nn is not None else f.namespace
        elif n.type == "namespace_use_declaration":
            _read_use(n, src, f)
        elif n.type in _CLASS_KINDS:
            sub_cls, sub_meth = _read_class(n, src, f), ""
        elif n.type in _FUNC_KINDS:
            mn = n.child_by_field_name("name")
            sub_meth = _txt(mn, src) if mn is not None else ""
            _read_params(n, src, f, sub_meth)
        elif n.type == "assignment_expression":
            _read_new(n, src, f, meth)
        elif _is_call_node(n.type):
            _read_call(n, src, f, cls, meth)
        _walk(n, src, f, cls=sub_cls, meth=sub_meth)


def _read_use(node, src: bytes, f: FileFacts) -> None:
    stack = [node]
    while stack:                                    # `use A\{B, C}` nests the clauses
        n = stack.pop()
        if n.type != "namespace_use_clause":
            stack.extend(n.children)
            continue
        names = [c for c in n.children if c.type in _NAMEY]
        if names:
            fqn = _txt(names[0], src).lstrip("\\")
            alias = _txt(names[1], src) if len(names) > 1 else fqn.rsplit("\\", 1)[-1]
            f.uses[alias] = fqn


def _read_class(node, src: bytes, f: FileFacts) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return ""
    parents: list[str] = []
    props: dict[str, str] = {}
    for n in node.children:
        if n.type in ("base_clause", "class_interface_clause"):
            parents += [_txt(c, src).lstrip("\\") for c in n.children if c.type in _NAMEY]
        elif n.type == "declaration_list":
            for m in n.children:
                if m.type == "use_declaration":              # a trait is a parent scope too
                    parents += [_txt(c, src).lstrip("\\") for c in m.children if c.type in _NAMEY]
                elif m.type == "property_declaration":
                    _read_property(m, src, props)
    name = _txt(name_node, src)
    f.classes[name] = (parents, props)
    return name


def _read_property(node, src: bytes, props: dict[str, str]) -> None:
    tname = ""
    for c in node.children:
        if c.type in ("named_type", "optional_type"):
            tname = _type_name(c, src)
        elif c.type == "property_element" and tname and c.child_count:
            vn = c.child(0)
            if vn.type == "variable_name":
                props[_txt(vn, src).lstrip("$")] = tname


def _read_params(node, src: bytes, f: FileFacts, meth: str) -> None:
    """Typed parameters. A written type is a declaration, not an inference."""
    params = node.child_by_field_name("parameters")
    for p in (params.children if params is not None else ()):
        if p.type != "simple_parameter":
            continue
        tname = vname = ""
        for c in p.children:
            if c.type in ("named_type", "optional_type"):
                tname = _type_name(c, src)
            elif c.type == "variable_name":
                vname = _txt(c, src).lstrip("$")
        if tname and vname:
            f.locals[(meth, vname)] = tname


def _read_new(node, src: bytes, f: FileFacts, meth: str) -> None:
    """`$v = new X()` — the class is named literally; nothing is guessed from the variable."""
    lhs, rhs = node.child_by_field_name("left"), node.child_by_field_name("right")
    if lhs is None or rhs is None or lhs.type != "variable_name":
        return
    if rhs.type != "object_creation_expression":
        return
    cn = next((c for c in rhs.children if c.type in _NAMEY), None)
    if cn is not None:
        f.locals[(meth, _txt(lhs, src).lstrip("$"))] = _txt(cn, src).lstrip("\\")


def _read_call(n, src: bytes, f: FileFacts, cls: str, meth: str) -> None:
    # Callee name and line come from the *shipped* helpers, never from a second reading of the
    # grammar. The hint is only useful joined back onto `extract_calls_with_lines`' output, so any
    # disagreement about what a call is called or which line it sits on silently drops the hint:
    # deriving `name` from the `name` field and the line from the name node cost every bare
    # `env(...)` in the fleet (the field is `function`) and every call whose name wrapped onto a
    # later line than its opening node. Both were measured before this ever ran in a sweep.
    callee = _unwrap_callee(_callee_node(n), src)
    if not callee:
        return                                      # `$obj->$dynamic()` names nothing
    line = n.start_point[0] + 1
    if n.type == "function_call_expression":
        f.calls.append((callee, line, ""))
        return
    recv = n.child_by_field_name("object") or n.child_by_field_name("scope")
    hint = ""
    if recv is None:
        pass
    elif recv.type == "relative_scope":              # self, static, parent
        hint = f"@{_txt(recv, src)}|{cls}"
    elif recv.type in _NAMEY:                        # Foo::m()
        hint = "C" + _txt(recv, src).lstrip("\\")
    elif recv.type == "variable_name":
        var = _txt(recv, src).lstrip("$")
        hint = f"@this|{cls}" if var == "this" else f"V{meth}|{var}"
    elif recv.type == "member_access_expression":
        obj, fld = recv.child_by_field_name("object"), recv.child_by_field_name("name")
        if (obj is not None and fld is not None and obj.type == "variable_name"
                and _txt(obj, src).lstrip("$") == "this"):
            hint = f"P{cls}|{_txt(fld, src)}"
    f.calls.append((callee, line, hint))


# How far up an inheritance chain to walk. Bounded because a cyclic hierarchy is a parse artefact,
# not a reason to hang, and cycle-guarded besides.
_CHAIN_DEPTH = 8


class Resolver:
    """The whole-root view a single file's facts cannot have: PSR-4, and every class by FQN.

    Same split as `_ImportResolver`, and for the same reason — a file parse knows what the source
    *says*, but turning a class name into a file needs the repo root, its manifests, and the set of
    files this index actually holds.
    """

    def __init__(self, psr4: list[tuple[str, list]], facts: dict[str, FileFacts]) -> None:
        self.psr4, self.facts = psr4, facts
        self.by_fqn: dict[str, tuple[str, list[str], dict[str, str]]] = {}
        for path, f in facts.items():
            for short, (parents, props) in f.classes.items():
                self.by_fqn[f"{f.namespace}\\{short}" if f.namespace else short] = (
                    path, parents, props)

    def fqn_of(self, f: FileFacts, cname: str) -> str:
        """A class name as one file writes it, to a fully-qualified name.

        The `use` head is tried before the bare name so `use App\\Domain;` resolves `Domain\\Order`
        as well as a direct `use App\\Domain\\Order;` does.
        """
        cname = cname.lstrip("\\")
        head, _, rest = cname.partition("\\")
        if head in f.uses:
            return f.uses[head] + (f"\\{rest}" if rest else "")
        if f.namespace and f"{f.namespace}\\{cname}" in self.by_fqn:
            return f"{f.namespace}\\{cname}"
        return cname

    def file_of(self, fqn: str) -> str:
        """The file an FQN lives in — a parsed class, or a PSR-4 prefix checked against disk.

        Never assumed: a prefix mapping to a path with no file behind it resolves to nothing.
        """
        entry = self.by_fqn.get(fqn)
        if entry is not None:
            return entry[0]
        for prefix, dirs in self.psr4:
            if fqn != prefix and not fqn.startswith(f"{prefix}\\"):
                continue
            rest = fqn[len(prefix):].lstrip("\\").replace("\\", "/")
            for d in dirs:
                cand = Path(d) / f"{rest}.php"
                if str(cand) in self.facts or cand.is_file():
                    return str(cand)
        return ""

    def chain(self, fqn: str) -> list[str]:
        """`fqn` then its ancestors — `extends`, interfaces, trait `use`. Breadth-first, bounded."""
        out: list[str] = []
        seen, queue = {fqn}, [fqn]
        while queue and len(out) < _CHAIN_DEPTH:
            cur = queue.pop(0)
            out.append(cur)
            entry = self.by_fqn.get(cur)
            f = self.facts.get(entry[0]) if entry is not None else None
            for parent in (entry[1] if entry is not None and f is not None else ()):
                pf = self.fqn_of(f, parent)
                if pf not in seen:
                    seen.add(pf)
                    queue.append(pf)
        return out

    def receiver_class(self, f: FileFacts, hint: str) -> str:
        """The FQN the receiver holds, or "" when nothing in the source declares one."""
        tag, body = (hint[0], hint[1:]) if hint else ("", "")
        if tag == "@":                                      # $this, self, static, parent
            rel, _, cls = body.partition("|")
            own = self.fqn_of(f, cls) if cls else ""
            if rel != "parent" or not own:
                return own
            entry = self.by_fqn.get(own)
            return self.fqn_of(f, entry[1][0]) if entry is not None and entry[1] else ""
        if tag == "C":                                      # Foo::m()
            return self.fqn_of(f, body)
        if tag == "P":                                      # $this-><typed prop>->m()
            cls, _, prop = body.partition("|")
            for step in (self.chain(self.fqn_of(f, cls)) if cls else ()):
                entry = self.by_fqn.get(step)
                if entry is not None and prop in entry[2]:
                    return self.fqn_of(self.facts.get(entry[0], f), entry[2][prop])
            return ""
        if tag == "V":                                      # typed param, or `new X()` in scope
            meth, _, var = body.partition("|")
            tname = f.locals.get((meth, var), "")
            return self.fqn_of(f, tname) if tname else ""
        return ""

    def narrow(self, f: FileFacts, hint: str, cands: dict[str, int]) -> str:
        """The one file among `cands` the receiver's type names, or "" if nothing narrows it.

        `cands` maps a candidate file to how many symbols of the callee's name it holds, and a file
        holding two is never an answer: the bar is one *symbol*, not one file.
        """
        recv = self.receiver_class(f, hint)
        if not recv:
            return ""
        direct = self.file_of(recv)
        if cands.get(direct) == 1:
            return direct
        hits = {s for step in self.chain(recv) if cands.get(s := self.file_of(step)) == 1}
        return hits.pop() if len(hits) == 1 else ""
