"""SC-A: what the tools advertise is what the tools do.

Written 2026-07-28, after `graph(relation="semantic_trace")` was found alive as a name with no
implementation behind it. It was deleted in 3fe4b29 and survived in the MCP docstring, the CLI help
and a skill file that *instructed* agents to call it — answering the whole time with `path`
semantics, because `path` was the fallthrough. Both its tests passed: one asserted the response was
a dict, the other asserted the `path` contract. Neither would have failed if it were named `banana`.

`test_capability_parity.py` already ties the `graph`/`overview` MCP docstrings to the
implementation's own constants. Added here: SC1/SC2 the CLI help, SC3 the callers, and — the
two that matter — SC4 every advertised relation reaches a branch, SC5 every advertised scope is a
distinct assembly. SC4/SC5 fail on the two shapes these surfaces actually had: a name with no
branch, and several names sharing one branch; SC3 on the third, a caller left behind when the
advertised set shrank.
"""
from __future__ import annotations

import json
import re
import sqlite3

import pytest

from rag_search.core.config import project_graph_db
from rag_search.query.ask import _SCOPES
from rag_search.query.graph_handler import _RELATIONS
from tests.live._sample_workspace import SampleWorkspace

pytestmark = pytest.mark.live


def _option_tokens(command_fn, param: str) -> set[str]:
    """Pull the `a|b|c` run out of a typer Option's help string."""
    import inspect

    opt = inspect.signature(command_fn).parameters[param].default
    help_text = getattr(opt, "help", "") or ""
    runs = re.findall(r"[a-z_]+(?:\|[a-z_]+)+", help_text)
    assert runs, f"--{param} help has no `a|b` value list to check: {help_text!r}"
    return set(runs[0].split("|"))


def test_sc1_cli_relation_help_matches_implementation():
    """SC1: `rag-search graph --relation` help lists exactly the implemented relations."""
    from rag_search.cli import graph as cli_graph

    assert _option_tokens(cli_graph, "relation") == set(_RELATIONS), (
        "CLI --relation help has drifted from graph_handler._RELATIONS — the CLI was one of the "
        "four places `semantic_trace` outlived its implementation"
    )


def test_sc2_cli_scope_help_matches_implementation():
    """SC2: `rag-search ask --scope` help lists exactly the implemented scopes."""
    from rag_search.cli import ask as cli_ask

    assert _option_tokens(cli_ask, "scope") == set(_SCOPES), (
        "CLI --scope help has drifted from ask._SCOPES"
    )


def test_sc3_no_caller_passes_an_unlisted_scope():
    """SC3: every literal scope handed to run_ask/compose_answer is in `_SCOPES`.

    SC3 used to read the MCP `ask` docstring, and left with that tool on 2026-07-29. What it was
    reaching for survives, so the slot gets a stronger occupant rather than a tombstone: the drift
    it existed to catch had *already happened on the other side of the call*, and it could not see
    it. 5f3033a narrowed `_SCOPES` from five names to two and left two callers passing `"global"` —
    fp14 and T3c. `run_ask` rejects it with a 49-character string, so fp14 (which demands >100
    chars) went red and T3c (which demands >20) stayed **green on the error message**. A false
    green is the expensive half, and no docstring check could have reached it.

    SC4/SC5 ask whether an advertised name is real. This asks the converse — whether a name in use
    is advertised — which is the direction a shrinking list breaks
    ([[feedback_allowlist_needs_sufficiency_test]]: derive the check from the code, don't
    hand-write the roster). Tests are in scope deliberately: both stragglers were tests.

    What it does not claim: run against 5f3033a it finds nothing, because those two callers reached
    the scope through the MCP `ask` tool, and keying on a deleted function would be dead weight.
    It covers the entry points that survive — `run_ask` and `compose_answer` — which is every way
    in that now exists. Demonstrated red on a synthetic aliased caller (both positional and
    keyword) and on removing SC5b's marker; the marker is checked per-call, not per-file, so
    exempting a deliberate bad scope cannot silently exempt an accidental one beside it.
    """
    import ast
    from pathlib import Path

    targets = {"run_ask", "compose_answer"}
    src_root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for py in src_root.rglob("*.py"):
        text = py.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError:  # pragma: no cover - fixture corpora may hold deliberate junk
            continue
        lines = text.splitlines()
        # Resolve `from … import run_ask as ra` before matching, or the check is alias-blind —
        # which is not hypothetical: the first draft of SC3 matched bare names, and found *zero*
        # offenders against the very commit that motivated it, because both stragglers called the
        # tool through `import ask as t` / `as ask_tool`. `mod.run_ask(…)` needs no resolving; the
        # Attribute branch below reads `.attr`.
        bound = {a.asname or a.name
                 for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                 for a in n.names if a.name in targets}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name not in targets and name not in bound:
                continue
            # run_ask(query, project_path, scope) — third positional; compose_answer takes
            # `scope` keyword-only. Non-literal scopes (a variable, a parametrize) are skipped:
            # SC5 already walks `_SCOPES` itself, so the parametrized callers are covered there.
            arg = next((k.value for k in node.keywords if k.arg == "scope"), None)
            if arg is None and len(node.args) >= 3:
                arg = node.args[2]
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                continue
            if arg.value in _SCOPES:
                continue
            span = "\n".join(lines[node.lineno - 1:(node.end_lineno or node.lineno)])
            if "sc3-exempt" in span:  # SC5b passes a bad scope on purpose — that is its subject
                continue
            offenders.append(f"{py.relative_to(src_root)}:{node.lineno} {name}"
                             f"(scope={arg.value!r})")

    assert not offenders, (
        f"callers pass scopes outside _SCOPES {sorted(_SCOPES)}:\n  " + "\n  ".join(offenders)
    )


