"""MM1-MM4 — properties true of *every* language, needing no ground truth.

Part 3 of W1-A's audit apparatus, and the part that answers "does the ladder handle any framework,
any structure" in a form that can actually go red. Ground-truth fixtures do not scale to 306
grammars and cannot be written for a language nobody here reads. Metamorphic properties can: they
relate an extraction to the extraction of a *transformed* input, so the expected answer is derived
rather than hand-counted, and the same four assertions cover every rung the ladder has.

Parametrised over one fixture per rung — `structure`, `highlights`, `data`, `embedded` — so a
regression in the long-tail rungs fails here and not only in the mainstream case everyone tests.

What these can and cannot catch: a rung that extracts the *wrong* symbol consistently satisfies
every property below, because they are all invariances. That failure is EL8's and EL12's job.
These catch the other family — extraction that is unstable under changes that must not matter.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from rag_search.graph.extractor import extract_symbols_with_stats

pytestmark = pytest.mark.live


class Fx:
    """One language fixture: two disjoint sources, a comment syntax, and a rename."""

    def __init__(self, rung: str, lang: str, name: str, a: str, b: str,
                 comment: str, old: str, new: str) -> None:
        self.rung, self.lang, self.name = rung, lang, name
        self.a, self.b, self.comment, self.old, self.new = a, b, comment, old, new

    def __repr__(self) -> str:  # pytest id
        return f"{self.rung}:{self.lang}"


FIXTURES = [
    Fx("structure", "python", "a.py",
       "def alpha(x):\n    return x\n",
       "def beta(y):\n    return y\n",
       "# a comment\n", "alpha", "renamed_alpha"),
    Fx("structure", "go", "a.go",
       "package m\n\nfunc Alpha(x int) int { return x }\n",
       "func Beta(y int) int { return y }\n",
       "// a comment\n", "Alpha", "RenamedAlpha"),
    Fx("highlights", "scss", "a.scss",
       "@mixin alpha($a) { color: $a; }\n",
       "@function beta($n) { @return $n; }\n",
       "// a comment\n", "alpha", "renamed_alpha"),
    Fx("data", "yaml", "a.yaml",
       "alpha:\n  one: 1\n",
       "beta:\n  two: 2\n",
       "# a comment\n", "alpha", "renamed_alpha"),
    Fx("embedded", "svelte", "a.svelte",
       "<script>\nfunction alpha(n) { return n }\n</script>\n",
       "<script>\nfunction beta(n) { return n + 1 }\n</script>\n",
       "<!-- a comment -->\n", "alpha", "renamed_alpha"),
]


def _names(fx: Fx, src: str) -> set[str]:
    return {s.name for s in extract_symbols_with_stats(Path(fx.name), src, fx.lang)[0]}


@pytest.mark.parametrize("fx", FIXTURES, ids=repr)
def test_mm0_each_fixture_reaches_the_rung_it_stands_for(fx: Fx) -> None:
    """MM0 — the fixtures are non-vacuous, and each still exercises its own rung.

    Not one of the four properties, and deliberately first: every assertion below is an
    invariance, and an invariance holds trivially over the empty set. Without this, a fixture that
    silently stopped extracting — or slid onto a different rung after a ladder reorder — would
    turn MM1-MM4 green while testing nothing. That is the failure mode this whole file exists to
    make impossible elsewhere, so it must not be possible here.
    """
    syms, st = extract_symbols_with_stats(Path(fx.name), fx.a, fx.lang)
    assert syms, f"{fx.lang}: fixture extracts nothing — MM1-MM4 would pass vacuously"
    assert st.rung == fx.rung, f"{fx.lang}: expected rung {fx.rung}, got {st.rung}"
    assert fx.old in {s.name for s in syms}, \
        f"{fx.lang}: rename target {fx.old!r} not among {[s.name for s in syms]}"


@pytest.mark.parametrize("fx", FIXTURES, ids=repr)
def test_mm1_inserting_a_comment_changes_no_symbol(fx: Fx) -> None:
    """MM1 — a comment carries no definition, in any language, so the symbol set is invariant.

    The transformation that must not matter. Prepending rather than interleaving keeps the input
    valid for every grammar here, including yaml and svelte, where an injected mid-file comment
    would change the document's structure rather than only its text.
    """
    assert _names(fx, fx.comment + fx.a) == _names(fx, fx.a), \
        f"{fx.lang}: a comment changed the symbol set"


@pytest.mark.parametrize("fx", FIXTURES, ids=repr)
def test_mm2_concatenation_yields_the_union(fx: Fx) -> None:
    """MM2 — extraction is local: symbols from A ++ B are the symbols of A plus those of B.

    Catches state leaking between definitions — a walk that stops at the first match, a stack
    reused across siblings, a rung that latches. The two sources are disjoint by construction, so
    the expected answer is derived from the parts and never hand-counted.
    """
    both = _names(fx, fx.a + fx.b)
    assert _names(fx, fx.a) | _names(fx, fx.b) <= both, (
        f"{fx.lang}: concatenation lost symbols — "
        f"missing {(_names(fx, fx.a) | _names(fx, fx.b)) - both}")


@pytest.mark.parametrize("fx", FIXTURES, ids=repr)
def test_mm3_renaming_one_identifier_changes_exactly_one_name(fx: Fx) -> None:
    """MM3 — the extractor reads the name it is looking at, not one nearby.

    The sharpest of the four: it fails if a walk attributes a name by position rather than by the
    node it belongs to, which is exactly how an off-by-one in a query capture or a member-unwrap
    presents. `old` appears once in each fixture, so a whole-source replace renames one definition.
    """
    before, after = _names(fx, fx.a), _names(fx, fx.a.replace(fx.old, fx.new))
    assert before - after == {fx.old} and after - before == {fx.new}, (
        f"{fx.lang}: renaming {fx.old!r} -> {fx.new!r} changed {before ^ after} "
        f"(before={sorted(before)} after={sorted(after)})")


@pytest.mark.parametrize("fx", FIXTURES, ids=repr)
def test_mm4_every_span_lies_within_the_file(fx: Fx) -> None:
    """MM4 — spans are 1-based and inside the file, on every rung including the remapped ones.

    Embedded blocks are the reason this is not trivially true: a `<script>` symbol's line comes
    from the *inner* parse and is remapped by the block's start row, so an off-by-one or a missed
    offset puts a symbol past the end of its own file. `graph(relation="definition")` reads these
    spans to slice source, so an out-of-range line is a wrong answer, not a cosmetic error.
    """
    src = fx.a + fx.b
    total = len(src.splitlines())
    for s in extract_symbols_with_stats(Path(fx.name), src, fx.lang)[0]:
        assert 1 <= s.start_line <= total, f"{fx.lang}: {s.name} starts at {s.start_line}/{total}"
        assert s.start_line <= s.end_line <= total, \
            f"{fx.lang}: {s.name} spans {s.start_line}-{s.end_line} of {total}"
