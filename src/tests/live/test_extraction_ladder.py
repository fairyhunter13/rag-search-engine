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

from rag_search.graph.extractor import (
    EXTRACTION_RUNGS,
    ExtractionStats,
    extract_symbols_with_stats,
)

pytestmark = pytest.mark.live

# Every rung `extract_symbols_with_stats` may report. Taken from the module's own declaration
# rather than re-spelled here: the hand-written copy silently went stale the moment rung 4
# landed — it never gained `highlights`, and EL1 stayed green because none of its cases reach
# that rung. A guard that only fails when someone remembers to update it is not a guard.
# Deliberateness lives at the `EXTRACTION_RUNGS` declaration, which is what it exists for.
_KNOWN_RUNGS = set(EXTRACTION_RUNGS)

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


_COMPOSE = 'version: "3"\nservices:\n  web:\n    image: nginx\n  db:\n    image: postgres\n'
_PKG_JSON = '{"name": "x", "scripts": {"build": "vite", "test": "vitest"}}'
_TF = 'resource "aws_s3_bucket" "b" {\n  bucket = "x"\n}\nvariable "v" { default = 1 }\n'
# The real X2 shape: a Katalon project ships 2,439 `.rs`/`.ts` files that are XML.
_XML = '<?xml version="1.0" encoding="UTF-8"?>\n<WebElementEntity>\n  <name>btn</name>\n</WebElementEntity>\n'


def test_el9_a_data_file_yields_its_structure_instead_of_a_blank_row() -> None:
    """EL9 — rung 5. These were filed under X4 "legitimately symbol-free", and for markdown or
    a `.env.example` that is true. For a compose file, a `package.json` or a terraform module it
    never was: the structure is there, tree-sitter can see it, and nothing was asking.

    `qualified_name` is asserted, not just `name`, because the depth-2 key is the useful one —
    `scripts.build`, `services.web` — and a rung that returned only the section headers would
    satisfy a name-only assertion while delivering nothing anyone would search for.
    """
    for name, src, lang, want in [
        ("compose.yaml", _COMPOSE, "yaml", {"services", "services.web", "services.db"}),
        ("package.json", _PKG_JSON, "json", {"scripts", "scripts.build", "scripts.test"}),
        ("main.tf", _TF, "hcl", {'resource."aws_s3_bucket"."b"', 'variable."v"'}),
    ]:
        syms, st = _stats(name, src, lang)
        assert st.rung == "data", f"{name}: expected rung 5, got {st.rung}"
        quals = {s.qualified_name for s in syms}
        assert want <= quals, f"{name}: missing {want - quals} (got {sorted(quals)})"
        assert all(s.start_line >= 1 for s in syms), [s.start_line for s in syms]


_GO_RECV = """package p

type Server struct{ addr string }

func (s *Server) Start() error { return nil }
func (s Server) Addr() string  { return s.addr }

type Stack[T any] struct{ xs []T }

func (st *Stack[T]) Push(v T) { st.xs = append(st.xs, v) }

func Helper() int { return 1 }
"""


def test_el11_a_method_is_qualified_by_the_type_it_is_declared_on() -> None:
    """EL11 — a receiver is a container, though no span encloses the method.

    `graph_handler.py:15` resolves `WHERE name = ? OR qualified_name = ?`, so asking for
    `Server.Start` when several types define `Start` only works if something wrote the dotted
    name. On the structure rung nothing did, and the fix the plan specified — take the name from
    the enclosing class span — recovers **zero** symbols here, because Go declares a method
    *outside* its type: `func (s *Server) Start()` is top-level and nested in nothing.

    So the container is read from the receiver **field**, which is grammar structure exactly like
    the `name` field `_generic_walk` already reads — no vocabulary about source text (P6/HR15),
    and nothing keyed on the language being Go.

    `Stack[T]` is the case that fixed the rule rather than confirming it. Taking the receiver's
    *last* identifier is right on 641 of 641 receivers in a real 400-file corpus and still wrong:
    a generic receiver ends in the type **parameter**, so the positional rule yields `Stack[T]`'s
    method under `T`. Reading the receiver's `type` field yields `Stack`. A corpus without generics
    could not have caught it, which is why it is pinned here.

    `Helper` is the negative half: a plain function has no receiver and must stay unqualified,
    never acquire the type declared above it.
    """
    syms, st = _stats("srv.go", _GO_RECV, "go")
    quals = {s.name: s.qualified_name for s in syms}
    for name, want in [("Start", "Server.Start"), ("Addr", "Server.Addr"),
                       ("Push", "Stack.Push")]:
        assert quals.get(name) == want, f"{name}: got {quals.get(name)!r}, want {want!r}"
    assert quals.get("Helper") == "Helper", (
        f"a receiverless function was given a container: {quals.get('Helper')!r}")
    assert st.rung in _KNOWN_RUNGS, st.rung