@pytest.fixture(scope="module")
def graph_proj(sample_workspace: SampleWorkspace) -> str:
    return sample_workspace.promo


@pytest.fixture(scope="module")
def any_symbol(graph_proj):
    con = sqlite3.connect(str(project_graph_db(graph_proj)))
    row = con.execute("SELECT name FROM symbols LIMIT 1").fetchone()
    con.close()
    assert row, f"graph_proj {graph_proj!r} has no symbols"
    return row[0]


@pytest.mark.parametrize("relation", _RELATIONS)
def test_sc4_every_advertised_relation_is_implemented(graph_proj, any_symbol, relation):
    """SC4: a relation in _RELATIONS reaches a real branch, not the unknown-relation rejection."""
    from rag_search.query.graph_handler import run_graph

    data = json.loads(run_graph(any_symbol, graph_proj, relation, to_symbol=any_symbol))
    assert not data.get("error", "").startswith("unknown relation"), (
        f"relation {relation!r} is advertised in _RELATIONS but has no branch in run_graph"
    )


def test_sc4b_unlisted_relation_is_rejected(graph_proj, any_symbol):
    """SC4b: SC4 only means something if the rejection it excludes can actually happen."""
    from rag_search.query.graph_handler import run_graph

    data = json.loads(run_graph(any_symbol, graph_proj, "not_a_relation", to_symbol=any_symbol))
    assert data.get("error", "").startswith("unknown relation"), (
        f"an unlisted relation must be rejected, not answered; got {data}"
    )
    assert set(data.get("valid", [])) == set(_RELATIONS), (
        "the rejection must name the real valid set, so a caller can correct itself"
    )


def test_sc5_every_advertised_scope_is_a_distinct_assembly():
    """SC5: no two scopes produce the same context — an alias is not a capability.

    This is the check the `ask` surface failed. `global`, `feature` and `business` each named a
    selector or filter that left with tier 3; afterwards they were three more spellings of the two
    assemblies that remained, still advertised as five choices. With no stores the community axis
    is empty, so only the ordering can differ — which is exactly the property under test.
    """
    from rag_search.query.ask import compose_answer

    chunks = [{"path": "a.py", "start_line": 1, "content": "def f(): pass"}]
    seen: dict[str, str] = {}
    for scope in _SCOPES:
        ctx = compose_answer("what does this do", chunks, [], scope=scope)
        assert not ctx.startswith("unknown scope"), (
            f"scope {scope!r} is advertised in _SCOPES but rejected by compose_answer"
        )
        dupe = next((s for s, c in seen.items() if c == ctx), None)
        assert dupe is None, (
            f"scopes {dupe!r} and {scope!r} produce byte-identical context — one is an alias "
            "advertised as a capability"
        )
        seen[scope] = ctx


def test_sc5b_unlisted_scope_is_rejected():
    """SC5b: the counterpart to SC4b, and the trap case — a scope error is a plain string.

    `ask` returns text, so an unrecognized scope that falls through to chunk context is
    indistinguishable from a real answer at the call site. That was the behaviour until 2026-07-28.
    """
    from rag_search.query.ask import compose_answer

    ctx = compose_answer("what does this do", [], [], scope="not_a_scope")  # sc3-exempt
    assert ctx.startswith("unknown scope"), f"an unlisted scope must be rejected; got {ctx[:200]!r}"
    for scope in _SCOPES:
        assert scope in ctx, f"the rejection must name {scope!r} so a caller can correct itself"
