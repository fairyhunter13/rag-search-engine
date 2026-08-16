"""Traceability guards for the repo's maps: a citation must resolve, and a map must be complete.

Two of them are the normative tables in `federation-ops-and-invariants.md`; the third is
`docs/decisions/README.md`, added 2026-08-15 for the same reason and with the weaker feedback loop
(§14 is read constantly, an unlinked decision record is read by nobody).

§14 cites test names; §13b defines the hard-requirement ids that every other table, comment and
dated record cites. Both are held to the same standard: a citation must resolve, and the escape
hatch is the table's own strikethrough notation. Since 2026-08-15 the pair is also held to each
other: every id §13b still holds must reach §14, which is what §14's first line has always claimed.
§14's File column is held to the same standard as its test names, because a citation to a module is
a claim about a path and a renamed file breaks it while every `def` it holds still resolves.
GPU-free, daemon-free, import-free.

Retargeted 2026-08-14 from an L3 register that was deleted with its own subject. The mechanism
moved rather than retiring: the ops doc records at §14 that a probe once found **70 names in
this table that no longer resolve**, that the L3 register had a gate and this table did not,
and that building one was the standing follow-up.
"""
from __future__ import annotations

import re
import subprocess
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

# The hard-requirement id, written the one way §13b's rows write it and every citation spells it.
_HR_ID = re.compile(r"\bHR(\d+)\b")


def _section_rows(heading: str) -> list[str]:
    """Every data row of the table under `heading`, whole, in document order.

    Stop at the next heading of any depth: §13c follows §13b at h3 and carries its own numbered
    table (`#1`–`#8`); reading it there would make an id defined by the wrong table.

    Data starts after the separator row, not after a prefix test on each line. Everything above the
    separator is header by definition, in any of the spellings a formatter emits (`|---|`,
    `| --- |`, `|:---:|`) — a `startswith("|---")` filter reads two of those three as data.
    """
    body = _DOC.read_text(encoding="utf-8")
    section = body.split(heading, 1)
    assert len(section) == 2, f"{_DOC.name} has no '{heading}' section."
    table = re.split(r"\n#{2,3} ", section[1], maxsplit=1)[0]
    lines = [ln for ln in table.splitlines() if ln.startswith("|")]
    sep = next((i for i, ln in enumerate(lines) if set(ln) <= set("|-: ")), None)
    assert sep is not None, f"'{heading}' has no table separator row."
    return lines[sep + 1:]


def _cells(row: str) -> list[str]:
    """A row's cells, without the empties its leading and trailing pipes produce.

    Indexed from the left, never from the right: a row that loses its trailing pipe renders
    identically in markdown but shifts a right-indexed File column onto the Test cell, where the
    module regex matches nothing — so the row goes unchecked and green, which is the failure this
    file exists to prevent.
    """
    cells = row.split("|")[1:]
    return cells[:-1] if cells and not cells[-1].strip() else cells


def _cited_test_names() -> list[str]:
    names: list[str] = []
    for row in _section_rows("## 14. Test coverage map"):
        names += re.findall(r"`(test_\w+)`", _STRUCK.sub("", row))
    return names


def _cited_test_modules() -> list[str]:
    """The modules §14's File column names, unstruck.

    The third cell only, deliberately. Prose in the other two legitimately names modules that
    are gone — "`test_bpre.py` is deleted" is a true sentence, and a scan of the whole row would
    read eleven such sentences as broken citations. The File column is the one cell that *asserts*
    a file exists, so it is the one cell that can be held to it.
    """
    names: list[str] = []
    for row in _section_rows("## 14. Test coverage map"):
        cells = _cells(row)
        if len(cells) < 3:
            continue
        names += re.findall(r"`(test_\w+\.py)`", _STRUCK.sub("", cells[2]))
    return names


def _all_test_modules() -> set[str]:
    return {p.name for p in _TESTS_DIR.rglob("test_*.py")}


def _all_test_names() -> set[str]:
    names: set[str] = set()
    for py in _TESTS_DIR.rglob("*.py"):
        for m in re.finditer(r"def (test_\w+)", py.read_text(errors="replace")):
            names.add(m.group(1))
    return names


