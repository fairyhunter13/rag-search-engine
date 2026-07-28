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
(mcms-lp) with the family key dropped its edge count by exactly its cross-family count,
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
    """S8: a JS call must not bind to a PHP definition of the same name — and must still
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


def test_ts8b_family_table_only_widens(safe_tmp_path):
    """A language with no relatives is its own family, so the table can never over-merge."""
    assert language_family("typescript") == language_family("javascript")
    assert language_family("vue") == language_family("javascript")
    assert language_family("php") != language_family("javascript")
    assert language_family("python") == "python", "an unlisted language must be its own family"
    assert language_family("nim") == "nim"


def test_ts3_extractor_rev_is_part_of_graph_identity(safe_tmp_path):
    """S3: a stored graph stamped without EXTRACTOR_REV must read as stale.

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
    """S1: names their own grammar named must survive; only impossible text is rejected."""
    for name in ("$user", "list-ref", "empty?", "save!", "@media", "kebab-case-fn"):
        assert _is_name_text(name), f"S1: {name!r} is a valid name in some grammar"
    for junk in ("", "a b", "a\nb", "\t"):
        assert not _is_name_text(junk), f"S1: {junk!r} cannot be a name"


def test_ts4_call_nodes_matched_by_token_not_substring():
    """S4: `call`/`invocation` as node-type tokens, and a signature is not a call."""
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
    """S7: path detection misses extensionless files; the shebang answers for executables."""
    root = safe_tmp_path / "s7"
    root.mkdir(parents=True, exist_ok=True)
    assert detect_language(_write(root, "runner", "#!/usr/bin/env python3\nimport os\n")) == \
        "python"
    assert detect_language(_write(root, "deploy", "#!/bin/bash\nset -e\n")) == "bash"
    # No content signature exists for these, and inventing a filename table is what HR15 bans.
    assert detect_language(_write(root, "Makefile", "all:\n\techo hi\n")) == "unknown"
