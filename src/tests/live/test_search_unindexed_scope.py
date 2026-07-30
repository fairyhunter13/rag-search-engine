"""SU1-SU3: a search that reached no index must say so, not report an empty result set.

`_search_sync` skips any resolved project whose `vectors.db` is missing. When that skips *every*
resolved project the caller used to receive the ordinary `{"results": [], "total": 0}` — which is a
statement about the *query*, and the truth was a statement about the *index*. The two are
indistinguishable at the call site, and only one of them means "look somewhere else".

That is the same defect shape as the 07-30 index-set drift: a completeness question answered by an
instrument that cannot express incompleteness. Every rung of the resolution ladder above this point
is deliberately fail-loud ("Never a silent fall-through" — `_default_or_error`); the bare `continue`
was the one step that undid it at the end.

Measured on this host 07-30: the registry wipe left 96 of 158 enabled rows with no store, and a
`search` whose scope resolved to any one of them answered "no results" while the index was merely
absent. This test exists because that answer cost a real session real time.

SU2/SU3 are the discrimination arms (`feedback_guard_tests_must_discriminate`): a guard that fires
when there *is* an index, or that swallows a partial federation, would be worse than none. They use
the already-built `sample_workspace` corpus rather than building their own — no new GPU work.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_CHILD = r"""
import json, sys
from rag_search.server.mcp import _search_sync
# No store can exist: RSE_INDEX_ROOT points at an empty dir, and `index_dir` resolved it at import.
print(_search_sync("anything at all", "code", ["/nonexistent/never-indexed"], 8, "compact"))
"""


def test_su1_a_scope_with_no_store_is_an_error_not_an_empty_result(tmp_path_factory):
    """SU1: total blindness must be reported as an error naming the unindexed projects.

    Subprocess-isolated with `RSE_INDEX_ROOT` redirected — `index_dir` resolves against a
    module-level constant read at import, so only a fresh interpreter can point it at a directory
    where the fleet's stores are unreachable. Running this in-process would consult the real
    INDEX_ROOT and pass for the wrong reason.
    """
    tmp = tmp_path_factory.mktemp("su1")
    env = {**os.environ,
           "RSE_REGISTRY_PATH": str(tmp / "projects.json"),
           "RSE_INDEX_ROOT": str(tmp / "indexes"),
           "PYTHONPATH": str(Path(__file__).parents[2])}
    r = subprocess.run([sys.executable, "-c", _CHILD],
                       capture_output=True, text=True, env=env, timeout=180)
    assert r.returncode == 0, f"child exit {r.returncode}:\n{r.stdout}\n{r.stderr}"
    payload = json.loads(r.stdout.strip().splitlines()[-1])

    assert "error" in payload, (
        "a search that opened zero stores reported an ordinary empty result set; "
        f"'no index' is not 'no match': {payload}")
    assert "results" not in payload or not payload.get("results"), payload
    assert "/nonexistent/never-indexed" in payload.get("unindexed", []), (
        f"the error must name what was not searched, or it cannot be acted on: {payload}")


def test_su2_a_scope_with_a_real_store_still_returns_results(sample_workspace):
    """SU2 (discrimination): the guard must not fire when there *is* an index.

    Without this arm SU1 is satisfied by a `_search_sync` that always errors. This runs the real
    embedder over the real session corpus — the same end-to-end path a client takes.
    """
    from rag_search.server.mcp import _search_sync

    payload = json.loads(_search_sync("function definition", "code",
                                      [str(sample_workspace)], 8, "compact"))

    assert "error" not in payload, (
        f"the unindexed guard fired on a project that has a store: {payload}")
    assert payload.get("projects_searched") == [str(sample_workspace)], payload


def test_su3_a_partial_federation_stays_a_result_not_an_error(sample_workspace):
    """SU3: one reachable store among unreachable ones is a *result*, not a failure.

    The honest report for a partial search already exists — `projects_searched` names the subset
    that was reached, so the caller can see what was skipped. Escalating that to an error would
    break every federation with a member mid-rebuild, which on this host is most of them.
    """
    from rag_search.server.mcp import _search_sync

    payload = json.loads(_search_sync("function definition", "code",
                                      [str(sample_workspace), "/nonexistent/not-indexed"],
                                      8, "compact"))

    assert "error" not in payload, (
        f"a partial federation was escalated to an error: {payload}")
    assert payload.get("projects_searched") == [str(sample_workspace)], (
        f"projects_searched must name exactly the stores actually opened: {payload}")
