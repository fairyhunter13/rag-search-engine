"""MP1 — the measurement gate must be able to refuse, proven from inside a run that it must refuse.

`scripts/measure_preconditions.py` exists so a performance number taken on a contended box is
never recorded. A gate like that fails silently in one direction only: if its process scan or its
load read quietly stops working it returns "warm and quiet" forever, and the numbers it was
supposed to block get banked as though they were clean. Nothing downstream would notice, because
a green gate looks identical whether it checked anything or not.

The live suite is the one place a refusal is guaranteed on the merits rather than arranged: this
test runs inside pytest, `bin/pytest` is a heavy pass by the gate's own definition, and a suite
that loads a real embedder is exactly the contention the gate is about. So the assertion needs no
fixture, no mock and no injected state — it is the real predicate against the real machine, which
is the only form in which "it can still fire" means anything.

Deliberately *not* asserting the load or GPU arms. Those depend on what else the box is doing, so
requiring them would make this test flake on a quiet machine — and a test that must be silenced on
good days teaches people to ignore it.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def test_mp1_the_gate_refuses_a_measurement_taken_inside_the_live_suite() -> None:
    """MP1 — running under pytest is a refusal, and the reason names the pytest process."""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[3] / "scripts" / "measure_preconditions.py"
    assert script.is_file(), f"the measurement precondition is missing from the repo: {script}"
    spec = importlib.util.spec_from_file_location("_measure_preconditions", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    ok, conditions = mod.check()
    assert not ok, (
        "the gate reported warm-and-quiet from inside the live suite; it can no longer refuse, "
        f"which means every number it passes is unverified: {conditions}")
    assert any("bin/pytest" in p for p in conditions["heavy_processes"]), (
        "the gate refused, but not for the pytest process it is running inside — its process scan "
        f"is not seeing this run: {conditions['heavy_processes']}")
