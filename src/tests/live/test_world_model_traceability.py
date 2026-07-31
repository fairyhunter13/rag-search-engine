"""World-model L3 RTM traceability guard.

Every test: name in docs/world-model/model.yaml L3_specs must resolve to a
real 'def test_<name>' function in src/tests/. GPU-free, daemon-free, import-free.

Prevents the L3 spec layer from silently rotting when tests are renamed.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_ROOT = Path(__file__).resolve().parents[3]
_YAML = _ROOT / "docs" / "world-model" / "model.yaml"
_TESTS_DIR = _ROOT / "src" / "tests"


def _parse_l3_tests() -> list[tuple[str, str]]:
    yaml_raw = _YAML.read_text()
    results = []
    block_re = re.compile(
        r"- id: (HR\d+)\s+spec: \"[^\"]+\"\s+test: (\S+)", re.MULTILINE
    )
    for m in block_re.finditer(yaml_raw):
        results.append((m.group(1), m.group(2)))
    return results


def _all_test_names() -> set[str]:
    names: set[str] = set()
    for py in _TESTS_DIR.rglob("*.py"):
        for m in re.finditer(r"def (test_\w+)", py.read_text(errors="replace")):
            names.add(m.group(1))
    return names


def test_l3_rtm_all_tests_resolve():
    """All model.yaml L3_specs test: names must map to a real def test_… in src/tests/."""
    specs = _parse_l3_tests()
    assert specs, f"No L3_specs parsed from {_YAML} — YAML format may have changed"

    live_tests = _all_test_names()
    broken = [(hr, name) for hr, name in specs if name not in live_tests]
    assert not broken, (
        "model.yaml L3_specs has broken HR→test mappings "
        "(test renamed or deleted without updating model.yaml):\n"
        + "\n".join(f"  {hr}: {name}" for hr, name in broken)
    )


def test_l1_conformance_checker_reports_conforms():
    """`check_world_model.py --all` must exit 0 — the L1 layer's own executable check.

    The L3 guard above has had teeth since it was written; the L1 checker had none. It is not
    run by CI, and it spent this week reporting a permanent false AT_RISK on P3 (a docstring
    *explaining* the invariant matched the invariant's own anti-pattern). Nothing failed, so
    nothing said so, and the natural reading of a checker that is always red is to stop running
    it — which is the same outcome as not having one.

    `--all` rather than the diff mode: the working tree is clean by the time this runs in CI, so
    the diff scan would be empty and the assertion vacuous.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "check_world_model.py"), "--all"],
        cwd=_ROOT, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, (
        "check_world_model.py reports an L1 invariant AT_RISK. Either the change under test "
        "violates it, or the check's pattern matches prose rather than code — fix whichever it "
        f"is; do not leave the checker red.\n{proc.stdout}\n{proc.stderr}"
    )