def _first_cells(heading: str) -> list[str]:
    """The first cell of every data row under `heading`. Both tables key their rows in column one."""
    return [_cells(row)[0] for row in _section_rows(heading) if _cells(row)]


def _defined_hr_ids() -> set[str]:
    """The ids §13b defines, struck rows included.

    Strikethrough means the opposite of what it means in §14, and the difference is the whole
    reason both tables can share this file. A struck §14 row says "this proof left, stop looking
    for it". A struck §13b row says "this requirement retired" — but its id must go on resolving,
    because the dated records that cite it are kept as written and cannot be renamed to match a
    later decision (`docs/decisions/README.md`). Retired is still defined.
    """
    ids: set[str] = set()
    for cell in _first_cells("## 13b."):
        ids |= {m.group(1) for m in _HR_ID.finditer(cell)}
    return ids


def _live_hr_ids() -> set[str]:
    """The ids §13b defines and has *not* struck: the requirements still in force.

    Struck ids are dropped here for the same reason they are kept in `_defined_hr_ids` — the two
    tables read strikethrough oppositely. A retired requirement must stay defined so its dated
    records resolve, and must not be required to have a live proof in §14.
    """
    ids: set[str] = set()
    for cell in _first_cells("## 13b."):
        ids |= {m.group(1) for m in _HR_ID.finditer(_STRUCK.sub("", cell))}
    return ids


def _mapped_hr_ids() -> set[str]:
    """The ids §14 keys a row on. Its other rows are keyed by prose (`#1 no-inlining`, `§15.4`)."""
    ids: set[str] = set()
    for cell in _first_cells("## 14. Test coverage map"):
        ids |= {m.group(1) for m in _HR_ID.finditer(cell)}
    return ids


def _citation_sites() -> list[Path]:
    """Every tracked file, because a scan set written by hand is a scan set that drifts.

    The first draft of this guard listed `docs/**/*.md`, `src/**/*.py`, `CLAUDE.md` and
    `README.md`, and missed nine files under `scripts/` that cite these ids — including four
    systemd drop-ins that are not `.py` at all. That is the same defect as the checker this
    overhaul deleted, which declared coverage of `docs/` and then enumerated `rglob("*.py")`.
    `git ls-files` cannot develop that gap: a citation in a directory nobody has created yet is
    still inside it. 284 tracked files, 2.5 MB, all text.
    """
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return [_ROOT / p for p in tracked]


def test_hr_ids_resolve_in_the_definition_table():
    """Every hard-requirement id cited anywhere in the tree must have a row in §13b.

    §13b is that table because the 2026-08-14 overhaul's Stage B1 — replace the id column with
    each rule's name — was reversed on contact: it was written against a `test:` column §13b does
    not have (guards are named in §14, whose own first column is the id), and the same plan
    exempted 51 dated records *because* they cite these ids, which only holds while the ids still
    resolve somewhere. The reversal made §13b the one definition table. This is the gate that
    keeps it one, so the property stops depending on a check somebody remembered to run by hand.
    """
    defined = _defined_hr_ids()
    assert len(defined) > 30, (
        f"Only {len(defined)} ids parsed out of §13b — the table's shape probably changed, and a "
        "guard that reads nothing reports the same green as a table with no dangling citations."
    )
    dangling: dict[str, list[str]] = {}
    for path in _citation_sites():
        if not path.exists():
            continue
        for m in _HR_ID.finditer(path.read_text(errors="replace")):
            if m.group(1) not in defined:
                dangling.setdefault(m.group(0), []).append(str(path.relative_to(_ROOT)))
    assert not dangling, (
        "These hard-requirement ids are cited but have no row in §13b of "
        f"{_DOC.name}. Add the row, or re-point the citation at the id that replaced it — a "
        "citation nobody can follow is how the six drifted copies this table replaced began:\n"
        + "\n".join(f"  {i} — {', '.join(sorted(set(f)))}" for i, f in sorted(dangling.items()))
    )


