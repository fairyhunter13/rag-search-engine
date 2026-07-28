"""SC-A: what the tools advertise is what the tools do.

Written 2026-07-28, after `graph(relation="semantic_trace")` was found alive as a name with no
implementation behind it. It was deleted in 3fe4b29 and survived in the MCP docstring, the CLI help
and a skill file that *instructed* agents to call it — answering the whole time with `path`
semantics, because `path` was the fallthrough. Both its tests passed: one asserted the response was
a dict, the other asserted the `path` contract. Neither would have failed if it were named `banana`.

`test_p21_capability_parity.py` already ties the `graph`/`overview` MCP docstrings to the
implementation's own constants. Added here: SC1/SC2 the CLI help, SC3 the `ask` docstring, and — the
two that matter — SC4 every advertised relation reaches a branch, SC5 every advertised scope is a
distinct assembly. SC4/SC5 fail on the two shapes these surfaces actually had: a name with no
branch, and several names sharing one branch.
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


def test_sc3_mcp_ask_docstring_matches_scopes():
    """SC3: the MCP `ask` docstring lists exactly the implemented scopes.

    P21's A2 covers `graph` and A1 covers `overview`; `ask` had no equivalent, which is why five
    scope names went on being advertised after three of them stopped selecting anything.
    """
    from rag_search.server.mcp import ask

    m = re.search(r"scope:\s*([\w|]+)", ask.__doc__ or "")
    assert m, f"ask docstring has no `scope:` value list: {ask.__doc__!r}"
    assert set(m.group(1).split("|")) == set(_SCOPES), (
        f"ask docstring scopes {sorted(m.group(1).split('|'))} != _SCOPES {sorted(_SCOPES)}"
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

    ctx = compose_answer("what does this do", [], [], scope="not_a_scope")
    assert ctx.startswith("unknown scope"), f"an unlisted scope must be rejected; got {ctx[:200]!r}"
    for scope in _SCOPES:
        assert scope in ctx, f"the rejection must name {scope!r} so a caller can correct itself"
