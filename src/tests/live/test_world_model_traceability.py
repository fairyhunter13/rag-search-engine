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
