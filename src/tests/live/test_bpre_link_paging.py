"""Live gates: the capped LLM link-resolution batch must be ranked and must page.

`_llm_link_resolve` slices `items[:30]` in insertion order and remembers nothing, so on
inosoft-project it sent DeepSeek the same 30 of 734 candidates on all 68 logged runs — paid
calls re-answering a solved question while 704 cross-service edges stayed permanently
unresolved. Output alone hides this: every run writes plausible edges and looks healthy.

LP1  two consecutive real runs must attempt *different* candidates (live, needs a key)
LP2  a high fan-in route must make the batch even when it was discovered last
LP3  once the whole backlog is attempted the cycle must restart, not go silent
"""
from __future__ import annotations

import sqlite3

import pytest

pytestmark = pytest.mark.live


def _items(n: int, start: int = 0) -> list[dict]:
    return [
        {"kind": "http", "caller": f"svc{i}", "topic_or_route": f"GET /r{i}"}
        for i in range(start, start + n)
    ]


def _con() -> sqlite3.Connection:
    from rag_search.kb.bpre import _SCHEMA
    con = sqlite3.connect(":memory:")
    con.executescript(_SCHEMA)
    return con


def _attempted(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT id FROM link_resolve_attempts")}


def test_lp1_two_runs_attempt_different_candidates():
    """LP1: the second real run must move on, not replay the first run's 30.

    Two live DeepSeek calls, same shape as TE7/TE8. The attempts table is the only place the
    difference is visible — the edges written are indistinguishable either way.
    """
    from rag_search.graph.llm import deepseek_key
    from rag_search.kb.bpre import _LLM_LINK_CAP, _llm_link_resolve

    con = _con()
    try:
        items = _items(2 * _LLM_LINK_CAP)
        svcs = [u["caller"] for u in items]
        _llm_link_resolve(con, items, svcs)
        first = _attempted(con)
        if not deepseek_key():
            # The no-skip policy applies to this suite, so assert the keyless contract instead:
            # nothing was sent, so nothing may be stamped as attempted either.
            assert first == set(), "LP1: no key means no call, and no call means no stamp"
            return
        assert len(first) == _LLM_LINK_CAP, (
            f"LP1: one run must attempt exactly the cap; got {len(first)}"
        )
        _llm_link_resolve(con, items, svcs)
        second = _attempted(con) - first
        assert len(second) == _LLM_LINK_CAP, (
            f"LP1: the second run re-sent {_LLM_LINK_CAP - len(second)} of the same candidates "
            "— the backlog is frozen exactly as it was before this fix"
        )
    finally:
        con.close()


def test_lp2_fan_in_beats_discovery_order():
    """LP2: ranking, not insertion order — the SEA admitted set is assumed to be ranked.

    The hot route is appended last, so it falls outside `items[:30]`; a batch that still
    contains it can only have come from a ranked selection. Touches no model.
    """
    from rag_search.kb.bpre import _LLM_LINK_CAP, _select_link_batch

    con = _con()
    try:
        cold = _items(_LLM_LINK_CAP + 5)
        hot = [
            {"kind": "http", "caller": f"hot{i}", "topic_or_route": "GET /shared/hot"}
            for i in range(5)
        ]
        batch = _select_link_batch(con, cold + hot)
        assert len(batch) == _LLM_LINK_CAP, "LP2: the cap itself must be unchanged"
        assert all(u in batch for u in hot), (
            "LP2: a route five services call was discovered last and got dropped — "
            "the slice is still in insertion order"
        )
    finally:
        con.close()


def test_lp3_exhausted_backlog_restarts_instead_of_going_silent():
    """LP3: when every candidate has been attempted, the cycle restarts.

    Without the reset the function would send an empty hint list forever once the backlog was
    walked — a quieter failure than the one being fixed, and unresolvable-today candidates
    become resolvable once their callee's service is scanned. Touches no model.
    """
    from rag_search.kb.bpre import _LLM_LINK_CAP, _link_item_id, _select_link_batch

    con = _con()
    try:
        items = _items(_LLM_LINK_CAP + 5)
        for _ in range(2):  # 30 then the remaining 5
            batch = _select_link_batch(con, items)
            con.executemany("INSERT OR IGNORE INTO link_resolve_attempts VALUES (?)",
                            [(_link_item_id(u),) for u in batch])
            con.commit()
        assert len(_attempted(con)) == len(items), "LP3: two passes must cover the whole backlog"

        batch = _select_link_batch(con, items)
        assert len(batch) == _LLM_LINK_CAP, (
            "LP3: an exhausted backlog must restart the cycle, not return an empty batch"
        )
        assert _attempted(con) == set(), "LP3: the restart must clear the stamps"
    finally:
        con.close()
