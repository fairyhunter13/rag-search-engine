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


def test_mp2_a_shell_merely_naming_a_heavy_pass_is_not_counted_as_one() -> None:
    """MP2 — a `sh -c` payload that mentions a heavy pass must not be counted as running one.

    MP1 above is satisfied by a scan that matches any command line containing `bin/pytest`,
    including the wrapper shell that launched this run — so on its own it does not prove the scan
    finds real interpreters. Measured 2026-07-31: with the wrapper counted, three consecutive
    readings against a *single* live suite reported 3, 6 and 4 heavy passes, the variation being
    the probe commands' own shells. A gate that counts its own observation cannot reach zero once
    any such wrapper lingers, which is a gate that never lets a measurement happen.

    Driven with a real subprocess rather than a synthetic cmdline: the property is about what
    `/proc` actually contains, so anything short of a live pid would assert the fixture instead.
    """
    import importlib.util
    import subprocess
    import time
    from pathlib import Path

    script = Path(__file__).resolve().parents[3] / "scripts" / "measure_preconditions.py"
    spec = importlib.util.spec_from_file_location("_measure_preconditions_mp2", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # The payload names a heavy pass but runs none. The trailing `true` is load-bearing: `sh -c`
    # *execs* its last simple command, which would replace the payload in /proc with a bare
    # `sleep 10` and leave nothing for the scan to wrongly match — the test would then pass
    # against the unfixed scan too.
    decoy = subprocess.Popen(
        ["/bin/sh", "-c", "# MP2-DECOY .venv/bin/pytest src/tests/live/\nsleep 10\ntrue"])
    try:
        time.sleep(0.5)  # let the fork land in /proc before scanning it
        assert "MP2-DECOY" in (Path(f"/proc/{decoy.pid}/cmdline").read_bytes().decode(
            errors="replace")), "the decoy exec'd away its payload, so this proves nothing"
        found = mod._heavy_processes()
        assert not any("MP2-DECOY" in p for p in found), (
            "a shell whose payload merely mentions bin/pytest was counted as a heavy pass; the "
            f"scan is reading command-line text as execution: {found}")
    finally:
        decoy.kill()
        decoy.wait(timeout=10)


def test_mp3_a_timeout_wrapped_pass_is_counted_once_not_twice() -> None:
    """MP3 — `timeout N <pass>` is one heavy pass, not two.

    MP2 removed the shell wrappers; `timeout` is the one that was left, and it is different in the
    way that matters. Shells run a payload and can be recognised by `-c`; `timeout` **forks and
    waits**, so it and the real interpreter are both live processes carrying the same fragment, and
    one pass reads as two. Observed 2026-07-31 against a single run: `2 heavy pass(es) running`,
    the first of them the `timeout` wrapper of the second.

    Both halves are asserted, because skipping a wrapper is exactly how a scan starts missing the
    thing it wraps: the supervisor must be dropped *and* the pass beneath it must still be found.
    An assertion of "not two" alone is satisfied by a scan that has gone blind.

    `env`/`nice`/`nohup`/`stdbuf` need no such handling — they exec into the target and are
    replaced by it, so they never appear beside it.
    """
    import importlib.util
    import subprocess
    import sys
    import time
    from pathlib import Path

    script = Path(__file__).resolve().parents[3] / "scripts" / "measure_preconditions.py"
    spec = importlib.util.spec_from_file_location("_measure_preconditions_mp3", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # The heavy fragment rides in argv so the *child* is a genuine match under its own
    # interpreter, while the `timeout` parent carries the identical text on its own command line.
    decoy = subprocess.Popen(
        ["timeout", "20", sys.executable, "-c", "import time; time.sleep(15)",
         "MP3-DECOY", "bin/pytest"])
    try:
        time.sleep(0.5)  # let both pids land in /proc before scanning
        found = [p for p in mod._heavy_processes() if "MP3-DECOY" in p]
        assert len(found) == 1, (
            f"a `timeout`-wrapped pass must count once — the supervisor and the interpreter "
            f"beneath it both carry the fragment, so 2 means the wrapper is still counted and 0 "
            f"means the scan lost the real pass: {found}")
        assert Path(found[0].split()[0]).name != "timeout", (
            f"the entry kept is the supervisor rather than the pass it launched: {found}")
    finally:
        decoy.kill()
        decoy.wait(timeout=10)
