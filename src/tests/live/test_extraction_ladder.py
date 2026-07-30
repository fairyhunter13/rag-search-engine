"""EL1-EL6 — the per-file extraction record that makes the dark set enumerable.

Numbered like CB1-CB6 in test_cpu_budget.py. These guard the *instrumentation* half of the
extraction ladder: every file the derive attempts gets a row naming which path produced its
symbols, and the symbols dropped for having no name are counted rather than discarded silently.

The property under test is not "extraction finds everything" — that is not a testable claim.
It is the weaker, checkable one: **no file disappears without a recorded reason.** Before this,
a grammar the pack does not serve, bytes that do not parse, a grammar with no structure output,
a file of anonymous arrow functions, and a file that genuinely defines nothing all rendered
identically as "0 symbols", which is why the gap was never measurable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from rag_search.graph.extractor import ExtractionStats, extract_symbols_with_stats

pytestmark = pytest.mark.live

# Every rung `extract_symbols_with_stats` may report. A new rung must be added here
# deliberately, so the ladder cannot grow a path that nothing accounts for.
_KNOWN_RUNGS = {"structure", "generic", "embedded", "unparsed", "no_grammar", "no_language"}

_ARROW_JS = """
const a = () => 1;
const b = function () { return 2; };
[1, 2].map(x => x * 2);
function named(q) { return q; }
"""

_SVELTE = """<script>
  const onClick = () => 1;
  function build(n) { return n + 1; }
