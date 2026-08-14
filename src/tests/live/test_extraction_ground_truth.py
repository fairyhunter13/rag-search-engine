"""GT1-GT5 — hand-counted ground truth, one fixture per language family.

Part 5 of W1-A's audit apparatus, and the only part that can catch **the ladder confidently
extracting the wrong thing**. Every property in `test_extraction_metamorphic.py` is an
*invariance*: it relates one extraction to the extraction of a transformed input, so a rung that
consistently returns the wrong answer satisfies all of them. Only a hand-counted expectation can
say "these are the symbols this file defines, and nothing else is."

It earned that on its first run, 2026-07-30, finding two defects the whole apparatus above had
missed:

  * **A class lost its members whenever the file also held a top-level function.** `class Shape`
    with a method `area` extracted both; adding an unrelated `def make` made `area` vanish —
    same shape in typescript with `class Svc { run() }` plus `export function go()`. Adding code
    removed symbols. The `_generic_walk` supplement was gated on "did this *file* yield any
    function", when what it means is "did this *class* yield its members". Fixed in `e6` with a
    span-containment arm; MM2 now carries a container-bearing fixture so the general property
    (concatenation never loses a symbol) would catch the next one of these on its own.
  * **A struct field was reported as a function.** Go's `type Point struct { X int }` yielded
    `X` with kind `function` — a name that call resolution could bind a `X()` call to. The
    generic walk's "conservative default" was not conservative; it was confidently wrong.

Deliberately *not* a coverage test. The fixtures are tiny and the expectations exact, so this
file fails when extraction changes shape — which is the point. Where the ladder is known to miss
something, GT1 records the miss in `known_gaps` rather than asserting the fix, so the gap is a
documented fact with a name instead of an absence nobody can see.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from rag_search.graph.extractor import extract_symbols_with_stats

pytestmark = pytest.mark.live

_WORD = re.compile(r"^\w+$")


class GT:
    """One family fixture: source, the exact `(name, kind)` set it defines, and known gaps."""

    def __init__(self, family: str, lang: str, name: str, src: str,
                 expect: set[tuple[str, str]], rung: str = "structure",
                 known_gaps: tuple[str, ...] = (), sym_lang: str | None = None) -> None:
        self.family, self.lang, self.name, self.src = family, lang, name, src
        self.expect, self.rung, self.known_gaps = expect, rung, known_gaps
        self.sym_lang = sym_lang or lang

    def __repr__(self) -> str:  # pytest id
        return f"{self.family}:{self.lang}"


FIXTURES = [
    GT("indentation", "python", "a.py",
       "class Shape:\n"
       "    def area(self):\n"
       "        return 0\n"
       "\n"
       "def make():\n"
       "    return Shape()\n",
       {("Shape", "class"), ("area", "function"), ("make", "function")}),

    GT("c-like", "go", "a.go",
       "package geo\n"
       "\n"
       "type Point struct { X int }\n"
       "\n"
       "func (p Point) Norm() int { return p.X }\n"
       "\n"
       "func New(x int) Point { return Point{x} }\n",
       {("Norm", "method"), ("New", "function")},
       known_gaps=(
           "`type Point struct` is not extracted: process() reports no structure entry for a Go "
           "type declaration, and _generic_walk cannot reach the name either — Go hangs it on a "
           "nested `type_spec`, not on a `name` field of the `type_declaration`. Recorded, not "
           "asserted away: the fix is a generic-walk change measured fleet-wide on its own, not "
           "a rider on the e6 containment fix.",
       )),

    GT("jvm", "java", "A.java",
       "package p;\n"
       "\n"
       "public class Widget {\n"
       "    public int size() { return 1; }\n"
       "    private void reset() {}\n"
       "}\n",
       {("p", "module"), ("Widget", "class"),
        ("size", "function"), ("reset", "function")}),

    GT("stylesheet", "scss", "a.scss",
       "@mixin flex($dir) { display: flex; }\n"
       "@function double($n) { @return $n * 2; }\n"
       ".card { color: red; }\n",
       {("flex", "function"), ("double", "function")}, rung="highlights"),

    GT("data", "yaml", "a.yaml",
       "services:\n"
       "  web:\n"
       "    image: nginx\n"
       "  db:\n"
       "    image: postgres\n",
       {("services", "data"), ("web", "data"), ("db", "data")}, rung="data"),

    GT("template", "svelte", "a.svelte",
       '<script lang="ts">\n'
       "export function load(id: string) { return id }\n"
       "function helper() { return 1 }\n"
       "</script>\n"
       "\n"
       "<div>hi</div>\n",
       {("load", "function"), ("helper", "function")},
       rung="embedded", sym_lang="typescript"),

    # e7's named-binding arm, stated as ground truth rather than as a count. Every form here is a
    # function *value* that `process()` reports anonymously, and the two negatives carry the
    # weight: `notFn: 3` is a pair whose value is not a function, and `conf` is a binding whose
    # value is an object — GT1's exact-set equality is what makes their absence an assertion
    # rather than a hope. `handler` takes no parameter on purpose: `process()` names an arrow
    # after its sole parameter (`async (req) => req` yields a `function req`), which predates this
    # arm and is not what this fixture is for.
    GT("named-binding", "javascript", "a.js",
       "const getUser = () => ({ id: 1 });\n"
       "const helper = function () { return 2; };\n"
       "export const handler = async () => 1;\n"
       "const conf = { onClick: () => {}, notFn: 3 };\n"
       "class K { m = () => {}; }\n"
       "function named(q) { return q; }\n",
       {("getUser", "function"), ("helper", "function"), ("handler", "function"),
        ("onClick", "function"), ("m", "function"), ("K", "class"), ("named", "function")}),

    # e7's markdown arm. The multi-word heading is the point: `_is_name_text` rejects all
    # whitespace, so screening headings with it would have recovered `Title One` and dropped
    # `A longer heading here`, leaving markdown looking covered while losing most of it.
    GT("prose", "markdown", "a.md",
       "# Title One\n"
       "\n"
       "Some prose that defines nothing.\n"
       "\n"
       "## A longer heading here\n",
       {("Title One", "section"), ("A longer heading here", "section")}, rung="highlights"),
]

# GT3: sources that define nothing. Every one of these is a *call* or a reference, and the whole
# safety argument for rung 4 is that a highlights capture says what a token is, never where it is
# defined — python's `@function` fires on the callee of `helper(1)`, bash's on the `echo`, SQL's
# `type` on a table merely selected from. If any of these ever yields a symbol, the ladder has
# started inventing definitions, which is worse than extracting none.
NO_DEFS = [
    ("python", "a.py", "helper(1)\nother(2)\n"),
    ("bash", "a.sh", "echo hi\nls -l\n"),
    ("sql", "a.sql", "SELECT id FROM customers WHERE x = 1;\n"),
    ("scss", "a.scss", ".card { color: red; }\n"),
]


def _extract(fx: GT):
    return extract_symbols_with_stats(Path(fx.name), fx.src, fx.lang)


@pytest.mark.parametrize("fx", FIXTURES, ids=repr)
def test_gt1_each_family_extracts_exactly_its_hand_counted_symbols(fx: GT) -> None:
    """GT1 — exact set equality, both directions.

    `>=` would pass a ladder that also invented three symbols, and `<=` would pass one that
    extracted nothing. The two directions are reported separately because they mean opposite
    things: a missing name is a gap, an extra name is a false definition entering call
    resolution, and only the second can make `graph` answer a question wrongly rather than
    incompletely.
    """
    syms, st = _extract(fx)
    got = {(s.name, s.kind) for s in syms}
    assert st.rung == fx.rung, f"{fx.lang}: rung {st.rung}, expected {fx.rung}"
    missing, extra = fx.expect - got, got - fx.expect
    assert not missing, (
        f"{fx.lang}: missing {sorted(missing)} — got {sorted(got)}"
        + ("\nknown gaps recorded in the fixture:\n  " + "\n  ".join(fx.known_gaps)
           if fx.known_gaps else ""))
    assert not extra, (
        f"{fx.lang}: invented {sorted(extra)} — these enter call resolution as definitions")


@pytest.mark.parametrize("fx", FIXTURES, ids=repr)
def test_gt2_every_symbol_is_named_on_the_line_it_claims(fx: GT) -> None:
    """GT2 — `start_line` points at the line that actually spells the name.

    The sharpest single check for "confidently wrong", and it needs no per-fixture expectation:
    `graph(relation="definition")` slices source by these spans, so a symbol whose declared line
    does not contain its own name is a wrong answer served with full confidence. It generalises
    the span bug MM4 caught in `_data_walk` — a row inside the file, but the wrong row, which
    MM4's range check cannot see.

    Restricted to plain-word names because the composite ones are not literal source text: hcl
    spells `resource."aws_s3_bucket"."b"` with quotes the file does not repeat in that order.
    Whether a name is a plain word is a question about this fixture's expectations, not an
    inference about source semantics — no HR15 surface here.
    """
    lines = fx.src.splitlines()
    for s in _extract(fx)[0]:
        if not _WORD.match(s.name or ""):
            continue
        assert 1 <= s.start_line <= len(lines), \
            f"{fx.lang}: {s.name} at line {s.start_line} of {len(lines)}"
        assert s.name in lines[s.start_line - 1], (
            f"{fx.lang}: {s.name!r} claims line {s.start_line}, which reads "
            f"{lines[s.start_line - 1]!r}")


@pytest.mark.parametrize("lang,fname,src", NO_DEFS, ids=[c[0] for c in NO_DEFS])
def test_gt3_a_call_site_never_becomes_a_definition(lang: str, fname: str, src: str) -> None:
    """GT3 — sources that define nothing extract nothing.

    Ground truth for rung 4's filter, stated as the property rather than as an implementation
    detail. Measured unfiltered, `_highlight_walk` emitted `helper` and `echo` as definitions;
    the filter is the whole reason the rung is safe to trust, so its absence must go red here.
    """
    syms, _ = extract_symbols_with_stats(Path(fname), src, lang)
    assert not syms, (
        f"{lang}: invented {sorted((s.name, s.kind) for s in syms)} from a file defining nothing")


def test_gt4_a_struct_field_is_never_reported_as_a_function() -> None:
    """GT4 — the generic walk's fallback kind must not lie about what it found.

    Go's `type Point struct { X int }` reported `X` with kind `function` until e6. Kind is
    stored and served, and a field masquerading as a function is a name a `X()` call can resolve
    to — the exact "confidently wrong" failure this file exists for. Asserted on the kind, not
    on whether `X` is extracted at all: reporting the field is defensible, calling it a function
    is not.
    """
    syms, _ = extract_symbols_with_stats(
        Path("a.go"), "package geo\n\ntype Point struct { X int }\n", "go")
    bad = [(s.name, s.kind) for s in syms if s.kind in ("function", "method")]
    assert not bad, f"struct field(s) reported as callable: {bad}"


@pytest.mark.parametrize("fx", FIXTURES, ids=repr)
def test_gt5_symbols_carry_the_language_they_were_parsed_as(fx: GT) -> None:
    """GT5 — embedded symbols carry the *inner* language, and that is deliberate.

    Pinned because it is load-bearing and counter-intuitive: a `<script lang="ts">` symbol in a
    `.svelte` file is stamped `typescript`, not `svelte`. That is why per-language coverage keyed
    on `symbols.language` systematically under-counts every host grammar with embedded scripts
    and over-counts javascript — the measurement error that made svelte look like 0 % for a whole
    session. `file_extraction` is keyed by file with the *host* language precisely so the two
    questions stop sharing one answer; this test keeps the two conventions from quietly merging.
    """
    for s in _extract(fx)[0]:
        assert s.language == fx.sym_lang, \
            f"{fx.lang}: {s.name} carries language {s.language!r}, expected {fx.sym_lang!r}"
