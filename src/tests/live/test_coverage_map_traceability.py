"""§14 test-coverage-map traceability guard.

Every test name the map cites must resolve to a real `def test_…` in src/tests/, unless the
map itself strikes it through. GPU-free, daemon-free, import-free.

Retargeted 2026-08-14 from `docs/world-model/model.yaml`'s L3 register, deleted with the world
model. The mechanism moved rather than retiring with its old subject: the ops doc records at
§14 that a probe once found **70 names in this table that no longer resolve**, that the L3
register had a gate and this table did not, and that building one was the standing follow-up.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_ROOT = Path(__file__).resolve().parents[3]
_DOC = _ROOT / "docs" / "architecture" / "federation-ops-and-invariants.md"
_TESTS_DIR = _ROOT / "src" / "tests"

# Strikethrough is the map's own notation for a name that left, used on every retired row. So it
# is also the escape hatch here, and the only one: a test whose proof moved must be struck in the
# same edit that moves it, which is a visible claim rather than an allowlist nobody rereads.
_STRUCK = re.compile(r"~~.*?~~", re.DOTALL)


def _cited_test_names() -> list[str]:
    body = _DOC.read_text(encoding="utf-8")
    section = body.split("## 14. Test coverage map", 1)
    assert len(section) == 2, f"{_DOC.name} has no '## 14. Test coverage map' section."
    table = section[1].split("\n## ", 1)[0]
    rows = [ln for ln in table.splitlines() if ln.startswith("|") and not ln.startswith("|---")]
    names: list[str] = []
    for row in rows:
        names += re.findall(r"`(test_\w+)`", _STRUCK.sub("", row))
    return names


def _all_test_names() -> set[str]:
    names: set[str] = set()
    for py in _TESTS_DIR.rglob("*.py"):
        for m in re.finditer(r"def (test_\w+)", py.read_text(errors="replace")):
            names.add(m.group(1))
    return names


def test_coverage_map_names_resolve():
    """Every unstruck `test_…` the §14 map cites must exist in src/tests/."""
    cited = _cited_test_names()
    assert len(cited) > 100, (
        f"Only {len(cited)} test names parsed out of §14 — the table's shape probably changed, "
        "and a guard that reads nothing reports the same green as a clean map."
    )
    live = _all_test_names()
    broken = sorted({n for n in cited if n not in live})
    assert not broken, (
        "§14 test coverage map cites tests that no longer exist (renamed or deleted without "
        "updating the map). Re-point the row at the successor test, or strike the name through "
        "with `~~…~~` if the proof genuinely left:\n" + "\n".join(f"  {n}" for n in broken)
    )