</script>
<button on:click={onClick}>go</button>
"""


def _stats(name: str, src: str, lang: str) -> tuple[list, ExtractionStats]:
    return extract_symbols_with_stats(Path(name), src, lang)


def test_el1_every_rung_reported_is_a_known_rung() -> None:
    """EL1 — the rung is always one of the declared set, never empty or invented."""
    cases = [("a.py", "def f():\n    return 1\n", "python"),
             ("a.js", _ARROW_JS, "javascript"),
             ("a.svelte", _SVELTE, "svelte"),
             ("a.txt", "hello", "unknown"),
             ("a.zzz", "hello", "not-a-real-grammar")]
    for name, src, lang in cases:
        _, st = _stats(name, src, lang)
        assert st.rung in _KNOWN_RUNGS, f"{name}: unknown rung {st.rung!r}"
        assert st.language == lang or st.language == "unknown"


def test_el2_no_unnamed_symbol_escapes_extraction() -> None:
    """EL2 — `Symbol(name=None)` must not leave the extractor.

    It used to: the structure path built symbols straight from `process()` entries with no name
    check (the `_generic_walk` path did check, via `_is_name_text`), and `sweeps.py` then threw
    them away with `if not sym.name: continue` and no counter. Anonymous and arrow functions are
    reported by `process()` with `name=None`, so a modern JS/TS file lost most of its extraction
    to a branch that recorded nothing.
    """
    for name, src, lang in [("a.js", _ARROW_JS, "javascript"),
                            ("a.svelte", _SVELTE, "svelte")]:
        syms, _ = _stats(name, src, lang)
        assert all(s.name for s in syms), \
            f"{name}: unnamed symbols escaped: {[s for s in syms if not s.name]}"


def test_el3_anonymous_drops_are_counted_not_silent() -> None:
    """EL3 — a file whose functions are anonymous reports them, so it cannot read as empty.

    Non-vacuous by construction: `_ARROW_JS` contains an arrow function, a function expression
    and an inline callback, none of which `process()` names.
    """
    syms, st = _stats("a.js", _ARROW_JS, "javascript")
    assert st.anon_count > 0, (
        "no anonymous drops counted for a file built from arrow functions and callbacks — "
        "either process() started naming them (retire this test with the counter) or the "
        f"counter stopped incrementing. rung={st.rung} symbols={[s.name for s in syms]}"
    )
    assert st.symbol_count == len(syms)


def test_el4_has_error_is_a_real_signal_not_an_attribute_probe() -> None:
    """EL4 — `has_error` must distinguish clean from broken source.

    `has_error` is a *method* on this binding's Node, so the natural
    `bool(getattr(root, "has_error", False))` is True for every file ever parsed — it measures
    that the attribute exists. Measured before the fix: 43/43 svelte and 50/50 python files
    "had errors". A signal that is always on is worse than none, because it reads as evidence.
    """
    _, clean = _stats("a.py", "def f():\n    return 1\n", "python")
    _, broken = _stats("b.py", "def f(:\n  return ???\n", "python")
    assert not clean.has_error, "a well-formed python file reported a parse error"
    assert broken.has_error, "a syntactically broken python file reported no parse error"


def test_el5_embedded_block_drops_belong_to_the_host_file() -> None:
    """EL5 — a .svelte whose <script> is all arrow functions must not read as an empty .svelte.

    The anonymous drops happen during the *inner* javascript sub-parse; if they stayed there the
    host file would still report `anon_count=0` and look like a file with no code in it.
    """
    _, st = _stats("a.svelte", _SVELTE, "svelte")
    assert st.anon_count > 0, f"host file lost its embedded block's drops (rung={st.rung})"
    assert st.rung in ("embedded", "structure"), st.rung


def test_el6_stats_symbol_count_matches_returned_symbols() -> None:
    """EL6 — the recorded count is the count, for every rung.

    `file_extraction.symbol_count` is what `overview(what="metrics")` sums into the coverage
    number, so a drift between it and the symbols actually stored would make the headline
    figure quietly wrong in the direction nobody checks.
    """
    for name, src, lang in [("a.py", "def f():\n    return 1\n", "python"),
                            ("a.js", _ARROW_JS, "javascript"),
                            ("a.svelte", _SVELTE, "svelte"),
                            ("a.txt", "hello", "unknown")]:
        syms, st = _stats(name, src, lang)
        assert st.symbol_count == len(syms), name


_SCSS = "@mixin fc($a) { color: $a; }\n@function double($n) { @return $n * 2; }\n.btn { color: red }\n"


def test_el7_a_structureless_grammar_reaches_rung_4_instead_of_zero() -> None:
    """EL7 — scss has no `process()` structure and no definition-suffixed nodes, so rung 3 and
    the generic walk both come back empty and it extracted **nothing** until rung 4 landed.

    It stands in for the long tail generally: `highlights.scm` exists for 237 of the pack's 306
    languages against 48 for `tags`, and measured on this fallback set `tags` is empty for every
    one of them — which is why the ladder has no tags rung at all.
    """
    syms, st = _stats("a.scss", _SCSS, "scss")
    assert st.rung == "highlights", f"expected rung 4, got {st.rung}"
    assert {s.name for s in syms} == {"fc", "double"}, [s.name for s in syms]


def test_el8_rung_4_does_not_invent_a_definition_for_a_call_site() -> None:
    """EL8 — the failure rung 4 exists to avoid, and the reason it is trusted last.

    `highlights.scm` says what a token *is*, never where it is defined: python's `@function`
    capture fires on the callee of `helper(a)` and bash's on the `echo` of a command. An
    unfiltered rung 4 would therefore mint a definition for every call site — "confidently
    extracting the wrong thing", which is worse than extracting nothing, because a wrong symbol
    is indistinguishable from a right one downstream.

    Asserted through the whole extractor rather than against `_highlight_walk` directly, so it
    still holds if the ladder is reordered and rung 4 starts firing where rung 3 does today.
    """
    cases = [("a.py", "def top(a):\n    return helper(a)\n", "python", "helper"),
             ("a.sh", "foo() {\n  echo hi\n}\n", "bash", "echo")]
    for name, src, lang, called in cases:
        syms, _ = _stats(name, src, lang)
        bogus = [s for s in syms if s.name == called and s.start_line > 1]
        assert not bogus, f"{lang}: call site {called!r} became a symbol: {bogus}"
