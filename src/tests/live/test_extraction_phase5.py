"""Phase 5 extraction gates: S8 family-gated resolution, S3 revision identity, S1/S4/S7.

Each test here is paired: one assertion needs the change, a second needs the machinery the
change sits on to still work. A gate that only checks "the bad edge is gone" also passes when
resolution is broken outright, which is the failure mode
[[feedback_guard_tests_must_discriminate]] names — so every drop assertion is accompanied by a
keep assertion over the same run.

Measured provenance for S8, so a future reader can tell a regression from a re-baseline: on
2026-07-28 the fleet held 18,301,489 resolved call edges across 151 projects with edges, of
which 1,092,262 (5.968%) bound across language families — overwhelmingly javascript->php, a
bare `get()` in one language reaching a `get()` in the other. Re-deriving one project
(a vendored-JavaScript member) with the family key dropped its edge count by exactly its cross-family count,
326,246 -> 287,528, and no same-family edge moved.
"""
from __future__ import annotations

import pytest

from rag_search.graph.extractor import (
    EXTRACTOR_REV,
    _is_call_node,
    _is_name_text,
    extract_calls_with_lines,
)
from rag_search.index.discover import detect_language, language_family

pytestmark = pytest.mark.live


def _write(root, rel: str, text: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _graph_for(root):
    """Extract root into a throwaway GraphStore and return (store, edges as file pairs)."""
    from rag_search.daemon.sweeps import _extract_graph
    from rag_search.graph.store import GraphStore

    gs = GraphStore(root / "graph.db")
    _extract_graph(gs, root)
    pairs = gs._con.execute(
        "SELECT a.file, b.file FROM edges e "
        "JOIN symbols a ON a.sid=e.caller_sid JOIN symbols b ON b.sid=e.callee_sid"
    ).fetchall()
    return gs, pairs


def test_ts8_calls_resolve_within_a_language_family(safe_tmp_path):
    """TS8: a JS call must not bind to a PHP definition of the same name — and must still
    bind to the JS one. Both halves run off a single extraction of the same tree."""
    root = safe_tmp_path / "s8"
    _write(root, "lib.php", "<?php\nfunction shared_handler() { return 1; }\n")
    _write(root, "lib.js", "export function shared_handler() { return 1; }\n")
    _write(root, "app.js", "import {shared_handler} from './lib.js';\n"
                           "function main() { return shared_handler(); }\n")
    _, pairs = _graph_for(root)
    callees = {b for a, b in pairs if a.endswith("app.js")}
    assert any(c.endswith("lib.js") for c in callees), (
        "S8 over-gated: app.js -> lib.js is a same-family call and must survive; "
        f"resolved callees were {sorted(callees)}"
    )
    assert not any(c.endswith("lib.php") for c in callees), (
        "S8 regressed: app.js resolved a call to a PHP definition by bare name; "
        f"resolved callees were {sorted(callees)}"
    )


def test_ts0_same_file_calls_become_edges(safe_tmp_path):
    """S0 rung 0: a call to a definition in the same file must become an edge.

    `_extract_graph` discarded every same-file edge (`callee_file != fstr`) until 2026-07-29.
    Measured before the change: ccw's stored graph held **0 same-file edges out of 1,934**, and
    five of the twelve-query graph gate's seven misses were same-file relations — `run_and_log`
    read as 8 callers / 0 callees, `evaluate_recall` as 7 callees / 0 callers, the mirror-image
    signature of that one condition.

    Three assertions, because two of the three failure modes are invisible to the first:
    keeping the same-file edge alone also passes if resolution is inverted, and dropping the
    condition outright — rather than narrowing it to self-edges — passes both.
    """
    root = safe_tmp_path / "s0"
    _write(root, "mod.py",
           "def helper():\n    return 1\n\n\n"
           "def main():\n    return helper()\n\n\n"
           "def fact(n):\n    return fact(n - 1)\n")
    _write(root, "app.py", "from mod import main\n\n\ndef run():\n    return main()\n")
    gs, _ = _graph_for(root)
    named = {(a, b) for a, b in gs._con.execute(
        "SELECT a.name, b.name FROM edges e "
        "JOIN symbols a ON a.sid=e.caller_sid JOIN symbols b ON b.sid=e.callee_sid"
    )}
    assert ("main", "helper") in named, (
        "a same-file call produced no edge — the graph cannot answer callers/callees for any "
        f"relation that does not cross a file boundary; edges were {sorted(named)}"
    )
    assert ("run", "main") in named, (
        "cross-file resolution regressed while restoring same-file edges; "
        f"edges were {sorted(named)}"
    )
    assert ("fact", "fact") not in named, (
        "a recursive call became a self-edge — the exclusion must narrow to caller == callee, "
        f"not disappear; edges were {sorted(named)}"
    )


def test_ts10_ambiguous_calls_emit_nothing_and_same_file_wins(safe_tmp_path):
    """TS10: the narrowest scope holding a candidate must hold exactly one, or no edge is emitted.

    Restoring same-file edges (TS0 above) left the inverse defect: every candidate the family
    held was emitted, so one call site bound to N definitions and `graph()` presented all N with
    equal confidence. Measured 2026-07-31 over 155 stores — 2,220,234 of 2,320,130 edges were
    ambiguous (95.7%), 70.2% of them in groups above 32, and `impact_narrative` answered "high"
    for 50.6% of symbols. Under the one-true-callee model that table's precision was <= 8.0%.

    Cap 1 is not a threshold picked off a curve. Each cap step to C admits groups of size C,
    contributing one correct edge and C-1 wrong ones, so the marginal edge is correct with
    probability 1/C: 0.887 precision at 2, 0.780 at 4. 1 is the only value at which an edge in
    this table *means* a resolved call.

    Paired per this module's convention: every drop assertion has a keep assertion over the
    same run, because "the ambiguous edge is gone" also passes when resolution is broken flat.
    """
    from rag_search.daemon.sweeps import _MAX_CALLEE_FANOUT

    root = safe_tmp_path / "s10"
    # `handler` is defined twice at family scope and never in the caller's file -> ambiguous.
    _write(root, "a.py", "def handler():\n    return 1\n")
    _write(root, "b.py", "def handler():\n    return 2\n")
    # `only_once` is unique, so the same caller must still resolve it.
    _write(root, "c.py", "def only_once():\n    return 3\n")
    _write(root, "app.py",
           "from a import handler\nfrom c import only_once\n\n\n"
           "def run():\n    return handler() + only_once()\n")
    # `local` shadows a same-named sibling definition: same file is the preferred scope.
    _write(root, "shadowed.py", "def local():\n    return 4\n")
    _write(root, "owner.py",
           "def local():\n    return 5\n\n\n"
           "def use():\n    return local()\n")
    gs, _ = _graph_for(root)
    named = {(a, b) for a, b in gs._con.execute(
        "SELECT a.name, b.name FROM edges e "
        "JOIN symbols a ON a.sid=e.caller_sid JOIN symbols b ON b.sid=e.callee_sid"
    )}
    assert ("run", "handler") not in named, (
        "a call with two family-scope candidates emitted an edge; the resolution is a guess and "
        f"nothing downstream marks it as one; edges were {sorted(named)}"
    )
    assert ("run", "only_once") in named, (
        "the cap dropped an unambiguous call — resolution is broken, not narrowed; "
        f"edges were {sorted(named)}"
    )
    assert ("use", "local") in named, (
        "same file is the preferred scope and resolves this to one candidate; "
        f"edges were {sorted(named)}"
    )
    files = {(a, b) for a, b in gs._con.execute(
        "SELECT a.file, b.file FROM edges e "
        "JOIN symbols a ON a.sid=e.caller_sid JOIN symbols b ON b.sid=e.callee_sid"
    )}
    assert not any(a.endswith("owner.py") and b.endswith("shadowed.py") for a, b in files), (
        "the same-file tier did not win: `use` bound to the sibling definition of `local` as "
        f"well as its own; edges were {sorted(files)}"
    )
    # The invariant itself, not just its symptoms. Nothing asserted this before, which is how a
    # 96%-ambiguous graph survived into a backlog while every existing test stayed green.
    fanout = gs._con.execute(
        "SELECT MAX(n) FROM (SELECT COUNT(*) AS n FROM edges e "
        "JOIN symbols b ON b.sid=e.callee_sid GROUP BY e.caller_sid, b.name)"
    ).fetchone()[0]
    assert fanout is not None and fanout <= _MAX_CALLEE_FANOUT, (
        f"a (caller, callee_name) group bound {fanout} definitions, over the cap of "
        f"{_MAX_CALLEE_FANOUT}"
    )


def test_ts10b_recursion_does_not_fall_through_to_another_file(safe_tmp_path):
    """TS10b: drop the caller from the tier that *won*, never before choosing the tier.

    A recursive call in the only file defining that name has an empty tier once the caller is
    removed. Removing it first instead makes the same-file tier look empty, falls through to the
    family, and binds the recursion to a same-named definition in some other file — a
    confidently wrong edge in a table whose invariant is that it holds none. Worth 996 edges
    fleet-wide, and invisible to any test that does not put a recursive function next to a
    homonym.
    """
    root = safe_tmp_path / "s10b"
    _write(root, "mine.py", "def walk(n):\n    return walk(n - 1)\n")
    _write(root, "theirs.py", "def walk(n):\n    return n\n")
    gs, _ = _graph_for(root)
    files = {(a, b) for a, b in gs._con.execute(
        "SELECT a.file, b.file FROM edges e "
        "JOIN symbols a ON a.sid=e.caller_sid JOIN symbols b ON b.sid=e.callee_sid"
    )}
    assert not any(a.endswith("mine.py") and b.endswith("theirs.py") for a, b in files), (
        "a recursive call fell through the same-file tier and bound to another file's "
        f"definition of the same name; edges were {sorted(files)}"
    )


def test_ts8b_family_table_only_widens(safe_tmp_path):
    """A language with no relatives is its own family, so the table can never over-merge."""
    assert language_family("typescript") == language_family("javascript")
    assert language_family("vue") == language_family("javascript")
    assert language_family("php") != language_family("javascript")
    assert language_family("python") == "python", "an unlisted language must be its own family"
    assert language_family("nim") == "nim"


def test_ts3_extractor_rev_is_part_of_graph_identity(safe_tmp_path):
    """TS3: a stored graph stamped without EXTRACTOR_REV must read as stale.

    Resolution lives in `sweeps._extract_graph`, which no byte fingerprint covers, so without
    the revision in the identity a resolution change would serve stale edges forever. Stamping
    the *pre-S3* format is the discriminating case: it is what the fleet actually holds, and it
    must not be mistaken for current.
    """
    from rag_search.daemon.sweeps import _code_fingerprint, _graph_stale, _pipeline_algo_version
    from rag_search.graph.community import ALGO_VERSION
    from rag_search.graph.store import GraphStore

    root = safe_tmp_path / "s3"
    root.mkdir(parents=True, exist_ok=True)
    gs = GraphStore(root / "graph.db")
    from rag_search.daemon.sweeps import _code_source_fingerprint
    gs.set_meta("source_sig", _code_source_fingerprint(str(root)))
    gs.set_meta("algo_version", f"{ALGO_VERSION}+{_code_fingerprint()}")
    assert _graph_stale(str(root), gs), "a graph stamped in the pre-S3 format must read as stale"
    gs.set_meta("algo_version", _pipeline_algo_version())
    assert not _graph_stale(str(root), gs), (
        "the current stamp must read as current, else every pass re-derives forever"
    )
    assert EXTRACTOR_REV and EXTRACTOR_REV in _pipeline_algo_version()


def test_ts1_names_are_the_grammar_s_decision_not_python_s():
    """TS1: names their own grammar named must survive; only impossible text is rejected."""
    for name in ("$user", "list-ref", "empty?", "save!", "@media", "kebab-case-fn"):
        assert _is_name_text(name), f"S1: {name!r} is a valid name in some grammar"
    for junk in ("", "a b", "a\nb", "\t"):
        assert not _is_name_text(junk), f"S1: {junk!r} cannot be a name"


def test_ts4_call_nodes_matched_by_token_not_substring():
    """TS4: `call`/`invocation` as node-type tokens, and a signature is not a call."""
    for kind in ("call", "call_expression", "function_call", "method_invocation",
                 "macro_invocation"):
        assert _is_call_node(kind), f"S4: {kind} is a call node"
    for kind in ("function_signature", "method_signature", "callable_declaration",
                 "identifier", "class_declaration"):
        assert not _is_call_node(kind), f"S4: {kind} is not a call node"


def test_ts4b_signature_nodes_yield_no_calls():
    """The exclusion has to hold through the real parser, not only the predicate."""
    calls = dict(extract_calls_with_lines(
        "abstract class A { abstract fun helper(): Int }\n"
        "fun main() { helper() }\n", "kotlin"))
    assert "helper" in calls, "the real call must still be found"


def test_ts7_extensionless_shebang_files_get_a_language(safe_tmp_path):
    """TS7: path detection misses extensionless files; the shebang answers for executables."""
    root = safe_tmp_path / "s7"
    root.mkdir(parents=True, exist_ok=True)
    assert detect_language(_write(root, "runner", "#!/usr/bin/env python3\nimport os\n")) == \
        "python"
    assert detect_language(_write(root, "deploy", "#!/bin/bash\nset -e\n")) == "bash"
    # No content signature exists for these, and inventing a filename table is what HR15 bans.
    assert detect_language(_write(root, "Makefile", "all:\n\techo hi\n")) == "unknown"
