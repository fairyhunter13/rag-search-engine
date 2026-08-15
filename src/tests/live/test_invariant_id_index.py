"""The generated invariant-id index must be current, and must be an index of something.

`docs/reference/invariant-ids.md` resolves the short ids the codebase argues in to the guard that
defines each. It is generated, so the only failure mode worth a test is staleness, which is the same
contract `test_coverage_map_names_resolve` enforces on §14: a map nobody re-derives stops describing
the tests it maps.

No id is named in this file on purpose — the generator scans `src/tests/` for references, so an
example cited here would enter the index as a reference to itself.

`scripts/gen_grammar_capability_matrix.py` writes to the same directory with no gate, which is why
this exists as its own file rather than as a note in that one.
GPU-free, daemon-free.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_ROOT = Path(__file__).resolve().parents[3]
_GEN = _ROOT / "scripts" / "gen_invariant_ids.py"
_DOC = _ROOT / "docs" / "reference" / "invariant-ids.md"


def test_invariant_id_index_is_current():
    """The checked-in index matches what the generator produces from today's tests."""
    result = subprocess.run(
        [sys.executable, str(_GEN), "--check"], capture_output=True, text=True, cwd=_ROOT
    )
    assert result.returncode == 0, (
        "docs/reference/invariant-ids.md is stale — a guard was added, renamed or re-lettered "
        "since it was last generated. Re-run `.venv/bin/python scripts/gen_invariant_ids.py` and "
        f"commit the result:\n{result.stderr.strip()}"
    )


def test_invariant_id_index_indexes_something():
    """A generator that silently stopped parsing renders an empty table and diffs clean.

    The staleness check above compares the file to the generator's output, so both going empty
    together is green. This pins the floor instead: 343 ids resolved on 2026-08-15, and the count
    only falls when guards are deleted — well before it halves.
    """
    text = _DOC.read_text(encoding="utf-8")
    counts = re.search(r"^(\d+) defined by one guard\b", text, re.M)
    assert counts, "the index lost its summary line — the generator's own shape changed"
    assert int(counts[1]) > 150, (
        f"only {counts[1]} ids resolved to a guard; the extractor in gen_invariant_ids.py has "
        "probably stopped matching the naming convention it reads"
    )
    assert text.count("\n| `") > 300, "the index's tables are near-empty"
    # The count above is dominated by guard-declared ids, so a regression confined to the comment
    # scanner leaves it green while twenty rows quietly become "unanchored" again.
    commented = re.search(r"· (\d+) by a source comment\b", text)
    assert commented and int(commented[1]) > 10, (
        "the source-comment scanner stopped resolving ids introduced by a `# ID: …` line"
    )