def test_el10_bytes_that_contradict_the_extension_are_recorded_not_parsed_as_claimed() -> None:
    """EL10 (X2) — a `.rs` file that is actually XML must not read as an empty rust file.

    That is the whole defect: 2,439 files claiming a language their bytes do not parse as, and
    an extraction record indistinguishable from a rust file that genuinely defines nothing.

    The negative half is the load-bearing one. The evidence is "these bytes do not parse as this
    grammar", which *degraded but correct* source can also produce — so the test pins the cases
    that must NOT be called a mismatch. Measured 2026-07-30: source truncated mid-token scores
    0.000 (tree-sitter recovers), minified javascript 0.000, NUL-injected python 0.120, against
    0.916-1.000 for wrong-language bytes. Nothing measured lands between. If this half goes red,
    the threshold is wrong and every broken file in the fleet is being mislabelled.
    """
    for name, lang in [("o.rs", "rust"), ("o.ts", "typescript"), ("o.go", "go")]:
        _, st = _stats(name, _XML, lang)
        assert st.rung == "language_mismatch", f"{name}: XML bytes read as {st.rung}"

    real_py = "def top(a):\n    return helper(a)\n\n\ndef helper(b):\n    return b\n"
    for name, src, lang in [
        ("t.py", real_py[: len(real_py) // 2], "python"),                     # truncated
        ("m.js", "function a(b){return b*2}var c=a(3);" * 40, "javascript"),  # minified
        ("n.py", "def f():\n\x00\x00\x00    return 1\n", "python"),           # NUL bytes
        ("x.xml", _XML, "xml"),                                # same bytes, told the truth
    ]:
        _, st = _stats(name, src, lang)
        assert st.rung != "language_mismatch", (
            f"{name}: degraded-but-correct {lang} was called a language mismatch")


_ASTRO = "---\nconst x = 1;\nfunction load() { return 2 }\n---\n<h1>{x}</h1>\n<script>function go(){return 3}</script>\n"
_VUE = '<template><button @click="go">x</button></template>\n<script>\nfunction go(){return 1}\n</script>\n'
_TS_SVELTE = '<script lang="ts">\nfunction typed(n: number){ return n }\n</script>\n'
_README = "# t\n\n```python\ndef readme_example():\n    return 1\n```\n"


def test_el11_injections_supplement_the_script_walk_and_never_replace_it() -> None:
    """EL11 — rung 1, and the three things that make it safe rather than a rewrite.

    (a) It **adds**: astro's frontmatter is a `frontmatter_js_block`, not a `script_element`, so
    `_iter_script_blocks` cannot see it and `load` was invisible. (b) It **does not replace**: the
    plan proposed swapping the hand-rolled walk for injections, but vue's injections query is
    *empty* and vue is the fleet's highest-coverage host grammar (92 %, 2,020 of 2,190 files), so
    the swap would have been a straight regression on the case that matters most. php's and erb's
    are empty too. (c) Ties go to the better-informed source: html's `#set!` says javascript for
    every script element, while the hand-rolled walk reads `<script lang="ts">` — keyed on the
    triple rather than the region, both would be kept and the file would report double symbols.
    """
    astro = {s.name for s in _stats("a.astro", _ASTRO, "astro")[0]}
    assert {"load", "go"} <= astro, f"astro frontmatter or script lost: {sorted(astro)}"

    for name, src, lang, want in [("a.vue", _VUE, "vue", "go"),
                                  ("a.svelte", _SVELTE, "svelte", "build"),
                                  ("t.svelte", _TS_SVELTE, "svelte", "typed")]:
        syms, _st = _stats(name, src, lang)
        names = [s.name for s in syms]
        assert want in names, f"{name}: host regressed, {want!r} not in {names}"
        assert names.count(want) == 1, f"{name}: {want!r} extracted twice — dedup failed: {names}"
    assert [s.language for s in _stats("t.svelte", _TS_SVELTE, "svelte")[0]] == ["typescript"], \
        'lang="ts" lost to an injection #set! that can only say javascript'


def test_el13_an_unresolvable_project_says_so_instead_of_reporting_an_empty_ladder() -> None:
    """EL13 — the metrics block must never answer "I was not asked" with "I found nothing".

    `_extraction_block` returned a bare `{}` when no `project_path` was given and none could be
    inferred. Measured 2026-07-30 on this fleet: 152 projects are enabled, so `_require_project`
    refuses on *every* unscoped call and `overview(what="metrics")` reported `extraction: {}`
    fleet-wide while each store held rows. Read literally that says the ladder recorded nothing,
    which is the one claim an instrument must never make when it simply was not asked — the same
    defect class as a write-only column, and it hid the first post-ladder coverage reading until
    the call was repeated with an explicit path.

    Asserted on the shape rather than the wording: what matters is that the two cases are
    distinguishable at all, not which sentence distinguishes them.
    """
    from rag_search.server._overview import _extraction_block

    class _P:
        def __init__(self, path: str) -> None:
            self.path, self.enabled = path, True

    ambiguous = _extraction_block("", [_P("/nonexistent/a"), _P("/nonexistent/b")])
    assert ambiguous, "ambiguous project returned a bare {} — indistinguishable from 'no rows'"
    assert "error" in ambiguous, ambiguous
    assert _extraction_block("", []), "no-project-available returned a bare {} as well"


def test_el12_a_documentation_fence_does_not_become_a_project_definition() -> None:
    """EL12 — why rung 1 takes only *statically* declared injection languages.

    A `(#set! injection.language "php")` is the grammar author stating a property of the host
    grammar. A dynamic `@injection.language` capture takes the language from the document's own
    text — a markdown fence's info string — which is reading a token to choose a grammar, the
    thing P6 forbids. It is also simply wrong: an example in a README is not a definition in the
    project, and enrolling one makes `graph(relation="definition")` answer with documentation.

    Measured across the pack's 110 injection-capable languages: 64 declare statically, 12 capture
    dynamically, 31 mark content with no language, 3 use the capture name. The rule keeps 64.
    """
    syms, st = _stats("README.md", _README, "markdown")
    assert "readme_example" not in {s.name for s in syms}, (
        f"a fenced README example became a symbol (rung={st.rung}): {[s.name for s in syms]}")


def test_au1_coverage_must_be_read_by_something_that_can_see_the_wal(safe_tmp_path) -> None:
    """AU1 — the guard against the measurement error that voided the first baseline.

    A first coverage pass reported 5,179 files / 49.8 %, with svelte at 0 of 86. The number was
    void: every store is `journal_mode=wal`, the fleet rebuild was writing throughout, and the
    identical query returned **0** before a checkpoint and **78** after, on a `graph.db` whose
    mtime never changed. A *stable wrong answer* is the failure mode here — it survived four
    independent cross-checks — so agreement between two readers proves nothing unless one of them
    is known to be able to go stale.

    Hence both halves. The positive half is the rule: coverage is read from the process that owns
    the writer, or by a connection that attaches the WAL. The negative half is the witness, and it
    is what makes the positive half non-vacuous: with 30 committed rows sitting in a ~100 KB
    `-wal`, an `immutable=1` connection does not merely under-count — it reports **no such table**,
    because it reads the main database alone and the table was created in the WAL too.

    If the negative half ever goes green, SQLite's behaviour changed and this test is measuring
    nothing; re-derive the witness before trusting any fleet coverage figure again.
    """
    from rag_search.graph.store import GraphStore

    db = safe_tmp_path / "au1" / "graph.db"
    gs = GraphStore(db)
    try:
        _au1_body(gs, db)
    finally:
        gs.close()


def _au1_body(gs, db) -> None:
    import sqlite3
    for i in range(30):
        gs.record_extraction(f"/x/f{i}.py", "python", "structure", 2, 0, 0)
    gs.commit()
    from_writer = sum(r["files"] for r in gs.extraction_summary())
    assert from_writer == 30, from_writer
    assert (db.parent / "graph.db-wal").stat().st_size > 0, \
        "nothing in the -wal — this test cannot witness the hazard it exists for"

    wal_aware = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert wal_aware.execute("SELECT COUNT(*) FROM file_extraction").fetchone()[0] == 30, \
            "a WAL-attaching reader disagreed with the writer"
    finally:
        wal_aware.close()

    stale = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            stale.execute("SELECT COUNT(*) FROM file_extraction").fetchone()
    finally:
        stale.close()


def test_au2_coverage_counts_files_not_groups(safe_tmp_path) -> None:
    """AU2 — the dark set must be counted per file, and one mixed group is enough to tell.

    AU1 makes the reader trustworthy; this makes the arithmetic trustworthy, and the second was
    wrong while the first was right. `_extraction_totals` used to ask `if r["symbols"]` — a
    *group* predicate — so one file carrying a symbol credited every other file in its
    (language, rung) group as covered. The block built to expose the dark set was therefore
    hiding part of it, and monotonically: coverage overstated, `dark_files` understated, never
    the reverse. Measured on a real 192-file store before the fix: 111 against 100, 57.81 %
    against 52.08 %.

    The fixture is one group of three files where a *minority* extracted, because that is the
    shape the old predicate got wrong and a uniform group cannot distinguish — every earlier
    check summed `files`, the one column that was already right, which is why this survived.
    The second group is all-zero so `dark_files` has to come from both.
    """
    from rag_search.graph.store import GraphStore
    from rag_search.server._overview import _extraction_totals

    gs = GraphStore(safe_tmp_path / "au2" / "graph.db")
    try:
        gs.record_extraction("/x/a.go", "go", "structure", 4, 0, 0)
        gs.record_extraction("/x/b.go", "go", "structure", 0, 0, 0)
        gs.record_extraction("/x/c.go", "go", "structure", 0, 0, 0)
        gs.record_extraction("/x/d.groovy", "groovy", "generic", 0, 0, 0)
        gs.commit()
        totals = _extraction_totals(gs.extraction_summary())
    finally:
        gs.close()
    assert totals["files"] == 4, totals
    assert totals["files_with_symbols"] == 1, (
        "coverage credited files that extracted nothing to a group that did: "
        f"{totals['files_with_symbols']} of {totals['files']}")
    assert totals["dark_files"] == 3, totals
    assert totals["coverage_pct"] == 25.0, totals


def test_au3_rung_rollup_reports_symbols_not_just_occupancy(safe_tmp_path) -> None:
    """AU3 — a rung's yield must be readable from the metrics block, not only its headcount.

    `generic` means "parsed, nothing found". Counted by files it is one of the largest rungs in
    the fleet; counted by symbols it produced 43 across 15,106 files (measured 2026-07-31 over
    118 graphs). A reader with only `files` per rung therefore rates it a success, which is how
    "84 % productive" and "48 % of files yield a symbol" can both be said of the same fleet.

    The fixture makes `generic` the *majority* rung by files, spanning two languages, while
    contributing zero symbols: a rung that is small, single-language, or that yields a little
    cannot distinguish a rollup reporting yield from one reporting occupancy.
    """
    from rag_search.graph.store import GraphStore
    from rag_search.server._overview import _extraction_totals

    gs = GraphStore(safe_tmp_path / "au3" / "graph.db")
    try:
        gs.record_extraction("/x/a.go", "go", "structure", 7, 0, 0)
        gs.record_extraction("/x/b.go", "go", "structure", 5, 0, 0)
        gs.record_extraction("/x/c.groovy", "groovy", "generic", 0, 0, 0)
        gs.record_extraction("/x/d.groovy", "groovy", "generic", 0, 0, 0)
        gs.record_extraction("/x/e.make", "make", "generic", 0, 0, 0)
        gs.commit()
        totals = _extraction_totals(gs.extraction_summary())
    finally:
        gs.close()

    assert totals["symbols"] == 12, (
        f"the rollup dropped the per-group symbol total it was handed: {totals.get('symbols')!r}")
    by_rung = {r["rung"]: r for r in totals["by_rung"]}
    assert set(by_rung) == {"structure", "generic"}, by_rung
    assert by_rung["generic"] == {"rung": "generic", "files": 3, "symbols": 0,
                                  "files_with_symbols": 0, "dark_files": 3,
                                  "coverage_pct": 0.0}, by_rung["generic"]
    assert by_rung["structure"]["symbols"] == 12, by_rung["structure"]
    # The biggest rung by files is the emptiest by symbols, both readable from one block.
    assert by_rung["generic"]["files"] > by_rung["structure"]["files"]


def test_au4_a_grammar_ceiling_is_reported_as_a_ceiling_not_a_coverage_gap(safe_tmp_path) -> None:
    """AU4 — dark files rungs 4-5 cannot reach by construction must say so.

    Rungs 4 and 5 begin by fetching the grammar's `highlights.scm` / `injections.scm` and return
    `{}` on empty text, so a language whose pack ships neither has a *ceiling*. Measured against
    the installed pack 2026-07-31: groovy and php ship neither — php being 20,014 fleet files.
    Reading those as a coverage bug is how ladder work gets aimed where it cannot pay.

    The fixture pairs a no-query language with one that has queries and one the pack does not know
    at all, because the last is the trap: `get_highlights_query('unknown')` returns None just like
    groovy's, so a check that only asks about queries files all 11,791 `no_language` files under
    "the grammar ships no queries" — asserting a grammar where there is none.
    """
    from rag_search.graph.store import GraphStore
    from rag_search.server._overview import _extraction_totals

    gs = GraphStore(safe_tmp_path / "au4" / "graph.db")
    try:
        gs.record_extraction("/x/a.groovy", "groovy", "generic", 0, 0, 0)
        gs.record_extraction("/x/b.groovy", "groovy", "structure", 3, 0, 0)
        gs.record_extraction("/x/c.py", "python", "structure", 4, 0, 0)
        gs.record_extraction("/x/d.bin", "unknown", "no_language", 0, 0, 0)
        gs.commit()
        ceilings = {r["language"]: r for r in _extraction_totals(
            gs.extraction_summary())["grammar_ceilings"]}
    finally:
        gs.close()

    assert set(ceilings) == {"groovy"}, (
        f"expected only the no-query grammar to report a ceiling, got {sorted(ceilings)}")
    assert ceilings["groovy"]["ceiling"] == "grammar_has_no_queries", ceilings["groovy"]
    assert ceilings["groovy"]["files"] == 2, ceilings["groovy"]
    # Dark, not total: one groovy file did reach rung 1, and a ceiling does not un-extract it.
    assert ceilings["groovy"]["dark_files"] == 1, ceilings["groovy"]


def test_au5_stale_pipeline_stores_are_countable_without_opening_a_graph_db() -> None:
    """AU5 — the re-derive's own convergence must be readable from the metrics block.

    The design compares each store's `meta.algo_version` against `_pipeline_algo_version()` and
    re-derives on mismatch. The mechanism worked; nothing *reported* it, so a fleet that had
    stopped converging was indistinguishable from one that had converged. Measured 2026-07-31 by
    opening 144 stores by hand — 128 stale, 51 four extractor revisions behind — which is exactly
    the external `mode=ro` read AU1 forbids, for exactly AU1's reason.

    `(unstamped)` counts as stale, not as its own category: a store with no `meta` row has never
    completed a derive, which is staler than an old stamp rather than exempt from the question.
    """
    from rag_search.daemon.sweeps import _pipeline_algo_version
    from rag_search.server._overview import _pipeline_block

    cur = _pipeline_algo_version()
    block = _pipeline_block({cur: 16, "fg2+e2+2db7": 50, "(unstamped)": 2})

    assert block["current"] == cur, block
    assert block["stores"] == 68 and block["stores_current"] == 16, block
    assert block["stale_stores"] == 52, (
        f"an unstamped store was not counted as stale: {block}")
    assert block["by_stamp"][0]["algo_version"] == "fg2+e2+2db7", (
        f"by_stamp must lead with the largest population, not the current one: {block['by_stamp']}")
    assert sum(r["stores"] for r in block["by_stamp"]) == block["stores"], block
    assert _pipeline_block({})["stale_stores"] == 0, "an empty federation must not report drift"