def test_every_defined_hr_id_is_mapped():
    """Every requirement §13b still holds must have a row in the §14 coverage map.

    The mirror of the guard above, and the half that was missing. §14 opens by asserting "Each §13
    invariant has a corresponding live test that proves it without mocks", and on 2026-08-15 that
    sentence was false for three ids — HR31, HR34 and HR41, the rows restated into §13b on
    2026-08-14 when the register was deleted. All three carried their guards inline in the
    requirement prose and never got a map row, so the entire public-hygiene family and the VRAM
    release proof were absent from the map while being fully proven in `src/tests/`. Coverage was
    never the gap; the map was, and an unchecked claim of completeness is the drift shape that
    overhaul existed to delete.

    Struck §13b rows are exempt: a retired requirement needs no live proof. That is the same
    strikethrough contract both other tests use, read the way §13b reads it.
    """
    live_ids = _live_hr_ids()
    assert len(live_ids) > 20, (
        f"Only {len(live_ids)} unstruck ids parsed out of §13b — the table's shape probably "
        "changed, and a guard that reads nothing reports the same green as a complete map."
    )
    unmapped = sorted(live_ids - _mapped_hr_ids(), key=int)
    assert not unmapped, (
        "These requirements are defined and in force in §13b but have no row in the §14 test "
        f"coverage map of {_DOC.name}. Add the row naming the guard that proves it, or strike the "
        "§13b row through if the requirement retired — naming a guard inline in §13b does not put "
        "it on the map, which is exactly how HR31, HR34 and HR41 went unmapped:\n"
        + "\n".join(f"  HR{i}" for i in unmapped)
    )


def test_coverage_map_files_resolve():
    """Every unstruck module §14's File column names must exist under src/tests/.

    The sibling of the test below, and the half it cannot reach. `test_coverage_map_names_resolve`
    catches a renamed or deleted `def test_…`; it says nothing about the file that holds it. Split a
    module, move it, rename it and keep the defs — the ordinary shape of a test-file refactor — and
    the map goes on naming a path that is gone with every guard green.

    Measured when this was written: 60 unstruck refs, 59 resolving, and the one that did not was
    HR27 — the same row that was the lone retired row in §13b announcing its retirement in prose
    instead of strikethrough. Every other retired row already writes ``~~`test_bpre.py`~~``. So this
    needs no allowlist: strikethrough is the escape hatch here as everywhere in these two tables.
    """
    cited = _cited_test_modules()
    assert len(cited) > 40, (
        f"Only {len(cited)} module names parsed out of §14's File column — the table's shape "
        "probably changed, and a guard that reads nothing reports the same green as a clean map."
    )
    on_disk = _all_test_modules()
    broken = sorted({m for m in cited if m not in on_disk})
    assert not broken, (
        "§14's File column names test modules that do not exist under src/tests/ (a file renamed, "
        "split or moved without updating the map). Re-point the row at the file that holds the "
        "proof now, or strike the cell through with `~~…~~` if the proof genuinely left:\n"
        + "\n".join(f"  {m}" for m in broken)
    )


def test_decisions_index_is_complete_and_resolves():
    """docs/decisions/README.md must link every record, and every link must exist.

    The third map held to the same standard, and the one with the weakest natural feedback: a
    record that never reaches the index is not missing from anywhere a reader looks, so nothing
    reports it. CLAUDE.md points at this directory for every "why", which is only true while the
    index is the whole directory.
    """
    d = _ROOT / "docs" / "decisions"
    records = {p.name for p in d.glob("*.md")} - {"README.md"}
    linked = set(re.findall(r"\]\(\.?/?([^)]+\.md)\)", (d / "README.md").read_text("utf-8")))

    assert records, "no decision records found — this guard would pass vacuously"
    assert not (dangling := sorted(linked - records - {"README.md"})), (
        "docs/decisions/README.md links records that do not exist (renamed or deleted without "
        "updating the index):\n" + "\n".join(f"  {n}" for n in dangling)
    )
    assert not (unlisted := sorted(records - linked)), (
        "decision records exist that the index never links, so nothing points a reader at them. "
        "Add a line to docs/decisions/README.md:\n" + "\n".join(f"  {n}" for n in unlisted)
    )


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
